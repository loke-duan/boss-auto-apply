"""test_ratelimiter_stub.py — DryRunLimiter + RealRateLimiter 工厂测试（设计 §5.13）。

M3：RealRateLimiter 已完整实现（设计 §9），不再是 NotImplementedError stub。
RealRateLimiter 的完整限流/熔断逻辑测试在 test_real_ratelimiter.py。
"""

from __future__ import annotations

from boss_auto_apply.ratelimiter import (
    AcquireContext,
    DryRunLimiter,
    RealRateLimiter,
    get_limiter,
)


# ============================================================
# 测试用例
# ============================================================
def test_dry_run_limiter_always_allow():
    """用例1：DryRunLimiter.acquire 永远 allow。"""
    limiter = DryRunLimiter()
    for _ in range(100):
        ctx = AcquireContext(job_id="j1", dry_run=True)
        decision = limiter.acquire(ctx)
        assert decision.allow is True


def test_dry_run_report_result_no_error():
    """用例2：report_result 不抛错。"""
    limiter = DryRunLimiter()
    limiter.report_result("j1", success=True)
    limiter.report_result("j2", success=False, risk_signal="验证码")
    # 不抛即通过


def test_get_limiter_dry_run():
    """用例3：get_limiter('dry-run') → DryRunLimiter。"""
    limiter = get_limiter("dry-run")
    assert isinstance(limiter, DryRunLimiter)


def test_get_limiter_real_is_m3():
    """M3：get_limiter('auto') → RealRateLimiter（已实现，acquire 返回决策而非抛错）。

    RealRateLimiter 在无 conn 时纯进程内运行；默认工作日白天闸门可能阻断，
    故这里只验证返回的是 AcquireDecision（不抛 NotImplementedError）。
    """
    limiter = get_limiter("auto", limits={"daily_total": 15})
    assert isinstance(limiter, RealRateLimiter)
    # acquire 不再抛 NotImplementedError（M3 已实现）
    ctx = AcquireContext(job_id="j1", dry_run=False)
    decision = limiter.acquire(ctx)
    assert hasattr(decision, "allow")
    assert hasattr(decision, "reason")
    report_result = limiter.report_result("j1", success=True)
    assert report_result is None  # report_result 无返回值
