"""test_circuit_breaker.py — CircuitBreaker 三态机 + 自动降级（设计 §9 / §14.2）。

cases：closed-default / consecutive-fail-soft / fail-rate-soft /
risk-hard / hard-24h / auto-degrade / recover / force-reset / is_open / sliding-window-trim。

用 now_fn 注入可控时间，不真 sleep。
"""

from __future__ import annotations

import pytest

from boss_auto_apply.circuit_breaker import (
    CIRCUIT_CLOSED,
    CIRCUIT_OPEN_HARD,
    CIRCUIT_OPEN_SOFT,
    CircuitBreaker,
    CircuitState,
)
from boss_auto_apply.errors import CircuitBreakerOpenError


class Clock:
    """可控时钟（now_fn 注入）。"""
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start
    def __call__(self) -> float:
        return self.t
    def advance(self, sec: float) -> None:
        self.t += sec


# ============================================================
# 用例1：初始状态 closed，is_open=False
# ============================================================
def test_initial_closed():
    """用例1：新建 CircuitBreaker 初始 closed，is_open=False。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock)
    assert cb.state.state == CIRCUIT_CLOSED
    assert cb.is_open() is False
    assert cb.state.remaining_sec() == 0.0


# ============================================================
# 用例2：连续失败 ≥ 阈值（3）→ 软熔断 30min
# ============================================================
def test_consecutive_failures_triggers_soft():
    """用例2：连续失败 3 次 → open_soft，30min（1800s）。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock, cfg={"circuit_breaker": {
        "consecutive_fail_threshold": 3,
    }})
    cb.report_failure(now=clock())
    cb.report_failure(now=clock())
    assert cb.state.state == CIRCUIT_CLOSED  # 2 次还没到阈值
    cb.report_failure(now=clock())  # 第 3 次
    assert cb.state.state == CIRCUIT_OPEN_SOFT
    assert cb.state.open_until > clock()
    assert cb.is_open() is True


# ============================================================
# 用例3：滑动窗口失败率 > 30%（≥5 样本）→ 软熔断 2h
# ============================================================
def test_fail_rate_triggers_soft():
    """用例3：5 样本 4 失败（80% > 30%）→ open_soft（2h=7200s）。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock, cfg={"circuit_breaker": {
        "fail_rate_threshold": 0.30,
        "fail_rate_min_samples": 5,
        "consecutive_fail_threshold": 100,  # 拉高，避免连续失败先触发
    }})
    # 1 成功 + 4 失败（失败率 80%）
    cb.report_success()
    cb.report_failure(now=clock())
    cb.report_failure(now=clock())
    cb.report_failure(now=clock())
    assert cb.state.state == CIRCUIT_CLOSED  # 4 样本不够
    cb.report_failure(now=clock())  # 第 5 样本（4 失败/5 = 80%）
    assert cb.state.state == CIRCUIT_OPEN_SOFT
    # 2h 时长
    assert cb.state.open_until - clock.t >= 7000


# ============================================================
# 用例4：risk_signal（captcha）→ 硬熔断 24h
# ============================================================
def test_risk_signal_triggers_hard():
    """用例4：report_failure(risk_signal='captcha') → open_hard 24h。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock)
    cb.report_failure(risk_signal="captcha", now=clock())
    assert cb.state.state == CIRCUIT_OPEN_HARD
    assert cb.state.hard_trip_count_24h == 1
    # 24h = 86400s
    assert cb.state.open_until - clock.t >= 86000


def test_risk_signal_rate_limited_triggers_hard():
    """补充：rate_limited 也触发硬熔断。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock)
    cb.report_failure(risk_signal="rate_limited", now=clock())
    assert cb.state.state == CIRCUIT_OPEN_HARD


# ============================================================
# 用例5：熔断开启 → check_or_raise 抛 CircuitBreakerOpenError
# ============================================================
def test_check_or_raise_when_open():
    """用例5：熔断开启时 check_or_raise 抛 CircuitBreakerOpenError。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock)
    cb.report_failure(risk_signal="captcha", now=clock())
    with pytest.raises(CircuitBreakerOpenError):
        cb.check_or_raise(now=clock())


def test_check_or_raise_passes_when_closed():
    """补充：closed 时 check_or_raise 不抛。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock)
    cb.check_or_raise(now=clock())  # 不抛


# ============================================================
# 用例6：自动降级 — 24h 内硬熔断 ≥2 次 → forced_mode='confirm'
# ============================================================
def test_auto_degrade_after_two_hard_trips():
    """用例6：24h 内 2 次硬熔断 → forced_mode='confirm'。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock, cfg={"circuit_breaker": {
        "auto_degrade_threshold": 2,
    }})
    cb.report_failure(risk_signal="captcha", now=clock())
    assert cb.state.forced_mode is None  # 第 1 次还没降级
    # 模拟第一次硬熔断后 force_reset（人工介入），再第二次
    cb.force_reset()
    cb.report_failure(risk_signal="captcha", now=clock())  # 第 2 次
    assert cb.state.hard_trip_count_24h >= 2
    assert cb.state.forced_mode == "confirm"


# ============================================================
# 用例7：maybe_recover 到期恢复到 closed
# ============================================================
def test_maybe_recover_after_expiry():
    """用例7：软熔断到期 → maybe_recover 恢复 closed。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock, cfg={"circuit_breaker": {
        "consecutive_fail_threshold": 1,
        "soft_duration_consecutive_sec": 100,
    }})
    cb.report_failure(now=clock())  # 1 次就软熔断
    assert cb.state.state == CIRCUIT_OPEN_SOFT
    # 未到期 → 不恢复
    assert cb.maybe_recover(now=clock()) is False
    assert cb.state.state == CIRCUIT_OPEN_SOFT
    # 到期 → 恢复
    clock.advance(101)
    assert cb.maybe_recover(now=clock()) is True
    assert cb.state.state == CIRCUIT_CLOSED
    assert cb.state.consecutive_failures == 0  # 恢复清计数


# ============================================================
# 用例8：force_reset 人工重置
# ============================================================
def test_force_reset():
    """用例8：force_reset 清所有状态。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock)
    cb.report_failure(risk_signal="captcha", now=clock())
    assert cb.state.state == CIRCUIT_OPEN_HARD
    cb.force_reset()
    assert cb.state.state == CIRCUIT_CLOSED
    assert cb.state.consecutive_failures == 0
    assert cb.state.sliding_window == []
    assert "force_reset" in cb.state.last_reason


# ============================================================
# 用例9：report_success 归零连续失败
# ============================================================
def test_report_success_resets_consecutive():
    """用例9：成功后连续失败归零（不熔断）。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock, cfg={"circuit_breaker": {
        "consecutive_fail_threshold": 3,
    }})
    cb.report_failure(now=clock())
    cb.report_failure(now=clock())  # consec=2
    cb.report_success()  # 归零
    assert cb.state.consecutive_failures == 0
    cb.report_failure(now=clock())  # consec=1，不熔断
    assert cb.state.state == CIRCUIT_CLOSED


# ============================================================
# 用例10：滑动窗口裁剪（超 sliding_window_max 弹旧）
# ============================================================
def test_sliding_window_trim():
    """用例10：滑动窗口超 max 时裁剪（保留最近 N 条）。"""
    clock = Clock()
    cb = CircuitBreaker(now_fn=clock, cfg={"circuit_breaker": {
        "sliding_window_max": 3,
        "consecutive_fail_threshold": 100,  # 不触发软熔断
        "fail_rate_threshold": 0.99,
    }})
    for _ in range(5):
        cb.report_success()
    # 窗口裁剪到 max=3
    assert len(cb.state.sliding_window) <= 3
