"""test_real_ratelimiter.py — RealRateLimiter 六道闸门 + 熔断联动（设计 §9 / §14.2）。

cases：circuit-open / active-hours / warmup / daily-total / burst / interval /
consec-fail / fail-rate / hard-trip / auto-degrade / rollback / force-reset / recover。

用 now_fn 注入可控时间戳（工作日 10:00 北京时间），conn=None 简化（纯进程内）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from boss_auto_apply.ratelimiter import (
    AcquireContext,
    DryRunLimiter,
    LimitsConfig,
    RealRateLimiter,
    get_date_key,
    get_limiter,
)


# ============================================================
# 工具
# ============================================================
def _weekday_noon_ts(day_offset: int = 0, hour: int = 11) -> float:
    """构造一个「工作日 11:00 北京时间」的时间戳。

    2026-07-13 是周一（weekday=0）。day_offset 偏移天数。
    """
    # 2026-07-13 11:00 北京时间 = 03:00 UTC
    base = datetime(2026, 7, 13, 3, 0, 0, tzinfo=timezone.utc).timestamp()
    return base + day_offset * 86400 + (hour - 11) * 3600


class Clock:
    def __init__(self, start: float = None) -> None:
        self.t = start if start is not None else _weekday_noon_ts()
    def __call__(self) -> float:
        return self.t
    def advance(self, sec: float) -> None:
        self.t += sec


def _make_limiter(*, limits=None, conn=None, clock=None, **kw) -> RealRateLimiter:
    """构造 RealRateLimiter（默认工作日白天，无 conn）。"""
    clock = clock or Clock()
    return RealRateLimiter(
        limits=limits or LimitsConfig(
            daily_total=15, per_session=10, min_interval_sec=45, max_interval_sec=120,
            burst_size=8, burst_rest_sec=(900, 1800),
            active_hours=(9, 18), active_weekdays_only=True,
            warmup_schedule=[5, 10, 15],
        ),
        conn=conn, now_fn=clock, **kw,
    )


# ============================================================
# 用例1：get_limiter dry-run → DryRunLimiter
# ============================================================
def test_get_limiter_dry_run():
    """用例1：get_limiter('dry-run') → DryRunLimiter。"""
    lim = get_limiter("dry-run")
    assert isinstance(lim, DryRunLimiter)
    d = lim.acquire(AcquireContext(job_id="j1"))
    assert d.allow is True


# ============================================================
# 用例2：get_limiter 非 dry-run → RealRateLimiter
# ============================================================
def test_get_limiter_real():
    """用例2：get_limiter('auto') → RealRateLimiter。"""
    lim = get_limiter("auto")
    assert isinstance(lim, RealRateLimiter)


# ============================================================
# 用例2b：get_limiter 传 limits 参数 → 读取用户配置（非默认值）
# ============================================================
def test_get_limiter_reads_config_limits():
    """回归：get_limiter('auto', limits=...) 应读取传入的 active_hours，而非用默认 (9,18)。

    Bug：pipeline.py 之前 get_limiter(mode) 没传 limits，导致用默认 (9,18)
    忽略用户配置的 [8,18]。
    """
    lim = get_limiter("auto", limits={"active_hours": [8, 18], "daily_total": 10})
    assert isinstance(lim, RealRateLimiter)
    assert lim.limits.active_hours == (8, 18), f"应读 config [8,18]，实际 {lim.limits.active_hours}"
    assert lim.limits.daily_total == 10


def test_get_limiter_no_limits_uses_default():
    """不传 limits → 用 LimitsConfig 默认值 (9,18)（确认默认行为不变）。"""
    lim = get_limiter("auto")
    assert isinstance(lim, RealRateLimiter)
    assert lim.limits.active_hours == (9, 18)  # 默认值


# ============================================================
# 用例3：熔断开启 → acquire 拒绝（wait_sec > 0）
# ============================================================
def test_circuit_open_blocks_acquire():
    """用例3：熔断开启时 acquire 返回 allow=False。"""
    clock = Clock()
    lim = _make_limiter(clock=clock)
    # 触发硬熔断
    lim.report_result("j1", success=False, risk_signal="captcha")
    d = lim.acquire(AcquireContext(job_id="j2"))
    assert d.allow is False
    assert d.wait_sec > 0
    assert "熔断" in d.reason


# ============================================================
# 用例4：工作日白天 → 允许（默认场景）
# ============================================================
def test_active_hours_weekday_allows():
    """用例4：工作日 11:00（在 9-18 活跃时段）→ 通过 active_hours 闸门。"""
    clock = Clock()  # 工作日 11:00
    lim = _make_limiter(clock=clock)
    d = lim.acquire(AcquireContext(job_id="j1"))
    # 无 conn 时 daily/burst/interval 都放行；active_hours 通过 → allow=True
    assert d.allow is True


# ============================================================
# 用例5：周末 → 拒绝（active_weekdays_only=True）
# ============================================================
def test_weekend_blocks():
    """用例5：周六（weekday=5）→ 拒绝。"""
    # 2026-07-18 是周六（base 2026-07-13 周一 + 5 天）
    clock = Clock(start=_weekday_noon_ts(day_offset=5))
    lim = _make_limiter(clock=clock)
    d = lim.acquire(AcquireContext(job_id="j1"))
    assert d.allow is False
    assert "周末" in d.reason


# ============================================================
# 用例6：活跃时段外（早 7 点）→ 拒绝
# ============================================================
def test_before_active_hours_blocks():
    """用例6：工作日 7:00（< 9:00）→ 拒绝。"""
    clock = Clock(start=_weekday_noon_ts(hour=7))
    lim = _make_limiter(clock=clock)
    d = lim.acquire(AcquireContext(job_id="j1"))
    assert d.allow is False
    assert "活跃时段" in d.reason


# ============================================================
# 用例7：活跃时段外（晚 20 点）→ 拒绝
# ============================================================
def test_after_active_hours_blocks():
    """用例7：工作日 20:00（>= 18:00）→ 拒绝。"""
    clock = Clock(start=_weekday_noon_ts(hour=20))
    lim = _make_limiter(clock=clock)
    d = lim.acquire(AcquireContext(job_id="j1"))
    assert d.allow is False
    assert "活跃时段" in d.reason


# ============================================================
# 用例8：连续失败 ≥ 阈值 → 软熔断
# ============================================================
def test_consecutive_failures_soft_trips():
    """用例8：连续 3 次失败 → 软熔断。"""
    clock = Clock()
    lim = _make_limiter(clock=clock)
    for _ in range(3):
        lim.report_result("j", success=False)
    assert lim.current_state().state == "open_soft"


# ============================================================
# 用例9：失败率超阈值（≥5 样本 >30%）→ 软熔断
# ============================================================
def test_fail_rate_soft_trips():
    """用例9：5 样本 4 失败（80%）→ 软熔断。"""
    clock = Clock()
    lim = _make_limiter(clock=clock)
    lim.report_result("j", success=True)
    for _ in range(4):
        lim.report_result("j", success=False)
    assert lim.current_state().state == "open_soft"


# ============================================================
# 用例10：risk_signal → 硬熔断
# ============================================================
def test_risk_signal_hard_trips():
    """用例10：report_result(risk_signal='captcha') → 硬熔断。"""
    clock = Clock()
    lim = _make_limiter(clock=clock)
    lim.report_result("j", success=False, risk_signal="captcha")
    assert lim.current_state().state == "open_hard"
    assert lim.current_state().hard_trip_count_24h == 1


# ============================================================
# 用例11：24h 内 2 次硬熔断 → forced_mode='confirm'
# ============================================================
def test_auto_degrade_to_confirm():
    """用例11：2 次硬熔断 → forced_mode='confirm'。"""
    clock = Clock()
    lim = _make_limiter(clock=clock)
    lim.report_result("j", success=False, risk_signal="captcha")
    assert lim.current_state().forced_mode is None
    lim.force_reset()
    lim.report_result("j", success=False, risk_signal="captcha")
    assert lim.current_state().hard_trip_count_24h >= 2
    assert lim.current_state().forced_mode == "confirm"


# ============================================================
# 用例12：force_reset 清熔断态
# ============================================================
def test_force_reset_clears():
    """用例12：force_reset 后熔断态 closed。"""
    clock = Clock()
    lim = _make_limiter(clock=clock)
    lim.report_result("j", success=False, risk_signal="captcha")
    assert lim.current_state().state == "open_hard"
    lim.force_reset()
    assert lim.current_state().state == "closed"


# ============================================================
# 用例13：熔断到期 → maybe_recover（acquire 时自动恢复）
# ============================================================
def test_recover_after_expiry():
    """用例13：软熔断到期后 acquire 自动恢复（maybe_recover）。"""
    clock = Clock()
    lim = _make_limiter(clock=clock, limits=LimitsConfig(
        daily_total=15, min_interval_sec=45, max_interval_sec=120,
        burst_size=8, burst_rest_sec=(900, 1800),
        active_hours=(9, 18), active_weekdays_only=True,
        warmup_schedule=[5, 10, 15],
    ))
    # 用 circuit_breaker 子配置设短软熔断时长
    lim.breaker.soft_duration_consecutive_sec = 60
    lim.breaker.soft_fail_threshold = 1
    lim.report_result("j", success=False)
    assert lim.current_state().state == "open_soft"
    # 未到期 → 拒绝
    d = lim.acquire(AcquireContext(job_id="j2"))
    assert d.allow is False
    # 到期（+61s 仍在工作日白天）→ 恢复 + 放行
    clock.advance(61)
    d2 = lim.acquire(AcquireContext(job_id="j2"))
    assert lim.current_state().state == "closed"


# ============================================================
# 用例14：日上限（无 conn 时此闸门不生效，但可验证逻辑）
# ============================================================
def test_daily_total_with_conn(tmp_conn):
    """用例14：有 conn 时 sent_count 达 daily_total → 拒绝。

    用 tmp_conn（已 init_db）。
    """
    clock = Clock()
    lim = _make_limiter(clock=clock, conn=tmp_conn, limits=LimitsConfig(
        daily_total=2, min_interval_sec=0, max_interval_sec=0,
        burst_size=100, burst_rest_sec=(1, 2),
        active_hours=(0, 23), active_weekdays_only=False,
        warmup_schedule=[100],  # 预热上限拉高，不干扰
    ))
    # 预扣 2 次（达上限）
    d1 = lim.acquire(AcquireContext(job_id="j1"))
    d2 = lim.acquire(AcquireContext(job_id="j2"))
    assert d1.allow and d2.allow
    # 第 3 次应被日上限拒
    d3 = lim.acquire(AcquireContext(job_id="j3"))
    assert d3.allow is False
    assert "日上限" in d3.reason or "预热" in d3.reason


# ============================================================
# 用例15：report_result 成功归零失败计数（不熔断）
# ============================================================
def test_report_success_no_trip():
    """用例15：成功反馈不触发熔断。"""
    clock = Clock()
    lim = _make_limiter(clock=clock)
    lim.report_result("j", success=True)
    assert lim.current_state().state == "closed"
    assert lim.current_state().consecutive_failures == 0


# ============================================================
# 用例16：get_date_key 本地日期格式
# ============================================================
def test_get_date_key():
    """补充：get_date_key 返回 YYYY-MM-DD 格式。"""
    dk = get_date_key()
    assert len(dk) == 10
    assert dk[4] == "-" and dk[7] == "-"
