"""test_browser_manager.py — BrowserManager 单例/启动/复用/关闭/登录态/冲突/stealth（设计 §14.2）。

10 cases：singleton / start / reuse / quit / login-detect / login-fail /
conflict-blacklist / stealth-options / stealth-inject / idle-quit。

全部注入 FakeChromium / FakeChromiumOptions，不真启动 Chrome。
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from boss_auto_apply.browser.manager import (
    BOSS_RECOMMEND_URL,
    BrowserConfig,
    BrowserManager,
    LoginStatus,
)
from boss_auto_apply.errors import (
    BrowserConflictError,
    BrowserLaunchError,
    LoginRequiredError,
)


# ============================================================
# 用例1：单例 — get_instance 多次返回同一实例
# ============================================================
def test_singleton_get_instance(make_browser_manager):
    """用例1：get_instance 多次返回同一实例（cfg 仅首次生效）。"""
    BrowserManager.reset_instance()
    mgr1 = BrowserManager.get_instance(BrowserConfig(check_chrome_running=False))
    mgr2 = BrowserManager.get_instance(BrowserConfig(headless=True))  # 新 cfg 被忽略
    assert mgr1 is mgr2
    assert mgr1.cfg.check_chrome_running is False  # 首次 cfg 生效
    BrowserManager.reset_instance()


# ============================================================
# 用例2：ensure_browser 启动浏览器并返回 tab
# ============================================================
def test_ensure_browser_starts_and_returns_tab(make_browser_manager):
    """用例2：ensure_browser 首次启动，返回 latest_tab。"""
    mgr = make_browser_manager()
    tab = mgr.ensure_browser()
    assert tab is not None
    assert mgr._browser is not None
    assert mgr.is_alive()


# ============================================================
# 用例3：已启动时复用（force_new=False）
# ============================================================
def test_ensure_browser_reuses_when_alive(make_browser_manager):
    """用例3：浏览器活着时，第二次 ensure_browser 复用，不重启。"""
    mgr = make_browser_manager()
    tab1 = mgr.ensure_browser()
    first_browser = mgr._browser
    tab2 = mgr.ensure_browser(force_new=False)
    assert mgr._browser is first_browser  # 没重建
    assert tab1 is tab2  # 同一 tab


# ============================================================
# 用例4：force_new=True 强制重启
# ============================================================
def test_force_new_restarts_browser(make_browser_manager):
    """用例4：force_new=True 关旧实例再启新。"""
    mgr = make_browser_manager()
    mgr.ensure_browser()
    old = mgr._browser
    assert old.quit_count == 0
    mgr.ensure_browser(force_new=True)
    assert old.quit_count == 1  # 旧实例被 quit


# ============================================================
# 用例5：quit 后状态清空
# ============================================================
def test_quit_clears_state(make_browser_manager):
    """用例5：quit 后 _browser/_tab 清空，is_alive=False。"""
    mgr = make_browser_manager()
    mgr.ensure_browser()
    assert mgr.is_alive()
    mgr.quit()
    assert mgr._browser is None
    assert mgr._tab is None
    assert not mgr.is_alive()


# ============================================================
# 用例6：check_login 已登录（cookie + DOM 双策略）
# ============================================================
def test_check_login_logged_in(make_browser_manager, mock_tab):
    """用例6：check_login 检测到 wt2 + zp_at cookie 且未被重定向 → logged_in=True。"""
    mock_tab.register_page("/web/geek/job-recommend", {})
    mock_tab.set_cookies([
        {"name": "wt2", "value": "x"},
        {"name": "zp_at", "value": "z"},
        {"name": "__zp_stoken__", "value": "y"},
    ])
    mgr = make_browser_manager(tab=mock_tab)
    status = mgr.check_login()
    assert isinstance(status, LoginStatus)
    assert status.logged_in is True
    assert status.has_stoken is True
    assert status.has_wt2 is True
    assert status.has_zp_at is True
    # 导航到了 recommend 页
    assert any(BOSS_RECOMMEND_URL in u for u in mock_tab.get_log)


# ============================================================
# 用例7：check_login 未登录（无 cookie 无 DOM）
# ============================================================
def test_check_login_not_logged_in(make_browser_manager, mock_tab):
    """用例7：check_login 无登录 cookie 无 DOM → logged_in=False。"""
    mock_tab.register_page("/web/geek/job-recommend", {})
    mgr = make_browser_manager(tab=mock_tab)
    status = mgr.check_login()
    assert status.logged_in is False
    assert status.has_stoken is False


# ============================================================
# 用例8：ensure_logged_in 未登录抛 LoginRequiredError
# ============================================================
def test_ensure_logged_in_raises_when_not_logged(make_browser_manager, mock_tab):
    """用例8：未登录时 ensure_logged_in 抛 LoginRequiredError。"""
    mock_tab.register_page("/web/geek/job-recommend", {})
    mgr = make_browser_manager(tab=mock_tab)
    with pytest.raises(LoginRequiredError):
        mgr.ensure_logged_in()


# ============================================================
# 用例9：误用日常 profile 黑名单 → BrowserConflictError
# ============================================================
def test_conflict_daily_profile_blacklist():
    """用例9：user_data_dir 指向日常 Chrome profile → BrowserConflictError。"""
    mgr = BrowserManager(BrowserConfig(
        user_data_dir="~/Library/Application Support/Google/Chrome/Default",
        check_chrome_running=False,
    ))
    with pytest.raises(BrowserConflictError, match="日常 Chrome profile"):
        mgr._check_chrome_conflict()


# ============================================================
# 用例10：stealth options 应用（--disable-blink-features 等）
# ============================================================
def test_apply_stealth_options_sets_anti_detect_args(make_browser_manager):
    """用例10：_apply_stealth_options 设置关键反检测参数。"""
    mgr = make_browser_manager(cfg=BrowserConfig(
        user_data_dir="/tmp/test-profile", check_chrome_running=False,
    ))
    # 复用 conftest 的 FakeChromiumOptions（通过 mgr 内部 options_cls 旁路）
    # 这里直接用一个轻量 mock 对象记录调用
    class _Opts:
        def __init__(self): self.arguments=[]; self.user_agent=None; self.user_data_path=None
        def set_user_data_path(self, p): self.user_data_path=p; return self
        def headless(self, f=True): return self
        def auto_port(self): return self
        def set_argument(self, a): self.arguments.append(a); return self
        def set_browser_path(self, p): return self
        def set_user_agent(self, u): self.user_agent=u; return self
        def set_load_mode(self, m): return self
    co = _Opts()
    mgr._apply_stealth_options(co)
    # 关键反检测参数
    assert any("disable-blink-features=AutomationControlled" in a for a in co.arguments)
    assert co.user_agent  # UA 被设置
    assert co.user_data_path == "/tmp/test-profile"
    # 窗口大小参数
    assert any(a.startswith("--window-size=") for a in co.arguments)


# ============================================================
# 用例11：stealth.js 注入（run_cdp addScriptToEvaluateOnNewDocument 被调用）
# ============================================================
def test_inject_stealth_js_calls_cdp(make_browser_manager, mock_tab):
    """用例11：stealth=True 时 _inject_stealth_js 调 run_cdp addScriptToEvaluateOnNewDocument。"""
    mgr = make_browser_manager(tab=mock_tab)
    mgr._inject_stealth_js(mock_tab)
    # run_cdp 被调用且 method 含 addScriptToEvaluateOnNewDocument
    assert any(method == "Page.addScriptToEvaluateOnNewDocument"
               for method, _ in mock_tab.run_cdp_log)


# ============================================================
# 用例12：maybe_quit_idle 空闲超时关闭
# ============================================================
def test_maybe_quit_idle_triggers_after_timeout(make_browser_manager):
    """用例12：空闲超过 auto_quit_idle_sec → maybe_quit_idle 返回 True 并 quit。"""
    mgr = make_browser_manager(cfg=BrowserConfig(
        check_chrome_running=False, auto_quit_idle_sec=1,
    ))
    mgr.ensure_browser()
    # 模拟 last_active_ts 是 2 秒前（> auto_quit_idle_sec=1）
    mgr._last_active_ts = time.time() - 2
    triggered = mgr.maybe_quit_idle()
    assert triggered is True
    assert mgr._browser is None  # 已 quit


# ============================================================
# 用例13：maybe_quit_idle 未超时不关闭
# ============================================================
def test_maybe_quit_idle_no_quit_when_recent(make_browser_manager):
    """用例13：刚活跃（< auto_quit_idle_sec）→ maybe_quit_idle 返回 False。"""
    mgr = make_browser_manager(cfg=BrowserConfig(
        check_chrome_running=False, auto_quit_idle_sec=600,
    ))
    mgr.ensure_browser()
    triggered = mgr.maybe_quit_idle()
    assert triggered is False
    assert mgr._browser is not None  # 未 quit


# ============================================================
# 用例14：_build_browser 异常 → BrowserLaunchError
# ============================================================
def test_build_browser_failure_raises(make_browser_manager):
    """用例14：Chromium(co) 抛异常 → ensure_browser 包装成 BrowserLaunchError。"""
    class _BoomChrome:
        def __init__(self, co):  # noqa: ARG002
            raise ConnectionError("chrome 死了")
    mgr = make_browser_manager(chromium_cls=_BoomChrome)
    with pytest.raises(BrowserLaunchError, match="Chrome 启动失败"):
        mgr.ensure_browser()
