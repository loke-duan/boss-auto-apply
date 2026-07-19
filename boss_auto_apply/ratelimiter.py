"""F8/M3 限流接口 + DryRunLimiter + RealRateLimiter + CircuitBreaker（设计 §9）。

M1+M2：``DryRunLimiter`` 永远放行。
M3：``RealRateLimiter`` 实现 PRD §4.7.5 全部参数（日上限/随机间隔/连投休息/
工作日白天/预热/熔断），配合 :class:`~boss_auto_apply.circuit_breaker.CircuitBreaker`。

六道闸门（按优先级）：
1. 熔断态（circuit_open）
2. 工作日白天（active_hours）
3. 预热（warmup）
4. 日上限（daily_total）
5. 连投（burst）
6. 单次间隔（interval）
"""

from __future__ import annotations

import datetime as _dt
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from .circuit_breaker import CircuitBreaker

__all__ = [
    "AcquireContext",
    "AcquireDecision",
    "LimitsConfig",
    "RateLimiter",
    "DryRunLimiter",
    "RealRateLimiter",
    "get_limiter",
    "get_date_key",
]


# ============================================================
# 数据结构
# ============================================================
@dataclass
class AcquireContext:
    """申请投递额度的上下文。"""
    job_id: str
    target_name: str = ""
    city: str = ""
    dry_run: bool = True


@dataclass
class AcquireDecision:
    """限流决策。"""
    allow: bool = True
    wait_sec: float = 0.0
    reason: str = ""
    cooldown_until: str | None = None  # ISO 时间


@dataclass
class LimitsConfig:
    """从 config.limits + config.circuit_breaker 映射（设计 §9.1）。"""
    daily_total: int = 15
    per_session: int = 10
    min_interval_sec: int = 45
    max_interval_sec: int = 120
    burst_size: int = 8                       # 连投上限
    burst_rest_sec: tuple[int, int] = (900, 1800)   # 连投后休息区间（15-30min）
    active_hours: tuple[int, int] = (9, 18)
    active_weekdays_only: bool = True
    cooldown_on_risk_sec: int = 1800           # soft 熔断时长
    warmup_schedule: list[int] = field(default_factory=lambda: [5, 10, 15])

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LimitsConfig":
        """从 config dict 构造（兼容 list/tuple）。"""
        if not d:
            return cls()
        kwargs: dict[str, Any] = {}
        for k in ("daily_total", "per_session", "min_interval_sec", "max_interval_sec",
                  "burst_size", "active_weekdays_only", "cooldown_on_risk_sec"):
            if k in d:
                kwargs[k] = d[k]
        if "burst_rest_sec" in d:
            v = d["burst_rest_sec"]
            kwargs["burst_rest_sec"] = tuple(v) if isinstance(v, (list, tuple)) else v
        if "active_hours" in d:
            v = d["active_hours"]
            kwargs["active_hours"] = tuple(v) if isinstance(v, (list, tuple)) else v
        if "warmup_schedule" in d:
            kwargs["warmup_schedule"] = list(d["warmup_schedule"])
        return cls(**kwargs)


@runtime_checkable
class RateLimiter(Protocol):
    """限流器协议。"""

    def acquire(self, ctx: AcquireContext) -> AcquireDecision:
        """申请是否允许投递。"""
        ...

    def report_result(self, job_id: str, success: bool, risk_signal: str | None = None) -> None:
        """回报单次投递结果（成功/失败/风控信号）。"""
        ...


# ============================================================
# 工具
# ============================================================
def get_date_key(now: float | None = None) -> str:
    """当前本地日期键（如 ``2026-07-10``）。

    用本地时区（Boss 限流按本地日历）。
    """
    n = datetime.fromtimestamp(now) if now is not None else datetime.now()
    return n.strftime("%Y-%m-%d")


# ============================================================
# DryRunLimiter（M1+M2）
# ============================================================
class DryRunLimiter:
    """M1+M2：acquire 永远返回 allow；report_result 只记录不熔断。"""

    def acquire(self, ctx: AcquireContext) -> AcquireDecision:  # noqa: ARG002
        return AcquireDecision(allow=True, reason="dry-run 永远放行")

    def report_result(self, job_id: str, success: bool, risk_signal: str | None = None) -> None:  # noqa: ARG002
        # dry-run 不真扣额度，不熔断
        return


# ============================================================
# RealRateLimiter（M3 真实现，设计 §9.2）
# ============================================================
class RealRateLimiter:
    """M3 真限流器（实现 PRD §4.7.5 全部参数）。

    状态：进程内持有 :class:`~boss_auto_apply.circuit_breaker.CircuitState`；
    持久化部分（daily_quota / hard_trip_count）落 SQLite。
    """

    def __init__(
        self,
        limits: LimitsConfig | dict[str, Any] | None = None,
        conn: Any = None,
        logger_obj: Any = None,
        *,
        now_fn: Any = time.time,
        tz_offset_sec: int = 8 * 3600,   # [假设] 默认东八区（Boss 按北京时间工作日白天）
    ) -> None:
        """初始化限流器。

        Args:
            limits: ``LimitsConfig`` 或 dict（兼容旧 stub 的 dict 参数）。
            conn: SQLite 连接（落 daily_quota；为 None 则不持久化，纯进程内）。
            logger_obj: loguru logger。
            now_fn: 时间函数（测试注入 mock time）。
            tz_offset_sec: 本地时区相对 UTC 的偏移秒（工作日白天检查用）。
        """
        if isinstance(limits, LimitsConfig) or limits is None:
            self.limits = limits or LimitsConfig()
        else:
            self.limits = LimitsConfig.from_dict(limits)
        self.conn = conn
        self.log = logger_obj or logger
        self._now = now_fn
        self._tz_offset = tz_offset_sec
        # CircuitBreaker（传 limits 作 cfg，读 circuit_breaker 子配置；这里 limits 无该子配置，
        # 用默认值。设计要求 limits + circuit_breaker 分开，但 breaker 阈值有默认，故可统一）
        self.breaker = CircuitBreaker(limits, now_fn=now_fn, logger_obj=self.log)

    # ============================================================
    # acquire（六道闸门，设计 §9.2）
    # ============================================================
    def acquire(self, ctx: AcquireContext) -> AcquireDecision:
        """完整闸门检查（按优先级）。

        1. 熔断态
        2. 工作日白天
        3. 预热
        4. 日上限
        5. 连投
        6. 单次间隔

        全部通过 → allow=True，预扣额度（daily_quota.sent_count += 1）。
        """
        now = self._now()
        # 先尝试恢复熔断（到期自动 closed）
        self.breaker.maybe_recover(now=now)
        # 1. 熔断态
        allow, reason, wait = self._check_circuit_open(now)
        if not allow:
            return AcquireDecision(allow=False, reason=reason, wait_sec=wait)
        # 2. 工作日白天
        allow, reason, wait = self._check_active_hours(now)
        if not allow:
            return AcquireDecision(allow=False, reason=reason, wait_sec=wait)
        # 3. 预热
        allow, reason, wait = self._check_warmup_limit(now)
        if not allow:
            return AcquireDecision(allow=False, reason=reason, wait_sec=wait)
        # 4. 日上限
        allow, reason, wait = self._check_daily_total(now)
        if not allow:
            return AcquireDecision(allow=False, reason=reason, wait_sec=wait)
        # 5. 连投
        allow, reason, wait = self._check_burst(now)
        if not allow:
            return AcquireDecision(allow=False, reason=reason, wait_sec=wait)
        # 6. 单次间隔
        allow, reason, wait = self._check_interval(now)
        if not allow:
            return AcquireDecision(allow=False, reason=reason, wait_sec=wait)
        # 全通过 → 预扣额度
        self._pre_acquire(now, ctx)
        return AcquireDecision(allow=True, reason="ok")

    # ============================================================
    # report_result（设计 §9.2）
    # ============================================================
    def report_result(self, job_id: str, success: bool, risk_signal: str | None = None) -> None:
        """投递结果反馈（影响熔断态）。

        1. 更新熔断器（成功归零连续失败；失败 +1；risk_signal → 硬熔断）
        2. success=False 且非风控 → 回滚 daily_quota.sent_count（乐观扣减失败回滚）
        3. risk_signal → daily_quota.risk_events += 1
        4. 自动降级写入 forced_mode
        """
        now = self._now()
        if success:
            self.breaker.report_success()
        else:
            self.breaker.report_failure(risk_signal=risk_signal, now=now)
            # 非风控失败 → 回滚预扣
            if risk_signal is None:
                self._rollback_daily(now)
        # 风控事件计数 + forced_mode 持久化
        if risk_signal is not None:
            self._incr_risk_event(now)
        self._sync_forced_mode()
        self.log.debug(
            f"report_result job={job_id} success={success} risk={risk_signal} "
            f"state={self.breaker.state.state} consec_fail={self.breaker.state.consecutive_failures}"
        )

    # ============================================================
    # 状态查询/重置
    # ============================================================
    def current_state(self):
        """返回当前熔断态（供 status 命令展示）。"""
        return self.breaker.current_state()

    def force_reset(self) -> None:
        """人工重置熔断（人工介入后调用）。"""
        self.breaker.force_reset()
        if self.conn is not None:
            from . import db as db_mod
            date_key = get_date_key(self._now())
            db_mod.update_quota(self.conn, date_key,
                                set_fields={"hard_trip_count": 0, "forced_mode": None,
                                            "cooldown_until": None})

    # ============================================================
    # 内部闸门
    # ============================================================
    def _check_circuit_open(self, now: float) -> tuple[bool, str, float]:
        """熔断态检查。"""
        if self.breaker.is_open(now=now):
            st = self.breaker.state
            wait = st.remaining_sec(now=now)
            return False, f"熔断开启：{st.last_reason}", wait
        return True, "", 0.0

    def _check_active_hours(self, now: float) -> tuple[bool, str, float]:
        """工作日白天检查（active_hours + active_weekdays_only）。

        用本地时区（tz_offset_sec）算星期/小时。
        """
        local = datetime.fromtimestamp(now, tz=timezone.utc) + _dt.timedelta(seconds=self._tz_offset)
        # weekday：周一=0 ... 周日=6
        weekday = local.weekday()
        hour = local.hour
        if self.limits.active_weekdays_only and weekday >= 5:
            # 周末：等到下周一 9 点（粗略）
            days_ahead = 7 - weekday  # 到下周一
            next_monday = local.replace(hour=self.limits.active_hours[0], minute=0, second=0)
            next_monday = next_monday + _dt.timedelta(days=days_ahead)
            wait = (next_monday - local).total_seconds()
            return False, f"周末（weekday={weekday}）不投递，等下周一", max(0.0, wait)
        start_h, end_h = self.limits.active_hours
        if hour < start_h:
            wait = (start_h - hour) * 3600
            return False, f"未到活跃时段（{start_h}:00-{end_h}:00，当前 {hour} 时）", wait
        if hour >= end_h:
            # 当日已过活跃时段 → 等次日 start_h（或下周一）
            tomorrow = local.replace(hour=start_h, minute=0, second=0) + _dt.timedelta(days=1)
            # 若次日是周末则顺延到周一
            if self.limits.active_weekdays_only and tomorrow.weekday() >= 5:
                tomorrow = tomorrow + _dt.timedelta(days=7 - tomorrow.weekday())
            wait = (tomorrow - local).total_seconds()
            return False, f"已过活跃时段（{end_h}:00）等次日", max(0.0, wait)
        return True, "", 0.0

    def _check_warmup_limit(self, now: float) -> tuple[bool, str, float]:
        """预热检查：读 daily_quota 连续运行天数 → warmup_schedule 上限。

        设计 §9.3：第1天≤5、第2天≤10、第3天起≤15。
        连续运行天数 = 历史有投递记录的连续天数（含今天）。
        """
        run_days = self._consecutive_run_days(now)
        schedule = self.limits.warmup_schedule or [self.limits.daily_total]
        # 第 N 天的上限：schedule[N-1]，超出 schedule 长度用 daily_total
        day_limit = schedule[min(run_days - 1, len(schedule) - 1)] if run_days >= 1 else schedule[0]
        sent = self._today_sent(now)
        if sent >= day_limit:
            return False, f"预热期超限（第{run_days}天上限{day_limit}，已发{sent}）", self._sec_to_next_day(now)
        return True, "", 0.0

    def _check_daily_total(self, now: float) -> tuple[bool, str, float]:
        """日上限检查：daily_quota.sent_count ≥ daily_total。"""
        sent = self._today_sent(now)
        if sent >= self.limits.daily_total:
            return False, f"日上限已达（{sent}/{self.limits.daily_total}）", self._sec_to_next_day(now)
        return True, "", 0.0

    def _check_burst(self, now: float) -> tuple[bool, str, float]:
        """连投检查：最近 burst_size 次投递在 burst_rest_sec 内 → 休息。

        设计 §9.3：连投 5-8 次休息 15-30min。
        """
        window = self._load_burst_window(now)
        # 只保留最近 burst_rest_sec[1]（上限）内的
        cutoff = now - self.limits.burst_rest_sec[1]
        recent = [ts for ts in window if ts >= cutoff]
        if len(recent) >= self.limits.burst_size:
            # 休息一个随机区间（设计 §9.3）
            rest = random.randint(self.limits.burst_rest_sec[0], self.limits.burst_rest_sec[1])
            return False, f"连投 {len(recent)} 次达上限 {self.limits.burst_size}，休息 {rest}s", float(rest)
        return True, "", 0.0

    def _check_interval(self, now: float) -> tuple[bool, str, float]:
        """单次间隔检查：距上次投递 ≥ random(min_interval, max_interval)。

        设计 §9.3：正态分布随机间隔（均值 70s，clip [45,120]）。
        每次检查随机一个目标间隔（拟人）。
        """
        last = self._last_send_ts(now)
        if last is None:
            return True, "", 0.0
        target = self._random_interval()
        elapsed = now - last
        if elapsed < target:
            wait = target - elapsed
            return False, f"距上次投递 {elapsed:.0f}s < 间隔 {target:.0f}s", wait
        return True, "", 0.0

    def _random_interval(self) -> float:
        """正态分布随机间隔（均值 70s，clip 到 [min, max]，设计 §9.3）。"""
        mu = (self.limits.min_interval_sec + self.limits.max_interval_sec) / 2
        sigma = (self.limits.max_interval_sec - self.limits.min_interval_sec) / 6
        val = random.gauss(mu, sigma)
        return float(max(self.limits.min_interval_sec, min(self.limits.max_interval_sec, val)))

    # ============================================================
    # 预扣 / 回滚 / 持久化
    # ============================================================
    def _pre_acquire(self, now: float, ctx: AcquireContext) -> None:
        """预扣额度：daily_quota.sent_count += 1，记 last_send_at + burst_window。"""
        if self.conn is None:
            return
        from . import db as db_mod
        date_key = get_date_key(now)
        # burst window：加载现有 → append now → 写回
        window = self._load_burst_window(now)
        window.append(now)
        # 只保留最近 24h
        window = [ts for ts in window if ts >= now - 86400]
        db_mod.update_quota(
            self.conn, date_key,
            incr_fields={"sent_count": 1},
            set_fields={
                "last_send_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "burst_window": json.dumps(window),
            },
        )

    def _rollback_daily(self, now: float) -> None:
        """回滚预扣（非风控失败）：sent_count -= 1（不低于 0）。"""
        if self.conn is None:
            return
        from . import db as db_mod
        date_key = get_date_key(now)
        q = db_mod.get_or_create_quota(self.conn, date_key)
        sent = max(0, (q.get("sent_count") or 0) - 1)
        db_mod.update_quota(self.conn, date_key, set_fields={"sent_count": sent})

    def _incr_risk_event(self, now: float) -> None:
        """风控事件计数 + 硬熔断计数同步。"""
        if self.conn is None:
            return
        from . import db as db_mod
        date_key = get_date_key(now)
        db_mod.update_quota(
            self.conn, date_key,
            incr_fields={"risk_events": 1, "hard_trip_count": self.breaker.state.hard_trip_count_24h},
            set_fields={"cooldown_until": datetime.fromtimestamp(
                self.breaker.state.open_until, tz=timezone.utc).isoformat()},
        )

    def _sync_forced_mode(self) -> None:
        """把 breaker.forced_mode 持久化到 daily_quota。"""
        if self.conn is None:
            return
        from . import db as db_mod
        date_key = get_date_key(self._now())
        db_mod.update_quota(
            self.conn, date_key,
            set_fields={"forced_mode": self.breaker.state.forced_mode},
        )

    # ============================================================
    # 读辅助
    # ============================================================
    def _today_sent(self, now: float) -> int:
        """当日已发数。"""
        if self.conn is None:
            return 0
        from . import db as db_mod
        q = db_mod.get_or_create_quota(self.conn, get_date_key(now))
        return int(q.get("sent_count") or 0)

    def _last_send_ts(self, now: float) -> float | None:
        """上次投递时间戳（从 daily_quota.last_send_at 解析）。"""
        if self.conn is None:
            return None
        from . import db as db_mod
        q = db_mod.get_or_create_quota(self.conn, get_date_key(now))
        last = q.get("last_send_at")
        if not last:
            return None
        try:
            return datetime.fromisoformat(last).timestamp()
        except Exception:
            return None

    def _load_burst_window(self, now: float) -> list[float]:
        """加载 burst_window（JSON 列表）。"""
        if self.conn is None:
            return []
        from . import db as db_mod
        q = db_mod.get_or_create_quota(self.conn, get_date_key(now))
        raw = q.get("burst_window")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [float(ts) for ts in data] if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _consecutive_run_days(self, now: float) -> int:
        """连续运行天数（含今天）。读最近 daily_quota 历史。

        [假设] 从今天往前数，连续有 sent_count>0 的天数（含今天）。
        今天若还没发，则从昨天往前数（返回的是「即将进入的第几天」）。
        """
        if self.conn is None:
            return 1
        from . import db as db_mod
        history = db_mod.get_quota_history(self.conn, days=10)
        by_date = {h["date_key"]: int(h.get("sent_count") or 0) for h in history}
        today = get_date_key(now)
        # 今天已发 → 计入；否则从昨天数
        days = 0
        cur = datetime.fromtimestamp(now, tz=timezone.utc) + _dt.timedelta(seconds=self._tz_offset)
        # 如果今天还没发，从昨天开始数连续天数（+1 表示今天将是下一天）
        if by_date.get(today, 0) == 0:
            days = 1
            cur = cur - _dt.timedelta(days=1)
        while True:
            dk = cur.strftime("%Y-%m-%d")
            if by_date.get(dk, 0) > 0:
                days += 1
                cur = cur - _dt.timedelta(days=1)
            else:
                break
        return max(1, days)

    def _sec_to_next_day(self, now: float) -> float:
        """到次日 0 点（本地）的秒数（粗略，用于 wait 提示）。"""
        local = datetime.fromtimestamp(now, tz=timezone.utc) + _dt.timedelta(seconds=self._tz_offset)
        tomorrow = (local + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0)
        return max(0.0, (tomorrow - local).total_seconds())


# ============================================================
# 工厂
# ============================================================
def get_limiter(mode: str, **kwargs: Any) -> RateLimiter:
    """工厂：dry-run → DryRunLimiter；其他 → RealRateLimiter（M3 实现）。

    Args:
        mode: ``"dry-run"`` / ``"auto"`` / ``"confirm"`` / ``"manual"``。
        **kwargs: RealRateLimiter 参数（limits/conn/logger_obj/now_fn）。

    Returns:
        限流器实例。
    """
    if mode in ("dry-run",):
        return DryRunLimiter()
    return RealRateLimiter(
        limits=kwargs.get("limits"),
        conn=kwargs.get("conn"),
        logger_obj=kwargs.get("logger_obj"),
        now_fn=kwargs.get("now_fn", time.time),
        tz_offset_sec=kwargs.get("tz_offset_sec", 8 * 3600),
    )
