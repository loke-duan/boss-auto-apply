"""WebSearcher — 网页版岗位搜索（设计 §5，替代 BossCliSearcher 主路径）。

在 DrissionPage 操控的真实 Chrome 里，导航到 Boss 网页搜索页，从 DOM 抓取
岗位列表，滚动加载更多，去重，与现有 :class:`~boss_auto_apply.core.searcher.JobRaw` 对齐。

⚠️ **范围说明**（设计 §5 开头）：WebSearcher 是 M3 新增能力，但**默认不在 M3 run
流程里调用**（M1+M2 已用 Mock/BossCli 入库的 job 直接进发送）。供「增量搜新岗」（M4）
和「重新搜」场景用。M3 主要保证发送链路；WebSearcher 接口先实现好，便于 M4 复用。

dry-run 安全：WebSearcher 是只读操作（搜索/抓取），不产生写副作用，
故无 dry_run 参数（dry-run 也可正常搜索）。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from ..errors import (
    CaptchaDetectedError,
    DomSelectorStaleError,
    LoginRequiredError,
    RateLimitedError,
    WebSearchTimeoutError,
)
from . import selectors as sel
from .manager import BrowserManager

__all__ = [
    "WebSearchParams",
    "WebJobRaw",
    "WebSearcher",
    "dedupe_web_jobs",
    "CITY_CODE",
    "SALARY_CODE",
    "EXPERIENCE_CODE",
    "DEGREE_CODE",
]


@dataclass
class WebSearchParams:
    """网页搜索参数（对齐 :class:`~boss_auto_apply.core.searcher.SearchParams`）。"""
    keyword: str                    # 如 "SEO"
    city: str = "全国"               # 中文名（映射 city_code）
    salary: str = ""                # 如 "12-18K"（映射 Boss salary 代码）
    experience: str = ""            # 如 "3-5年"
    degree: str = ""                # 如 "大专"
    page: int = 1                   # 页码（Boss 网页分页/无限滚动）
    scroll_count: int = 3           # 滚动加载次数（无限滚动场景）
    per_page_limit: int = 30        # 单页/单次抓取上限


@dataclass
class WebJobRaw:
    """从网页 DOM 抓的单个岗位（与 searcher.JobRaw 对齐）。"""
    job_id: str                     # securityId（从卡片 data 属性或链接抓）
    title: str = ""
    company: str = ""
    city: str = ""
    salary: str = ""
    experience: str = ""
    degree: str = ""
    jd_url: str | None = None
    security_id: str | None = None  # Boss 用 securityId 查详情/沟通
    brand_id: str | None = None
    raw_html: str | None = None     # 原始卡片 HTML（debug 用）
    skills_required: list[str] = field(default_factory=list)
    jd_full: str = ""


# ============================================================
# URL 参数映射（设计 §5.3，[假设] 待实测 T3）
# ============================================================
CITY_CODE: dict[str, str] = {
    "上海": "101020100",
    "武汉": "101200100",
    "北京": "101010100",
    "深圳": "101280100",
    "杭州": "101210100",
    "广州": "101280100",
    "成都": "101270100",
    "全国": "100010000",
}

# salary 字符串 → Boss 代码（[假设] 需实测映射表）
SALARY_CODE: dict[str, str] = {
    "5-8K": "403", "8-12K": "404", "10-15K": "404",
    "12-18K": "405", "15-20K": "406", "15-25K": "406",
    "20-30K": "407", "30-50K": "408",
}

# 经验字符串 → 代码（[假设]）
EXPERIENCE_CODE: dict[str, str] = {
    "应届": "100", "1年": "101", "1-3年": "103", "1-3年以内": "103",
    "3-5年": "104", "3-5年以内": "104", "5-10年": "105", "5年以上": "105",
    "10年以上": "106", "不限": "109",
}

# 学历字符串 → 代码（[假设]）
DEGREE_CODE: dict[str, str] = {
    "初中": "209", "中专": "208", "高中": "206",
    "大专": "202", "本科": "203", "硕士": "204", "博士": "205",
    "不限": "209",
}


class WebSearcher:
    """DrissionPage 网页版搜索器（M3+ 主路径，设计 §5.2）。

    通过 :class:`BrowserManager` 复用浏览器实例。
    """

    SEARCH_URL = "https://www.zhipin.com/web/geek/job"

    def __init__(
        self,
        browser: BrowserManager,
        logger_obj: Any = None,
        *,
        sleep_fn: Any = time.sleep,
        selectors_mod: Any = None,
    ) -> None:
        """初始化搜索器。

        Args:
            browser: BrowserManager 实例。
            logger_obj: loguru logger。
            sleep_fn: sleep 函数（测试注入 mock）。
            selectors_mod: selectors 模块（测试可注入 mock）。
        """
        self.browser = browser
        self.log = logger_obj or logger
        self._sleep = sleep_fn
        self._sel = selectors_mod or sel
        self._last_security_id: str | None = None  # fetch_detail 时从详情页 URL 提取

    # ============================================================
    # 主接口
    # ============================================================
    def search(self, params: WebSearchParams) -> list[WebJobRaw]:
        """网页搜索岗位，返回 :class:`WebJobRaw` 列表。

        流程（设计 §5.2）：
        1. browser.ensure_browser()
        2. 构造 URL（query/city/salary/experience/degree）
        3. tab.get(url)
        4. ``_wait_job_list_loaded``（等 .job-card-wrapper 出现）
        5. ``_scroll_to_load_more``（scroll_count 次无限滚动）
        6. ``_parse_job_cards`` → list[WebJobRaw]
        7. 去重（job_id/securityId）

        Args:
            params: 搜索参数。

        Returns:
            去重后的 :class:`WebJobRaw` 列表。

        Raises:
            WebSearchTimeoutError: 页面加载超时。
            LoginRequiredError: 登录态丢失（跳登录页）。
            CaptchaDetectedError: 验证码弹窗。
            RateLimitedError: 「操作频繁」提示。
        """
        tab = self.browser.ensure_browser()
        url = self._build_search_url(params)
        self.log.info(f"WebSearcher 导航：{url}")
        try:
            tab.get(url)
        except Exception as e:
            raise WebSearchTimeoutError(f"导航搜索页失败：{e}") from e
        # 风控检测（列表加载后一次即可：验证码/风控 toast 在加载完成后才出现）
        self._wait_job_list_loaded(tab, timeout=15)
        self._check_risk(tab)
        # 无限滚动加载
        self._scroll_to_load_more(tab, params.scroll_count)
        # 解析卡片
        jobs = self._parse_job_cards(tab)
        # 截断到 per_page_limit
        if params.per_page_limit > 0:
            jobs = jobs[: params.per_page_limit]
        # 去重
        jobs = dedupe_web_jobs(jobs)
        self.log.info(f"WebSearcher 搜索 keyword={params.keyword!r} city={params.city!r} → {len(jobs)} 条")
        return jobs

    def fetch_detail(self, job_id_or_url: str) -> str:
        """导航到岗位详情页 → 抓 JD 全文。

        2026-07-11 实测修正：Boss 列表页只给 /job_detail/xxx.html（加密 ID），
        不给 securityId。直接用列表页抓到的 jd_url 导航即可，详情页加载后
        Boss JS 会自动在 URL 里生成 securityId（供后续 greet 用）。

        Args:
            job_id_or_url: 可以是完整 jd_url（https://www.zhipin.com/job_detail/xxx.html）
                           或加密 ID（xxx）。完整 URL 优先。

        Returns:
            纯文本 JD。
        """
        import urllib.parse as up
        tab = self.browser.ensure_browser()
        # 构造详情 URL
        if job_id_or_url.startswith("http"):
            detail_url = job_id_or_url
        elif "/job_detail/" in job_id_or_url:
            detail_url = "https://www.zhipin.com" + job_id_or_url
        else:
            # 纯加密 ID
            detail_url = f"https://www.zhipin.com/job_detail/{job_id_or_url}.html"
        self.log.debug(f"WebSearcher fetch_detail：{detail_url}")
        try:
            tab.get(detail_url)
        except Exception as e:
            raise WebSearchTimeoutError(f"导航详情页失败：{e}") from e
        # 等页面加载（详情页加载较慢）
        time.sleep(2)
        self._check_risk(tab)
        # 检测详情页 URL 是否被重定向到 job-detail?securityId=...（Boss JS 行为）
        try:
            final_url = tab.url or ""
            if "securityId" in final_url:
                parsed = up.urlparse(final_url)
                qs = up.parse_qs(parsed.query)
                sid = (qs.get("securityId") or [""])[0]
                if sid:
                    self.log.debug(f"详情页获取到 securityId：{sid[:20]}...")
                    # 缓存到实例供 greet 用
                    self._last_security_id = sid
        except Exception:
            pass
        # 等 JD 容器
        jd_el = sel.find_element(tab, "detail.jd_full", timeout=15)
        if jd_el is None:
            raise DomSelectorStaleError("detail.jd_full", sel.get_selectors("detail.jd_full"))
        try:
            return jd_el.text or ""
        except Exception:
            return ""

    # ============================================================
    # 内部
    # ============================================================
    def _build_search_url(self, params: WebSearchParams) -> str:
        """构造搜索 URL（含 city_code/salary 映射，设计 §5.3）。"""
        q: dict[str, str] = {"query": params.keyword}
        # city：中文名 → 代码，未知则原样传
        q["city"] = CITY_CODE.get(params.city, params.city)
        if params.salary and params.salary in SALARY_CODE:
            q["salary"] = SALARY_CODE[params.salary]
        if params.experience and params.experience in EXPERIENCE_CODE:
            q["experience"] = EXPERIENCE_CODE[params.experience]
        if params.degree and params.degree in DEGREE_CODE:
            q["degree"] = DEGREE_CODE[params.degree]
        if params.page > 1:
            q["page"] = str(params.page)
        return f"{self.SEARCH_URL}?{urlencode(q)}"

    def _wait_job_list_loaded(self, tab: Any, timeout: int = 15) -> None:
        """等 .job-card-wrapper 首个卡片出现（隐式等待 + 显式轮询）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                el = tab.ele(f"css:{sel.get_selector('search.job_card')}", timeout=2)
                if el:
                    return
            except Exception:
                pass
            self._sleep(0.5)
        raise WebSearchTimeoutError(
            f"搜索页 {timeout}s 内未出现岗位卡片（{sel.get_selector('search.job_card')}）"
        )

    def _scroll_to_load_more(self, tab: Any, count: int) -> None:
        """无限滚动加载（设计 §5.4）。

        每次 ``window.scrollTo(0, document.body.scrollHeight)``，随机 sleep 1-2s（拟人）。
        检测：滚动后卡片数不再增加 → 提前停止。
        """
        prev_count = self._card_count(tab)
        for i in range(max(0, count)):
            try:
                tab.run_js("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                break
            self._sleep(random.uniform(1.0, 2.0))
            new_count = self._card_count(tab)
            if new_count <= prev_count:
                # 卡片数不增 → 到底了，提前停
                self.log.debug(f"滚动第 {i+1} 次卡片数不增（{prev_count}→{new_count}），停止")
                break
            prev_count = new_count

    def _card_count(self, tab: Any) -> int:
        """当前页面岗位卡片数。"""
        try:
            cards = tab.eles(f"css:{sel.get_selector('search.job_card')}", timeout=2)
            return len(cards) if cards else 0
        except Exception:
            return 0

    def _parse_job_cards(self, tab: Any) -> list[WebJobRaw]:
        """从 DOM 抓所有 .job-card-wrapper → :class:`WebJobRaw`。"""
        try:
            cards = tab.eles(f"css:{sel.get_selector('search.job_card')}", timeout=2)
        except Exception:
            cards = []
        if not cards:
            self.log.warning("未抓到任何岗位卡片（可能选择器失效或页面为空）")
            return []
        out: list[WebJobRaw] = []
        for card in cards:
            try:
                out.append(self._parse_single_card(card))
            except Exception as e:
                self.log.debug(f"单卡片解析失败（跳过）：{e}")
        return out

    def _parse_single_card(self, card: Any) -> WebJobRaw:
        """单卡片解析（选择器见 selectors.py §12）。

        2026-07-11 实测修正：Boss 列表页卡片**不含 securityId**——只有加密的
        job_detail URL（/job_detail/956f367280dd950c0nBy2dq8F1RU.html）。securityId
        在点击进详情页时由 JS 动态生成。故：
        - job_id = job_detail URL 的路径段（加密 ID，用于去重）
        - jd_url = 完整 job_detail URL（fetch_detail 用它导航）
        - security_id = None（详情页才获取）

        部分字段缺失 → 空字符串（不抛错，记录 warning）。
        """
        import urllib.parse as up
        import re as _re

        # 1) 从 .job-name 的 href 抓 job_detail URL（加密 ID）
        jd_url: str | None = None
        job_detail_id = ""
        try:
            link_el = sel.find_element(card, "search.job_link", timeout=0.3)
            if link_el:
                href = link_el.attr("href") or ""
                # 完整化 URL
                jd_url = href if href.startswith("http") else (
                    "https://www.zhipin.com" + href if href.startswith("/") else href
                )
                # 从 /job_detail/XXXX.html 提取加密 ID
                m = _re.search(r"/job_detail/([^.]+)\.html", href)
                if m:
                    job_detail_id = m.group(1)
                # 如果 href 里恰好有 securityId（某些情况），也提取
                if "securityId" in href:
                    parsed = up.urlparse(href)
                    qs = up.parse_qs(parsed.query)
                    sid = (qs.get("securityId") or [""])[0]
                    if sid:
                        job_detail_id = sid  # securityId 优先做 job_id
        except Exception:
            pass

        # 2) 岗位名（.job-name 的文本）
        title = ""
        try:
            name_el = sel.find_element(card, "search.job_name", timeout=0.3)
            if name_el:
                title = (name_el.text or "").strip()
        except Exception:
            pass

        # 3) 公司名（.job-card-footer 内的 .company-name 或 a.boss-info）
        company = ""
        try:
            comp_el = sel.find_element(card, "search.company_name", timeout=0.3)
            if comp_el:
                company = (comp_el.text or "").strip()
        except Exception:
            pass

        # 4) 薪资
        salary = self._card_text(card, "search.salary")

        # 5) 经验/学历（从 .tag-list li 列表抓）
        experience = ""
        degree = ""
        city = ""
        try:
            tag_el = sel.find_element(card, "search.job_info", timeout=0.3)
            if tag_el:
                tags = tag_el.eles("css:li") if hasattr(tag_el, "eles") else []
                tag_texts = [(t.text or "").strip() for t in tags]
                experience, degree = _split_tags(tag_texts)
        except Exception:
            pass

        # job_id：用 job_detail_id（加密 ID），没有则随机兜底
        final_job_id = job_detail_id or f"unknown-{random.randint(10000, 99999)}"
        if not job_detail_id:
            self.log.warning(f"卡片缺 job_detail_id（title={title}, company={company}）")

        return WebJobRaw(
            job_id=final_job_id,
            title=title,
            company=company,
            city=city,
            salary=salary,
            experience=experience,
            degree=degree,
            jd_url=jd_url,
            security_id=None,  # 列表页无 securityId，详情页才获取
        )

    def _card_text(self, card: Any, name: str) -> str:
        """从卡片取某字段文本（找不到返回空串）。"""
        try:
            el = sel.find_element(card, name, timeout=0.3)
            return el.text if el else ""
        except Exception:
            return ""

    def _check_risk(self, tab: Any) -> None:
        """风控信号检测（设计 §5.6 / §13.2）。

        Raises:
            LoginRequiredError: 跳登录页。
            CaptchaDetectedError: 验证码弹窗。
            RateLimitedError: 「操作频繁」提示。
        """
        # 登录页重定向：用 URL 判断（最可靠，避免搜索页页头的登录按钮误判）
        try:
            current_url = tab.url or ""
        except Exception:
            current_url = ""
        if "/user" in current_url or "/login" in current_url.lower():
            raise LoginRequiredError(f"搜索页被重定向到登录页（{current_url}，登录态丢失）")
        # 验证码（超时 0.2s：风控元素要么存在要么不存在，无需长等）
        if sel.find_element(tab, "risk.captcha", timeout=0.2):
            raise CaptchaDetectedError("搜索页出现验证码弹窗")
        # 操作频繁 toast（配合文本匹配）
        toast = sel.find_element(tab, "risk.rate_limited_toast", timeout=0.2)
        if toast:
            try:
                txt = toast.text or ""
            except Exception:
                txt = ""
            if any(kw in txt for kw in ("操作频繁", "请稍后再试", "账号被限制")):
                raise RateLimitedError(f"搜索页出现风控提示：{txt}")


def _split_job_info(info: str) -> tuple[str, str, str]:
    """切分 .job-info 文本为 (经验, 学历, 城市)。

    [假设] Boss 卡片 info 格式如「上海 3-5年 大专」用空格/·分隔。
    启发式：含「年」→ 经验；含「大专/本科/硕士」→ 学历；其余 → 城市。
    """
    if not info:
        return "", "", ""
    parts = [p.strip() for p in info.replace("·", " ").split() if p.strip()]
    exp = deg = city = ""
    for p in parts:
        if "年" in p and not exp:
            exp = p
        elif any(d in p for d in ("大专", "本科", "硕士", "博士", "中专", "高中", "不限")) and not deg:
            deg = p
        elif not city:
            city = p
    return exp, deg, city


def _split_tags(tag_texts: list[str]) -> tuple[str, str]:
    """从 .tag-list 的 li 文本列表切分 (经验, 学历)。

    2026-07-11 实测：Boss 卡片 .tag-list 下是独立 li，如 ['经验不限', '学历不限']
    或 ['3-5年', '大专']。启发式判断每个 tag 属于经验还是学历。
    """
    exp = deg = ""
    for t in tag_texts:
        t = t.strip()
        if not t:
            continue
        # 学历关键词
        if any(d in t for d in ("大专", "本科", "硕士", "博士", "中专", "高中", "学历不限")):
            if not deg:
                deg = t
        # 经验关键词
        elif "年" in t or "经验不限" in t or "在校" in t or "应届" in t:
            if not exp:
                exp = t
    return exp, deg


# ============================================================
# 去重（设计 §5.5，与 searcher.dedupe_jobs 一致）
# ============================================================
def dedupe_web_jobs(
    jobs: list[WebJobRaw],
    *,
    by_company: bool = False,
    exclude_companies: list[str] | None = None,
    seen_job_ids: set[str] | None = None,
) -> list[WebJobRaw]:
    """去重逻辑（按 job_id/securityId）。

    Args:
        jobs: 待过滤列表。
        by_company: 是否同公司去重（保留首条）。
        exclude_companies: 排除公司名单。
        seen_job_ids: 已见 job_id 集合。

    Returns:
        去重后的列表。
    """
    exclude = set(exclude_companies or [])
    seen = seen_job_ids or set()
    out: list[WebJobRaw] = []
    seen_local: set[str] = set()
    seen_company: set[str] = set()
    for j in jobs:
        if j.job_id in seen or j.job_id in seen_local:
            continue
        if j.company in exclude:
            continue
        seen_local.add(j.job_id)
        if by_company and j.company:
            if j.company in seen_company:
                continue
            seen_company.add(j.company)
        out.append(j)
    return out
