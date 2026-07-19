"""Boss 网页 DOM 选择器集中管理（设计 §12）。

⚠️ **所有选择器标注「待实测确认」**——Boss 改版频繁，DOM 可能随版本变。
最后实测：2026-07-10（部分）；未实测项标 ``verified=False``。

设计原则（§12.1）：
- 用常量集中管理（``SELECTORS`` dict），Boss 改版只改这里。
- 每个选择器配主选择器 + 备选择器（列表，按顺序试）。
- 标注「待实测」+ 最后实测日期 + Boss 版本。
- 提供统一访问函数 ``get_selector`` / ``get_selectors`` / ``find_element``。

定位失败统一抛 :class:`~boss_auto_apply.errors.DomSelectorStaleError`，
状态机据此标 ``business`` 类 → ``skipped``（不重试失效选择器）。
"""

from __future__ import annotations

from typing import Any

from ..errors import DomSelectorStaleError

__all__ = [
    "SELECTORS",
    "get_selector",
    "get_selectors",
    "find_element",
    "find_elements",
    "SELECTOR_LAST_VERIFIED",
]

# 选择器最后实测日期（Boss DOM 变更后需更新）
SELECTOR_LAST_VERIFIED = "2026-07-19"


# ============================================================
# 选择器清单（设计 §12.2，全部待实测）
# ============================================================
SELECTORS: dict[str, dict[str, Any]] = {
    # ============================================================
    # 登录页（/web/user）
    # ============================================================
    "login.user_info": {
        "selectors": [".user-info", ".user-avatar", ".user-info .name"],
        "note": "登录后头像/昵称容器（登录态标志）",
        "verified": False,
    },
    "login.signup_button": {
        "selectors": [".btn-signup", ".login-btn", "a[href*='login']", ".sign-btn"],
        "note": "未登录态登录按钮",
        "verified": False,
    },

    # ============================================================
    # 搜索结果页 (/web/geek/job)
    # ============================================================
    "search.job_card": {
        "selectors": [".job-card-wrap", ".job-card-wrap.active",
                      ".card-area .job-card-wrap"],
        "note": "单个岗位卡片容器（2026-07-11 实测：.job-card-wrap 是真实 class，15 个/页；"
                "注意 li[ka] 是筛选项不是卡片！卡片内层是 .job-card-box）",
        "verified": True,
    },
    "search.job_list": {
        "selectors": [".recommend-result-job", ".card-area",
                      ".search-job-result", ".job-list-box"],
        "note": "岗位列表容器（2026-07-11 实测：.recommend-result-job / .card-area.is-seen）",
        "verified": True,
    },
    "search.job_name": {
        "selectors": [".job-name", ".job-title .job-name", "a.job-name"],
        "note": "岗位名（2026-07-11 实测：a.job-name，文本=岗位名，href 含 job_detail 链接）",
        "verified": True,
    },
    "search.company_name": {
        "selectors": [".company-name", ".job-card-footer .company-name",
                      ".company-info a"],
        "note": "公司名（在 .job-card-footer 内）",
        "verified": False,
    },
    "search.salary": {
        "selectors": [".job-salary", ".job-title .job-salary",
                      ".salary"],
        "note": "薪资（2026-07-11 实测：.job-salary，在 .job-title 内）",
        "verified": True,
    },
    "search.job_info": {
        "selectors": [".tag-list", ".job-info .tag-list", ".job-card-info"],
        "note": "经验/学历（2026-07-11 实测：.tag-list 下的 li，如「经验不限」「学历不限」）",
        "verified": True,
    },
    "search.job_link": {
        "selectors": ["a.job-name", ".job-title a", "a[href*='job_detail']"],
        "note": "岗位链接（href=/job_detail/xxx.html?securityId=...，2026-07-11 实测）",
        "verified": True,
    },

    # ============================================================
    # 岗位详情页 (/web/geek/job-detail)
    # ============================================================
    "detail.greet_button": {
        "selectors": [".btn-startchat", ".job-detail-container .btn-greet",
                      ".detail-op .btn-greet", ".job-box .btn-greet"],
        "note": "「立即沟通」/「继续沟通」按钮（详情页，2026-07-19 实测：a.btn.btn-startchat；"
                "首次沟通文本=「立即沟通」，已建立沟通关系后文本=「继续沟通」，class 都是 .btn-startchat）",
        "verified": True,
    },
    "detail.continue_chat_button": {
        "selectors": [".btn-startchat"],  # ⚠️ 与 greet_button 同 class，靠文本区分（见 web_greeter）
        "note": "「继续沟通」按钮（2026-07-19 实测：与「立即沟通」共用 .btn-startchat，"
                "靠 button.text == '继续沟通' 区分；旧 .btn-continuechat class 不存在）",
        "verified": True,
    },
    "detail.jd_full": {
        "selectors": [".job-detail-section .text", ".job-sec-text", ".job-detail .text",
                      ".job-sec-text .text"],
        "note": "JD 全文容器",
        "verified": False,
    },
    "detail.job_title": {
        "selectors": [".job-banner .name", ".info-primary .name", ".name .job-name"],
        "note": "详情页岗位标题",
        "verified": False,
    },

    # ============================================================
    # 搜索结果页卡片上的「立即沟通」（备选，不进详情页直接 greet）
    # ============================================================
    "search.greet_button": {
        "selectors": [".job-card-wrapper .btn-greet", ".job-card .btn-startchat",
                      ".job-card-li .btn-greet"],
        "note": "卡片上的立即沟通按钮（hover 显示）",
        "verified": False,
    },

    # ============================================================
    # 聊天页 (/web/geek/chat)
    # ============================================================
    "chat.input_textarea": {
        "selectors": [".chat-input textarea", ".message-input textarea",
                      "#chat-input", ".chat-input-edit textarea"],
        "note": "聊天输入框 textarea（DrissionPage 旧路径，实测 Boss 聊天页用 contenteditable）",
        "verified": False,
    },
    "chat.input_contenteditable": {
        "selectors": ['.chat-input[contenteditable="true"]',
                      '[contenteditable="true"].chat-input',
                      '.chat-input[contenteditable=""]',
                      '[contenteditable="true"]'],
        "note": "聊天输入框 contenteditable（2026-07-12 nodriver 实测：Boss 聊天页输入框是 "
                ".chat-input[contenteditable='true']，不是 textarea）",
        "verified": True,
    },
    "chat.send_button": {
        "selectors": [".chat-send", ".btn-send", ".send-icon", ".chat-input .btn",
                      ".btn-send-msg"],
        "note": "发送按钮",
        "verified": False,
    },
    "chat.file_input_image": {
        "selectors": [
            'input[type="file"][accept*="image"]',
            'input[type="file"][accept="image/*"]',
            'input[type="file"].image-upload',
            'input[type="file"][name*="image"]',
        ],
        "note": "隐藏的图片上传 input（关键，文件上传用）",
        "verified": False,
    },
    "chat.image_tool_button": {
        "selectors": [".chat-tool-image", ".tool-image", ".icon-image", ".toolbar-image"],
        "note": "「图片」工具按钮（点开后 input 可见，兜底用）",
        "verified": False,
    },
    "chat.message_list": {
        "selectors": [".chat-message-list", ".message-list", ".chat-content",
                      ".chat-msg-list"],
        "note": "聊天消息流容器",
        "verified": False,
    },
    "chat.message_item": {
        "selectors": [".chat-message", ".message-item", ".chat-msg", ".message-box"],
        "note": "单条消息",
        "verified": False,
    },
    "chat.message_content": {
        "selectors": [".message-content", ".chat-message .text", ".msg-text",
                      ".message-item .text"],
        "note": "消息文本内容",
        "verified": False,
    },
    "chat.message_image": {
        "selectors": [".message-image", ".chat-message img", ".msg-image",
                      ".message-item img"],
        "note": "消息中的图片",
        "verified": False,
    },
    "chat.mine_message": {
        "selectors": [".chat-message.mine", ".message-item.self", ".chat-msg.self-message",
                      ".message-box.mine"],
        "note": "自己发的消息（发送确认用）",
        "verified": False,
    },
    "chat.friend_list_item": {
        "selectors": [".friend-list-item", ".chat-list-item", ".friend-item",
                      ".chat-friend-item"],
        "note": "左侧聊天对象列表项",
        "verified": False,
    },
    "chat.search_box": {
        "selectors": [".friend-search input", ".chat-search input", ".search-friend input"],
        "note": "聊天对象搜索框（按 brandName 定位 HR）",
        "verified": False,
    },
    "chat.upload_progress": {
        "selectors": [".upload-progress", ".image-loading", ".uploading"],
        "note": "图片上传进度指示（消失表示上传完成）",
        "verified": False,
    },

    # ============================================================
    # 风控信号（设计 §13.2）
    # ============================================================
    "risk.captcha": {
        "selectors": [".captcha", ".nc_wrapper", "#captcha", ".slider-captcha",
                      "#nc_1_wrapper"],
        "note": "验证码弹窗（滑块/点选）",
        "verified": False,
    },
    "risk.rate_limited_toast": {
        "selectors": [".toast", ".error-toast", ".ant-message", ".ant-message-notice"],
        "note": "「操作频繁」toast 提示（配合文本匹配）",
        "verified": False,
    },
    "risk.already_friend_dialog": {
        "selectors": [".dialog-already-friend", ".friend-exist", ".ant-modal",
                      ".dialog-content"],
        "note": "「你们已经是好友」弹窗",
        "verified": False,
    },
    "risk.login_page_redirect": {
        "selectors": ["#wrap.login-page", ".user-login-page"],
        "note": "被重定向到登录页（登录态丢失标志）。2026-07-17 实测：去掉 .sign-form/.login-box"
                "——这两个在已登录的详情页底部常驻（登录推广区块「找工作/BOSS直聘直接谈/登录」），"
                "会误判为登录态丢失。登录页判断应以 URL（/web/user）为主，此 DOM 选择器仅辅助。",
        "verified": True,
    },
}


def get_selector(name: str) -> str:
    """取主选择器（第一个）。

    Args:
        name: SELECTORS 的 key（如 ``"search.job_card"``）。

    Returns:
        主选择器字符串（css 前缀由调用方加）。

    Raises:
        KeyError: name 不存在。
    """
    return SELECTORS[name]["selectors"][0]


def get_selectors(name: str) -> list[str]:
    """取全部选择器（主 + 备）。

    Args:
        name: SELECTORS 的 key。

    Returns:
        选择器列表（按顺序试，第一个命中的用）。

    Raises:
        KeyError: name 不存在。
    """
    return SELECTORS[name]["selectors"]


def _css(sel: str) -> str:
    """给选择器加 ``css:`` 前缀（DrissionPage 的 ele 定位语法）。

    若已带定位前缀（xpath:/text:/css:）则原样返回。
    """
    if sel.startswith(("css:", "xpath:", "text:", "@", "tag:")):
        return sel
    return f"css:{sel}"


def find_element(tab: Any, name: str, timeout: int = 5) -> Any:
    """按 name 顺序试所有选择器，返回首个找到的元素；都找不到返回 None。

    不抛错（调用方据 None 决定是「选择器失效」还是「正常情况」）。

    Args:
        tab: DrissionPage 的 ChromiumTab。
        name: SELECTORS 的 key。
        timeout: 单个选择器的等待秒数。

    Returns:
        首个命中的元素，或 None。
    """
    tried = get_selectors(name)
    for sel in tried:
        try:
            el = tab.ele(_css(sel), timeout=timeout)
            if el:
                return el
        except Exception:
            # 单个选择器异常（超时/页面未就绪）→ 试下一个
            continue
    return None


def find_elements(tab: Any, name: str, timeout: int = 5) -> list[Any]:
    """按 name 取主选择器的所有匹配元素（用于列表抓取）。

    Args:
        tab: DrissionPage 的 ChromiumTab。
        name: SELECTORS 的 key。
        timeout: 等待秒数。

    Returns:
        元素列表（可能为空）。
    """
    sel = get_selector(name)
    try:
        return tab.eles(_css(sel), timeout=timeout)
    except Exception:
        return []


def require_element(tab: Any, name: str, timeout: int = 5) -> Any:
    """定位元素，找不到抛 :class:`DomSelectorStaleError`。

    用于「必须存在」的关键元素（greet 按钮/输入框/发送按钮等）。
    失效时抛 business 类异常 → 状态机标 skipped（不重试失效选择器）。

    Args:
        tab: DrissionPage 的 ChromiumTab。
        name: SELECTORS 的 key。
        timeout: 单个选择器等待秒数。

    Returns:
        首个命中的元素。

    Raises:
        DomSelectorStaleError: 所有选择器都没找到元素。
    """
    el = find_element(tab, name, timeout=timeout)
    if el is None:
        raise DomSelectorStaleError(name, get_selectors(name))
    return el
