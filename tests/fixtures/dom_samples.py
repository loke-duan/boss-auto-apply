"""Boss 页面 DOM 快照样本（用于 selector / WebSearcher 测试，设计 §14.2）。

提供模拟 DrissionPage 元素的最小 mock 对象（MockTab / MockElement），
以及预置的「岗位卡片」「聊天消息」DOM 数据。

这些 mock 不依赖真 Chrome，让 WebSearcher/WebGreeter/WebChatSender 单测可纯 Python 跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


__all__ = [
    "MockElement",
    "MockTab",
    "make_job_cards",
    "make_chat_messages",
    "make_search_page_tab",
    "make_chat_page_tab",
    "make_detail_page_tab",
]


# ============================================================
# Mock 元素（模拟 DrissionPage 的 Element）
# ============================================================
class MockElement:
    """模拟 DrissionPage Element。

    支持：``.text`` / ``.attr(name)`` / ``.click()`` / ``.input(text)`` / ``.run_js(script)``。
    子元素查找：``.ele(locator, timeout)`` / ``.eles(locator, timeout)`` 返回预置的 children。
    """

    def __init__(
        self,
        *,
        text: str = "",
        attrs: dict[str, str] | None = None,
        children: dict[str, list["MockElement"]] | None = None,
        click_fn: Callable[[], None] | None = None,
        input_fn: Callable[[str], None] | None = None,
        natural_width: int = 0,
        tag: str = "div",
    ) -> None:
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}   # selector_str → [MockElement]
        self._click_fn = click_fn
        self._input_fn = input_fn
        self.naturalWidth = natural_width
        self.tag = tag
        self.click_count = 0
        self.input_log: list[str] = []

    # ---- DrissionPage API ----
    @property
    def text(self) -> str:
        return self._text

    def attr(self, name: str) -> str:
        return self._attrs.get(name, "")

    def click(self) -> None:
        self.click_count += 1
        if self._click_fn:
            self._click_fn()

    def input(self, value: str) -> None:
        self.input_log.append(value)
        if self._input_fn:
            self._input_fn(value)

    def run_js(self, script: str) -> Any:
        if "naturalWidth" in script:
            return self.naturalWidth
        return None

    def ele(self, locator: str, timeout: int = 5) -> "MockElement | None":
        # locator 形如 "css:.job-name"
        sel = locator.split(":", 1)[1] if ":" in locator else locator
        kids = self._children.get(sel) or self._children.get(locator)
        if kids:
            return kids[0]
        return None

    def eles(self, locator: str, timeout: int = 5) -> list["MockElement"]:
        sel = locator.split(":", 1)[1] if ":" in locator else locator
        return list(self._children.get(sel) or self._children.get(locator) or [])

    # 测试辅助
    def add_child(self, selector: str, el: "MockElement") -> None:
        self._children.setdefault(selector, []).append(el)


class MockTab:
    """模拟 DrissionPage ChromiumTab。

    维护一个「当前页面」概念：通过 ``get(url)`` 切换页面类型，
    ``.ele/.eles`` 按当前页面的预置元素返回。
    """

    def __init__(self) -> None:
        self.url: str = ""
        self._pages: dict[str, dict[str, Any]] = {}   # url_pattern → {selectors→elements}
        self._current: dict[str, Any] = {}
        self.get_log: list[str] = []
        self.run_js_log: list[str] = []
        self.run_cdp_log: list[tuple[str, dict]] = []   # (method, kwargs)
        self._cookies: list[dict[str, str]] = []
        # scroll 时的「加载更多」回调（模拟无限滚动后卡片数增加）
        self._scroll_callbacks: list[Callable[[], None]] = []

    # ---- 页面管理 ----
    def register_page(self, url_contains: str, elements: dict[str, Any]) -> None:
        """注册一个页面：当 get(url) 含 url_contains 时，elements 生效。"""
        self._pages[url_contains] = elements

    def set_cookies(self, cookies: list[dict[str, str]]) -> None:
        self._cookies = cookies

    def get(self, url: str) -> None:
        self.url = url
        self.get_log.append(url)
        # 匹配注册的页面
        self._current = {}
        for key, elems in self._pages.items():
            if key in url:
                self._current = elems
                break

    def on_scroll(self, fn: Callable[[], None]) -> None:
        """注册滚动回调（模拟无限滚动加载更多卡片）。"""
        self._scroll_callbacks.append(fn)

    # ---- DrissionPage API ----
    def ele(self, locator: str, timeout: int = 5) -> MockElement | None:
        sel = locator.split(":", 1)[1] if ":" in locator else locator
        # 直接匹配 selector，或匹配 selectors.py 的任一选择器
        if sel in self._current:
            el = self._current[sel]
            return el[0] if isinstance(el, list) else el
        return None

    def eles(self, locator: str, timeout: int = 5) -> list[MockElement]:
        sel = locator.split(":", 1)[1] if ":" in locator else locator
        el = self._current.get(sel)
        if isinstance(el, list):
            return list(el)
        if el is not None:
            return [el]
        return []

    def cookies(self) -> list[dict[str, str]]:
        return list(self._cookies)

    def run_js(self, script: str) -> Any:
        self.run_js_log.append(script)
        if "scrollTo" in script:
            for fn in self._scroll_callbacks:
                fn()
        if "naturalWidth" in script:
            return None
        # v2 web_greeter 用 JS 读 location.href 绕过 tab.url 同步慢
        if script == "location.href":
            return self.url
        # v2 web_greeter 用 JS 读 body.innerText（DrissionPage body.text 偶尔返回空）
        if script == "document.body.innerText":
            # 合并所有元素文本
            parts = []
            for els in self._current.values():
                if isinstance(els, list):
                    for el in els:
                        if hasattr(el, "text") and el.text:
                            parts.append(el.text)
                elif hasattr(els, "text") and els.text:
                    parts.append(els.text)
            return "\n".join(parts)
        return None

    def run_cdp(self, method: str, **kwargs: Any) -> Any:
        self.run_cdp_log.append((method, kwargs))
        return None


# ============================================================
# DOM 数据工厂
# ============================================================
def make_job_card(
    *,
    title: str = "SEO 运营",
    company: str = "某科技公司",
    salary: str = "12-18K",
    info: str = "上海 3-5年 大专",
    security_id: str = "sec-001",
    href: str = "/job_detail/sec-001.html",
) -> MockElement:
    """构造一个岗位卡片 MockElement（2026-07-11 实测结构：.job-card-wrap > .job-card-box）。"""
    # job-name 是 <a> 标签：文本=岗位名，href=/job_detail/xxx.html
    job_name_a = MockElement(text=title, attrs={"href": href, "class": "job-name"})
    # tag-list 下的 li（经验/学历）
    exp_li = MockElement(text="3-5年")
    deg_li = MockElement(text="大专")
    tag_list = MockElement(children={"li": [exp_li, deg_li]})
    # company（在 job-card-footer 内）
    company_el = MockElement(text=company)
    salary_el = MockElement(text=salary)
    card = MockElement(
        attrs={"class": "job-card-wrap"},
        children={
            # selectors.py search.job_name 的选择器列表
            ".job-name": [job_name_a], "a.job-name": [job_name_a],
            ".job-title .job-name": [job_name_a],
            # search.salary
            ".job-salary": [salary_el], ".job-title .job-salary": [salary_el], ".salary": [salary_el],
            # search.job_info（tag-list）
            ".tag-list": [tag_list], ".job-info .tag-list": [tag_list],
            # search.company_name
            ".company-name": [company_el], ".job-card-footer .company-name": [company_el],
            # search.job_link（a 标签 href 含 job_detail）
            "a[href*='job_detail']": [job_name_a], ".job-title a": [job_name_a],
        },
    )
    return card


def make_job_cards(count: int = 3) -> list[MockElement]:
    """构造 N 个岗位卡片（不同 job_detail_id，2026-07-11 实测：列表页无 securityId）。"""
    cards = []
    for i in range(count):
        cards.append(make_job_card(
            title=f"SEO 运营 {i+1}",
            company=f"公司{i+1}",
            salary="12-18K",
            href=f"/job_detail/detail-{i+1:03d}.html",
        ))
    return cards


def make_search_page_tab(cards: list[MockElement] | None = None,
                         *, cookies: list[dict[str, str]] | None = None) -> MockTab:
    """构造一个搜索结果页 MockTab。"""
    tab = MockTab()
    cards = cards if cards is not None else make_job_cards(3)
    tab.register_page("/web/geek/job", {
        ".job-card-wrap": cards,
        ".job-card-wrap.active": cards,
        ".recommend-result-job": [MockElement()],
        ".card-area": [MockElement()],
    })
    if cookies is not None:
        tab.set_cookies(cookies)
    return tab


def make_detail_page_tab(*, greet_outcome: str = "chat_page",
                         greet_button_present: bool = True) -> MockTab:
    """构造一个岗位详情页 MockTab。

    Args:
        greet_outcome: 点击 greet 后的结果：``chat_page`` / ``already_friend`` /
                       ``rate_limited`` / ``captcha`` / ``no_button``。
        greet_button_present: 是否有 greet 按钮。
    """
    tab = MockTab()
    elems: dict[str, Any] = {}
    if greet_button_present and greet_outcome != "no_button":
        greet_btn = MockElement()
        elems[".job-detail-container .btn-greet"] = [greet_btn]
        elems[".btn-startchat"] = [greet_btn]

        def _on_click():
            if greet_outcome == "chat_page":
                tab.url = "https://www.zhipin.com/web/geek/chat"
                tab._current = tab._pages.get("/web/geek/chat", {})
            elif greet_outcome == "already_friend":
                tab._current.setdefault(".dialog-already-friend", [MockElement()])
            elif greet_outcome == "rate_limited":
                tab._current.setdefault(".toast", [MockElement(text="操作频繁，请稍后再试")])
            elif greet_outcome == "captcha":
                tab._current.setdefault(".captcha", [MockElement()])
        greet_btn._click_fn = _on_click
    # 注册两种详情页 URL 模式（WebGreeter._navigate_to_job 用 /job_detail/{id}.html，
    # 旧测试 setup 用 /web/geek/job-detail?securityId=...；共用同一组元素）
    tab.register_page("/web/geek/job-detail", elems)
    tab.register_page("/job_detail/", elems)
    return tab


def make_chat_messages(texts: list[str] | None = None,
                       *, mine: bool = True) -> list[MockElement]:
    """构造聊天消息流。"""
    texts = texts or ["您好，看到贵司 SEO 岗位"]
    cls = ".chat-message.mine" if mine else ".chat-message"
    return [MockElement(text=t) for t in texts]


def make_chat_page_tab(*, textarea_input_ok: bool = True,
                       send_button_present: bool = True,
                       file_input_present: bool = True,
                       mine_messages: list[MockElement] | None = None,
                       image_appears: bool = True,
                       upload_progress: bool = False) -> MockTab:
    """构造一个聊天页 MockTab（可控制各元素是否存在/成功）。"""
    tab = MockTab()
    elems: dict[str, Any] = {}

    # textarea
    if textarea_input_ok:
        textarea = MockElement()
        elems[".chat-input textarea"] = [textarea]
        elems[".message-input textarea"] = [textarea]

    # send button
    send_btn = None
    if send_button_present:
        send_btn = MockElement()
        elems[".chat-send"] = [send_btn]
        elems[".btn-send"] = [send_btn]
        # 点 send 后 mine 消息流出现文本（话术发送成功的确认）
        def _on_send():
            msgs = mine_messages if mine_messages is not None else make_chat_messages()
            tab._current[".chat-message.mine"] = msgs
            tab._current[".message-item.self"] = msgs
        send_btn._click_fn = _on_send

    # file input
    if file_input_present:
        file_input = MockElement()
        elems['input[type="file"][accept*="image"]'] = [file_input]
        # 上传图片后出现图片
        def _on_input(value):
            if image_appears:
                img = MockElement(natural_width=1080)
                tab._current[".message-image"] = [img]
                tab._current[".chat-message img"] = [img]
        file_input._input_fn = _on_input

    if upload_progress:
        elems[".upload-progress"] = [MockElement()]

    tab.register_page("/web/geek/chat", elems)
    return tab
