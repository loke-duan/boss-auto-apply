"""CircuitBreaker 熔断器（设计 §9 三态机 + §13.3 风控熔断）。

三态：
- ``closed``（正常放行）
- ``open_soft``（软熔断：连续失败/失败率过高，暂停 30min/2h）
- ``open_hard``（硬熔断：风控信号，24h + 人工介入）

自动降级：24h 内硬熔断 ≥2 次 → ``forced_mode='confirm'``（每条投递前终端确认）。

状态持久化部分（hard_trip_count_24h / forced_mode）由 RealRateLimiter 落 SQLite，
进程内持有 :class:`CircuitState`（连续失败计数 + 滑动窗口）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger

from .errors import CircuitBreakerOpenError

__all__ = [
    "CircuitState",
    "CircuitBreaker",
    "CIRCUIT_CLOSED",
    "CIRCUIT_OPEN_SOFT",
    "CIRCUIT_OPEN_HARD",
]

CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN_SOFT = "open_soft"
CIRCUIT_OPEN_HARD = "open_hard"

CircuitStateName = Literal["closed", "open_soft", "open_hard"]


@dataclass
class CircuitState:
    """熔断器状态（进程内）。

    设计 §9.1。持久化字段（hard_trip_count_24h / forced_mode）由
    RealRateLimiter 落 daily_quota 表，此处只持有易失态。
    """
    state: CircuitStateName = CIRCUIT_CLOSED
    open_until: float = 0.0              # 熔断到期时间戳（0=未熔断）
    consecutive_failures: int = 0        # 连续失败数
    sliding_window: list[tuple[float, bool]] = field(default_factory=list)  # [(ts, success)]
    hard_trip_count_24h: int = 0         # 24h 内硬熔断次数
    last_hard_trip_ts: float = 0.0       # 最近一次硬熔断时间戳
    forced_mode: Literal["auto", "confirm", "manual"] | None = None  # 自动降级模式
    last_reason: str = ""                # 最近熔断原因（日志/展示用）

    def remaining_sec(self, now: float | None = None) -> float:
        """距离熔断到期剩余秒数（未熔断返回 0）。"""
        if self.state == CIRCUIT_CLOSED:
            return 0.0
        n = now if now is not None else time.time()
        return max(0.0, self.open_until - n)


class CircuitBreaker:
    """三态熔断器（设计 §9）。

    用法（RealRateLimiter 持有）：
        cb = CircuitBreaker(cfg)
        cb.maybe_recover(now=time.time())        # acquire 前检查是否到期恢复
        if cb.is_open(now=time.time()):
            raise CircuitBreakerOpenError(...)
        cb.report_failure(risk_signal='captcha')  # report_result 后调用
        cb.report_success()
        cb.force_reset()                          # 人工介入后

    配置常量从 :class:`~boss_auto_apply.ratelimiter.LimitsConfig` 取，
    也可直接传 dict。默认值对齐设计 §9.2 / §9.4。
    """

    # 软熔断阈值（连续失败）
    DEFAULT_SOFT_FAIL_THRESHOLD = 3
    # 软熔断阈值（失败率）
    DEFAULT_SOFT_FAIL_RATE = 0.30
    DEFAULT_SOFT_FAIL_RATE_MIN_SAMPLES = 5
    # 软熔断时长（连续失败 / 失败率）
    DEFAULT_SOFT_DURATION_CONSECUTIVE_SEC = 1800   # 30min
    DEFAULT_SOFT_DURATION_RATE_SEC = 7200          # 2h
    # 硬熔断时长
    DEFAULT_HARD_DURATION_SEC = 86400              # 24h
    # 自动降级阈值（24h 内硬熔断次数）
    DEFAULT_AUTO_DEGRADE_THRESHOLD = 2
    # 滑动窗口
    DEFAULT_SLIDING_WINDOW_SEC = 1800              # 30min
    DEFAULT_SLIDING_WINDOW_MAX = 20

    def __init__(
        self,
        cfg: Any | None = None,
        *,
        now_fn: Any = time.time,
        logger_obj: Any = None,
    ) -> None:
        """初始化熔断器。

        Args:
            cfg: ``LimitsConfig`` 或 dict，提供 circuit_breaker 相关阈值。
                 为 None 用默认值。
            now_fn: 时间函数（测试注入 mock time 用），默认 ``time.time``。
            logger_obj: loguru logger。
        """
        self._now_fn = now_fn
        self.log = logger_obj or logger
        self.state = CircuitState()
        # 阈值（从 cfg 读，缺省用默认）
        c = self._get(cfg, "circuit_breaker") or {}
        self.soft_fail_threshold = int(c.get("consecutive_fail_threshold",
                                              self.DEFAULT_SOFT_FAIL_THRESHOLD))
        self.soft_fail_rate = float(c.get("fail_rate_threshold", self.DEFAULT_SOFT_FAIL_RATE))
        self.soft_fail_rate_min_samples = int(c.get("fail_rate_min_samples",
                                                     self.DEFAULT_SOFT_FAIL_RATE_MIN_SAMPLES))
        self.soft_duration_consecutive_sec = int(c.get("soft_duration_consecutive_sec",
                                                        self.DEFAULT_SOFT_DURATION_CONSECUTIVE_SEC))
        self.soft_duration_rate_sec = int(c.get("soft_duration_rate_sec",
                                                 self.DEFAULT_SOFT_DURATION_RATE_SEC))
        self.hard_duration_sec = int(c.get("hard_duration_sec", self.DEFAULT_HARD_DURATION_SEC))
        self.auto_degrade_threshold = int(c.get("auto_degrade_threshold",
                                                 self.DEFAULT_AUTO_DEGRADE_THRESHOLD))
        self.sliding_window_sec = int(c.get("sliding_window_sec", self.DEFAULT_SLIDING_WINDOW_SEC))
        self.sliding_window_max = int(c.get("sliding_window_max", self.DEFAULT_SLIDING_WINDOW_MAX))

    @staticmethod
    def _get(cfg: Any, key: str) -> Any:
        """从 LimitsConfig 或 dict 取 key（兼容 dataclass / dict）。"""
        if cfg is None:
            return None
        if isinstance(cfg, dict):
            return cfg.get(key)
        # dataclass / pydantic：尝试 getattr
        return getattr(cfg, key, None)

    # ============================================================
    # 查询
    # ============================================================
    def is_open(self, *, now: float | None = None) -> bool:
        """熔断器是否开启（open_soft/open_hard 且未到期）。"""
        n = now if now is not None else self._now_fn()
        if self.state.state == CIRCUIT_CLOSED:
            return False
        return self.state.open_until > n

    def current_state(self) -> CircuitState:
        """返回当前状态对象（供 status 命令展示）。"""
        return self.state

    def check_or_raise(self, *, now: float | None = None) -> None:
        """熔断开启则抛 :class:`CircuitBreakerOpenError`（acquire 闸门用）。"""
        n = now if now is not None else self._now_fn()
        if self.is_open(now=n):
            raise CircuitBreakerOpenError(self.state.last_reason,
                                          self.state.remaining_sec(now=n))

    # ============================================================
    # 恢复
    # ============================================================
    def maybe_recover(self, *, now: float | None = None) -> bool:
        """熔断到期则恢复到 closed。返回是否发生了恢复。

        RealRateLimiter.acquire 在检查前调用。
        """
        n = now if now is not None else self._now_fn()
        if self.state.state == CIRCUIT_CLOSED:
            return False
        if self.state.open_until <= n:
            self.state.state = CIRCUIT_CLOSED
            self.state.open_until = 0.0
            self.state.consecutive_failures = 0   # 恢复后清连续失败计数
            # 滑动窗口保留（供失败率继续统计），但 hard_trip_count 不清
            self.log.info(f"熔断恢复到 closed（reason={self.state.last_reason}）")
            return True
        return False

    def force_reset(self) -> None:
        """人工重置熔断（人工介入后调用，设计 §9.2）。"""
        self.state.state = CIRCUIT_CLOSED
        self.state.open_until = 0.0
        self.state.consecutive_failures = 0
        self.state.sliding_window.clear()
        self.state.last_reason = "force_reset by user"
        self.log.warning("熔断器人工重置（force_reset）")

    # ============================================================
    # 反馈（report_result 后调用）
    # ============================================================
    def report_success(self) -> None:
        """上报成功：连续失败归零，push 滑动窗口。"""
        self._push_window(success=True)
        self.state.consecutive_failures = 0
        # 成功不主动改变熔断态（让 maybe_recover 按时间恢复）

    def report_failure(
        self,
        *,
        risk_signal: str | None = None,
        now: float | None = None,
    ) -> None:
        """上报失败：连续失败 +1，push 滑动窗口，重算熔断态。

        Args:
            risk_signal: 风控信号（captcha/rate_limited/login_lost）→ 触发硬熔断。
            now: 当前时间戳（测试注入）。
        """
        n = now if now is not None else self._now_fn()
        self._push_window(success=False, now=n)
        self.state.consecutive_failures += 1
        self._recompute(risk_signal=risk_signal, now=n)

    def _push_window(self, *, success: bool, now: float | None = None) -> None:
        """push 滑动窗口并裁剪（超 SLIDING_WINDOW_MAX 弹旧的）。"""
        n = now if now is not None else self._now_fn()
        self.state.sliding_window.append((n, success))
        # 裁剪：超出滑动时间窗的丢弃
        cutoff = n - self.sliding_window_sec
        self.state.sliding_window = [
            (ts, ok) for ts, ok in self.state.sliding_window if ts >= cutoff
        ]
        # 超数量上限弹最早的
        if len(self.state.sliding_window) > self.sliding_window_max:
            self.state.sliding_window = self.state.sliding_window[-self.sliding_window_max:]

    def _recompute(
        self,
        *,
        risk_signal: str | None = None,
        now: float | None = None,
    ) -> None:
        """根据连续失败 + 滑动窗口失败率 + risk_signal 重算熔断态（设计 §9.4）。

        优先级：risk_signal > 连续失败 ≥3 > 失败率 >30%。
        """
        n = now if now is not None else self._now_fn()
        # 1) 风控信号 → 硬熔断 24h
        if risk_signal in ("captcha", "rate_limited", "login_lost"):
            self._trip_hard(reason=f"risk_signal={risk_signal}", now=n)
            return
        # 2) 连续失败 ≥ 阈值 → 软熔断 30min
        if self.state.consecutive_failures >= self.soft_fail_threshold:
            self._trip_soft(
                reason=f"consecutive_failures={self.state.consecutive_failures}",
                duration=self.soft_duration_consecutive_sec, now=n,
            )
            return
        # 3) 滑动窗口失败率 > 阈值（样本足够）→ 软熔断 2h
        samples = self.state.sliding_window
        if len(samples) >= self.soft_fail_rate_min_samples:
            fails = sum(1 for _, ok in samples if not ok)
            rate = fails / len(samples)
            if rate > self.soft_fail_rate:
                self._trip_soft(
                    reason=f"fail_rate={rate:.0%}({fails}/{len(samples)})",
                    duration=self.soft_duration_rate_sec, now=n,
                )

    def _trip_soft(self, *, reason: str, duration: int, now: float) -> None:
        """触发软熔断。"""
        self.state.state = CIRCUIT_OPEN_SOFT
        self.state.open_until = now + duration
        self.state.last_reason = f"soft({reason}, {duration}s)"
        self.log.warning(f"软熔断触发：{reason}（暂停 {duration}s）")

    def _trip_hard(self, *, reason: str, now: float) -> None:
        """触发硬熔断 + 计数 + 检查自动降级。"""
        self.state.state = CIRCUIT_OPEN_HARD
        self.state.open_until = now + self.hard_duration_sec
        self.state.hard_trip_count_24h += 1
        self.state.last_hard_trip_ts = now
        self.state.last_reason = f"hard({reason}, {self.hard_duration_sec}s)"
        self.log.error(
            f"🚨 硬熔断触发：{reason}（暂停 {self.hard_duration_sec}s，需人工介入）"
            f" 24h 硬熔断计数={self.state.hard_trip_count_24h}"
        )
        self._check_auto_degrade(now=now)

    def _check_auto_degrade(self, *, now: float) -> None:
        """24h 内硬熔断 ≥ 阈值 → forced_mode='confirm'（设计 §9.4）。"""
        # 先清超过 24h 的硬熔断计数（按 last_hard_trip_ts 推断）
        if self.state.last_hard_trip_ts and \
                (now - self.state.last_hard_trip_ts) > self.hard_duration_sec:
            self.state.hard_trip_count_24h = 1  # 重置为本次
        if self.state.hard_trip_count_24h >= self.auto_degrade_threshold and \
                self.state.forced_mode != "confirm":
            self.state.forced_mode = "confirm"
            self.log.error(
                f"🚨 自动降级：24h 内硬熔断 {self.state.hard_trip_count_24h} 次 "
                f"≥ {self.auto_degrade_threshold} → 切 confirm 模式（每条投递前确认）"
            )
