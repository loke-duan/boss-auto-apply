"""WebChatSender — 网页版聊天发送（话术 + 图片，设计 §7）。

在 Boss 聊天页定位聊天对象，**先发文字话术，再发图片简历**，每步确认消息出现在聊天流。

顺序约束（设计 §7.4）：先话术后图片。理由：话术是钩子，先建立「为什么发图」的上下文，
与真实求职者行为一致；图片先发会让 HR 困惑且阻塞话术。

dry-run 安全：``send_text/send_image/send_full(dry_run=True)`` 只 log 不真发。
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..errors import ImagePreprocessError
from ..models import JobRow
from . import selectors as sel
from .image_util import ImagePreprocessResult, preprocess_resume_image
from .manager import BOSS_CHAT_URL, BrowserManager

__all__ = [
    "ChatSendResult",
    "ImagePreprocessResult",
    "WebChatSender",
]


@dataclass
class ChatSendResult:
    """聊天发送结果（话术 + 图片，设计 §7.1）。"""
    job_id: str
    text_sent: bool = False          # 话术发送成功
    image_sent: bool = False         # 图片发送成功
    text_confirmed: bool = False     # 话术出现在聊天流（确认）
    image_confirmed: bool = False    # 图片出现在聊天流（确认）
    image_path: str | None = None    # 实际发送的图片路径（预处理后）
    error: str | None = None
    risk_signal: str | None = None
    elapsed_sec: float = 0.0


# ImagePreprocessResult 从 .image_util 导入（re-export 保持向后兼容）


class WebChatSender:
    """网页版聊天发送（话术 + 图片，设计 §7.2 / ADR-0003）。

    双引擎分发（``driver`` 参数）：
    - ``"drissionpage"``（默认）：DrissionPage 引擎。已知 Boss 聊天页 SPA 被
      zpAegis 反爬阻断不渲染（ADR-0003），仅 dry-run 有意义。
    - ``"nodriver"``：委托 :class:`NodriverChatSender`，绕过 zpAegis 真发送。
      nodriver 自管浏览器生命周期，``browser`` 参数可为 None。
    """

    CHAT_URL = BOSS_CHAT_URL

    def __init__(
        self,
        browser: BrowserManager | None = None,
        logger_obj: Any = None,
        *,
        sleep_fn: Any = time.sleep,
        selectors_mod: Any = None,
        max_text_len: int = 200,
        image_max_kb: int = 1024,
        image_width_px: int = 1080,
        driver: str = "drissionpage",
        user_data_dir: str = "~/.boss-auto-apply/chrome-profile",
        headless: bool = False,
    ) -> None:
        """初始化。

        Args:
            browser: BrowserManager 实例（drissionpage 引擎需要；nodriver 可 None）。
            logger_obj: loguru logger。
            sleep_fn: sleep 函数（测试注入）。
            selectors_mod: selectors 模块（测试注入）。
            max_text_len: 聊天框字数上限（[假设] 200，待实测 T6）。
            image_max_kb: 图片压缩到多大以下（KB）。
            image_width_px: 图片目标宽度（px）。
            driver: 引擎选择——``"drissionpage"``（默认）或 ``"nodriver"``（ADR-0003）。
            user_data_dir: nodriver 引擎的 Chrome profile 路径。
            headless: nodriver 引擎是否无头。
        """
        self.browser = browser
        self.log = logger_obj or logger
        self._sleep = sleep_fn
        self._sel = selectors_mod or sel
        self.max_text_len = max_text_len
        self.image_max_kb = image_max_kb
        self.image_width_px = image_width_px
        self.driver = driver
        # nodriver 引擎（延迟构造，仅在 driver="nodriver" 且真发送时才 import）
        self._nodriver_sender: Any = None
        self._nodriver_cfg = {
            "user_data_dir": user_data_dir,
            "headless": headless,
            "max_text_len": max_text_len,
            "image_max_kb": image_max_kb,
            "image_width_px": image_width_px,
            "logger_obj": logger_obj,
        }

    # ============================================================
    # 主接口
    # ============================================================
    def _send_via_nodriver(
        self,
        job: JobRow,
        greet_text: str,
        image_path: str,
        *,
        dry_run: bool = False,
    ) -> ChatSendResult:
        """委托 NodriverChatSender 发送（ADR-0003）。

        延迟构造 NodriverChatSender（首次调用时 import nodriver）。
        """
        if self._nodriver_sender is None:
            from .nodriver_sender import NodriverChatSender
            self._nodriver_sender = NodriverChatSender(**self._nodriver_cfg)
        self.log.info(f"[nodriver] 委托 NodriverChatSender 发送（job={job.job_id}）")
        return self._nodriver_sender.send_full(
            job, greet_text, image_path, dry_run=dry_run
        )

    def send_full(
        self,
        job: JobRow,
        greet_text: str,
        image_path: str,
        *,
        dry_run: bool = False,
        wait_relation_sec: int = 8,
    ) -> ChatSendResult:
        """完整发送流程（先话术后图片，设计 §7.2）。

        1. browser.ensure_browser()
        2. ``_navigate_to_chat``（聊天页定位对象）
        3. ``_wait_relation``（等待沟通关系建立）
        4. ``send_text``（→ text_sent）
        5. text_sent 成功 → ``send_image``（→ image_sent）
        6. 合并返回 :class:`ChatSendResult`

        部分失败处理（设计 §7.4）：
        - 话术成功 + 图片失败 → 状态留 text_sent（下次 resume 重试图片）
        - 话术失败 → 不发图片（避免 HR 困惑）

        Args:
            job: 目标岗位。
            greet_text: 话术文本（已校验）。
            image_path: 图片简历绝对路径。
            dry_run: True 则话术和图片都不真发。
            wait_relation_sec: greet 后等待沟通关系秒数。

        Returns:
            :class:`ChatSendResult`。
        """
        # 引擎分发（ADR-0003）：nodriver 委托 NodriverChatSender
        if self.driver == "nodriver":
            return self._send_via_nodriver(job, greet_text, image_path, dry_run=dry_run)
        # 默认 DrissionPage 引擎
        start = time.time()
        if self.browser is None:
            raise RuntimeError("drissionpage 引擎需要 BrowserManager 实例（browser=None）")
        self.browser.ensure_browser()
        result = ChatSendResult(job_id=job.job_id)
        self._navigate_to_chat(job)
        self._wait_relation(wait_relation_sec)
        # 4. 发话术
        try:
            ok = self.send_text(job, greet_text, dry_run=dry_run)
        except Exception as e:
            result.error = f"text_send_error: {e}"
            result.elapsed_sec = time.time() - start
            return result
        result.text_sent = ok
        if not ok:
            result.error = result.error or "text_send_failed"
            result.elapsed_sec = time.time() - start
            # 话术失败 → 不发图片（设计 §7.4）
            self.log.warning(f"话术发送失败，跳过图片（job={job.job_id}）")
            return result
        # 5. 发图片（话术成功才发）
        if dry_run:
            self.log.info(f"[dry-run] WebChatSender 会发图片 {image_path}（job={job.job_id}），但跳过真发")
            result.image_sent = True  # dry-run mock 成功
            result.elapsed_sec = time.time() - start
            return result
        try:
            img_ok = self.send_image(job, image_path, dry_run=dry_run)
        except Exception as e:
            result.error = f"image_send_error: {e}"
            result.elapsed_sec = time.time() - start
            return result
        result.image_sent = img_ok
        if not img_ok:
            result.error = result.error or "image_send_failed"
        result.elapsed_sec = time.time() - start
        return result

    def send_text(self, job: JobRow, text: str, *, dry_run: bool = False) -> bool:
        """发文字话术（设计 §7.2）。

        1. 定位 textarea
        2. dry_run → 返回 True 不真发
        3. textarea.input(text)（截断到 max_text_len）
        4. ``_click_send_button``
        5. ``_confirm_text_appeared``（在聊天流找该文本，确认成功）

        Args:
            job: 目标岗位。
            text: 话术文本（已校验长度）。
            dry_run: True 则不真发。

        Returns:
            是否发送成功（含确认）。
        """
        tab = self.browser.get_tab()
        textarea = self._locate_input_textarea()
        if textarea is None:
            self.log.error(f"找不到聊天输入框（job={job.job_id}）")
            return False
        # 截断（防超字数）
        truncated = text[: self.max_text_len]
        if len(truncated) < len(text):
            self.log.warning(f"话术超 {self.max_text_len} 字，已截断（job={job.job_id}）")
        if dry_run:
            self.log.info(f"[dry-run] WebChatSender 会发话术「{truncated[:30]}...」（job={job.job_id}），但跳过真发")
            return True
        try:
            textarea.input(truncated)
        except Exception as e:
            self.log.error(f"textarea.input 失败：{e}")
            return False
        self._sleep(random.uniform(0.3, 0.8))
        # 点发送
        try:
            self._click_send_button()
        except Exception as e:
            self.log.error(f"点发送按钮失败：{e}")
            return False
        # 确认
        confirmed = self._confirm_text_appeared(truncated)
        if not confirmed:
            self.log.warning(f"话术发送后聊天流未出现该文本（可能静默拦截，job={job.job_id}）")
        return confirmed

    def send_image(self, job: JobRow, image_path: str, *, dry_run: bool = False) -> bool:
        """发图片简历（设计 §7.2 / §7.3）。

        1. ``_preprocess_image``（压缩到 <1MB / 1080px 宽）
        2. dry_run → 返回 True 不真发
        3. 定位隐藏 input[type=file][accept*="image"]
        4. file_input.input(processed_path)
        5. ``_wait_upload_complete``
        6. ``_confirm_image_appeared``

        Args:
            job: 目标岗位。
            image_path: 图片绝对路径。
            dry_run: True 则不真发。

        Returns:
            是否发送成功（含确认）。
        """
        if not os.path.exists(image_path):
            self.log.error(f"图片不存在：{image_path}")
            return False
        if dry_run:
            self.log.info(f"[dry-run] WebChatSender 会发图片 {image_path}（job={job.job_id}），但跳过真发")
            return True
        # 预处理
        try:
            pre = self._preprocess_image(image_path, tag=job.job_id)
        except ImagePreprocessError:
            raise
        except Exception as e:
            raise ImagePreprocessError(f"图片预处理失败：{e}") from e
        send_path = pre.processed_path
        # 定位隐藏 input
        file_input = self._locate_image_input()
        if file_input is None:
            # 兜底：先点「图片」按钮让 input 可见
            self.log.debug("隐藏 input 定位失败，尝试点「图片」工具按钮")
            tool_btn = self._sel.find_element(self.browser.get_tab(), "chat.image_tool_button", timeout=2)
            if tool_btn is not None:
                try:
                    tool_btn.click()
                except Exception:
                    pass
                self._sleep(0.5)
                file_input = self._locate_image_input()
            if file_input is None:
                self.log.error(f"找不到图片上传 input（job={job.job_id}）")
                return False
        try:
            file_input.input(send_path)
        except Exception as e:
            self.log.error(f"file_input.input 失败：{e}")
            return False
        # 等上传完成
        self._wait_upload_complete()
        # 确认
        confirmed = self._confirm_image_appeared()
        if not confirmed:
            self.log.warning(f"图片发送后聊天流未出现图片（可能上传失败，job={job.job_id}）")
        return confirmed

    # ============================================================
    # 内部：导航/等待
    # ============================================================
    def _navigate_to_chat(self, job: JobRow) -> None:
        """定位聊天对象（设计 §7.2）。

        greet（点击详情页「立即沟通」/「继续沟通」）成功后，浏览器已跳转到带完整
        securityId token 的聊天页 ``/web/geek/chat?id=...&jobId=...&securityId=...``，
        聊天 SPA 据此 token 渲染输入框。此时**不能**再 ``tab.get(CHAT_URL)``（裸 URL
        无 token）—— 会覆盖已加载的有效聊天页，导致 SPA 不渲染、输入框找不到。

        历史 bug（已修）：曾无条件 ``tab.get(CHAT_URL)`` 覆盖，使每次 send_text 都
        因输入框找不到而失败（text_send_failed）。正确做法：已在聊天页则只等待渲染，
        不在才导航（兜底）。
        """
        tab = self.browser.get_tab()
        try:
            cur_url = tab.url or ""
        except Exception:
            cur_url = ""
        if "/web/geek/chat" in cur_url:
            # greet 已把我们带到有效聊天页（带 token）—— 不要覆盖，只等 SPA 渲染
            self.log.debug(f"已在聊天页（{cur_url[:80]}...），不重复导航，等 SPA 渲染")
            self._sleep(random.uniform(1.5, 2.5))
            return
        # 兜底：不在聊天页才导航（裸 URL，可能因缺 token 渲染不全，仅作 last resort）
        self.log.debug(f"不在聊天页（当前 {cur_url[:60]}），导航 {self.CHAT_URL}")
        try:
            tab.get(self.CHAT_URL)
        except Exception as e:
            self.log.debug(f"导航聊天页异常（可能已在页内）：{e}")
        self._sleep(random.uniform(1.5, 2.5))  # 等聊天列表渲染

    def _wait_relation(self, sec: int) -> None:
        """等待沟通关系建立（greet 后关系建立可能有延迟）。

        随机 5-15s（设计 §7.2，sec 作为均值）。
        """
        wait = random.uniform(max(2, sec - 3), sec + 7)
        self.log.debug(f"等待沟通关系建立 {wait:.1f}s")
        self._sleep(wait)

    # ============================================================
    # 内部：定位元素
    # ============================================================
    def _locate_input_textarea(self) -> Any:
        """定位聊天输入框（设计 §7.2 / §12）。

        Boss 聊天页输入框是 ``.chat-input[contenteditable="true"]``（2026-07-12 nodriver
        实测 verified=True），不是 textarea。先用 contenteditable 选择器，找不到再兜底
        旧 textarea 选择器（防 Boss 改版回退）。
        """
        tab = self.browser.get_tab()
        # 主：contenteditable（实测 Boss 聊天页用此结构）
        el = self._sel.find_element(tab, "chat.input_contenteditable", timeout=8)
        if el is not None:
            return el
        # 兜底：旧 textarea 选择器（防 Boss 改版回退）
        return self._sel.find_element(tab, "chat.input_textarea", timeout=3)

    def _locate_image_input(self) -> Any:
        """定位隐藏的图片上传 input[type=file][accept*="image]（设计 §7.5）。

        Returns:
            input 元素，或 None。
        """
        return self._sel.find_element(self.browser.get_tab(), "chat.file_input_image", timeout=3)

    def _click_send_button(self) -> None:
        """点发送按钮 / 回车发送（设计 §7.2 / §12）。

        Boss 聊天页 contenteditable 输入框回车即发送。优先找显式发送按钮；
        找不到则对 contenteditable 聚焦 + dispatch keydown Enter（nodriver 2026-07-12
        实测验证的发送方式），再兜底 ``.input('\\n')``。

        Raises:
            RuntimeError: 所有发送方式都失败。
        """
        tab = self.browser.get_tab()
        btn = self._sel.find_element(tab, "chat.send_button", timeout=3)
        if btn is not None:
            try:
                btn.click()
                return
            except Exception as e:
                self.log.debug(f"点击发送按钮异常，改回车发送：{e}")
        # 兜底 1：contenteditable dispatch Enter（Boss 实际发送方式）
        self.log.debug("找不到发送按钮，尝试 contenteditable Enter 发送")
        try:
            tab.run_js(
                "(()=>{const i=document.querySelector('.chat-input[contenteditable=\"true\"]')"
                "||document.querySelector('.chat-input');if(!i)return 'no_input';"
                "i.focus();const e=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',"
                "keyCode:13,which:13,bubbles:true,cancelable:true});i.dispatchEvent(e);"
                "return 'enter_dispatched';})()"
            )
            return
        except Exception as e:
            self.log.debug(f"JS Enter dispatch 失败，兜底 .input：{e}")
        # 兜底 2：对输入元素 input 换行
        el = self._locate_input_textarea()
        if el is not None:
            try:
                el.input("\n")
                return
            except Exception:
                pass
        raise RuntimeError("所有发送方式失败（无发送按钮 + Enter dispatch 失败）")

    # ============================================================
    # 内部：确认（防「假成功」，设计 §7.6）
    # ============================================================
    def _confirm_text_appeared(self, text: str, timeout: int = 8) -> bool:
        """在聊天流（自己消息）找 text 子串。

        Args:
            text: 待确认的话术（取前 30 字做子串匹配，避免 emoji/标点差异）。
            timeout: 最长等待秒数。

        Returns:
            是否在聊天流找到。
        """
        needle = text[:30] if len(text) > 30 else text
        if not needle:
            return False
        tab = self.browser.get_tab()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # 优先查自己消息
                mine = tab.eles(f"css:{sel.get_selector('chat.mine_message')}", timeout=2)
            except Exception:
                mine = []
            if not mine:
                try:
                    mine = tab.eles(f"css:{sel.get_selector('chat.message_item')}", timeout=2)
                except Exception:
                    mine = []
            for msg in mine:
                try:
                    content = msg.text or ""
                except Exception:
                    content = ""
                if needle in content:
                    return True
            self._sleep(0.8)
        return False

    def _confirm_image_appeared(self, timeout: int = 15) -> bool:
        """在聊天流找最近的图片元素（设计 §7.6）。"""
        tab = self.browser.get_tab()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                imgs = tab.eles(f"css:{sel.get_selector('chat.message_image')}", timeout=2)
            except Exception:
                imgs = []
            if imgs:
                # 检查最后一张是否加载完成（naturalWidth>0）
                last = imgs[-1]
                try:
                    nw = last.run_js("return this.naturalWidth || 0") if hasattr(last, "run_js") else 1
                except Exception:
                    nw = 1
                if nw and nw > 0:
                    return True
            self._sleep(1.0)
        return False

    def _wait_upload_complete(self, timeout: int = 30) -> None:
        """等图片上传进度条消失（设计 §7.2）。

        [假设] 上传中显示 .upload-progress，完成后消失。
        """
        tab = self.browser.get_tab()
        deadline = time.time() + timeout
        # 先等进度条出现（最多 2s），再等它消失
        appeared = False
        t0 = time.time()
        while time.time() - t0 < 2:
            if self._sel.find_element(tab, "chat.upload_progress", timeout=0.5):
                appeared = True
                break
            self._sleep(0.3)
        if not appeared:
            # 没出现进度条，可能瞬间完成，直接返回
            return
        # 等进度条消失
        while time.time() < deadline:
            if not self._sel.find_element(tab, "chat.upload_progress", timeout=1):
                return
            self._sleep(0.8)
        self.log.warning(f"上传进度条 {timeout}s 未消失，可能上传卡住")

    # ============================================================
    # 图片预处理（委托共享纯函数，设计 §7.3）
    # ============================================================
    def _preprocess_image(self, path: str, *, tag: str = "resume") -> ImagePreprocessResult:
        """图片预处理：委托 :func:`preprocess_resume_image` 共享纯函数。

        保留实例方法签名以兼容现有调用（``self._preprocess_image(path, tag=...)``），
        内部把 ``self.image_max_kb`` / ``self.image_width_px`` / ``self.log``
        传给模块级函数，避免两条发送链路逻辑分叉。
        """
        return preprocess_resume_image(
            path,
            max_kb=self.image_max_kb,
            width_px=self.image_width_px,
            tag=tag,
            logger_obj=self.log,
        )
