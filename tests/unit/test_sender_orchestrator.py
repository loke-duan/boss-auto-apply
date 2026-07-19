"""test_sender_orchestrator.py — Sender 编排 + 话术校验（设计 §10 / §13.4 / §14.2）。

cases：validate-greet-empty / validate-greet-salary / validate-greet-gap /
validate-greet-adr / validate-greet-truncate / send-dry-run-full /
send-greet-risk-captcha / send-text-only-resume / send-image-only-resume /
circuit-blocks / missing-image-path。

用 mock BrowserManager + mock greeter/chat_sender（注入构造参数）。
"""

from __future__ import annotations

import pytest

from boss_auto_apply.core.sender import Sender, validate_greet_before_send
from boss_auto_apply.browser.web_greeter import GreetResult
from boss_auto_apply.browser.web_chat_sender import ChatSendResult
from boss_auto_apply.errors import (
    CircuitBreakerOpenError,
    GreetValidationError,
)
from boss_auto_apply.models import JobRow
from boss_auto_apply.ratelimiter import AcquireContext, AcquireDecision, DryRunLimiter


# ============================================================
# helpers
# ============================================================
class _FakeGreetResult:
    """GreetResult 工厂。"""
    @staticmethod
    def ok(job_id="j1"):
        return GreetResult(job_id=job_id, relation_established=True, chat_page_opened=True)
    @staticmethod
    def captcha(job_id="j1"):
        return GreetResult(job_id=job_id, risk_signal="captcha")
    @staticmethod
    def not_found(job_id="j1"):
        return GreetResult(job_id=job_id, error="button_not_found")


class FakeGreeter:
    def __init__(self, result=None):
        self.result = result or _FakeGreetResult.ok()
        self.calls = []
    def click_and_greet(self, job, *, dry_run=False):
        self.calls.append({"job": job.job_id, "dry_run": dry_run})
        return self.result


class FakeChatSender:
    def __init__(self, text_ok=True, image_ok=True, image_path="/tmp/x.png"):
        self.text_ok = text_ok
        self.image_ok = image_ok
        self.image_path = image_path
        self.full_calls = []
        self.image_only_calls = []
    def send_full(self, job, greet_text, image_path, *, dry_run=False, wait_relation_sec=8):
        self.full_calls.append({"job": job.job_id, "text": greet_text,
                                 "image": image_path, "dry_run": dry_run})
        return ChatSendResult(job_id=job.job_id, text_sent=self.text_ok,
                              image_sent=self.image_ok, image_path=self.image_path)
    def send_image(self, job, image_path, *, dry_run=False):
        self.image_only_calls.append({"job": job.job_id, "image": image_path, "dry_run": dry_run})
        return self.image_ok


def _make_job(status="image_ready", **kw) -> JobRow:
    base = dict(job_id="j1", target_name="SEO/上海", city="上海", title="SEO 运营",
                company="某公司", status=status,
                tailored_greet="您好，看到贵司 SEO 岗位，希望沟通",
                resume_image_path="/tmp/resume.png")
    base.update(kw)
    return JobRow(**base)


def _make_sender(tmp_conn, *, greeter=None, chat_sender=None, limiter=None):
    """构造 Sender（mock browser_manager + DryRunLimiter 或注入 limiter）。"""
    from boss_auto_apply.config import AppConfig, PipelineCfg, LlmCfg, SearchCfg, SenderCfg
    cfg = AppConfig(
        pipeline=PipelineCfg(mode="dry-run", dry_run=True),
        cities=["上海"], target_constraints={"salary": {"上海": "12-18K"}},
        llm=LlmCfg(backend="claude"), paths={},
        search=SearchCfg(provider="mock"),
        sender=SenderCfg(),
    )
    # mock browser_manager（只 stub 用到的方法）
    class _FakeBrowser:
        def ensure_browser(self): pass
        def ensure_logged_in(self): pass
        def maybe_quit_idle(self): pass
    limiter = limiter or DryRunLimiter()
    return Sender(cfg, tmp_conn, _FakeBrowser(), limiter,
                  greeter=greeter or FakeGreeter(),
                  chat_sender=chat_sender or FakeChatSender())


# ============================================================
# 话术校验（validate_greet_before_send）
# ============================================================
def test_validate_greet_empty_uses_fallback():
    """用例1：空话术 → 用兜底默认（含 title）。"""
    job = _make_job(title="SEO 运营")
    out = validate_greet_before_send("", job=job)
    assert "SEO 运营" in out
    assert "贵司" in out


def test_validate_greet_salary_keyword_rejected():
    """用例2：话术含「薪资」→ GreetValidationError。"""
    with pytest.raises(GreetValidationError, match="薪资"):
        validate_greet_before_send("您好，请问薪资多少？")


def test_validate_greet_gap_keyword_rejected():
    """用例3：话术含「空窗」→ GreetValidationError。"""
    with pytest.raises(GreetValidationError, match="空窗"):
        validate_greet_before_send("我有段空窗期")


def test_validate_greet_adr_forbidden_rejected():
    """用例4：话术含 ADR-0002 禁忌词「精通」→ GreetValidationError。"""
    with pytest.raises(GreetValidationError, match="ADR-0002"):
        validate_greet_before_send("我精通 SEO 优化")


def test_validate_greet_truncate():
    """用例5：超长话术 → 截断（到句号）。"""
    long_text = "您好。我对该岗位很感兴趣。" + "细节" * 200
    out = validate_greet_before_send(long_text, max_len=30)
    assert len(out) <= 30


def test_validate_greet_degree_allowed_when_jd_requires():
    """补充：JD 要求大专时，话术提「大专」放行（学历关键词在 JD degree 中）。"""
    job = _make_job(degree="大专")
    out = validate_greet_before_send("我是大专毕业，符合要求", job=job)
    assert "大专" in out


# ============================================================
# Sender 编排
# ============================================================
def test_send_dry_run_full_flow(tmp_conn):
    """用例6：dry-run 全流程 → text_sent + image_sent（mock 推进状态机）。"""
    sender = _make_sender(tmp_conn)
    from boss_auto_apply import db as db_mod
    db_mod.upsert_job(tmp_conn, _make_job(status="image_ready"))
    res = sender.send_application(_make_job(status="image_ready"), dry_run=True)
    assert res.text_sent is True
    assert res.image_sent is True
    # 状态推进到 image_sent
    job = db_mod.get_job(tmp_conn, "j1")
    assert job.status == "image_sent"


def test_send_greet_captcha_blocks(tmp_conn):
    """用例7：greet 遇 captcha → risk_signal，状态留 image_ready（失败标 failed）。"""
    greeter = FakeGreeter(result=_FakeGreetResult.captcha())
    sender = _make_sender(tmp_conn, greeter=greeter)
    from boss_auto_apply import db as db_mod
    db_mod.upsert_job(tmp_conn, _make_job(status="image_ready"))
    res = sender.send_application(_make_job(status="image_ready"), dry_run=False)
    assert res.risk_signal == "captcha"
    assert res.text_sent is False


def test_send_text_sent_resume_only_image(tmp_conn):
    """用例8：从 text_sent 断点续传 → 只发图片（跳过 greet + 话术）。"""
    chat = FakeChatSender(image_ok=True)
    sender = _make_sender(tmp_conn, chat_sender=chat)
    from boss_auto_apply import db as db_mod
    # 走完整链路到 text_sent
    db_mod.upsert_job(tmp_conn, _make_job(status="text_sent"))
    res = sender.send_application(_make_job(status="text_sent"), dry_run=False)
    assert res.image_sent is True
    # chat_sender.send_image 被调用（send_full 不该被调）
    assert len(chat.image_only_calls) == 1
    assert len(chat.full_calls) == 0


def test_send_greeted_resumes_full_chat(tmp_conn):
    """用例9：从 greeted 续传 → 跳过 greet，发话术+图片（send_full）。"""
    chat = FakeChatSender()
    sender = _make_sender(tmp_conn, chat_sender=chat)
    from boss_auto_apply import db as db_mod
    db_mod.upsert_job(tmp_conn, _make_job(status="greeted"))
    res = sender.send_application(_make_job(status="greeted"), dry_run=False)
    assert res.text_sent is True
    assert res.image_sent is True
    assert len(chat.full_calls) == 1


def test_send_circuit_blocks_raises(tmp_conn):
    """用例10：限流拒绝 → CircuitBreakerOpenError。"""
    class _BlockLimiter:
        def acquire(self, ctx):
            return AcquireDecision(allow=False, reason="熔断中", wait_sec=600)
        def report_result(self, *a, **kw): pass
    sender = _make_sender(tmp_conn, limiter=_BlockLimiter())
    from boss_auto_apply import db as db_mod
    db_mod.upsert_job(tmp_conn, _make_job(status="image_ready"))
    with pytest.raises(CircuitBreakerOpenError):
        sender.send_application(_make_job(status="image_ready"), dry_run=False)


def test_send_missing_image_path_marks_failed(tmp_conn):
    """用例11：image_ready 但无 resume_image_path → 标 failed（缺图）。"""
    chat = FakeChatSender()
    sender = _make_sender(tmp_conn, chat_sender=chat)
    from boss_auto_apply import db as db_mod
    job = _make_job(status="greeted", resume_image_path=None)
    db_mod.upsert_job(tmp_conn, job)
    res = sender.send_application(job, dry_run=False)
    # 缺图 → mark_failed
    assert res.error is not None or res.image_sent is False
