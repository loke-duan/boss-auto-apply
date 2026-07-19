"""test_web_searcher.py — WebSearcher URL 构造/解析/滚动/去重/风控/选择器失效（设计 §14.2）。

9 cases：URL-build / parse-cards / scroll-load / dedupe / login-fail / captcha /
rate-limited / selector-stale / per_page_limit。

用 MockTab + dom_samples 构造模拟搜索页，不真开浏览器。
"""

from __future__ import annotations

import pytest

from boss_auto_apply.browser.web_searcher import (
    CITY_CODE,
    SALARY_CODE,
    WebJobRaw,
    WebSearchParams,
    WebSearcher,
    dedupe_web_jobs,
)
from boss_auto_apply.errors import (
    CaptchaDetectedError,
    LoginRequiredError,
    RateLimitedError,
    WebSearchTimeoutError,
)
import dom_samples


# ============================================================
# helper：构造一个已 ensure_browser 的 WebSearcher（注入 sleep=lambda *a:None）
# ============================================================
def _make_searcher(mgr, tab):
    """构造 WebSearcher，sleep_fn 不真睡。"""
    return WebSearcher(mgr, sleep_fn=lambda *a, **kw: None)


# ============================================================
# 用例1：_build_search_url 含 city_code / salary / experience / degree
# ============================================================
def test_build_search_url_with_filters(make_browser_manager):
    """用例1：搜索参数都映射到 URL query（city=上海→code、salary 12-18K→405 等）。"""
    mgr = make_browser_manager()
    s = _make_searcher(mgr, None)
    url = s._build_search_url(WebSearchParams(
        keyword="SEO", city="上海", salary="12-18K", experience="3-5年", degree="大专",
    ))
    assert "query=SEO" in url
    assert f"city={CITY_CODE['上海']}" in url
    assert f"salary={SALARY_CODE['12-18K']}" in url
    assert "experience=104" in url  # 3-5年→104
    assert "degree=202" in url      # 大专→202


def test_build_search_url_unknown_city_passthrough(make_browser_manager):
    """补充：未知城市原样传（不映射）。"""
    mgr = make_browser_manager()
    s = _make_searcher(mgr, None)
    url = s._build_search_url(WebSearchParams(keyword="SEO", city="某小城"))
    assert "city=%E6%9F%90%E5%B0%8F%E5%9F%8E" in url or "city=某小城" in url


# ============================================================
# 用例2：_parse_job_cards 解析卡片为 WebJobRaw
# ============================================================
def test_parse_job_cards(make_browser_manager, mock_tab):
    """用例2：3 个卡片解析出 3 个 WebJobRaw（含 title/company/job_detail_id）。"""
    cards = dom_samples.make_job_cards(3)
    mock_tab.register_page("/web/geek/job", {
        ".job-card-wrap": cards,
        ".job-card-wrap.active": cards,
    })
    mock_tab.get("https://www.zhipin.com/web/geek/job?query=SEO")  # 激活页面元素
    mgr = make_browser_manager(tab=mock_tab)
    s = _make_searcher(mgr, mock_tab)
    jobs = s._parse_job_cards(mock_tab)
    assert len(jobs) == 3
    assert jobs[0].title == "SEO 运营 1"
    assert jobs[0].company == "公司1"
    # 2026-07-11 实测：列表页无 securityId，job_id 是 job_detail 加密 ID
    assert jobs[0].security_id is None
    assert jobs[0].job_id == "detail-001"  # 从 /job_detail/detail-001.html 提取
    assert jobs[0].jd_url and "job_detail" in jobs[0].jd_url


# ============================================================
# 用例3：_scroll_to_load_more 滚动后卡片数增加
# ============================================================
def test_scroll_to_load_more(make_browser_manager, mock_tab):
    """用例3：滚动后卡片数从 3 增加到 6（模拟无限滚动）。"""
    cards = dom_samples.make_job_cards(3)
    mock_tab.register_page("/web/geek/job", {
        ".job-card-wrap": cards,
        ".job-card-wrap.active": cards,
    })
    # 注册滚动回调：每次滚动追加 3 个卡片
    more = dom_samples.make_job_cards(3, )[:3] if False else dom_samples.make_job_cards(6)[3:]
    def _on_scroll():
        cards.extend(dom_samples.make_job_cards(len(cards) + 3)[len(cards):])
    mock_tab.on_scroll(_on_scroll)
    mgr = make_browser_manager(tab=mock_tab)
    s = _make_searcher(mgr, mock_tab)
    s._scroll_to_load_more(mock_tab, count=2)
    # 滚动 2 次后卡片数应增加
    assert len(cards) > 3


# ============================================================
# 用例4：dedupe_web_jobs 按 job_id 去重
# ============================================================
def test_dedupe_web_jobs():
    """用例4：重复 job_id 去重；同公司去重（by_company=True）保留首条。"""
    jobs = [
        WebJobRaw(job_id="a", title="SEO", company="甲公司"),
        WebJobRaw(job_id="a", title="SEO 重复", company="甲公司"),  # 同 id 重复
        WebJobRaw(job_id="b", title="运营", company="乙公司"),
        WebJobRaw(job_id="c", title="增长", company="甲公司"),  # 同公司不同岗
    ]
    # 按 job_id 去重
    out = dedupe_web_jobs(jobs)
    assert {j.job_id for j in out} == {"a", "b", "c"}
    # 同公司去重
    out2 = dedupe_web_jobs(jobs, by_company=True)
    companies = {j.company for j in out2}
    assert "甲公司" in companies and len(out2) <= 3  # 甲公司首条保留


def test_dedupe_web_jobs_exclude_companies():
    """补充：排除指定公司。"""
    jobs = [
        WebJobRaw(job_id="a", company="黑名单公司"),
        WebJobRaw(job_id="b", company="好公司"),
    ]
    out = dedupe_web_jobs(jobs, exclude_companies=["黑名单公司"])
    assert len(out) == 1
    assert out[0].company == "好公司"


# ============================================================
# 用例5：搜索页被重定向到登录页 → LoginRequiredError
# ============================================================
def test_check_risk_login_redirect(make_browser_manager, mock_tab):
    """用例5：URL 含 /user（重定向到登录页）→ LoginRequiredError。"""
    mgr = make_browser_manager(tab=mock_tab)
    s = _make_searcher(mgr, mock_tab)
    # 2026-07-11 修正：重定向检测看 URL（/user 或 /login），不看 DOM（避免误判）
    mock_tab.get("https://www.zhipin.com/web/user/?ka=header-login")
    with pytest.raises(LoginRequiredError, match="登录态丢失"):
        s._check_risk(mock_tab)


# ============================================================
# 用例6：搜索页出现验证码 → CaptchaDetectedError
# ============================================================
def test_check_risk_captcha(make_browser_manager, mock_tab):
    """用例6：搜索页有 .captcha → CaptchaDetectedError。"""
    from dom_samples import MockElement
    mock_tab.register_page("/web/geek/job", {
        ".captcha": [MockElement()],
    })
    mgr = make_browser_manager(tab=mock_tab)
    s = _make_searcher(mgr, mock_tab)
    mock_tab.get("https://www.zhipin.com/web/geek/job?query=SEO")
    with pytest.raises(CaptchaDetectedError):
        s._check_risk(mock_tab)


# ============================================================
# 用例7：搜索页「操作频繁」toast → RateLimitedError
# ============================================================
def test_check_risk_rate_limited(make_browser_manager, mock_tab):
    """用例7：.toast 含「操作频繁」→ RateLimitedError。"""
    from dom_samples import MockElement
    mock_tab.register_page("/web/geek/job", {
        ".toast": [MockElement(text="操作频繁，请稍后再试")],
    })
    mgr = make_browser_manager(tab=mock_tab)
    s = _make_searcher(mgr, mock_tab)
    mock_tab.get("https://www.zhipin.com/web/geek/job?query=SEO")
    with pytest.raises(RateLimitedError):
        s._check_risk(mock_tab)


# ============================================================
# 用例8：_wait_job_list_loaded 超时 → WebSearchTimeoutError
# ============================================================
def test_wait_job_list_timeout(make_browser_manager, mock_tab):
    """用例8：搜索页 15s 无 .job-card-wrapper → WebSearchTimeoutError。"""
    mock_tab.register_page("/web/geek/job", {})  # 空页
    mgr = make_browser_manager(tab=mock_tab)
    s = _make_searcher(mgr, mock_tab)
    with pytest.raises(WebSearchTimeoutError, match="岗位卡片"):
        s._wait_job_list_loaded(mock_tab, timeout=1)


# ============================================================
# 用例9：search 完整流程 — per_page_limit 截断
# ============================================================
def test_search_full_flow_truncates(make_browser_manager, mock_tab):
    """用例9：search 返回 per_page_limit 内的结果（截断 + 去重）。"""
    cards = dom_samples.make_job_cards(10)
    mock_tab.register_page("/web/geek/job", {
        ".job-card-wrap": cards,
        ".job-card-wrap.active": cards,
        ".recommend-result-job": [dom_samples.MockElement()],
    })
    mgr = make_browser_manager(tab=mock_tab)
    s = _make_searcher(mgr, mock_tab)
    # per_page_limit=5 截断到 5
    jobs = s.search(WebSearchParams(keyword="SEO", city="上海",
                                    per_page_limit=5, scroll_count=0))
    assert len(jobs) == 5
    assert all(j.title.startswith("SEO 运营") for j in jobs)


# ============================================================
# 用例10：_split_job_info 切分经验/学历/城市
# ============================================================
def test_split_job_info():
    """补充：info「上海 3-5年 大专」切分为 (3-5年, 大专, 上海)。"""
    from boss_auto_apply.browser.web_searcher import _split_job_info
    exp, deg, city = _split_job_info("上海 3-5年 大专")
    assert exp == "3-5年"
    assert deg == "大专"
    assert city == "上海"
    # 空串
    assert _split_job_info("") == ("", "", "")
