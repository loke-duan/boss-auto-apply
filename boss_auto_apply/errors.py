"""全部自定义异常 + 分类函数（设计 §5.1 / §11）。

分类函数 ``classify_error`` 把任意异常归到四类（network/business/risk/llm），
供状态机决定「留原态重试」还是「跳过终态」还是「全局熔断」。
"""

from __future__ import annotations

import subprocess
from typing import Literal

__all__ = [
    # 基类
    "BossAutoError",
    # 前置/配置
    "PreflightError",
    "ConfigValidationError",
    # Claude CLI / LLM
    "ClaudeLoginRequiredError",
    "ClaudeInvocationError",
    "LlmTimeoutError",
    "LlmJsonParseError",
    "LlmRateLimitError",
    # 系统/工具
    "TypstNotInstalledError",
    "FontMissingError",
    # 业务流程
    "ParserError",
    "HealthCheckFailedError",
    "SearcherError",
    "MatcherRejectError",
    "GeneratorError",
    "ImagerError",
    # M3 真发送（设计附录 C / §16）
    "SenderError",
    "BrowserConflictError",
    "BrowserLaunchError",
    "LoginTimeoutError",
    "LoginRequiredError",
    "WebSearchTimeoutError",
    "DomSelectorStaleError",
    "CaptchaDetectedError",
    "RateLimitedError",
    "ImagePreprocessError",
    "GreetValidationError",
    "CircuitBreakerOpenError",
    # 分类
    "classify_error",
    "ErrorCategory",
]

ErrorCategory = Literal["network", "business", "risk", "llm"]


# ============================================================
# 基类
# ============================================================
class BossAutoError(Exception):
    """所有 boss-auto-apply 自定义异常的基类。"""


# ============================================================
# 前置 / 配置（P1–P9、config.yaml 校验）
# ============================================================
class PreflightError(BossAutoError):
    """前置检查 P1–P9 任一失败（除登录/typst 等有专门子类外）。"""


class ConfigValidationError(BossAutoError):
    """config.yaml 未通过 JSON Schema / Pydantic 校验。

    携带 ``errors`` 字段，列出 jsonschema 的明细错误，便于人读。
    """

    def __init__(self, msg: str, errors: list[dict] | None = None) -> None:
        super().__init__(msg)
        self.errors = errors or []


# ============================================================
# Claude CLI / LLM（§5.5 核心）
# ============================================================
class ClaudeLoginRequiredError(BossAutoError):
    """Claude CLI 未登录（``claude auth status`` 返回 ``loggedIn != true``）。

    携带 ``login_hint``，main.py 据此打印首次登录指引。
    """

    def __init__(self, login_hint: str) -> None:
        msg = (
            "Claude CLI 未登录（claude auth status 返回 loggedIn != true）。"
            f"首次使用请先登录：\n    {login_hint}"
        )
        super().__init__(msg)
        self.login_hint = login_hint


class ClaudeInvocationError(BossAutoError):
    """claude 命令缺失 / subprocess 调用层错误（不重试）。"""


class LlmTimeoutError(BossAutoError):
    """LLM subprocess 超时（可重试，指数退避）。"""


class LlmJsonParseError(BossAutoError):
    """LLM 输出无法解析为合法 JSON（可重试 1 次，重试时加「只输出 JSON」）。

    携带 ``raw`` 原始输出和 ``expected_schema``，便于排查。
    """

    def __init__(self, raw: str, expected_schema: str = "") -> None:
        super().__init__(f"LLM 输出 JSON 解析失败（expected: {expected_schema}）")
        self.raw = raw
        self.expected_schema = expected_schema


class LlmRateLimitError(BossAutoError):
    """LLM 返回 rate / 429 / quota（可重试，更激进退避 base*4^n）。"""


# ============================================================
# 系统 / 工具依赖
# ============================================================
class TypstNotInstalledError(PreflightError):
    """typst 可执行文件未找到（提示 brew install typst）。"""


class FontMissingError(PreflightError):
    """思源黑体（Noto Sans CJK SC）未被 typst 找到（非致命，走 fallback）。"""


# ============================================================
# 业务流程
# ============================================================
class ParserError(BossAutoError):
    """简历解析失败（PDF 损坏/加密/规则切分失败等）。"""


class HealthCheckFailedError(BossAutoError):
    """F1.6 体检未通过：红线项未修正 / 用户主动 skip / 非交互模式且未默认接受。

    携带 ``report_path`` 指向体检报告，便于人工查看。
    """

    def __init__(self, report_path: str = "", reason: str = "") -> None:
        msg = f"F1.6 HRBP 体检未通过：{reason}" if reason else "F1.6 HRBP 体检未通过"
        if report_path:
            msg += f"\n    体检报告：{report_path}"
        super().__init__(msg)
        self.report_path = report_path


class SearcherError(BossAutoError):
    """岗位搜索失败（boss-cli 调用失败、Mock fixtures 缺失、job_id 不存在等）。"""


class MatcherRejectError(BossAutoError):
    """业务跳过：JD 不匹配 / 硬过滤未命中 / 黑名单（永久不再处理，不重试）。

    携带 ``reason``，状态机据此把 job 标 ``skipped(business)``。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"匹配拒绝：{reason}")
        self.reason = reason


class GeneratorError(BossAutoError):
    """Typst 编译失败（语法错/模板缺变量），携带 typst stderr。"""


class ImagerError(BossAutoError):
    """PNG 转换失败（typst_to_png 与 PyMuPDF 兜底双双失败）。"""


# ============================================================
# M3 真发送模块异常（设计附录 C / §16）
# ============================================================
class SenderError(BossAutoError):
    """M3 发送编排器通用错误（Sender.send_application 内部抛的基类）。"""


class BrowserConflictError(SenderError):
    """专用 profile 被其他进程占用，或 config 误指向日常 Chrome profile。"""

    def __init__(self, msg: str = "") -> None:
        super().__init__(msg or "浏览器 profile 冲突：被其他进程占用或指向了日常 profile")


class BrowserLaunchError(SenderError):
    """Chrome 启动失败（未装/profile 损坏/DrissionPage 不可用）。"""


class LoginTimeoutError(SenderError):
    """扫码登录超时（wait_for_login 在 timeout_sec 内未检测到登录态）。"""


class LoginRequiredError(SenderError):
    """未登录却尝试真发送（提示用户先跑 ``python -m boss_auto_apply login``）。"""

    def __init__(self, msg: str = "") -> None:
        super().__init__(
            msg or "Boss 未登录，请先运行：python -m boss_auto_apply login"
        )


class WebSearchTimeoutError(SenderError):
    """网页搜索页加载超时（等 .job-card-wrapper 超过 timeout）。"""


class DomSelectorStaleError(SenderError):
    """DOM 选择器失效（Boss 改版）。

    携带 selector_name 和试过的选择器列表，便于更新 selectors.py。
    """

    def __init__(self, selector_name: str, tried: list[str]) -> None:
        self.selector_name = selector_name
        self.tried = tried
        super().__init__(
            f"选择器失效：{selector_name}。试过 {tried}。"
            "可能 Boss 改版，请更新 boss_auto_apply/browser/selectors.py 后重试。"
        )


class CaptchaDetectedError(SenderError):
    """检测到验证码/人机验证（触发 CircuitBreaker 硬熔断 24h）。"""


class RateLimitedError(SenderError):
    """检测到「操作频繁/账号被限制」（触发 CircuitBreaker 硬熔断）。"""


class ImagePreprocessError(SenderError):
    """图片预处理失败（图片损坏/Pillow 不可用/压不到 <1MB）。"""


class GreetValidationError(SenderError):
    """话术发送前校验违规（含薪资/空窗/学历/禁忌词）。

    携带 reason 和违规话术原文，便于用户改 master 重生成。
    """

    def __init__(self, reason: str, greet: str = "") -> None:
        self.greet = greet
        super().__init__(f"话术校验失败：{reason}")


class CircuitBreakerOpenError(SenderError):
    """熔断器开启（CircuitBreaker 状态 open_hard/open_soft 未到期）。

    携带剩余等待秒数和原因，便于提示用户。
    """

    def __init__(self, reason: str, wait_sec: float) -> None:
        self.reason = reason
        self.wait_sec = wait_sec
        super().__init__(f"熔断器开启：{reason}，剩余等待 {wait_sec:.0f}s")


# ============================================================
# 分类函数（§11.2）
# ============================================================
# 登录失效串与风险关键词，供 classify_error 判 risk
_RISK_KEYWORDS = ("验证码", "captcha", "风险", "限制", "登录失效", "账号被限")


def classify_error(e: BaseException) -> ErrorCategory:
    """把任意异常归到四类。

    - ``llm``：LLM 超时/限流/JSON 解析失败/幻觉被后置校验拒（留原态重试）
    - ``business``：业务跳过（不匹配/黑名单/重试超限）（→ skipped 终态，不重试）
    - ``risk``：登录失效/验证码/限流提示（触发 CircuitBreaker，全局冷却）
    - ``network``：subprocess 超时/typst 崩溃等（兜底归类，留原态重试）

    Args:
        e: 任意异常。

    Returns:
        四类之一。
    """
    if isinstance(e, (LlmTimeoutError, LlmRateLimitError, LlmJsonParseError)):
        return "llm"
    if isinstance(e, MatcherRejectError):
        return "business"
    if isinstance(e, ClaudeLoginRequiredError):
        # 登录失效归 risk（全局需重新登录，不能只重试单 job）
        return "risk"
    # ---- M3 真发送异常分类（设计附录 C）----
    # risk：风控信号（验证码/操作频繁/登录失效）→ 触发熔断
    if isinstance(e, (CaptchaDetectedError, RateLimitedError, LoginRequiredError)):
        return "risk"
    # business：DOM 选择器失效/话术违规 → skipped 终态，不重试
    if isinstance(e, (DomSelectorStaleError, GreetValidationError)):
        return "business"
    # network：浏览器启动/超时/图片预处理 → 留原态重试
    if isinstance(e, (BrowserLaunchError, LoginTimeoutError, WebSearchTimeoutError,
                      ImagePreprocessError)):
        return "network"
    if isinstance(e, CircuitBreakerOpenError):
        # 熔断开启不归类到单 job，由 pipeline 直接停止；这里兜底 risk
        return "risk"
    if isinstance(e, (subprocess.TimeoutExpired, GeneratorError)):
        return "network"
    # 文本启发式：含风险关键词 → risk
    text = str(e).lower()
    if any(kw in text for kw in _RISK_KEYWORDS):
        return "risk"
    # 兜底归 network（可重试）
    return "network"
