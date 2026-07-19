"""test_web_greeter.py — WebGreeter 点击沟通各种结果（设计 §14.2）。

8 cases：chat-redirect / already-friend / rate-limited / captcha /
button-not-found / continue-chat-button / dry-run / human-click。

用 make_detail_page_tab 构造详情页 MockTab（greet 按钮的 click 回调改 tab 状态），
注入 sleep_fn=lambda 不真睡。
"""

from __future__ import annotations

import pytest

from boss_auto_apply.browser.web_greeter import GreetResult, WebGreeter
from boss_auto_apply.models import JobRow
import dom_samples


def _make_greeter(mgr):
    """构造 WebGreeter，sleep_fn 不真睡。"""
    return WebGreeter(mgr, sleep_fn=lambda *a, **kw: None)


def _make_job(job_id: str = "sec-001") -> JobRow:
    return JobRow(job_id=job_id, target_name="SEO/上海", city="上海",
                  title="SEO 运营", company="某公司")


def _setup_detail(make_browser_manager, *, greet_outcome: str,
                  greet_button_present: bool = True):
    """构造 BrowserManager + WebGreeter，detail tab 已激活到 job-detail 页。

    返回 (greeter, tab, greet_btn)。
    """
    tab = dom_samples.make_detail_page_tab(
        greet_outcome=greet_outcome, greet_button_present=greet_button_present,
    )
    # 注册 chat 页（chat_page outcome 跳转用）
    tab.register_page("/web/geek/chat", {})
    # 激活 detail 页（用 _navigate_to_job 同款 /job_detail/ URL）
    tab.get("https://www.zhipin.com/job_detail/sec-001.html")
    mgr = make_browser_manager(tab=tab)
    g = _make_greeter(mgr)
    greet_btn = tab._current.get(".job-detail-container .btn-greet", [None])[0]
    return g, tab, greet_btn


# ============================================================
# 用例1：点「立即沟通」后等 3s 返回 relation_established（v3 重构）
# ============================================================
def test_greet_success_chat_redirect(make_browser_manager):
    """用例1（v3 重构）：点「立即沟通」后等 3s → relation_established=True。

    v3 变化（2026-07-19 click_timeout 实测复现后重构）：
    Boss 详情页点头次「立即沟通」**不会自动跳转聊天页，也不会给明确成功信号**。
    v3 删除了 7 种成功信号轮询，改为「等 3s 后视为 relation_established=True」，
    让 sender quit DrissionPage，由 nodriver 主动跳 /web/geek/chat 真发文字+图片。
    所以 chat_page_opened=False（Boss 没自动跳），但 relation_established=True。
    """
    g, tab, _ = _setup_detail(make_browser_manager, greet_outcome="chat_page")
    res = g.click_and_greet(_make_job())
    assert res.relation_established is True
    # v3：chat_page_opened=False（Boss 不会自动跳，由 nodriver 接管主动跳）
    assert res.chat_page_opened is False


# ============================================================
# 用例2：点头次后无风控信号 → relation_established=True（v3 删除 already_friend 弹窗检测）
# ============================================================
def test_greet_already_friend(make_browser_manager):
    """用例2（v3）：Boss 已是好友场景的 DOM 在 v3 下不再触发 already_friend。

    v3 删除了 already_friend 弹窗检测（7 种信号之一），改为：
    - 点头次「立即沟通」后只检测风控信号（captcha/rate_limited/login_lost）
    - 3s 内无风控 → relation_established=True（不区分首次还是已好友）
    - 真正的 already_friend 场景由按钮文本=「继续沟通」识别（见用例6）

    所以这里 DOM 即使有 .dialog-already-friend，v3 也视作点头次成功。
    """
    g, tab, _ = _setup_detail(make_browser_manager, greet_outcome="already_friend")
    res = g.click_and_greet(_make_job())
    # v3：点头次后 already_friend=False（不再有 already_friend 弹窗检测路径）
    assert res.already_friend is False
    # 但 relation_established=True（3s 后视为建立）
    assert res.relation_established is True


# ============================================================
# 用例3：操作频繁 → risk_signal='rate_limited'
# ============================================================
def test_greet_rate_limited(make_browser_manager):
    """用例3：点 greet 后出现 .toast 含「操作频繁」→ risk_signal='rate_limited'。"""
    g, tab, _ = _setup_detail(make_browser_manager, greet_outcome="rate_limited")
    res = g.click_and_greet(_make_job())
    assert res.risk_signal == "rate_limited"


# ============================================================
# 用例4：验证码弹窗 → risk_signal='captcha'
# ============================================================
def test_greet_captcha(make_browser_manager):
    """用例4：点 greet 后出现 .captcha → risk_signal='captcha'。"""
    g, tab, _ = _setup_detail(make_browser_manager, greet_outcome="captcha")
    res = g.click_and_greet(_make_job())
    assert res.risk_signal == "captcha"


# ============================================================
# 用例5：找不到 greet 按钮 且 非 continue → error='button_not_found'
# ============================================================
def test_greet_button_not_found(make_browser_manager):
    """用例5：详情页无 greet 按钮也无 continue → error='button_not_found'。"""
    g, tab, _ = _setup_detail(make_browser_manager, greet_outcome="no_button",
                               greet_button_present=False)
    res = g.click_and_greet(_make_job())
    assert res.error == "button_not_found"
    assert res.relation_established is False


# ============================================================
# 用例6：按钮文本=「继续沟通」→ already_friend=True（关系已建，v2 修复）
# ============================================================
def test_greet_continue_chat_button(make_browser_manager, mock_tab):
    """用例6（v2）：按钮 .btn-startchat 但文本=「继续沟通」→ 跳过 click，already_friend=True。

    v2 修复背景（2026-07-19 CDP 诊断）：Boss 详情页「立即沟通」和「继续沟通」
    共用 .btn-startchat class，只能靠 button.text 区分。旧测试用 .btn-continuechat
    selector 是错的（这个 class 在 Boss DOM 里不存在）。
    """
    from dom_samples import MockElement
    # 模拟真实 DOM：.btn-startchat 按钮文本=「继续沟通」
    continue_btn = MockElement(text="继续沟通")
    mock_tab.register_page("/job_detail/", {
        ".btn-startchat": [continue_btn],
        ".job-detail-container .btn-greet": [continue_btn],  # selectors.py 主选择器
    })
    mock_tab.get("https://www.zhipin.com/job_detail/sec-001.html")
    mgr = make_browser_manager(tab=mock_tab)
    g = _make_greeter(mgr)
    res = g.click_and_greet(_make_job())
    assert res.already_friend is True
    assert res.relation_established is True
    # 关键：不应调用 click（continue_btn.click_count == 0）
    assert continue_btn.click_count == 0


# ============================================================
# 用例7：dry_run=True → 不真点，返回 dry_run=True
# ============================================================
def test_greet_dry_run_skips_click(make_browser_manager):
    """用例7：dry_run=True → click_and_greet 只 log 不真点，返回 dry_run=True。"""
    g, tab, greet_btn = _setup_detail(make_browser_manager, greet_outcome="chat_page")
    res = g.click_and_greet(_make_job(), dry_run=True)
    assert res.dry_run is True
    assert res.error == "dry_run"
    # 验证没真点（greet 按钮 click_count 应为 0）
    if greet_btn is not None:
        assert greet_btn.click_count == 0


# ============================================================
# 用例8：_click_with_human_delay 真的调用了 btn.click()
# ============================================================
def test_click_with_human_delay_clicks(make_browser_manager):
    """用例8：_click_with_human_delay 调用 btn.click()（验证点击发生）。"""
    mgr = make_browser_manager()
    g = _make_greeter(mgr)
    btn = dom_samples.MockElement()
    assert btn.click_count == 0
    g._click_with_human_delay(btn)
    assert btn.click_count == 1


# ============================================================
# 用例9：_click_with_human_delay 第一次 click 异常 → 重试一次
# ============================================================
def test_click_with_human_delay_retries(make_browser_manager):
    """补充：第一次 click 抛异常 → 重试成功。"""
    mgr = make_browser_manager()
    g = _make_greeter(mgr)
    btn = dom_samples.MockElement()
    calls = {"n": 0}
    def _click():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("首次点击失败")
        btn.click_count += 1
    btn._click_fn = _click
    g._click_with_human_delay(btn)
    assert calls["n"] == 2  # 重试了一次


# ============================================================
# 用例10：_navigate_to_job 用 /job_detail/{id}.html 构造详情页 URL
# ============================================================
def test_navigate_to_job_uses_job_detail_url(make_browser_manager, mock_tab):
    """补充：_navigate_to_job 导航到 /job_detail/<job_id>.html（与 fetch_detail 同款 URL）。

    历史 bug（已修）：曾用 job-detail?securityId=<job_id>，但 job_id 不是 securityId，
    导致 button_not_found。2026-07-16 实测：/job_detail/{id}.html 能正常加载详情页 +
    渲染「立即沟通」按钮（a.btn.btn-startchat）。
    """
    mgr = make_browser_manager(tab=mock_tab)
    g = _make_greeter(mgr)
    g.browser.ensure_browser()  # 必须，_navigate_to_job 用 get_tab
    g._navigate_to_job(_make_job("abc123"))
    # 断言：用 /job_detail/{job_id}.html，不是 job-detail?securityId=
    assert any("/job_detail/abc123.html" in u for u in mock_tab.get_log), \
        f"应导航到 /job_detail/abc123.html，实际：{mock_tab.get_log}"
    assert not any("job-detail?securityId=abc123" in u for u in mock_tab.get_log), \
        "不应再用 job-detail?securityId=<job_id>（job_id 非 securityId）"


# ============================================================
# 用例11：真登录态丢失 — URL 跳转 /web/user → login_lost
# ============================================================
def test_greet_login_lost_on_url_redirect(make_browser_manager, mock_tab):
    """用例11：点击后 URL 跳到 /web/user → login_lost（真登录态丢失）。

    判断依据是 URL（/web/user），不是 DOM 选择器。
    """
    from dom_samples import MockElement
    tab = mock_tab
    tab.register_page("/job_detail/", {".btn-startchat": []})
    # 详情页有按钮
    greet_btn = MockElement()
    tab.register_page("/job_detail/", {".btn-startchat": [greet_btn]})

    def _on_click():
        # 点击后跳转到登录页（真登录态丢失）
        tab.url = "https://www.zhipin.com/web/user/"
        tab._current = {}
    greet_btn._click_fn = _on_click

    tab.get("https://www.zhipin.com/job_detail/sec-001.html")
    mgr = make_browser_manager(tab=tab)
    g = _make_greeter(mgr)
    res = g.click_and_greet(_make_job())
    assert res.risk_signal == "login_lost"
    assert res.error == "login_lost"


# ============================================================
# 用例12：登录推广区块常驻但不误判（防 login_lost 误判回归）
# ============================================================
def test_greet_no_false_login_lost_with_sign_form_present(make_browser_manager, mock_tab):
    """用例12：详情页底部常驻 .sign-form 登录推广区块（已登录），点击后 URL 不变
    → 不应误判 login_lost（2026-07-17 实测 bug 回归防护）。

    历史 bug：risk.login_page_redirect 含 .sign-form，匹配到详情页底部营销区块，
    导致每次 greet 都误判 login_lost → 24h 误熔断。
    """
    from dom_samples import MockElement
    tab = mock_tab
    greet_btn = MockElement()
    # 详情页：有 greet 按钮 + 常驻 .sign-form 营销区块（但已登录）
    tab.register_page("/job_detail/", {
        ".btn-startchat": [greet_btn],
        ".sign-form": [MockElement(text="找工作\nBOSS直聘直接谈\n登录")],
        ".login-box": [MockElement(text="找工作")],
    })

    def _on_click():
        # 点击后 URL 不变（正常情况：弹窗或无反应），.sign-form 仍在
        # 不应判 login_lost
        pass
    greet_btn._click_fn = _on_click

    tab.get("https://www.zhipin.com/job_detail/sec-001.html")
    mgr = make_browser_manager(tab=tab)
    g = _make_greeter(mgr)
    res = g.click_and_greet(_make_job())
    # 核心：不判 login_lost（即使 .sign-form 存在）
    assert res.risk_signal != "login_lost", \
        f"误判 login_lost！.sign-form 是详情页常驻营销区块，URL 未跳转不应判登录态丢失。result={res}"
