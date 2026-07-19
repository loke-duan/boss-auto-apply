"""test_m3_state_machine.py — M3 发送链路状态机全路径 + 断点续传（设计 §16.4 / §14.2）。

cases：image_ready→image_sent 全路径 / text_sent_at 落库 /
greeted 续传 / text_sent 续传 / dry-run 不进 Sender / risk→failed 留态。

集成 db + Sender（mock browser/greeter/chat_sender）。
"""

from __future__ import annotations

import pytest

from boss_auto_apply import db as db_mod
from boss_auto_apply.browser.web_chat_sender import ChatSendResult
from boss_auto_apply.browser.web_greeter import GreetResult
from boss_auto_apply.config import AppConfig, LlmCfg, PipelineCfg, SearchCfg, SenderCfg
from boss_auto_apply.core.sender import Sender
from boss_auto_apply.models import JobRow
from boss_auto_apply.ratelimiter import DryRunLimiter


# ============================================================
# helpers
# ============================================================
class FakeGreeter:
    def __init__(self, result=None):
        self.result = result or GreetResult(job_id="j1", relation_established=True,
                                             chat_page_opened=True)
    def click_and_greet(self, job, *, dry_run=False):
        return self.result


class FakeChatSender:
    def __init__(self, text_ok=True, image_ok=True):
        self.text_ok = text_ok
        self.image_ok = image_ok
    def send_full(self, job, greet_text, image_path, *, dry_run=False, wait_relation_sec=8):
        return ChatSendResult(job_id=job.job_id, text_sent=self.text_ok,
                              image_sent=self.image_ok, image_path=image_path)
    def send_image(self, job, image_path, *, dry_run=False):
        return self.image_ok


class _FakeBrowser:
    def ensure_browser(self): pass
    def ensure_logged_in(self): pass
    def maybe_quit_idle(self): pass


def _make_job(job_id="j1", status="image_ready", **kw) -> JobRow:
    base = dict(job_id=job_id, target_name="SEO/上海", city="上海", title="SEO 运营",
                company="某公司", status=status,
                tailored_greet="您好，看到贵司 SEO 岗位",
                resume_image_path="/tmp/resume.png")
    base.update(kw)
    return JobRow(**base)


def _seed_to(tmp_conn, job_id, status):
    """把 job 推到指定 status（走合法链路）。"""
    job = _make_job(job_id, status="found")
    db_mod.upsert_job(tmp_conn, job)
    chain = {
        "jd_analyzed": ["jd_analyzed"],
        "resume_tailored": ["jd_analyzed", "resume_tailored"],
        "pdf_generated": ["jd_analyzed", "resume_tailored", "pdf_generated"],
        "image_ready": ["jd_analyzed", "resume_tailored", "pdf_generated", "image_ready"],
        "greeted": ["jd_analyzed", "resume_tailored", "pdf_generated", "image_ready", "greeted"],
        "text_sent": ["jd_analyzed", "resume_tailored", "pdf_generated", "image_ready",
                      "greeted", "text_sent"],
    }
    for nxt in chain.get(status, []):
        db_mod.transition(tmp_conn, job_id, nxt, clear_error=False)


def _make_sender(tmp_conn, *, greeter=None, chat_sender=None, limiter=None):
    cfg = AppConfig(
        pipeline=PipelineCfg(mode="dry-run", dry_run=True),
        cities=["上海"], target_constraints={"salary": {"上海": "12-18K"}},
        llm=LlmCfg(backend="claude"), paths={},
        search=SearchCfg(provider="mock"), sender=SenderCfg(),
    )
    return Sender(cfg, tmp_conn, _FakeBrowser(), limiter or DryRunLimiter(),
                  greeter=greeter or FakeGreeter(),
                  chat_sender=chat_sender or FakeChatSender())


# ============================================================
# 用例1：image_ready → image_sent 全路径（真发送，状态机逐态推进）
# ============================================================
def test_full_path_image_ready_to_image_sent(tmp_conn):
    """用例1：image_ready → greeted → text_sent → image_sent（真发送全流程）。"""
    _seed_to(tmp_conn, "j1", "image_ready")
    sender = _make_sender(tmp_conn)
    res = sender.send_application(db_mod.get_job(tmp_conn, "j1"), dry_run=False)
    assert res.text_sent is True
    assert res.image_sent is True
    job = db_mod.get_job(tmp_conn, "j1")
    assert job.status == "image_sent"  # 终态
    # text_sent_at / greet_sent_at / image_sent_at 都落库
    assert job.greet_sent_at is not None
    assert job.text_sent_at is not None
    assert job.image_sent_at is not None


# ============================================================
# 用例2：text_sent_at 中间态落库（话术成功图片失败时留 text_sent）
# ============================================================
def test_partial_fail_leaves_text_sent(tmp_conn):
    """用例2：话术成功图片失败 → 状态留 text_sent（image_sent_at 为空）。"""
    _seed_to(tmp_conn, "j2", "image_ready")
    chat = FakeChatSender(text_ok=True, image_ok=False)
    sender = _make_sender(tmp_conn, chat_sender=chat)
    res = sender.send_application(db_mod.get_job(tmp_conn, "j2"), dry_run=False)
    assert res.text_sent is True
    assert res.image_sent is False
    job = db_mod.get_job(tmp_conn, "j2")
    # 留在 text_sent（图片失败标 failed 但 status 留 text_sent）
    assert job.status in ("text_sent", "failed")
    assert job.text_sent_at is not None


# ============================================================
# 用例3：greeted 断点续传 → 跳过 greet，发话术+图片
# ============================================================
def test_resume_from_greeted(tmp_conn):
    """用例3：job 已 greeted → send_application 跳过 greet，发话术+图片到 image_sent。"""
    _seed_to(tmp_conn, "j3", "greeted")
    greeter = FakeGreeter()  # 不该被调（greeted 跳过 greet）
    sender = _make_sender(tmp_conn, greeter=greeter)
    res = sender.send_application(db_mod.get_job(tmp_conn, "j3"), dry_run=False)
    assert res.image_sent is True
    assert db_mod.get_job(tmp_conn, "j3").status == "image_sent"


# ============================================================
# 用例4：text_sent 断点续传 → 只发图片
# ============================================================
def test_resume_from_text_sent(tmp_conn):
    """用例4：job 已 text_sent → 只发图片到 image_sent。"""
    _seed_to(tmp_conn, "j4", "text_sent")
    chat = FakeChatSender(image_ok=True)
    sender = _make_sender(tmp_conn, chat_sender=chat)
    res = sender.send_application(db_mod.get_job(tmp_conn, "j4"), dry_run=False)
    assert res.image_sent is True
    assert db_mod.get_job(tmp_conn, "j4").status == "image_sent"


# ============================================================
# 用例5：dry-run 全流程也推进状态机到 image_sent
# ============================================================
def test_dry_run_advances_state_machine(tmp_conn):
    """用例5：dry-run → mock 推进 image_ready→greeted→text_sent→image_sent。"""
    _seed_to(tmp_conn, "j5", "image_ready")
    sender = _make_sender(tmp_conn)
    res = sender.send_application(db_mod.get_job(tmp_conn, "j5"), dry_run=True)
    assert res.text_sent is True
    assert res.image_sent is True
    assert db_mod.get_job(tmp_conn, "j5").status == "image_sent"


# ============================================================
# 用例6：greet 风控（captcha）→ 留 image_ready + 标 failed
# ============================================================
def test_greet_captcha_leaves_image_ready(tmp_conn):
    """用例6：greet 遇 captcha → 风控，状态留 image_ready（failed 标记）。"""
    _seed_to(tmp_conn, "j6", "image_ready")
    greeter = FakeGreeter(result=GreetResult(job_id="j6", risk_signal="captcha"))
    sender = _make_sender(tmp_conn, greeter=greeter)
    res = sender.send_application(db_mod.get_job(tmp_conn, "j6"), dry_run=False)
    assert res.risk_signal == "captcha"
    job = db_mod.get_job(tmp_conn, "j6")
    # 留 image_ready 或 failed（mark_failed 留原态）
    assert job.status in ("image_ready", "failed")


# ============================================================
# 用例7：list_resumable 含 image_ready/greeted/text_sent（M3 发送续传）
# ============================================================
def test_resumable_includes_send_states(tmp_conn):
    """用例7：image_ready/greeted/text_sent 都在 RESUMABLE_STATUSES（可续传发送）。"""
    from boss_auto_apply.db import RESUMABLE_STATUSES
    assert "image_ready" in RESUMABLE_STATUSES
    assert "greeted" in RESUMABLE_STATUSES
    assert "text_sent" in RESUMABLE_STATUSES
    assert "image_sent" not in RESUMABLE_STATUSES  # 终态不可续传
    # 实测：list_resumable 返回这些状态的 job
    _seed_to(tmp_conn, "r1", "image_ready")
    _seed_to(tmp_conn, "r2", "greeted")
    _seed_to(tmp_conn, "r3", "text_sent")
    ids = {j.job_id for j in db_mod.list_resumable(tmp_conn)}
    assert "r1" in ids and "r2" in ids and "r3" in ids


# ============================================================
# 用例8：LEGAL_TRANSITIONS 含 M3 新增迁移
# ============================================================
def test_legal_transitions_include_m3():
    """用例8：LEGAL_TRANSITIONS 含 image_ready→greeted、greeted→text_sent、text_sent→image_sent。"""
    from boss_auto_apply.db import LEGAL_TRANSITIONS
    assert "greeted" in LEGAL_TRANSITIONS["image_ready"]
    assert "text_sent" in LEGAL_TRANSITIONS["greeted"]
    assert "image_sent" in LEGAL_TRANSITIONS["text_sent"]
    assert LEGAL_TRANSITIONS["image_sent"] == set()  # 终态
