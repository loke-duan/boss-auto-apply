"""BrowserManager — DrissionPage 浏览器生命周期管理（设计 §4）。

职责：DrissionPage 浏览器的启动/复用/关闭、专用 profile 管理、
stealth 配置、登录态检测、日常 Chrome 冲突处理。**单例**（一个进程内只有一个浏览器）。

关键接口：
- :meth:`BrowserManager.get_instance` — 单例工厂
- :meth:`BrowserManager.ensure_browser` — 确保浏览器启动，返回最新 tab
- :meth:`BrowserManager.get_tab` / :meth:`new_tab` — 取/建标签页
- :meth:`check_login` / :meth:`wait_for_login` — 登录态检测/等待扫码
- :meth:`quit` / :meth:`maybe_quit_idle` — 关闭/空闲自动退出

dry-run 安全：BrowserManager 本身不产生写副作用（只是浏览器生命周期）；
真写副作用在 WebGreeter/WebChatSender，由它们的 dry_run 控制。
"""

from __future__ import annotations

import os
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from ..errors import (
    BrowserConflictError,
    BrowserLaunchError,
    LoginRequiredError,
    LoginTimeoutError,
)

__all__ = [
    "BrowserConfig",
    "LoginStatus",
    "BrowserManager",
    "DEFAULT_PROFILE_DIR",
    "REAL_UA_POOL",
]

# 专用 profile 默认路径（PRD §4.7.4.1）
DEFAULT_PROFILE_DIR = "~/.boss-auto-apply/chrome-profile"

# Boss 关键 URL
BOSS_LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
BOSS_RECOMMEND_URL = "https://www.zhipin.com/web/geek/job-recommend"
BOSS_SEARCH_URL = "https://www.zhipin.com/web/geek/job"
BOSS_CHAT_URL = "https://www.zhipin.com/web/geek/chat"

# 登录态 cookie 名（[假设] 设计 §15 T2 待实测：wt2/wbg/__zp_stoken__）
LOGIN_COOKIE_NAMES = ("wt2", "wbg", "__zp_stoken__")
# 登录态主 cookie（wt2 + __zp_stoken__ 同时存在视为登录）
LOGIN_REQUIRED_COOKIES = ("wt2", "__zp_stoken__")


@dataclass
class BrowserConfig:
    """浏览器配置（从 config.sender 映射，设计 §4.1）。"""
    user_data_dir: str = DEFAULT_PROFILE_DIR      # 专用 profile
    headless: bool = False                        # 反检测要求有头
    stealth: bool = True                          # 注入 stealth.js
    check_chrome_running: bool = True             # 启动前检测日常 Chrome 冲突
    auto_quit_idle_sec: int = 600                 # 空闲 10min 自动关闭
    browser_path: str | None = None               # Chrome 路径（None 自动探测）
    load_mode: Literal["eager", "normal", "none"] = "eager"  # 页面加载策略
    local_port: int = 9559                        # 🔴 专用端口（避开 DrissionPage 默认 9222，防复用日常 Chrome）
    # 日常 Chrome profile 路径黑名单（误指向这些会阻断，防污染日常 cookie）
    daily_profile_markers: tuple[str, ...] = (
        "Library/Application Support/Google/Chrome",
        "AppData/Local/Google/Chrome/User Data",
    )


@dataclass
class LoginStatus:
    """登录态检测结果（设计 §4.1）。"""
    logged_in: bool
    has_stoken: bool                  # __zp_stoken__ cookie 是否存在
    has_wt2: bool = False             # wt2 cookie（登录态核心 token）
    has_zp_at: bool = False           # zp_at cookie（登录态核心 token）
    username: str | None = None       # 登录用户名（从 DOM 抓）
    detected_at: str = ""             # ISO 时间
    reason: str = ""                  # 未登录原因


# 真实 UA 池（设计 §8.5）。[假设] 需定期随 Chrome 版本迭代更新。
REAL_UA_POOL: list[str] = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# 窗口尺寸池（防固定窗口指纹）
_WINDOW_SIZE_POOL: list[tuple[int, int]] = [
    (1536, 864), (1920, 1080), (1440, 900), (1680, 1050),
]


def _random_real_ua() -> str:
    """随机选一个真实 UA（每次启动浏览器时）。"""
    return random.choice(REAL_UA_POOL)


def _random_window_size() -> tuple[int, int]:
    """随机选窗口尺寸（防固定窗口指纹）。"""
    return random.choice(_WINDOW_SIZE_POOL)


def _read_stealth_js() -> str:
    """读取 stealth.js 内容（与 manager.py 同目录）。"""
    stealth_path = Path(__file__).parent / "stealth.js"
    if not stealth_path.exists():
        # stealth.js 缺失不致命（基线反检测已够），降级空脚本
        return ""
    return stealth_path.read_text(encoding="utf-8")


class BrowserManager:
    """DrissionPage 浏览器单例管理器（设计 §4.2）。

    单例：一个进程内 :meth:`ensure_browser` 返回同一实例，避免多 Chrome 进程。
    幂等：已启动则复用；quit 后再次 ensure 会重启。

    测试注入：构造时传 ``chromium_cls`` / ``options_cls`` 可替换 DrissionPage 真类，
    避免单测真启动 Chrome。
    """

    _instance: "BrowserManager | None" = None  # 单例

    def __init__(
        self,
        cfg: BrowserConfig,
        logger_obj: Any = None,
        *,
        chromium_cls: Any = None,
        options_cls: Any = None,
    ) -> None:
        """初始化管理器（不启动浏览器）。

        Args:
            cfg: 浏览器配置。
            logger_obj: loguru logger。
            chromium_cls: ``DrissionPage.Chromium``（测试可注入 mock）。
            options_cls: ``DrissionPage.ChromiumOptions``（测试可注入 mock）。
        """
        self.cfg = cfg
        self.log = logger_obj or logger
        # 延迟导入 DrissionPage（preflight 已检查装了；测试注入可绕过）
        self._chromium_cls = chromium_cls
        self._options_cls = options_cls
        self._browser: Any = None     # Chromium 实例
        self._tab: Any = None         # 当前主 tab
        self._last_active_ts: float = 0.0
        self._stealth_injected_tabs: set[int] = set()  # 已注入 stealth 的 tab id

    # ============================================================
    # 单例
    # ============================================================
    @classmethod
    def get_instance(
        cls,
        cfg: BrowserConfig,
        logger_obj: Any = None,
        **kwargs: Any,
    ) -> "BrowserManager":
        """单例工厂。首次调用建实例；后续调用返回同一实例。

        注意：cfg 仅在首次生效；后续调用忽略新 cfg（保持已建实例）。
        """
        if cls._instance is None:
            cls._instance = cls(cfg, logger_obj, **kwargs)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（测试用：每个测试隔离单例）。"""
        cls._instance = None

    # ============================================================
    # 启动 / 复用
    # ============================================================
    def ensure_browser(self, *, force_new: bool = False) -> Any:
        """确保浏览器已启动并返回最新 tab。

        - 已启动且未退出 → 直接返回 latest_tab
        - 未启动 / 已退出 / force_new=True → 启动新实例

        启动流程（设计 §4.2）：
          1. ``_check_chrome_conflict``（日常 Chrome 冲突检测）
          2. ``_apply_stealth_options``（ChromiumOptions + stealth flags）
          3. ``Chromium(co)`` 启动
          4. ``_inject_stealth_js``（document_start 注入 stealth.js）
          5. 返回 latest_tab

        Args:
            force_new: True 则强制重启（即使已活着）。

        Returns:
            DrissionPage 的 ChromiumTab。

        Raises:
            BrowserConflictError: profile 冲突。
            BrowserLaunchError: 启动失败。
        """
        # 已启动且存活且不强制重建 → 复用
        if not force_new and self._browser is not None and self.is_alive():
            self._touch()
            return self.get_tab()
        # 需要启动
        if force_new and self._browser is not None:
            self.quit()

        self._check_chrome_conflict()
        co = self._make_options()
        try:
            self.log.info("启动 DrissionPage Chrome（专用 profile）...")
            browser = self._build_browser(co)
        except Exception as e:
            raise BrowserLaunchError(f"Chrome 启动失败：{e}") from e
        self._browser = browser
        try:
            self._tab = browser.latest_tab
        except Exception as e:
            raise BrowserLaunchError(f"获取 latest_tab 失败：{e}") from e
        # stealth 注入
        if self.cfg.stealth:
            self._inject_stealth_js(self._tab)
        self._touch()
        self.log.info("浏览器启动成功 ✅")
        return self._tab

    def _make_options(self) -> Any:
        """构造 ChromiumOptions 并应用 stealth 启动参数（设计 §4.6）。"""
        co = self._new_options()
        self._apply_stealth_options(co)
        return co

    def _new_options(self) -> Any:
        """新建 ChromiumOptions（延迟导入 DrissionPage）。"""
        if self._options_cls is not None:
            return self._options_cls()
        from DrissionPage import ChromiumOptions
        return ChromiumOptions()

    def _build_browser(self, co: Any) -> Any:
        """用 Chromium(co) 启动浏览器（延迟导入）。"""
        if self._chromium_cls is not None:
            return self._chromium_cls(co)
        from DrissionPage import Chromium
        return Chromium(co)

    def _apply_stealth_options(self, co: Any) -> None:
        """应用 stealth 启动参数（设计 §4.6）。

        Args:
            co: ChromiumOptions 实例。
        """
        co.set_user_data_path(os.path.expanduser(self.cfg.user_data_dir))
        # headless（[假设] DrissionPage 4.1 headless() 接受 bool）
        try:
            co.headless(self.cfg.headless)
        except Exception:
            pass  # 某些版本 API 不同，降级
        # 🔴 关键：强制独立端口（避开 DrissionPage 默认 ini 的 9222，防止复用日常 Chrome）
        # 实测：auto_port() 在日常 Chrome 运行时不稳定（会连到已运行实例，profile 不隔离）。
        # 用固定端口 9559（避开 9222 默认）+ set_address 双保险，确保启动独立实例。
        # [假设] 9559 端口未被占用；若占用则后续加端口探测。
        dedicated_port = self.cfg.local_port
        try:
            co.set_local_port(dedicated_port)
            co.set_address(f"127.0.0.1:{dedicated_port}")
        except Exception:
            pass
        # 禁用自动化标志（关键：去 webdriver）
        for arg in (
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
        ):
            try:
                co.set_argument(arg)
            except Exception:
                pass
        # Chrome 路径（自动探测时不设）
        if self.cfg.browser_path:
            try:
                co.set_browser_path(self.cfg.browser_path)
            except Exception:
                pass
        # UA 随机化
        ua = _random_real_ua()
        try:
            co.set_user_agent(ua)
        except Exception:
            pass
        self.log.debug(f"stealth UA: {ua}")
        # 窗口大小随机化
        w, h = _random_window_size()
        try:
            co.set_argument(f"--window-size={w},{h}")
        except Exception:
            pass
        # 语言
        try:
            co.set_argument("--lang=zh-CN")
        except Exception:
            pass
        # [假设] load_mode eager 减少等待
        if self.cfg.load_mode:
            try:
                co.set_load_mode(self.cfg.load_mode)
            except Exception:
                pass

    def _inject_stealth_js(self, tab: Any) -> None:
        """document_start 时机注入 stealth.js（设计 §8.3）。

        Args:
            tab: DrissionPage 的 ChromiumTab。
        """
        stealth_js = _read_stealth_js()
        if not stealth_js:
            self.log.warning("stealth.js 内容为空，跳过注入（基线反检测已够）")
            return
        # document_start 注入（影响后续导航）
        try:
            tab.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=stealth_js)
        except Exception as e:
            # [假设] 备选：tab.set.start_script()；再不行每次 get 后 run_js
            self.log.debug(f"run_cdp addScriptToEvaluateOnNewDocument 失败，尝试 set.start_script：{e}")
            try:
                tab.set.start_script(stealth_js)
            except Exception:
                self.log.warning(f"stealth.js 注入失败（{e}），降级每次 get 后 run_js")
        # 当前 tab 立即生效
        try:
            tab.run_js(stealth_js)
        except Exception:
            pass

    # ============================================================
    # tab 操作
    # ============================================================
    def get_tab(self) -> Any:
        """获取当前 tab（已启动前提）。

        Returns:
            DrissionPage 的 ChromiumTab。

        Raises:
            BrowserLaunchError: 浏览器未启动。
        """
        if self._browser is None or not self.is_alive():
            raise BrowserLaunchError("浏览器未启动，先调 ensure_browser()")
        try:
            self._tab = self._browser.latest_tab
        except Exception:
            pass  # 用缓存的 _tab
        self._touch()
        return self._tab

    def new_tab(self, url: str | None = None) -> Any:
        """新建 tab（可选导航到 url）。每次新建后注入 stealth。"""
        if self._browser is None or not self.is_alive():
            self.ensure_browser()
        tab = self._browser.new_tab(url) if url else self._browser.new_tab()
        if self.cfg.stealth:
            self._inject_stealth_js(tab)
        self._tab = tab
        self._touch()
        return tab

    def quit(self) -> None:
        """关闭浏览器（优雅退出）。"""
        if self._browser is None:
            return
        try:
            self._browser.quit()
        except Exception as e:
            self.log.debug(f"quit 时异常（忽略）：{e}")
        finally:
            self._browser = None
            self._tab = None
            self._stealth_injected_tabs.clear()
            self._last_active_ts = 0.0
        self.log.info("浏览器已关闭")

    def is_alive(self) -> bool:
        """浏览器进程是否存活。"""
        if self._browser is None:
            return False
        try:
            # DrissionPage 4.1：browser.process 存在且 pid 有效
            proc = getattr(self._browser, "process", None)
            if proc is not None:
                # proc.poll() None=还在跑
                return proc.poll() is None if hasattr(proc, "poll") else True
            # 兜底：latest_tab 能取到说明活着
            _ = self._browser.latest_tab
            return True
        except Exception:
            return False

    # ============================================================
    # 空闲自动退出
    # ============================================================
    def _touch(self) -> None:
        """更新最后活跃时间戳。"""
        self._last_active_ts = time.time()

    def maybe_quit_idle(self) -> bool:
        """若空闲超 auto_quit_idle_sec → quit() 返回 True。

        Sender 每处理完一个 job 后调用（释放资源）。

        Returns:
            是否触发了 quit。
        """
        if self._browser is None or self._last_active_ts == 0.0:
            return False
        idle = time.time() - self._last_active_ts
        if idle > self.cfg.auto_quit_idle_sec:
            self.log.info(f"浏览器空闲 {idle:.0f}s > {self.cfg.auto_quit_idle_sec}s，自动关闭")
            self.quit()
            return True
        return False

    # ============================================================
    # 登录态检测（设计 §4.4）
    # ============================================================
    def check_login(self, *, navigate: bool = True) -> LoginStatus:
        """检测 Boss 登录态。

        策略 A（DOM 检测，主）：导航到 job-recommend，查 ``.user-info`` 存在 → 已登录。
        策略 B（cookie 检测，兜底）：wt2 + __zp_stoken__ 同时存在 → 已登录。

        Args:
            navigate: True 则先导航到 job-recommend（需要登录的页面）。

        Returns:
            :class:`LoginStatus`。
        """
        from datetime import datetime, timezone
        tab = self.ensure_browser()
        if navigate:
            try:
                tab.get(BOSS_RECOMMEND_URL)
            except Exception as e:
                self.log.debug(f"导航 job-recommend 异常：{e}")
        # 策略 B：cookie 检测（先做，不依赖 DOM 渲染）
        has_stoken = False
        has_wt2 = False
        has_zp_at = False
        cookies: list[dict[str, Any]] = []
        try:
            cookies = list(tab.cookies()) if hasattr(tab, "cookies") else []
        except Exception:
            cookies = []
        cookie_names = {c.get("name") for c in cookies if isinstance(c, dict)}
        has_stoken = "__zp_stoken__" in cookie_names
        has_wt2 = "wt2" in cookie_names
        has_zp_at = "zp_at" in cookie_names

        # 🔴 关键：URL 重定向检测（最可靠的反向判断）
        # Boss 未登录访问 /web/geek/ 会被重定向到 /web/user/（登录页）
        current_url = ""
        try:
            current_url = tab.url or ""
        except Exception:
            pass
        redirected_to_login = "/user" in current_url or "login" in current_url.lower()

        # 策略 B（主判断）：cookie wt2 + zp_at 同时存在（Boss 登录态核心 token）
        cookie_logged = has_wt2 and has_zp_at

        # 策略 A（辅助）：DOM 检测，但仅在 cookie 通过时才采信 username
        dom_logged = False
        username: str | None = None
        if cookie_logged and not redirected_to_login:
            try:
                user_info = tab.ele("css:.user-info", timeout=3)
                if user_info:
                    dom_logged = True
                    try:
                        name_el = tab.ele("css:.user-info .name", timeout=1)
                        if name_el:
                            username = name_el.text
                    except Exception:
                        pass
            except Exception:
                pass

        # 最终判断：cookie 主 + URL 未重定向；DOM 不单独决定（避免广告元素误判）
        logged_in = cookie_logged and not redirected_to_login
        if redirected_to_login:
            reason = f"被重定向到登录页（{current_url}），未登录"
        elif not cookie_logged:
            missing = [k for k, v in [("wt2", has_wt2), ("zp_at", has_zp_at)] if not v]
            reason = f"缺少登录态 cookie：{missing}"
        else:
            reason = ""
        self._touch()
        return LoginStatus(
            logged_in=logged_in,
            has_stoken=has_stoken,
            has_wt2=has_wt2,
            has_zp_at=has_zp_at,
            username=username,
            detected_at=datetime.now(timezone.utc).isoformat(),
            reason=reason,
        )

    def ensure_logged_in(self) -> LoginStatus:
        """确保已登录，未登录抛 :class:`LoginRequiredError`。

        run/dry-run 真发送前调用。

        Returns:
            :class:`LoginStatus`（logged_in=True）。

        Raises:
            LoginRequiredError: 未登录。
        """
        status = self.check_login(navigate=True)
        if not status.logged_in:
            raise LoginRequiredError()
        return status

    def wait_for_login(self, timeout_sec: int = 300) -> LoginStatus:
        """阻塞等待用户扫码登录（首次 login 命令用）。

        流程：
          1. 导航到 Boss 登录页（只导一次，之后不再跳转）
          2. 等待 ``initial_wait_sec``（默认 15s）给用户扫码
          3. 之后每 5s 用 ``check_login(navigate=False)`` 轮询（只查 cookie，不导航）
          4. 检测到 wt2+zp_at cookie → 登录成功

        Args:
            timeout_sec: 最大等待秒数。

        Returns:
            :class:`LoginStatus`（logged_in=True）。

        Raises:
            LoginTimeoutError: 扫码超时。
        """
        initial_wait_sec = 15  # 给用户至少 15s 扫码，期间不轮询不跳转
        self.log.info(f"请在浏览器中扫码登录 Boss 直聘（{initial_wait_sec}s 后开始检测，最多等 {timeout_sec}s）...")
        # 首次导航到登录页（唯一一次导航，之后不跳转，避免打断扫码）
        tab = self.ensure_browser()
        try:
            tab.get(BOSS_LOGIN_URL)
        except Exception as e:
            self.log.debug(f"导航登录页异常：{e}")
        # 首次等待：给用户足够时间扫码（不轮询、不导航）
        self.log.info(f"⏳ 等待 {initial_wait_sec}s 供扫码...")
        time.sleep(initial_wait_sec)
        # 后续轮询：只查 cookie，不导航（navigate=False），避免跳转打断扫码
        deadline = time.time() + timeout_sec
        poll = 5
        while time.time() < deadline:
            status = self.check_login(navigate=False)
            if status.logged_in:
                self.log.info(f"✅ 登录成功（用户={status.username or '未知'}），profile 已保存")
                return status
            time.sleep(poll)
        raise LoginTimeoutError(f"扫码登录超时（{timeout_sec}s），请重跑 login")

    # ============================================================
    # 冲突检测（设计 §4.3）
    # ============================================================
    def _check_chrome_conflict(self) -> None:
        """检测日常 Chrome 是否占用同一 profile，或误用日常 profile。

        1. config.user_data_dir 指向日常 profile（黑名单标记）→ 阻断。
        2. check_chrome_running=True → ps 检测 Chrome 进程 → 警告（不阻断）。

        Raises:
            BrowserConflictError: 误用日常 profile。
        """
        profile = os.path.expanduser(self.cfg.user_data_dir)
        # 1. 黑名单：误用日常 profile
        for marker in self.cfg.daily_profile_markers:
            if marker in profile:
                raise BrowserConflictError(
                    f"user_data_dir 指向了日常 Chrome profile（{profile}），"
                    f"会污染日常 cookie。请改用专用 profile：{DEFAULT_PROFILE_DIR}"
                )
        # 2. 日常 Chrome 运行中（警告不阻断，因为专用 profile 独立）
        if self.cfg.check_chrome_running:
            running = self._chrome_running()
            if running:
                self.log.warning(
                    f"检测到日常 Google Chrome 进程在运行（{running} 个）。"
                    "专用 profile 独立，一般不冲突；若启动报「profile 已占用」，请关闭日常 Chrome。"
                )

    @staticmethod
    def _chrome_running() -> int:
        """检测日常 Chrome 进程数（macOS ps aux）。返回进程数。

        [假设] macOS 上 ps aux 能可靠列出 Chrome 进程。
        """
        try:
            proc = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5,
            )
            out = proc.stdout or ""
            # 排除本进程（DrissionPage 启动的 Chrome 会带 user-data-dir 参数）
            # 这里粗略统计含 "Google Chrome" 的行数
            lines = [ln for ln in out.splitlines()
                     if "Google Chrome" in ln and "grep" not in ln]
            return len(lines)
        except Exception:
            return 0
