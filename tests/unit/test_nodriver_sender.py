"""test_nodriver_sender.py — NodriverChatSender 单元测试（ADR-0003）。

测试策略：
- 不真启动 nodriver/Chrome——用 mock nodriver 模块 + mock async_runner
- 验证：dry-run / 图片预处理 / send_full 流程（话术+图片）/ 确认逻辑 / 错误处理
- async→sync 桥接：用 ``async_runner=asyncio.run`` 跑真 async mock（或注入同步 runner）

关键 mock 设计：
- ``FakeNodriverPage``：模拟 nodriver Tab 的 async API（find/click/evaluate/query_selector/send_keys/send_file）
- ``FakeNodriverBrowser``：模拟 browser.start/get/stop
- ``fake_nodriver_mod``：模拟 ``import nodriver as uc``，``uc.start()`` 返回 FakeNodriverBrowser
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from boss_auto_apply.browser.nodriver_sender import NodriverChatSender


# async sleep 的 no-op 实现（测试注入，不真睡）
async def _noop_async_sleep(seconds):
    """async sleep 的 no-op（测试用，立即返回）。"""
    return None


class FakeTime:
    """模拟 time.time()——每次调用推进 step 秒，让超时循环快速但不立即退出。

    step=0.1s：循环每轮推进 0.1s，25s 超时循环跑约 250 轮（毫秒级完成），
    但每轮都有机会检查条件（URL 变更/SPA 渲染/确认）。
    """

    def __init__(self, start: float = 1000.0, step: float = 0.1):
        self._current = start
        self._step = step

    def __call__(self) -> float:
        self._current += self._step
        return self._current
from boss_auto_apply.errors import ImagePreprocessError
from boss_auto_apply.models import JobRow


def _make_job(job_id: str = "sec-001") -> JobRow:
    return JobRow(job_id=job_id, target_name="AI产品/上海", city="上海",
                  title="AI产品经理", company="MiniMax")


def _make_image(tmp_path, *, size_kb: int = 50, width: int = 2000) -> str:
    """生成一张真 PNG 图片（用 Pillow）。"""
    from PIL import Image
    img = Image.new("RGB", (width, int(width * 1.4)), color=(255, 128, 0))
    path = str(tmp_path / "resume.png")
    img.save(path, "PNG")
    return path


# ============================================================
# Fake nodriver 组件
# ============================================================
class FakeNodriverElement:
    """模拟 nodriver Element。"""

    def __init__(self, *, tag: str = "div", text: str = "",
                 accept: str = "image/gif,image/jpeg,image/jpg,image/png"):
        # v2 默认 accept 是聊天图片格式（让 _locate_chat_image_input 能匹配）
        self._tag = tag
        self._text = text
        self._accept = accept
        self.click_called = False
        self.send_keys_called = False
        self.send_keys_text = ""
        self.send_file_called = False
        self.send_file_path = ""

    async def click(self) -> None:
        self.click_called = True

    async def send_keys(self, text: str) -> None:
        self.send_keys_called = True
        self.send_keys_text = text

    async def send_file(self, *file_paths: str) -> None:
        self.send_file_called = True
        self.send_file_path = file_paths[0] if file_paths else ""

    async def apply(self, js_fn: str) -> Any:
        """v2：模拟 nodriver element.apply(js_fn)。

        _locate_chat_image_input 用它读 file input 的 accept 属性。
        js_fn 形如 ``(el) => el.accept || el.getAttribute("accept") || ""``
        """
        # 简化：只要 js_fn 含 "accept" 就返回元素的 accept
        if "accept" in js_fn:
            return self._accept
        return None


class FakeNodriverPage:
    """模拟 nodriver Tab/Page 的 async API。"""

    def __init__(
        self,
        *,
        greet_btn_text: str = "继续沟通",
        chat_url: str = "https://www.zhipin.com/web/geek/chat?id=xxx",
        chat_input_selector: str = '.chat-input[contenteditable="true"]',
        body_text_after_send: str = "送达\n您好，测试话术",
        input_cleared_after_send: bool = True,
        file_inputs_count: int = 1,
        img_confirm_count: int = 1,
    ):
        self._greet_btn_text = greet_btn_text
        self._chat_url = chat_url
        self._chat_input_selector = chat_input_selector
        self._body_text = body_text_after_send
        self._input_cleared = input_cleared_after_send
        self._file_inputs_count = file_inputs_count
        self._img_confirm_count = img_confirm_count
        self.current_url = "https://www.zhipin.com/job_detail/sec-001.html"
        self.evaluate_calls: list[str] = []
        self._text_sent = False
        self._image_sent = False
        self.url = self.current_url
        # 点击沟通按钮后，find 返回的元素的 click 会触发跳转
        self._greet_clicked = False

    async def find(self, text: str, timeout: int = 10) -> FakeNodriverElement | None:
        if text == self._greet_btn_text:
            page = self  # 闭包引用

            class _GreetBtn(FakeNodriverElement):
                async def click(self_inner) -> None:
                    await super().click()
                    page._greet_clicked = True
                    page.current_url = page._chat_url
                    page.url = page._chat_url

            return _GreetBtn(text=text)
        return None

    async def click(self) -> None:
        pass

    async def evaluate(self, expr: str, return_by_value: bool = False) -> Any:
        self.evaluate_calls.append(expr)
        # window.location.href
        if "window.location.href" in expr:
            return self.current_url
        # _confirm_text 组合查询（JSON.stringify({input:..., body:...})）
        if "JSON.stringify" in expr and "input" in expr and "body" in expr:
            input_val = "" if (self._input_cleared and self._text_sent) else "您好，测试话术"
            body_val = self._body_text if (self._text_sent or self._image_sent) else "详情页内容"
            return json.dumps({"input": input_val, "body": body_val})
        # body.innerText（旧格式兼容）
        if "document.body.innerText" in expr and "chat-input" not in expr:
            if self._text_sent or self._image_sent:
                return self._body_text
            return "详情页内容"
        # chat-input innerText（旧格式兼容）
        if "chat-input" in expr and "innerText" in expr:
            if self._input_cleared and self._text_sent:
                return ""
            return "您好，测试话术"
        # execCommand insertText（话术输入）→ 标记文本已写入
        if "execCommand" in expr and "insertText" in expr:
            return True
        # Enter dispatch / keydown
        if "dispatchEvent" in expr or "keydown" in expr:
            self._text_sent = True
            return "enter_dispatched"
        # v2 _count_chat_content_images（_confirm_image_by_count 用）
        # 模拟：发图前返回初始数量，发图后数量 +1（_FileInput.send_file 触发 _image_sent=True）
        if "bosszhipin.com/beijin" in expr and "message-item" in expr:
            return 1 if self._image_sent else 0
        # v2 _locate_chat_image_input evaluate 兜底（返回纯图片 input 的索引）
        if "input[type=\"file\"]" in expr and "application/pdf" in expr:
            return 0  # 第 0 个就是聊天图片 input
        # 图片确认（旧版 _confirm_image 兼容）
        if "naturalWidth" in expr or "message-item img" in expr:
            if self._image_sent:
                return json.dumps({"count": self._img_confirm_count, "last_loaded": True})
            return json.dumps({"count": 0, "last_loaded": False})
        # JS 点击兜底
        if "querySelector('.btn-startchat')" in expr:
            self._greet_clicked = True
            self.current_url = self._chat_url
            self.url = self._chat_url
            return "clicked"
        # 通用 JS 设置
        if "innerText =" in expr:
            return None
        return None

    async def query_selector(self, selector: str) -> FakeNodriverElement | None:
        if selector == self._chat_input_selector and self._greet_clicked:
            return FakeNodriverElement(tag="div")
        return None

    async def get(self, url: str) -> "FakeNodriverPage":
        """nodriver Tab.get 导航 URL（_send_full_async 启动时打开 detail_url 用）。

        v3.1：_navigate_to_chat 不再直接 page.get(/web/geek/chat)，而是在详情页
        点「继续沟通」让 Boss 自动跳转。所以本方法只用于初始打开详情页。
        chat-input 的可见性由 find(btn).click() 触发（_greet_clicked=True）。
        """
        self.current_url = url
        self.url = url
        return self

    async def query_selector_all(self, selector: str) -> list[Any]:
        if 'input[type="file"]' in selector:
            page = self

            class _FileInput(FakeNodriverElement):
                async def send_file(self_inner, *file_paths: str) -> None:
                    await super().send_file(*file_paths)
                    page._image_sent = True

            return [_FileInput(tag="input", accept="image/*")
                    for _ in range(self._file_inputs_count)]
        return []

    async def send_keys(self, text: str) -> None:
        pass


class FakeNodriverBrowser:
    """模拟 nodriver Browser。"""

    def __init__(self, page: FakeNodriverPage):
        self._page = page
        self.stopped = False

    async def get(self, url: str) -> FakeNodriverPage:
        self._page.current_url = url
        self._page.url = url
        return self._page

    def stop(self) -> None:
        self.stopped = True


class FakeNodriverMod:
    """模拟 ``import nodriver as uc`` 模块。"""

    def __init__(self, browser: FakeNodriverBrowser):
        self._browser = browser
        self.start_calls: list[dict] = []

    async def start(self, **kwargs) -> FakeNodriverBrowser:
        self.start_calls.append(kwargs)
        return self._browser


# ============================================================
# 工厂
# ============================================================
def _make_sender(
    fake_nodriver: FakeNodriverMod | None = None,
    *,
    sleep_fn: Any = lambda *a, **k: None,
    async_runner: Any = None,
    **kw,
) -> NodriverChatSender:
    """构造 NodriverChatSender，sleep_fn/async_sleep_fn 不真睡。"""
    kw.setdefault("sleep_fn", sleep_fn)
    kw.setdefault("async_sleep_fn", _noop_async_sleep)
    kw.setdefault("time_fn", FakeTime())
    kw.setdefault("async_runner", async_runner or asyncio.run)
    kw.setdefault("nodriver_mod", fake_nodriver)
    return NodriverChatSender(**kw)


# ============================================================
# 用例1：dry-run → mock 成功，不启动浏览器
# ============================================================
def test_send_full_dry_run(tmp_path):
    """dry-run 模式：不启动浏览器，返回 mock 成功。"""
    image = _make_image(tmp_path)
    fake_mod = FakeNodriverMod(FakeNodriverBrowser(FakeNodriverPage()))
    s = _make_sender(fake_mod)
    result = s.send_full(_make_job(), "您好，测试话术", image, dry_run=True)
    assert result.text_sent is True
    assert result.image_sent is True
    assert result.error is None
    # dry-run 不启动浏览器
    assert len(fake_mod.start_calls) == 0


# ============================================================
# 用例2：send_full 成功（话术 + 图片都发送+确认）
# ============================================================
def test_send_full_success(tmp_path):
    """完整发送成功：详情页→点沟通→聊天页渲染→发话术→发图片。"""
    image = _make_image(tmp_path)
    page = FakeNodriverPage(
        body_text_after_send="送达\n您好，测试话术",
        input_cleared_after_send=True,
        file_inputs_count=1,
        img_confirm_count=1,
    )
    browser = FakeNodriverBrowser(page)
    fake_mod = FakeNodriverMod(browser)
    s = _make_sender(fake_mod)
    result = s.send_full(_make_job(), "您好，测试话术", image, dry_run=False)
    assert result.text_sent is True
    assert result.image_sent is True
    assert result.error is None
    assert browser.stopped is True  # 浏览器已关闭


# ============================================================
# 用例3：图片预处理（压缩到 <1MB）
# ============================================================
def test_preprocess_image_compress(tmp_path):
    """大图压缩到 <1MB / 1080px 宽。"""
    image = _make_image(tmp_path, width=3000)  # 大图
    s = _make_sender(FakeNodriverMod(FakeNodriverBrowser(FakeNodriverPage())))
    pre = s._preprocess_image(image, tag="test")
    assert pre.processed_path != image
    assert pre.size_kb <= 1024
    assert pre.width <= 1080
    assert os.path.exists(pre.processed_path)


# ============================================================
# 用例4：图片不存在 → ImagePreprocessError
# ============================================================
def test_preprocess_image_not_found():
    """图片路径不存在 → ImagePreprocessError。"""
    s = _make_sender(FakeNodriverMod(FakeNodriverBrowser(FakeNodriverPage())))
    with pytest.raises(ImagePreprocessError, match="图片不存在"):
        s._preprocess_image("/tmp/nonexistent_resume.png")


# ============================================================
# 用例5：send_full 图片不存在 → ImagePreprocessError
# ============================================================
def test_send_full_image_not_found():
    """send_full 时图片不存在 → ImagePreprocessError（预处理阶段）。"""
    fake_mod = FakeNodriverMod(FakeNodriverBrowser(FakeNodriverPage()))
    s = _make_sender(fake_mod)
    with pytest.raises(ImagePreprocessError):
        s.send_full(_make_job(), "您好", "/tmp/nonexistent.png", dry_run=False)


# ============================================================
# 用例6：话术发送失败 → 不发图片
# ============================================================
def test_send_full_text_fail_skips_image(tmp_path):
    """话术发送失败（确认未通过）→ 不发图片。"""
    image = _make_image(tmp_path)
    page = FakeNodriverPage(
        body_text_after_send="其他内容（不含话术）",  # 确认时找不到话术
        input_cleared_after_send=False,  # 输入框未清空
    )
    browser = FakeNodriverBrowser(page)
    fake_mod = FakeNodriverMod(browser)
    s = _make_sender(fake_mod)
    result = s.send_full(_make_job(), "您好，测试话术", image, dry_run=False)
    assert result.text_sent is False
    assert result.image_sent is False
    assert "text_send_failed" in (result.error or "")
    assert browser.stopped is True


# ============================================================
# 用例7：聊天页 SPA 未渲染 → error
# ============================================================
def test_send_full_chat_not_rendered(tmp_path):
    """聊天页 SPA 未渲染（chat-input 找不到）→ error。"""
    image = _make_image(tmp_path)
    page = FakeNodriverPage(
        chat_input_selector="nonexistent_selector",  # 永远找不到
    )
    browser = FakeNodriverBrowser(page)
    fake_mod = FakeNodriverMod(browser)
    s = _make_sender(fake_mod)
    result = s.send_full(_make_job(), "您好", image, dry_run=False)
    assert result.text_sent is False
    assert "chat_page_not_rendered" in (result.error or "")
    assert browser.stopped is True


# ============================================================
# 用例8：nodriver 未安装 → error 记录在 result.error
# ============================================================
def test_nodriver_not_installed(tmp_path):
    """nodriver_mod=None 且 import 失败 → result.error 含 nodriver_async_error。

    _get_nodriver 在 _send_full_async 内调用，异常被 send_full 的 try/except 捕获，
    记录到 result.error（不重新抛出，保证浏览器资源被清理）。
    """
    image = _make_image(tmp_path)
    s = NodriverChatSender(
        nodriver_mod=None,
        sleep_fn=lambda *a, **k: None,
        async_sleep_fn=_noop_async_sleep,
        time_fn=FakeTime(),
        async_runner=asyncio.run,
    )
    s._nodriver = None
    # monkey-patch 让 _get_nodriver 抛 SenderError
    import boss_auto_apply.browser.nodriver_sender as ns
    original_get = ns.NodriverChatSender._get_nodriver

    def _raise(self):
        from boss_auto_apply.errors import SenderError
        raise SenderError("nodriver 未安装（mock）")

    ns.NodriverChatSender._get_nodriver = _raise
    try:
        result = s.send_full(_make_job(), "您好", image, dry_run=False)
        assert result.text_sent is False
        assert result.image_sent is False
        assert "nodriver_async_error" in (result.error or "")
        assert "nodriver 未安装" in (result.error or "")
    finally:
        ns.NodriverChatSender._get_nodriver = original_get


# ============================================================
# 用例9：async_runner 桥接验证（用同步 runner 模拟）
# ============================================================
def test_async_runner_called(tmp_path):
    """验证 send_full 真发送时调用了 async_runner。"""
    image = _make_image(tmp_path)
    page = FakeNodriverPage()
    browser = FakeNodriverBrowser(page)
    fake_mod = FakeNodriverMod(browser)

    runner_called = []

    def sync_runner(coro):
        runner_called.append(True)
        return asyncio.run(coro)

    s = _make_sender(fake_mod, async_runner=sync_runner)
    s.send_full(_make_job(), "您好，测试话术", image, dry_run=False)
    assert len(runner_called) == 1


# ============================================================
# 用例10：话术截断（超 max_text_len）
# ============================================================
def test_text_truncation(tmp_path):
    """话术超 max_text_len → 截断（dry-run 不报错）。"""
    image = _make_image(tmp_path)
    long_text = "您好" * 200  # 400 字
    s = _make_sender(FakeNodriverMod(FakeNodriverBrowser(FakeNodriverPage())),
                     max_text_len=50)
    result = s.send_full(_make_job(), long_text, image, dry_run=True)
    assert result.text_sent is True  # dry-run 仍成功


# ============================================================
# 用例11：uc.start 传 sandbox=False（macOS 持久化 profile 必须）
# ============================================================
def test_uc_start_passes_sandbox_false(tmp_path):
    """sandbox=False 必须传给 uc.start（否则 macOS 持久化 profile 连不上浏览器）。

    回归：2026-07-17 实测 nodriver uc.start 默认 sandbox=True，macOS 下用持久化
    user_data_dir 时 Chrome 启动后立即退出，报「Failed to connect to browser」。
    sandbox=False 才能连上。NodriverChatSender 默认 sandbox=False 并传给 uc.start。
    """
    image = _make_image(tmp_path)
    page = FakeNodriverPage(
        body_text_after_send="送达\n您好，测试话术",
        input_cleared_after_send=True,
        file_inputs_count=1,
        img_confirm_count=1,
    )
    browser = FakeNodriverBrowser(page)
    fake_mod = FakeNodriverMod(browser)
    s = _make_sender(fake_mod)  # 默认 sandbox=False
    assert s.sandbox is False
    s.send_full(_make_job(), "您好，测试话术", image, dry_run=False)
    # uc.start 被调用，且 sandbox=False 透传
    assert len(fake_mod.start_calls) == 1
    assert fake_mod.start_calls[0]["sandbox"] is False


def test_uc_start_sandbox_configurable(tmp_path):
    """sandbox 可显式配置（True 时也透传，不硬编码）。"""
    image = _make_image(tmp_path)
    page = FakeNodriverPage(
        body_text_after_send="送达\n您好，测试话术",
        input_cleared_after_send=True,
        file_inputs_count=1,
        img_confirm_count=1,
    )
    browser = FakeNodriverBrowser(page)
    fake_mod = FakeNodriverMod(browser)
    s = _make_sender(fake_mod, sandbox=True)
    assert s.sandbox is True
    s.send_full(_make_job(), "您好，测试话术", image, dry_run=False)
    assert fake_mod.start_calls[0]["sandbox"] is True


# ============================================================
# v2 新增：_locate_chat_image_input 精准选 file input 测试
# ============================================================
@pytest.mark.asyncio
async def test_locate_chat_image_input_picks_pure_image():
    """v2：file_inputs 中只选 accept 纯图片格式（不含 pdf/docx）的那个。

    场景：Boss 聊天页有 3 个 file input：
      - [0] 聊天图片 accept=image/gif,image/jpeg...
      - [1] 简历附件 accept=image/jpg, ..., application/pdf, application/msword
      - [2] 简历对话框 accept=同 [1]
    应返回 [0]。
    """
    from boss_auto_apply.browser.nodriver_sender import NodriverChatSender

    class _FakeInput:
        def __init__(self, accept):
            self._accept = accept
            self.send_file_called = False

        async def apply(self, js_fn):
            if "accept" in js_fn:
                return self._accept
            return None

        async def send_file(self, *paths):
            self.send_file_called = True

    class _FakePage:
        def __init__(self, inputs):
            self._inputs = inputs

        async def query_selector_all(self, selector):
            return self._inputs if 'file' in selector else []

        async def evaluate(self, expr, return_by_value=False):
            # _locate_chat_image_input 的 evaluate 兜底（不应被调用，apply 已能读）
            if "application/pdf" in expr:
                return 0
            return None

    inputs = [
        _FakeInput("image/gif,image/jpeg,image/jpg,image/png"),  # 聊天图片
        _FakeInput("image/jpg, image/jpeg, application/pdf, application/msword"),  # 简历附件
        _FakeInput("image/jpg, application/pdf"),  # 对话框
    ]
    page = _FakePage(inputs)

    # 构造 sender（不真启动浏览器）
    from boss_auto_apply.config import load_config
    cfg = load_config("config/config.yaml")
    sender = NodriverChatSender(user_data_dir="~/.boss-auto-apply/chrome-profile",
                                logger_obj=__import__('loguru').logger)

    selected = await sender._locate_chat_image_input(page)
    assert selected is inputs[0], f"应选第 0 个（聊天图片），实际选了 {selected}"


@pytest.mark.asyncio
async def test_locate_chat_image_input_fallback_when_no_match():
    """v2：所有 file input 都不符合纯图片格式时，兜底用第一个。"""
    from boss_auto_apply.browser.nodriver_sender import NodriverChatSender

    class _FakeInput:
        def __init__(self, accept):
            self._accept = accept

        async def apply(self, js_fn):
            return self._accept if "accept" in js_fn else None

    class _FakePage:
        def __init__(self, inputs):
            self._inputs = inputs

        async def query_selector_all(self, selector):
            return self._inputs if 'file' in selector else []

        async def evaluate(self, expr, return_by_value=False):
            if "application/pdf" in expr:
                return -1  # 兜底也找不到
            return None

    inputs = [_FakeInput("application/pdf, application/msword")]
    page = _FakePage(inputs)
    sender = NodriverChatSender(user_data_dir="~/.boss-auto-apply/chrome-profile",
                                logger_obj=__import__('loguru').logger)
    selected = await sender._locate_chat_image_input(page)
    assert selected is inputs[0], "兜底应返回第一个 file input"
