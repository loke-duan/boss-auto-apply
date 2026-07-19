"""NodriverChatSender — nodriver 引擎的聊天页发送（话术 + 图片，ADR-0003）。

Boss 聊天页的 zpAegis 安全 SDK 会检测 CDP ``Runtime.enable`` 调用——这是
DrissionPage / Playwright / Scrapling 自动化时都会发出的命令，会在 V8 运行时
留下可检测的插桩痕迹。检测到后 zpAegis 阻断 ``/wapi/zppassport/set/zpToken``
API（2ms / 0 字节），导致 ``hasLoadedUser=false``，Vue Router 卡在 ``/``，
SPA 不渲染。

nodriver（undetected-chromedriver 的继任者）用原始 CDP 通信但**刻意不调
``Runtime.enable``**，从而绕过 zpAegis 检测。实测：zpToken 返回 200（37ms /
361 字节），SPA 完整渲染，聊天输入框 ``.chat-input[contenteditable="true"]``
可见。

同步封装：nodriver 是 async API，项目是纯 sync。本类用 ``asyncio.run`` 在
内部桥接，对外暴露同步 ``send_full`` 接口，与 :class:`WebChatSender` 对齐。

⚠️ **profile 互斥**：nodriver 和 DrissionPage 不能同时用同一 ``user_data_dir``
——会互相覆写 cookies。用 nodriver 前必须确保 DrissionPage 的 Chrome 已关闭。

dry-run 安全：``send_full(dry_run=True)`` 只 log 不真发，不启动浏览器。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import socket
import subprocess
import time
from typing import Any

from loguru import logger

from ..errors import ImagePreprocessError, SenderError
from ..models import JobRow
from . import selectors as sel
from .image_util import ImagePreprocessResult, preprocess_resume_image
from .web_chat_sender import ChatSendResult

__all__ = ["NodriverChatSender"]


class NodriverChatSender:
    """nodriver 引擎的聊天页发送（话术 + 图片，ADR-0003）。

    与 :class:`WebChatSender` 接口对齐（``send_full`` → :class:`ChatSendResult`），
    但内部用 nodriver（async）而非 DrissionPage（sync）。``asyncio.run`` 桥接。

    生命周期：每次 ``send_full`` 启动并关闭浏览器（nodriver 不复用实例，
    避免与 DrissionPage profile 冲突）。

    测试注入：构造时传 ``nodriver_mod``（mock nodriver 模块）、``sleep_fn``、
    ``async_runner``（替换 asyncio.run）可避免真启动浏览器。
    """

    # Boss 聊天页详情页 URL 模板（job_id 即 securityId/encrypted ID）
    JOB_DETAIL_URL_TMPL = "https://www.zhipin.com/job_detail/{job_id}.html"

    def __init__(
        self,
        user_data_dir: str = "~/.boss-auto-apply/chrome-profile",
        headless: bool = False,
        logger_obj: Any = None,
        *,
        nodriver_mod: Any = None,
        sleep_fn: Any = time.sleep,
        async_sleep_fn: Any = None,
        time_fn: Any = time.time,
        async_runner: Any = None,
        max_text_len: int = 200,
        image_max_kb: int = 1024,
        image_width_px: int = 1080,
        browser_args: list[str] | None = None,
        sandbox: bool = False,
    ) -> None:
        """初始化。

        Args:
            user_data_dir: Chrome 专用 profile 路径（与 DrissionPage 共用，但不可同时运行）。
            headless: 是否无头（反检测建议 False）。
            logger_obj: loguru logger。
            nodriver_mod: nodriver 模块（测试注入 mock；None 则延迟 import）。
            sleep_fn: 同步 sleep 函数（测试注入）。
            async_sleep_fn: async sleep 函数（默认 asyncio.sleep；测试注入 no-op）。
            time_fn: 时间函数（默认 time.time；测试注入可控制超时循环）。
            async_runner: async→sync 桥接函数（默认 asyncio.run；测试可注入 mock）。
            max_text_len: 话术字数上限。
            image_max_kb: 图片压缩目标大小（KB）。
            image_width_px: 图片压缩目标宽度（px）。
            browser_args: 传给 nodriver 的 Chrome 启动参数。
            sandbox: Chrome sandbox 开关。默认 False——nodriver ``uc.start`` 默认
                ``sandbox=True``，但 macOS 下用持久化 user_data_dir 时 sandbox 会
                阻断 CDP 连接（Chrome 启动后立即退出，报「Failed to connect to
                browser」）。2026-07-17 实测：sandbox=True 持久化 profile 必失败，
                sandbox=False 才能连上（temp profile 两者皆可）。
        """
        self.user_data_dir = os.path.expanduser(user_data_dir)
        self.headless = headless
        self.log = logger_obj or logger
        self._nodriver = nodriver_mod  # 延迟 import
        self._sleep = sleep_fn
        self._time = time_fn
        self._async_sleep = async_sleep_fn or asyncio.sleep
        self._async_runner = async_runner or asyncio.run
        self.max_text_len = max_text_len
        self.image_max_kb = image_max_kb
        self.image_width_px = image_width_px
        self.sandbox = sandbox
        # sandbox=False 时必须显式加 --no-sandbox 到 browser_args
        # （nodriver 0.50.3 的 sandbox=False 参数对 Chrome 150 不生效，需 browser_args 兜底）
        default_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
        ]
        if not sandbox and "--no-sandbox" not in (browser_args or []):
            default_args.append("--no-sandbox")
        self.browser_args = browser_args or default_args

    # ============================================================
    # 主接口（同步）
    # ============================================================
    def send_full(
        self,
        job: JobRow,
        greet_text: str,
        image_path: str,
        *,
        dry_run: bool = False,
        wait_relation_sec: int = 8,
    ) -> ChatSendResult:
        """完整发送流程（先话术后图片，与 WebChatSender.send_full 对齐）。

        1. dry_run → 直接返回 mock 成功（不启动浏览器）
        2. 启动 nodriver 浏览器（专用 profile）
        3. 导航详情页 → 点「继续沟通/立即沟通」→ 等聊天页 SPA 渲染
        4. ``send_text``（→ text_sent）
        5. text_sent 成功 → ``send_image``（→ image_sent）
        6. 关闭浏览器，返回 :class:`ChatSendResult`

        Args:
            job: 目标岗位（job_id 用于构造详情页 URL）。
            greet_text: 话术文本（已校验）。
            image_path: 图片简历绝对路径。
            dry_run: True 则不真发（不启动浏览器）。
            wait_relation_sec: greet 后等待沟通关系秒数（nodriver 路径下通常已是好友，短等待）。

        Returns:
            :class:`ChatSendResult`。
        """
        start = time.time()
        result = ChatSendResult(job_id=job.job_id)

        if dry_run:
            self.log.info(
                f"[dry-run] NodriverChatSender 会发话术「{greet_text[:30]}...」"
                f"+ 图片 {image_path}（job={job.job_id}），但跳过真发"
            )
            result.text_sent = True
            result.image_sent = True
            result.elapsed_sec = time.time() - start
            return result

        # 截断话术
        truncated = greet_text[: self.max_text_len]
        if len(truncated) < len(greet_text):
            self.log.warning(f"话术超 {self.max_text_len} 字，已截断（job={job.job_id}）")

        # 图片预处理（同步，纯 Pillow）
        try:
            pre = self._preprocess_image(image_path, tag=job.job_id)
        except ImagePreprocessError:
            raise
        except Exception as e:
            raise ImagePreprocessError(f"图片预处理失败：{e}") from e
        result.image_path = pre.processed_path

        # async→sync 桥接：运行完整发送流程
        try:
            self._async_runner(
                self._send_full_async(job, truncated, pre.processed_path, result)
            )
        except Exception as e:
            result.error = result.error or f"nodriver_async_error: {e}"
            self.log.error(f"nodriver 发送异常（job={job.job_id}）：{e}")

        result.elapsed_sec = time.time() - start
        return result

    # ============================================================
    # Chrome 启动 + nodriver 连接
    # ============================================================
    def _find_free_port(self) -> int:
        """找一个空闲端口（绑定后立即释放，给 Chrome 用）。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _wait_port_ready(self, port: int, timeout: float = 30.0) -> bool:
        """轮询端口是否可连（Chrome DevTools 就绪）。

        nodriver 默认只等 2.75s（0.25 + 5×0.5），持久化 profile 启动需 3-5s，
        经常超时。这里给足 30s。
        """
        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ).read()
                return True
            except Exception:
                self._sleep(0.3)
        return False

    async def _launch_chrome_and_connect(self, uc: Any) -> Any:
        """预启动 Chrome 到固定端口 + nodriver host/port 连接（绕过 nodriver 启动超时）。

        nodriver 0.50.3 的 ``uc.start`` 自己启动 Chrome 后只等 2.75s（0.25 + 5×0.5）
        就判定连接失败。但 macOS + 持久化 profile 启动需 3-5s，导致间歇性
        "Failed to connect to browser"。本方法改为：先用 subprocess 启动 Chrome
        （轮询端口就绪，给 30s），再用 ``nodriver.start(host, port)`` 连接已启动实例。

        Chrome 进程存到 ``self._chrome_proc``，``browser.stop()`` 后兜底 kill。
        """
        port = self._find_free_port()
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if not os.path.exists(chrome_path):
            # Linux/Windows 路径兜底（ nodriver 的 config 解析也能找到，这里主防 mac 路径变）
            from nodriver.core.config import Config
            chrome_path = Config().browser_executable_path

        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.user_data_dir}",
            "--remote-allow-origins=*",
        ] + self.browser_args
        if not self.sandbox and "--no-sandbox" not in args:
            args.append("--no-sandbox")

        self.log.debug(f"[nodriver] 预启动 Chrome（port={port}，pid 待定）")
        # stderr/devnull 避免 PIPE 缓冲阻塞；Chrome 作为独立进程，不依赖父进程
        self._chrome_proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if not self._wait_port_ready(port, timeout=30):
            self._kill_chrome()
            raise SenderError(
                f"Chrome 预启动后 30s 内端口 {port} 未就绪（profile 可能损坏/被锁）"
            )
        self.log.debug(f"[nodriver] Chrome 端口就绪（port={port}），nodriver 连接...")

        # nodriver 连接已启动的 Chrome（connect_existing 模式，传 host+port）
        browser = await uc.start(
            host="127.0.0.1", port=port,
            browser_args=self.browser_args,
            sandbox=self.sandbox,
        )
        return browser

    def _kill_chrome(self) -> None:
        """兜底 kill 预启动的 Chrome 进程（browser.stop 可能不彻底）。"""
        proc = getattr(self, "_chrome_proc", None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                self._sleep(0.5)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        self._chrome_proc = None

    # ============================================================
    # async 核心流程
    # ============================================================
    async def _send_full_async(
        self,
        job: JobRow,
        greet_text: str,
        image_path: str,
        result: ChatSendResult,
    ) -> None:
        """异步完整发送流程（在 asyncio.run 内执行）。

        填充 ``result`` 的 text_sent / image_sent / text_confirmed / image_confirmed / error。
        """
        uc = self._get_nodriver()
        detail_url = self.JOB_DETAIL_URL_TMPL.format(job_id=job.job_id)
        self.log.info(f"[nodriver] 启动浏览器（profile={self.user_data_dir}，sandbox={self.sandbox}）...")

        browser = await self._launch_chrome_and_connect(uc)
        try:
            page = await browser.get(detail_url)
            await self._async_sleep(5)  # 等详情页渲染

            # 点击「继续沟通」/「立即沟通」→ 等聊天页跳转 + SPA 渲染
            chat_ready = await self._navigate_to_chat(page, job.job_id)
            if not chat_ready:
                result.error = result.error or "chat_page_not_rendered"
                self.log.error(f"聊天页 SPA 未渲染（job={job.job_id}）")
                return

            # 发话术
            text_ok = await self._send_text_async(page, greet_text, job.job_id)
            result.text_sent = text_ok
            result.text_confirmed = text_ok  # _send_text_async 返回值已含确认结果
            if not text_ok:
                result.error = result.error or "text_send_failed"
                self.log.warning(f"话术发送失败，跳过图片（job={job.job_id}）")
                return

            # 等沟通关系（已是好友时短等即可）
            self._sleep(random.uniform(2, 4))

            # 发图片
            img_ok = await self._send_image_async(page, image_path, job.job_id)
            result.image_sent = img_ok
            result.image_confirmed = img_ok  # _send_image_async 返回值已含确认结果
            if not img_ok:
                result.error = result.error or "image_send_failed"

        finally:
            try:
                browser.stop()
            except Exception as e:
                self.log.debug(f"browser.stop 异常（忽略）：{e}")
            # 兜底 kill 预启动的 Chrome 进程（browser.stop 可能不彻底）
            self._kill_chrome()

    async def _navigate_to_chat(self, page: Any, job_id: str) -> bool:
        """导航到聊天页并等 SPA 渲染（v3.1 重构：点「继续沟通」让 Boss 自动跳转）。

        v3.1 修正（2026-07-19 实测 chat_page_not_rendered 后）：
        v3 曾尝试「不点按钮，直接 page.get('/web/geek/chat')」，但裸 URL 不带
        id/securityId 参数，Boss 聊天页拿不到沟通对象信息 → SPA 部分渲染
        （只有联系人列表，没聊天输入框）→ chat_page_not_rendered。

        v3.1 回归 v2 验证过的成功路径：
        1. nodriver 启动后访问详情页（DrissionPage web_greeter 已点头次并等 8s）
        2. 此时详情页按钮文案应已变为「继续沟通」（Boss 后端已建立关系）
        3. nodriver 点击「继续沟通」→ **Boss 自动跳转** `/web/geek/chat?id=xxx&securityId=xxx`
           （带参数，SPA 才能完整渲染聊天区）
        4. 等 SPA 渲染（`.chat-input[contenteditable="true"]` 出现）

        兜底：如果按钮还是「立即沟通」（Boss 后端慢），也点（重复点头次 Boss 会判
        already_friend，不会重复发请求）。

        Args:
            page: nodriver 的 Tab/Page 对象（已在 detail_url）。
            job_id: 岗位 ID（日志用）。

        Returns:
            聊天页 SPA 是否渲染成功。
        """
        # v3.1：在详情页找「继续沟通」按钮（点头次后 Boss 会把按钮文案改为这个）
        # 优先找「继续沟通」（沟通关系已建立），兜底找「立即沟通」（Boss 后端慢）
        clicked = False
        clicked_btn_text = ""
        for btn_text in ("继续沟通", "立即沟通"):
            try:
                btn = await page.find(btn_text, timeout=10)
                if btn:
                    await btn.click()
                    clicked = True
                    clicked_btn_text = btn_text
                    self.log.info(
                        f"[nodriver] 点击「{btn_text}」（job={job_id}，"
                        f"让 Boss 自动跳转带参数 /web/geek/chat?id=xxx&securityId=xxx）"
                    )
                    break
            except Exception as e:
                self.log.debug(f"[nodriver] find「{btn_text}」失败：{e}")

        if not clicked:
            # 兜底：JS 点击 .btn-startchat
            try:
                js_result = await page.evaluate(
                    "(()=>{const b=document.querySelector('.btn-startchat')"
                    "||document.querySelector('.btn-greet');if(b){b.click();return 'clicked';}return 'not_found';})()",
                    return_by_value=True,
                )
                if isinstance(js_result, tuple):
                    js_result = js_result[0]
                if js_result == "clicked":
                    clicked = True
                    clicked_btn_text = "(JS click)"
                    self.log.info(f"[nodriver] JS 点击沟通按钮成功（job={job_id}）")
            except Exception as e:
                self.log.debug(f"[nodriver] JS 点击失败：{e}")

        if not clicked:
            self.log.error(f"[nodriver] 未找到沟通按钮（job={job_id}）")
            return False

        # 等 Boss 自动跳转到 /web/geek/chat?id=xxx&securityId=xxx
        # v2 实测：点「继续沟通」后 3-5s 内 Boss 会跳转
        await self._async_sleep(3)

        # 验证是否跳转到聊天页（带参数）
        try:
            cur_url = await page.evaluate("window.location.href", return_by_value=True)
            if isinstance(cur_url, tuple):
                cur_url = cur_url[0]
            cur_url = str(cur_url or "")
        except Exception:
            cur_url = ""

        if "/web/geek/chat" in cur_url:
            self.log.info(
                f"[nodriver] 已跳转聊天页（job={job_id}，btn={clicked_btn_text}，"
                f"url 含参数={('securityId' in cur_url or 'id=' in cur_url)}）"
            )
        else:
            # 仍在详情页 → 主动跳（兜底，可能不带参数导致 SPA 部分渲染）
            self.log.warning(
                f"[nodriver] 点击「{clicked_btn_text}」后仍在详情页（{cur_url[:60]}），"
                f"主动跳 /web/geek/chat（job={job_id}，注意：可能因缺参数 SPA 渲染不全）"
            )
            try:
                await page.get("https://www.zhipin.com/web/geek/chat")
            except Exception as e:
                self.log.debug(f"[nodriver] page.get(/web/geek/chat) 异常（继续）：{e}")
            await self._async_sleep(3)

        # 等 SPA 渲染（.chat-input[contenteditable="true"] 出现）
        await self._async_sleep(5)  # 先等 zpToken + userInfo 加载
        t0 = self._time()
        while self._time() - t0 < 40:
            try:
                chat_input = await page.query_selector(
                    sel.get_selector("chat.input_contenteditable")
                )
                if chat_input:
                    self.log.info(
                        f"[nodriver] 聊天页 SPA 渲染成功（job={job_id}，{self._time()-t0+5:.0f}s）"
                    )
                    return True
            except Exception:
                pass
            await self._async_sleep(2)

        self.log.error(f"[nodriver] 聊天页 SPA 40s 未渲染（job={job_id}）")
        return False

    async def _click_latest_friend(self, page: Any, job_id: str) -> bool:
        """在聊天页点击「最近联系人」列表的第一项（刚建立沟通关系的 HR）。

        v2（2026-07-19）：点头次「立即沟通」后跳到 `/web/geek/chat`，
        聊天页默认显示联系人列表，需要点击第一位（最近联系人）才能进入对应聊天区。

        Args:
            page: nodriver Tab 对象。
            job_id: 岗位 ID（日志用）。

        Returns:
            是否成功点击最近联系人。
        """
        # 候选 selector：Boss 聊天页联系人列表项
        # 2026-07-19 实测：.chat-user / .friend-item / .user-item 是常见 class
        click_js = """
        (() => {
            // 找联系人列表项（多种 selector 兜底）
            const selectors = [
                '.chat-user', '.chat-user.v2', '.friend-item', '.user-item',
                '.chat-friend', '.contact-item', '.list-item',
                '.chat-list li', '.friend-list li', '.user-list li'
            ];
            for (const sel of selectors) {
                const items = document.querySelectorAll(sel);
                if (items.length > 0) {
                    // 点第一个（最近联系人）
                    const first = items[0];
                    first.click();
                    return JSON.stringify({clicked: true, selector: sel, count: items.length});
                }
            }
            // 兜底：找所有 click 元素
            const allClickable = document.querySelectorAll('[class*="chat-user"], [class*="friend"], [class*="contact"]');
            if (allClickable.length > 0) {
                allClickable[0].click();
                return JSON.stringify({clicked: true, selector: 'fallback', count: allClickable.length});
            }
            return JSON.stringify({clicked: false});
        })()
        """
        try:
            r = await page.evaluate(click_js, return_by_value=True)
            if isinstance(r, tuple):
                r = r[0]
            r = json.loads(r) if isinstance(r, str) else r
            if r.get("clicked"):
                self.log.info(
                    f"[nodriver] 点击最近联系人（{r.get('selector')}，共 {r.get('count')} 个，job={job_id}）"
                )
                return True
        except Exception as e:
            self.log.debug(f"[nodriver] 点击最近联系人失败：{e}")
        return False

    async def _send_text_async(self, page: Any, text: str, job_id: str) -> bool:
        """发文字话术（nodriver async）。

        1. 定位 ``.chat-input[contenteditable="true"]``
        2. click 聚焦 → ``send_keys(text)``
        3. JS dispatch Enter keydown 发送
        4. 确认话术出现在聊天流 + 输入框已清空

        Args:
            page: nodriver Tab 对象。
            text: 话术文本（已截断）。
            job_id: 岗位 ID。

        Returns:
            是否发送成功（含确认）。
        """
        try:
            chat_input = await page.query_selector(
                sel.get_selector("chat.input_contenteditable")
            )
        except Exception as e:
            self.log.error(f"[nodriver] 定位聊天输入框失败：{e}")
            return False

        if not chat_input:
            self.log.error(f"[nodriver] 聊天输入框未找到（job={job_id}）")
            return False

        # 聚焦 + 输入：用 execCommand('insertText')（contenteditable 标准写入方式）。
        # nodriver 的 send_keys 对 Boss contenteditable 无效（2026-07-17 实测：输入框仍空），
        # execCommand 触发完整 input 事件链，Vue/React 都能响应。
        text_js = json.dumps(text)
        try:
            await chat_input.click()
            await self._async_sleep(0.3)
            ok = await page.evaluate(
                "(()=>{const c=document.querySelector('.chat-input[contenteditable=\"true\"]');"
                "if(!c)return false;c.focus();document.execCommand('selectAll');"
                f"return document.execCommand('insertText', false, {text_js});}})()"
            )
            ok_str = str(ok)
            if "true" not in ok_str.lower():
                # execCommand 失败（部分浏览器已弃用）→ 兜底 innerText + InputEvent
                await page.evaluate(
                    "(()=>{const c=document.querySelector('.chat-input[contenteditable=\"true\"]');"
                    f"if(c){{c.focus();c.innerText={text_js};"
                    f"c.dispatchEvent(new InputEvent('input',{{bubbles:true,data:{text_js},inputType:'insertText'}}));}}}})()"
                )
            self.log.info(f"[nodriver] 话术已输入（job={job_id}）")
        except Exception as e:
            self.log.error(f"[nodriver] 输入话术失败：{e}")
            return False

        await self._async_sleep(random.uniform(0.5, 1.0))

        # 发送：回车。Boss contenteditable 监听 keydown Enter。
        # 优先 dispatch keydown（实测有效），兜底 send_keys('\n')。
        try:
            await page.evaluate(
                "(()=>{const c=document.querySelector('.chat-input[contenteditable=\"true\"]');"
                "if(!c)return 'no_input';c.focus();"
                "const e=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true});"
                "c.dispatchEvent(e);return 'ok';})()"
            )
            self.log.info(f"[nodriver] Enter 发送（job={job_id}）")
        except Exception as e:
            self.log.debug(f"[nodriver] JS Enter 失败，回退 send_keys：{e}")
            try:
                await chat_input.send_keys('\n')
            except Exception as e2:
                self.log.error(f"[nodriver] Enter 发送全部失败：{e2}")
                return False

        # 确认话术出现在聊天流 + 输入框清空
        confirmed = await self._confirm_text(page, text, job_id)
        if not confirmed:
            self.log.warning(f"[nodriver] 话术未确认（可能静默拦截，job={job_id}）")
        return confirmed

    async def _send_image_async(self, page: Any, image_path: str, job_id: str) -> bool:
        """发图片简历（nodriver async）。

        v2 修复（2026-07-19）：
        - **精确选择聊天图片 input**：聊天页通常有 3 个 file input（聊天图片/简历附件/对话框），
          只选 `accept` 以 `image/gif` 或 `image/jpeg` **开头**且不含 `application/pdf` 的那个
          （聊天图片入口的 accept 是纯图片格式，简历附件的 accept 含 pdf/docx）。
        - **基于"图片数量增加"判成功**：记录 send_file 前的聊天内容图片数量，
          发送后等待数量增加（旧版只看"有 img naturalWidth>0"，被历史图假阳性坑过）。

        Args:
            page: nodriver Tab 对象。
            image_path: 预处理后的图片路径。
            job_id: 岗位 ID。

        Returns:
            是否发送成功（含确认）。
        """
        # 1. 精确定位聊天图片 input（不是简历附件 input）
        chat_image_input = await self._locate_chat_image_input(page)
        if chat_image_input is None:
            self.log.error(f"[nodriver] 未找到聊天图片 input（job={job_id}，可能页面未渲染）")
            return False

        # 2. 记录发送前的内容图片数量
        before_count = await self._count_chat_content_images(page)
        self.log.info(f"[nodriver] 发图前聊天内容图片数={before_count}（job={job_id}）")

        # 3. send_file
        try:
            await chat_image_input.send_file(image_path)
            self.log.info(f"[nodriver] 图片 send_file 完成（job={job_id}）")
        except Exception as e:
            self.log.error(f"[nodriver] send_file 失败：{e}")
            return False

        # 4. 基于数量增加确认（不再用"有 img naturalWidth>0"这种宽松判据）
        confirmed = await self._confirm_image_by_count(page, job_id, before_count)
        if not confirmed:
            self.log.warning(f"[nodriver] 图片未确认（send_file 调用成功但 60s 内聊天流未出现新图，job={job_id}）")
        return confirmed

    async def _locate_chat_image_input(self, page: Any) -> Any:
        """精确定位聊天图片发送的 file input。

        Boss 聊天页 file input 区分（2026-07-19 实测）：
        - **聊天图片**：`accept="image/gif,image/jpeg,image/jpg,image/png"`，在 `chat-editor > chat-controls` 下
        - 简历附件：`accept="image/jpg, ..., application/pdf, application/msword, ..."`，在 `upload-resume-dialog` 下
        - 上传对话框：同简历附件，在另一个 dialog 下

        本方法只返回聊天图片 input（accept 只含图片格式，不含 pdf/docx）。

        Returns:
            file input 元素，或 None（未找到）。
        """
        try:
            file_inputs = await page.query_selector_all('input[type="file"]')
        except Exception as e:
            self.log.debug(f"[nodriver] query_selector_all file input 失败：{e}")
            return None

        for inp in file_inputs:
            try:
                # nodriver apply: js 函数接受 elem 作为参数
                accept = await inp.apply('(el) => el.accept || el.getAttribute("accept") || ""')
                if isinstance(accept, tuple):
                    accept = accept[0]
                accept = str(accept or "")
            except Exception:
                # fallback: 用 page.evaluate 读属性
                try:
                    accept_js = f"""(()=>{{const inputs=document.querySelectorAll('input[type="file"]');
                        for (const inp of inputs) {{
                            if (inp === arguments[0]) return inp.accept || '';
                        }}
                        return '';
                    }})()"""
                    # 简单兜底：用 query_selector_all 的索引重新读
                    accept = ""
                except Exception:
                    accept = ""
                continue
            # 聊天图片入口的 accept：纯图片格式（gif/jpeg/jpg/png），不含 pdf/docx/ppt
            accept_lower = accept.lower()
            if ("image/" in accept_lower
                and "application/pdf" not in accept_lower
                and "application/msword" not in accept_lower
                and "application/vnd" not in accept_lower):
                return inp
        # 兜底：用 page.evaluate 找到正确索引的 input，再 return 对应的 file_inputs[index]
        try:
            idx_js = """(() => {
                const inputs = document.querySelectorAll('input[type="file"]');
                for (let i = 0; i < inputs.length; i++) {
                    const acc = (inputs[i].accept || '').toLowerCase();
                    if (acc.includes('image/') && !acc.includes('application/pdf')
                        && !acc.includes('application/msword') && !acc.includes('application/vnd')) {
                        return i;
                    }
                }
                return -1;
            })()"""
            idx = await page.evaluate(idx_js)
            if isinstance(idx, tuple):
                idx = idx[0]
            idx = int(idx) if idx is not None else -1
            if 0 <= idx < len(file_inputs):
                self.log.debug(f"[nodriver] 通过 evaluate 找到聊天图片 input 索引={idx}")
                return file_inputs[idx]
        except Exception as e:
            self.log.debug(f"[nodriver] evaluate 找索引失败：{e}")
        self.log.debug("[nodriver] 未匹配到纯图片 accept 的 file input，兜底用第一个")
        return file_inputs[0] if file_inputs else None

    async def _count_chat_content_images(self, page: Any) -> int:
        """统计聊天消息区的"内容图片"数量（不含 UI 头像/icon）。

        内容图片 selector（2026-07-19 实测）：
        - `img[src*="bosszhipin.com/beijin"]`：Boss 上传的图片资源（简历/截图/头像）
        - `img[src*="chat/file"]`：聊天文件图片
        - `img[src*="imgaz.bosszhipin.com"]`：聊天图片 CDN
        - `img[class*="content-img"]` / `img.image-msg`：内容图片 class

        限定在消息容器内（`.chat-message / .message-item / [class*="message-content"]`），
        避免把 sidebar 头像算进来。
        """
        js = """
        (() => {
            const msgs = document.querySelectorAll(
                '.chat-message, .message-item, [class*="message-content"], .message-line'
            );
            let count = 0;
            for (const m of msgs) {
                count += m.querySelectorAll(
                    'img[src*="bosszhipin.com/beijin"], img[src*="chat/file"], '
                    + 'img[src*="imgaz.bosszhipin.com"], img[class*="content-img"], '
                    + 'img.image-msg, img[src*="chat/image"]'
                ).length;
            }
            return count;
        })()
        """
        try:
            r = await page.evaluate(js)
            if isinstance(r, str):
                r = json.loads(r) if r.strip().isdigit() else int(r)
            return int(r) if r else 0
        except Exception:
            return 0

    async def _confirm_image_by_count(self, page: Any, job_id: str,
                                       before_count: int, timeout: int = 60) -> bool:
        """基于内容图片数量增加来确认发送成功（v2 修复假阳性）。

        旧版 _confirm_image 只看"聊天区最后一张 img naturalWidth>0"，
        但聊天区天然有历史图片/UI 图标，永远会返回 True——即使新图根本没上传。

        新版判据：send_file 后 60s 内，内容图片数量必须**真正增加**才算成功。

        Args:
            page: nodriver Tab 对象。
            job_id: 岗位 ID。
            before_count: send_file 前的内容图片数量。
            timeout: 最长等待秒数（图片上传 + 渲染可能较慢，给 60s）。

        Returns:
            是否确认成功。
        """
        t0 = self._time()
        last_count = before_count
        while self._time() - t0 < timeout:
            cur_count = await self._count_chat_content_images(page)
            if cur_count > before_count:
                self.log.info(
                    f"[nodriver] 图片确认成功（内容图片 {before_count} → {cur_count}，"
                    f"+{cur_count - before_count}，job={job_id}）"
                )
                return True
            if cur_count != last_count:
                self.log.debug(f"[nodriver] 内容图片数变化 {last_count} → {cur_count}（job={job_id}，继续等）")
                last_count = cur_count
            await self._async_sleep(2)
        return False

    # ============================================================
    # async 确认（防「假成功」）
    # ============================================================
    async def _confirm_text(self, page: Any, text: str, job_id: str, timeout: int = 10) -> bool:
        """确认话术发送成功。

        判据（满足任一即成功）：
        1. 输入框已清空（Enter 发送成功的强信号）+ body 含话术或"送达"
        2. body 含话术 + 含"送达"/"已读"等发送状态词

        用 JSON.stringify 包装 evaluate 返回值，避免 nodriver 0.50.3 的
        RemoteObject 序列化问题（str(RemoteObject) ≠ innerText）。

        Args:
            page: nodriver Tab 对象。
            text: 待确认的话术（取前 30 字做子串匹配）。
            job_id: 岗位 ID。
            timeout: 最长等待秒数。

        Returns:
            是否确认成功。
        """
        needle = text[:30] if len(text) > 30 else text
        if not needle:
            return False
        needle_js = json.dumps(needle)

        t0 = self._time()
        while self._time() - t0 < timeout:
            try:
                # 一次 evaluate 取全部状态（JSON.stringify 避免 RemoteObject 问题）
                state = await page.evaluate(
                    "(()=>{const c=document.querySelector('.chat-input[contenteditable=\"true\"]');"
                    "const body=document.body?.innerText||'';"
                    f"return JSON.stringify({{input:(c?.innerText||'').trim(),body:body}});}})()"
                )
                s = str(state)
                import json as _json
                try:
                    d = _json.loads(s)
                except Exception:
                    d = {"input": "", "body": s}
                input_empty = len(d.get("input", "")) < 3
                body = d.get("body", "")
                has_needle = needle in body
                has_delivered = any(kw in body for kw in ("送达", "已读", "已发送", "发送成功"))

                if input_empty and (has_needle or has_delivered):
                    return True
                if has_needle and has_delivered:
                    return True
            except Exception:
                pass
            await self._async_sleep(1)
        return False

    async def _confirm_image(self, page: Any, job_id: str, timeout: int = 30) -> bool:
        """[deprecated v2] 旧版图片确认（已弃用，保留供历史测试参考）。

        旧判据：聊天消息区最后一张 img 的 naturalWidth > 0。
        问题：聊天区天然有历史图片/UI 图标，永远返回 True，导致假阳性。
        新代码用 ``_confirm_image_by_count``（基于图片数量增加）替代。
        """
        t0 = self._time()
        while self._time() - t0 < timeout:
            try:
                result = await page.evaluate(
                    "(()=>{const ms=document.querySelectorAll("
                    "'.message-item img, .chat-message img, .msg-image, "
                    ".message-image, img[class*=\"image\"], img[class*=\"msg\"], "
                    "img[src*=\"bosszhipin.com\"], img[src*=\"chat/file\"]');"
                    "return JSON.stringify({count:ms.length,"
                    "last_loaded:ms.length>0?(ms[ms.length-1].naturalWidth>0):false,"
                    "last_src:ms.length>0?(ms[ms.length-1].src||'').slice(0,60):''});})()"
                )
                s = str(result)
                try:
                    r = json.loads(s)
                except Exception:
                    r = {}
                if r.get("count", 0) > 0 and r.get("last_loaded"):
                    self.log.debug(
                        f"[nodriver] 图片确认成功（{r.get('count')} 张，"
                        f"last_src={r.get('last_src','')!r}）"
                    )
                    return True
            except Exception:
                pass
            await self._async_sleep(2)
        return False

    # ============================================================
    # 图片预处理（委托共享纯函数，复用 WebChatSender 逻辑）
    # ============================================================
    def _preprocess_image(self, path: str, *, tag: str = "resume") -> ImagePreprocessResult:
        """图片预处理：委托 :func:`preprocess_resume_image` 共享纯函数。

        保留实例方法签名以兼容现有调用。``self.image_max_kb`` /
        ``self.image_width_px`` / ``self.log`` 传给模块级函数。
        """
        return preprocess_resume_image(
            path,
            max_kb=self.image_max_kb,
            width_px=self.image_width_px,
            tag=tag,
            logger_obj=self.log,
        )

    # ============================================================
    # 内部工具
    # ============================================================
    def _get_nodriver(self) -> Any:
        """延迟 import nodriver（避免未装时 import 失败）。

        Returns:
            nodriver 模块。

        Raises:
            SenderError: nodriver 未安装。
        """
        if self._nodriver is not None:
            return self._nodriver
        try:
            import nodriver as uc
            self._nodriver = uc
            return uc
        except ImportError as e:
            raise SenderError(
                f"nodriver 未安装（pip install nodriver）：{e}"
            ) from e
