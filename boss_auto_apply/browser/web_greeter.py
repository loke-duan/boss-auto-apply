"""WebGreeter — 网页版打招呼（点击沟通，设计 §6）。

在岗位详情页或搜索结果页，点击「立即沟通」按钮 → 触发 Boss 默认招呼 + 建立沟通关系
→ 检测关系是否建立。

dry-run 安全：``click_and_greet(dry_run=True)`` 只 log「会点击 X 按钮」不真点。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..models import JobRow
from . import selectors as sel
from .manager import BrowserManager

__all__ = ["GreetResult", "WebGreeter"]


@dataclass
class GreetResult:
    """单次打招呼（点击沟通）结果（设计 §6.1）。"""
    job_id: str
    relation_established: bool = False      # 沟通关系是否建立
    already_friend: bool = False            # 本来就是好友
    chat_page_opened: bool = False          # 是否已跳转聊天页
    boss_default_greet_sent: bool = False   # Boss 是否自动发了默认招呼
    error: str | None = None
    risk_signal: str | None = None          # 风控信号（captcha/rate_limited/login_lost）
    dry_run: bool = False


class WebGreeter:
    """DrissionPage 网页版打招呼（点击立即沟通，设计 §6.2）。"""

    def __init__(
        self,
        browser: BrowserManager,
        logger_obj: Any = None,
        *,
        sleep_fn: Any = time.sleep,
        selectors_mod: Any = None,
    ) -> None:
        """初始化。

        Args:
            browser: BrowserManager 实例。
            logger_obj: loguru logger。
            sleep_fn: sleep 函数（测试注入）。
            selectors_mod: selectors 模块（测试注入）。
        """
        self.browser = browser
        self.log = logger_obj or logger
        self._sleep = sleep_fn
        self._sel = selectors_mod or sel

    # ============================================================
    # 主接口
    # ============================================================
    def click_and_greet(self, job: JobRow, *, dry_run: bool = False) -> GreetResult:
        """点击「立即沟通」→ 建立沟通关系（v3 重构，ADR-0003 + ADR-0005）。

        v3 修复（2026-07-19 实测验证）：
        旧版 v2 在 click 后轮询 7 种成功信号（跳转/弹窗/toast/浮层）10s，
        但 Boss 详情页点头次「立即沟通」**只会弹小浮层**（只输入文字），
        **不会自动跳转聊天页，也不会给明确成功信号**（用户多次反馈）。
        → 7 种信号全不触发 → click_timeout 误报（实测 9a83d3c8/31ad353ec 两次复现）。

        v3 设计：
        - 点完「立即沟通」后**只检测风控信号**（验证码/操作频繁/登录丢失）
        - 等 3 秒让 Boss 后端建立沟通关系（异步写库）
        - 不等任何成功 UI 信号，直接返回 relation_established=True
        - sender 主动 quit DrissionPage，让 nodriver 跳 `/web/geek/chat` 真发文字+图片
          （真正可靠的「关系已建立」信号是聊天页联系人列表能找到对方）

        「继续沟通」场景（已建立过关系）：跳过 click 直接返回 already_friend=True。

        Args:
            job: 目标岗位。
            dry_run: True 则不真点，返回 dry_run 标记的结果。

        Returns:
            :class:`GreetResult`。
        """
        self.browser.ensure_browser()
        self._navigate_to_job(job)
        if dry_run:
            self.log.info(f"[dry-run] WebGreeter 会点击「立即沟通」（job={job.job_id}），但跳过真点击")
            return GreetResult(job_id=job.job_id, dry_run=True, error="dry_run")
        btn = self._locate_greet_button()
        if btn is None:
            self.log.warning(f"未找到沟通按钮（job={job.job_id}）")
            return GreetResult(job_id=job.job_id, error="button_not_found")
        # 读按钮文本判断是「继续沟通」还是「立即沟通」（共用 class .btn-startchat）
        btn_text = self._read_btn_text(btn)
        self.log.info(f"按钮文本={btn_text!r}（job={job.job_id}）")
        if btn_text == "继续沟通":
            self.log.info(f"按钮文本=「继续沟通」（已建立沟通关系），跳过 click：{job.job_id}")
            return GreetResult(job_id=job.job_id, relation_established=True,
                               already_friend=True, chat_page_opened=False)
        # 「立即沟通」正常 click 路径
        self.log.info(f"按钮文本=「立即沟通」类（{btn_text!r}），走 click 路径（job={job.job_id}）")
        self._click_with_human_delay(btn)
        return self._detect_greet_outcome(job.job_id)

    # ============================================================
    # 内部
    # ============================================================
    def _navigate_to_job(self, job: JobRow) -> None:
        """导航到岗位详情页（设计 §6.2）。

        用 ``/job_detail/{job_id}.html`` 导航（与 WebSearcher.fetch_detail 同款 URL）。
        job_id 是搜索时从 ``/job_detail/xxx.html`` 提取的加密 ID（非 securityId）。
        Boss JS 加载详情页后会自动渲染「立即沟通」按钮。

        历史 bug（已修）：曾用 ``job-detail?securityId={job_id}`` —— 但 job_id 不是
        securityId（securityId 是详情页加载后由 Boss JS 动态生成的），导致导航到无效
        URL，找不到「立即沟通」按钮（button_not_found）。
        """
        tab = self.browser.get_tab()
        url = f"https://www.zhipin.com/job_detail/{job.job_id}.html"
        self.log.debug(f"WebGreeter 导航详情页：{url}")
        try:
            tab.get(url)
        except Exception as e:
            self.log.debug(f"导航详情页异常（忽略，可能已在页内）：{e}")
        self._sleep(random.uniform(1.0, 2.0))  # 拟人等待页面渲染

    def _locate_greet_button(self) -> Any:
        """定位「立即沟通」/「继续沟通」按钮（设计 §6.2 / §12）。

        主：详情页 .btn-startchat（2026-07-19 实测，首次/二次沟通都是这个 class）
        备：搜索页卡片 .job-card-wrapper .btn-greet

        Returns:
            按钮元素，或 None（找不到）。**调用方需读按钮文本**判断是「立即沟通」还是「继续沟通」。
        """
        tab = self.browser.get_tab()
        # 先详情页 greet 按钮
        btn = self._sel.find_element(tab, "detail.greet_button", timeout=5)
        if btn is not None:
            return btn
        # 备：搜索页卡片上的 greet（可能需要 hover）
        return self._sel.find_element(tab, "search.greet_button", timeout=2)

    def _read_btn_text(self, btn: Any) -> str:
        """读按钮文本，用于区分「立即沟通」vs「继续沟通」。

        2026-07-19 实测：两个按钮共用 class .btn-startchat，只能靠 textContent 区分。
        DrissionPage 的 .text 可能含空白，统一 strip + 取前 10 字。

        Args:
            btn: 按钮元素。

        Returns:
            按钮文本（已 strip），如 "立即沟通" / "继续沟通"；读取失败返回 ""。
        """
        try:
            text = (btn.text or "").strip()
        except Exception as e:
            self.log.debug(f"读按钮文本失败（忽略，按「立即沟通」走 click）：{e}")
            return ""
        # DrissionPage 偶尔返回多行文本，取第一行（按钮文案）
        if "\n" in text:
            text = text.split("\n", 1)[0].strip()
        return text[:10]

    def _click_with_human_delay(self, btn: Any) -> None:
        """拟人点击（反检测，设计 §6.2）。

        1. 鼠标移到按钮附近随机点（hover）— DrissionPage click 自带 hover
        2. 随机停顿 0.3-0.8s
        3. click()

        [假设] DrissionPage 的 click() 已模拟真实鼠标事件（hover+down+up）。
        """
        self._sleep(random.uniform(0.3, 0.8))
        try:
            btn.click()
        except Exception as e:
            self.log.debug(f"click 异常（重试一次）：{e}")
            try:
                btn.click()
            except Exception as e2:
                raise RuntimeError(f"点击「立即沟通」失败：{e2}") from e2
        self._sleep(random.uniform(0.5, 1.2))  # 点击后等响应

    def _detect_greet_outcome(self, job_id: str, timeout: int = 8) -> GreetResult:
        """检测点击「立即沟通」后的结果（v3.1 重构）。

        v3.1 调整（2026-07-19 实测 chat_page_not_rendered 后）：
        旧 v3 等 3s 太短——Boss 后端建立沟通关系 + 详情页按钮文案从「立即沟通」
        变成「继续沟通」需要 5-8s。如果只等 3s，nodriver 接管后详情页按钮还是
        「立即沟通」，nodriver 再点会触发 Boss 重复点击风控。
        v3.1 改为等 8s，让按钮稳定变为「继续沟通」，nodriver 点「继续沟通」让
        Boss 自动跳转到带参数的 /web/geek/chat?id=xxx&securityId=xxx（SPA 才能完整渲染）。

        v3 核心变化（保留）：
        - 删除 v2 的 7 种成功信号轮询（实测全不触发）
        - 只保留 3 种风控信号检测（captcha/rate_limited/login_lost）
        - 不依赖 Boss 详情页给「关系已建立」的明确信号

        Args:
            job_id: 岗位 ID。
            timeout: 等待 Boss 后端建立沟通关系 + 按钮变「继续沟通」的秒数（默认 8s）。

        Returns:
            风控命中 → GreetResult(risk_signal=...)；否则 relation_established=True。
        """
        tab = self.browser.get_tab()
        deadline = time.time() + timeout
        while time.time() < deadline:
            # E. 验证码（风控硬熔断，立即返回）
            if self._sel.find_element(tab, "risk.captcha", timeout=1):
                self.log.error(f"🚨 greet 后出现验证码（job={job_id}）→ 风控硬熔断")
                return GreetResult(job_id=job_id, risk_signal="captcha")
            # D. 操作频繁 toast（风控硬熔断）
            toast = self._sel.find_element(tab, "risk.rate_limited_toast", timeout=1)
            if toast:
                try:
                    txt = toast.text or ""
                except Exception:
                    txt = ""
                if any(kw in txt for kw in ("操作频繁", "请稍后再试", "账号被限制", "请求过于频繁")):
                    self.log.error(f"🚨 greet 后「操作频繁」（job={job_id}）→ 风控硬熔断")
                    return GreetResult(job_id=job_id, risk_signal="rate_limited")
            # 登录态丢失（URL 跳转登录页）
            cur_url = self._read_url_js(tab)
            if "/web/user" in cur_url or cur_url.endswith("/user"):
                self.log.error(f"🚨 greet 后跳转登录页（{cur_url}）→ 登录态丢失")
                return GreetResult(job_id=job_id, risk_signal="login_lost",
                                   error="login_lost")
            self._sleep(0.8)
        # 8s 内无风控信号 → 视为沟通关系已建立
        # Boss 后端会在 5-8s 内把详情页按钮文案改为「继续沟通」
        # nodriver 接管后点「继续沟通」让 Boss 自动跳转带参数 URL
        self.log.info(
            f"greet 点头次「立即沟通」完成，等 8s 后视为 relation_established=True"
            f"（job={job_id}，Boss 后端应已建立关系 + 按钮变「继续沟通」，"
            f"由 nodriver 点「继续沟通」让 Boss 自动跳转带参数 URL）"
        )
        return GreetResult(job_id=job_id, relation_established=True,
                           chat_page_opened=False, boss_default_greet_sent=True)

    def _read_url_js(self, tab: Any) -> str:
        """用 JS 读 ``location.href``（绕过 DrissionPage ``tab.url`` 同步慢）。

        v2 修复（2026-07-19）：DrissionPage 的 ``tab.url`` 在 Vue Router push 后
        同步慢几秒（实测 10s 内仍读不到新 URL），导致 click_timeout 误报。
        改用 JS ``location.href`` 直接读浏览器当前 URL，与 Vue Router 同步。
        失败兜底用 ``tab.url``。
        """
        try:
            url = tab.run_js("location.href") or ""
            return str(url)
        except Exception as e:
            self.log.debug(f"run_js location.href 失败，兜底用 tab.url：{e}")
            try:
                return str(tab.url or "")
            except Exception:
                return ""

    def _read_body_text(self, tab: Any) -> str:
        """用 JS 读 ``document.body.innerText``（DrissionPage body.text 偶尔返回空）。"""
        try:
            text = tab.run_js("document.body.innerText") or ""
            return str(text)
        except Exception:
            try:
                body = tab.ele("css:body", timeout=1)
                return (body.text or "") if body else ""
            except Exception:
                return ""
