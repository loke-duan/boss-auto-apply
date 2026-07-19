"""启动期前置检查 P1–P9（设计 §2.4，改造1：zcode→claude）。

非致命项（如字体缺失）只 warning；致命项（node/claude/typst/config 缺失）抛对应异常。
登录探测由 ``llm.check_login`` 完成，preflight 在 dry-run 模式下可选跳过（dry-run 不真调 LLM）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .errors import (
    ClaudeLoginRequiredError,
    ConfigValidationError,
    FontMissingError,
    PreflightError,
    TypstNotInstalledError,
)

__all__ = [
    "CheckResult",
    "PreflightReport",
    "run_preflight",
    "run_m3_preflight",
    "check_node",
    "check_claude_bin",
    "check_claude_login",
    "check_typst",
    "check_font",
    "check_template",
    "check_resume_input",
    "check_config",
    "check_data_dir",
    "check_drissionpage",
    "check_chrome_app",
    "check_profile_dir",
    "check_pillow",
]

# 默认 claude 登录指引
DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_LOGIN_HINT = "claude auth login"


@dataclass
class CheckResult:
    """单项检查结果。"""

    code: str           # P1..P9
    name: str
    passed: bool
    fatal: bool = True  # True=失败抛异常；False=只 warning
    message: str = ""
    detail: str = ""


@dataclass
class PreflightReport:
    """预检报告。"""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results if r.fatal)

    @property
    def fatal_failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and r.fatal]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and not r.fatal]


# ============================================================
# 各项检查（P1–P9）
# ============================================================
def check_node() -> CheckResult:
    """P1：node 在 PATH（claude CLI 不依赖 node，仅作环境提示，非致命）。

    改造1：claude CLI 不需要 node，node 检查降级为非致命 warning。
    """
    path = shutil.which("node")
    if not path:
        return CheckResult("P1", "node（可选）", False, fatal=False,
                           message="node 不在 PATH", detail="claude CLI 不依赖 node，可忽略")
    try:
        import subprocess
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        ver = (proc.stdout or "").strip()
        major = int(ver.lstrip("v").split(".")[0]) if ver else 0
        return CheckResult("P1", "node（可选）", True, message=f"{ver} @ {path}")
    except Exception as e:
        return CheckResult("P1", "node（可选）", False, fatal=False, message=f"node 探测失败：{e}")


def check_claude_bin(claude_bin: str = DEFAULT_CLAUDE_BIN) -> CheckResult:
    """P2：claude CLI 在 PATH。"""
    path = shutil.which(claude_bin)
    if not path:
        return CheckResult(
            "P2", "claude CLI", False, fatal=True,
            message=f"claude 命令未找到（{claude_bin}）",
            detail="请装 Claude CLI：npm install -g @anthropic-ai/claude-code",
        )
    try:
        import subprocess
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        ver = (proc.stdout or "").strip() or "ok"
        return CheckResult("P2", "claude CLI", True, message=f"{ver} @ {path}")
    except Exception as e:
        return CheckResult("P2", "claude CLI", False, fatal=True, message=f"claude 探测失败：{e}")


def check_claude_login(claude_bin: str = DEFAULT_CLAUDE_BIN, llm: Any = None,
                       login_hint: str = DEFAULT_LOGIN_HINT, *,
                       skip_in_dry_run: bool = True, is_dry_run: bool = True) -> CheckResult:
    """P3：claude 已登录（跑 ``claude auth status``，解析 JSON）。

    dry-run 模式默认 skip（不真调），但标 warning 让用户知道真跑前要登录。
    """
    if skip_in_dry_run and is_dry_run:
        return CheckResult(
            "P3", "claude 登录", False, fatal=False,
            message="dry-run 模式跳过登录探测",
            detail=f"真跑前请登录：{login_hint}",
        )
    # 优先用 llm 实例（含登录缓存）
    if llm is not None:
        try:
            llm.check_login()
            return CheckResult("P3", "claude 登录", True, message="claude auth status loggedIn=true")
        except ClaudeLoginRequiredError:
            return CheckResult("P3", "claude 登录", False, fatal=True,
                               message="未登录（claude auth status loggedIn != true）",
                               detail=login_hint)
    # 否则直接跑 claude auth status
    path = shutil.which(claude_bin)
    if not path:
        return CheckResult("P3", "claude 登录", False, fatal=False,
                           message="claude 命令未找到，跳过登录探测", detail=login_hint)
    try:
        import json as _json
        import subprocess
        proc = subprocess.run([path, "auth", "status"], capture_output=True, text=True, timeout=15)
        data = _json.loads((proc.stdout or "").strip()) if (proc.stdout or "").strip() else {}
        if data.get("loggedIn") is True:
            return CheckResult("P3", "claude 登录", True, message="claude auth status loggedIn=true")
        return CheckResult("P3", "claude 登录", False, fatal=True,
                           message="未登录（claude auth status loggedIn != true）",
                           detail=login_hint)
    except Exception as e:
        return CheckResult("P3", "claude 登录", False, fatal=False,
                           message=f"claude auth status 探测失败：{e}", detail=login_hint)


def check_typst(typst_bin: str = "typst") -> CheckResult:
    """P4：typst 可执行。"""
    path = shutil.which(typst_bin)
    if not path:
        return CheckResult("P4", "typst", False, fatal=True,
                           message=f"typst 未找到（{typst_bin}）", detail="brew install typst")
    try:
        import subprocess
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return CheckResult("P4", "typst", True, message=(proc.stdout or "").strip())
    except Exception:
        pass
    return CheckResult("P4", "typst", False, fatal=True, message="typst --version 失败")


def check_font(typst_bin: str = "typst", font_name: str = "Noto Sans CJK SC") -> CheckResult:
    """P5：思源黑体可被 typst 找到（非致命 warning）。"""
    path = shutil.which(typst_bin)
    if not path:
        return CheckResult("P5", f"字体 {font_name}", False, fatal=False,
                           message="typst 未装，跳过字体探测")
    try:
        import subprocess
        proc = subprocess.run([path, "fonts"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and font_name.lower() in (proc.stdout or "").lower():
            return CheckResult("P5", f"字体 {font_name}", True, message="找到")
    except Exception:
        pass
    return CheckResult("P5", f"字体 {font_name}", False, fatal=False,
                       message=f"未找到 {font_name}，typst 将用 fallback",
                       detail="brew install --cask font-noto-sans-cjk-sc")


def check_template(template_dir: str = "templates/brilliant-cv") -> CheckResult:
    """P6：brilliant-cv 模板目录存在。"""
    exists = os.path.isdir(template_dir)
    return CheckResult(
        "P6", "brilliant-cv 模板", exists, fatal=False,
        message=f"{template_dir} 存在" if exists else f"模板不存在：{template_dir}",
        detail="" if exists else "git clone https://github.com/yunanwg/brilliant-CV.git templates/brilliant-cv",
    )


def check_resume_input(resume_path: str = "input/resume.pdf") -> CheckResult:
    """P7：简历文件存在且格式受支持（支持 pdf/docx/md/txt）。"""
    exists = os.path.exists(resume_path)
    if not exists:
        return CheckResult(
            "P7", "简历文件", False, fatal=False,
            message=f"简历不存在：{resume_path}",
            detail="把简历放到 input/ 下（支持 .pdf/.docx/.md/.txt）",
        )
    # 检查扩展名是否受支持
    ext = os.path.splitext(resume_path)[1].lower()
    supported = (".pdf", ".docx", ".md", ".txt")
    if ext not in supported:
        return CheckResult(
            "P7", "简历格式", False, fatal=False,
            message=f"不支持的格式：{ext}",
            detail=f"支持 {'/'.join(supported)}，请在 config.paths.resume_input 指定正确路径",
        )
    return CheckResult(
        "P7", "简历文件", True, fatal=False,
        message=f"{resume_path}（{ext}）",
        detail="",
    )


def check_config(config_path: str = "config/config.yaml") -> CheckResult:
    """P8：config.yaml 通过 JSON Schema（弱探测：只看文件存在，强校验由 load_config 做）。"""
    exists = os.path.exists(config_path)
    return CheckResult(
        "P8", "config.yaml", exists, fatal=True,
        message=config_path if exists else f"config 不存在：{config_path}",
        detail="" if exists else "cp config/config.example.yaml config/config.yaml",
    )


def check_data_dir(data_dir: str = "data") -> CheckResult:
    """P9：data/ 目录可写。"""
    try:
        os.makedirs(data_dir, exist_ok=True)
        # 测试可写
        test_file = os.path.join(data_dir, ".write_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return CheckResult("P9", "data/ 可写", True, message=data_dir)
    except OSError as e:
        return CheckResult("P9", "data/ 可写", False, fatal=True, message=f"不可写：{e}")


# ============================================================
# M3 真发送前置检查 P10–P14（设计 §16.6）
# ============================================================
def check_drissionpage() -> CheckResult:
    """P10：DrissionPage 已装（M3 真发送主驱动）。"""
    try:
        import DrissionPage  # noqa: F401
        try:
            ver = getattr(DrissionPage, "__version__", "ok")
        except Exception:
            ver = "ok"
        return CheckResult("P10", "DrissionPage", True, fatal=True, message=f"{ver} 已装")
    except ImportError:
        return CheckResult(
            "P10", "DrissionPage", False, fatal=True,
            message="DrissionPage 未装",
            detail="pip install DrissionPage==4.1.1.4（或跑 setup_m3.sh）",
        )


def check_chrome_app(chrome_path: str = "/Applications/Google Chrome.app") -> CheckResult:
    """P11：Google Chrome 已装（macOS 默认路径）。"""
    exists = os.path.exists(chrome_path)
    return CheckResult(
        "P11", "Google Chrome", exists, fatal=True,
        message=chrome_path if exists else f"Chrome 未找到：{chrome_path}",
        detail="" if exists else "请安装 Google Chrome",
    )


def check_profile_dir(user_data_dir: str = "~/.boss-auto-apply/chrome-profile") -> CheckResult:
    """P12：专用 profile 目录存在或可创建（首次 login 前可能未建）。"""
    profile = os.path.expanduser(user_data_dir)
    if os.path.exists(profile):
        return CheckResult("P12", "专用 Chrome profile", True, fatal=False,
                           message=f"{profile} 已建（登录态可能已存）")
    # 尝试创建
    try:
        os.makedirs(profile, exist_ok=True)
        return CheckResult(
            "P12", "专用 Chrome profile", False, fatal=False,
            message=f"{profile} 首次创建（尚未登录）",
            detail="首次 run 前请跑：python -m boss_auto_apply login",
        )
    except OSError as e:
        return CheckResult("P12", "专用 Chrome profile", False, fatal=True,
                           message=f"无法创建 profile 目录：{e}")


def check_pillow() -> CheckResult:
    """P13：Pillow 可用（M3 图片预处理：压缩到 <1MB）。"""
    try:
        import PIL  # noqa: F401
        from PIL import Image  # noqa: F401
        ver = getattr(PIL, "__version__", "ok")
        return CheckResult("P13", "Pillow", True, fatal=True, message=f"{ver} 已装")
    except ImportError:
        return CheckResult(
            "P13", "Pillow", False, fatal=True,
            message="Pillow 未装（图片预处理需要）",
            detail="pip install Pillow>=10.0",
        )


def check_boss_login(browser_manager: Any = None, *, skip: bool = True) -> CheckResult:
    """P14：Boss 登录态有效（专用 profile cookie wt2 存在）。

    ⚠️ 此检查会启动浏览器，开销大；默认 skip=True（仅提示）。
    真跑前由 BrowserManager.ensure_logged_in 兜底，故 preflight 阶段只 warning。

    Args:
        browser_manager: BrowserManager 实例（None 则跳过）。
        skip: True 则不真检测，只提示（默认）。
    """
    if skip or browser_manager is None:
        return CheckResult(
            "P14", "Boss 登录态", False, fatal=False,
            message="preflight 跳过登录态真检测（开销大）",
            detail="真发送前由 BrowserManager.ensure_logged_in 兜底；"
                   "首次使用请跑：python -m boss_auto_apply login",
        )
    try:
        status = browser_manager.check_login(navigate=True)
        if status.logged_in:
            return CheckResult("P14", "Boss 登录态", True, fatal=False,
                               message=f"已登录（用户={status.username or '未知'}）")
        return CheckResult(
            "P14", "Boss 登录态", False, fatal=False,
            message="未登录（未检测到 wt2 cookie / .user-info DOM）",
            detail="请跑：python -m boss_auto_apply login",
        )
    except Exception as e:
        return CheckResult("P14", "Boss 登录态", False, fatal=False,
                           message=f"登录态探测失败：{e}",
                           detail="请跑：python -m boss_auto_apply login")


def run_m3_preflight(
    cfg: Any = None,
    *,
    browser_manager: Any = None,
    skip_login_check: bool = True,
    logger_obj: Any = None,
) -> PreflightReport:
    """M3 真发送前置检查（run 命令用，设计 §16.6）。

    跑 P10–P14：
    - P10: DrissionPage 已装
    - P11: Google Chrome 已装
    - P12: 专用 profile 目录（可创建）
    - P13: Pillow 可用
    - P14: Boss 登录态（默认 skip，开销大）

    Args:
        cfg: AppConfig（读 sender.user_data_dir）；None 用默认。
        browser_manager: BrowserManager（P14 用；None 则 skip）。
        skip_login_check: True 则 P14 不真检测。
        logger_obj: loguru logger。

    Returns:
        :class:`PreflightReport`。
    """
    log = logger_obj or logger
    user_data_dir = "~/.boss-auto-apply/chrome-profile"
    if cfg is not None:
        sender = getattr(cfg, "sender", None)
        if sender is not None:
            user_data_dir = getattr(sender, "user_data_dir", user_data_dir) or user_data_dir
    report = PreflightReport(results=[
        check_drissionpage(),
        check_chrome_app(),
        check_profile_dir(user_data_dir),
        check_pillow(),
        check_boss_login(browser_manager, skip=skip_login_check),
    ])
    for r in report.results:
        if r.passed:
            log.info(f"[{r.code}] ✅ {r.name}: {r.message}")
        elif r.fatal:
            log.error(f"[{r.code}] ❌ {r.name}: {r.message}  → {r.detail}")
        else:
            log.warning(f"[{r.code}] ⚠️ {r.name}: {r.message}  → {r.detail}")
    return report


# ============================================================
# run_preflight
# ============================================================
def run_preflight(
    *,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
    login_hint: str = DEFAULT_LOGIN_HINT,
    typst_bin: str = "typst",
    font_name: str = "Noto Sans CJK SC",
    template_dir: str = "templates/brilliant-cv",
    resume_path: str = "input/resume.pdf",
    config_path: str = "config/config.yaml",
    data_dir: str = "data",
    llm: Any = None,
    is_dry_run: bool = True,
    skip_login: bool = True,
    logger_obj: Any = None,
) -> PreflightReport:
    """跑全部 P1–P9 检查，返回报告。

    失败的致命项不立即抛（收集完再让调用方决定）；调用方可据 ``report.fatal_failures`` 抛异常。

    Args:
        claude_bin: claude 命令名或路径。
        login_hint: 登录指引串。
        typst_bin/...: 其余各检查参数。
        llm: ClaudeClient（登录探测用；None 则直接跑 claude auth status）。
        is_dry_run: dry-run 模式（登录探测可跳过）。
        skip_login: True 则 dry-run 跳过登录真探测。
        logger_obj: loguru logger。

    Returns:
        ``PreflightReport``。
    """
    log = logger_obj or logger
    report = PreflightReport(results=[
        check_node(),
        check_claude_bin(claude_bin),
        check_claude_login(claude_bin, llm, login_hint,
                           skip_in_dry_run=skip_login, is_dry_run=is_dry_run),
        check_typst(typst_bin),
        check_font(typst_bin, font_name),
        check_template(template_dir),
        check_resume_input(resume_path),
        check_config(config_path),
        check_data_dir(data_dir),
    ])
    # 打印
    for r in report.results:
        if r.passed:
            log.info(f"[{r.code}] ✅ {r.name}: {r.message}")
        elif r.fatal:
            log.error(f"[{r.code}] ❌ {r.name}: {r.message}  → {r.detail}")
        else:
            log.warning(f"[{r.code}] ⚠️ {r.name}: {r.message}  → {r.detail}")
    return report


def raise_on_fatal(report: PreflightReport) -> None:
    """有致命失败则抛 ``PreflightError``。"""
    fails = report.fatal_failures
    if fails:
        msgs = [f"[{r.code}] {r.name}: {r.message}" for r in fails]
        raise PreflightError("预检致命失败：\n  " + "\n  ".join(msgs))
