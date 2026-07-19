"""F1.6 HRBP 简历体检（质量闸门，ADR-0001，设计 §5.6）。

数据流：
    master.json(pending) → run_health_check → HealthReport(JSON)
      → present_report(rich 打印) → ⏸️ await_user_confirmation
      → 用户改 master.json → validate_fixes
      → 通过 → master.health.json 写 'passed' → 解锁 F1.5/F4
      → 不通过 → HealthCheckFailedError（流程停在此）

体检是闸门：``master.health_check_status != 'passed'`` 时，F1.5 和 F4 都必须抛 ``HealthCheckFailedError``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from ..errors import HealthCheckFailedError, LlmJsonParseError

__all__ = [
    "HealthReport",
    "HealthItem",
    "run_health_check",
    "present_report",
    "await_user_confirmation",
    "validate_fixes",
    "render_hrbp_prompt",
    "require_health_passed",
    "FORBIDDEN_WORDS",
]

# 禁忌词 — 通用过度承诺词（适用所有候选人，设计 §5.10 第 4 步）
# 候选人专属禁忌词（如特定年限/数据）应从 config.forbidden_words_extra 注入，
# 不在此硬编码。validate_fixes 会合并两者扫描。
FORBIDDEN_WORDS = ("精通", "100%", "完美", "极致")


@dataclass
class HealthItem:
    """体检条目。"""

    item: str
    current: str = ""
    should_be: str = ""
    reason: str = ""
    adr: str = ""
    severity: str = "yellow"  # red/yellow/warn


@dataclass
class HealthReport:
    """体检报告（F1.6 产物）。"""

    must_fix: list[dict] = field(default_factory=list)
    suggest_fix: list[dict] = field(default_factory=list)
    need_user_input: list[dict] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    red_lines_violated: list[str] = field(default_factory=list)
    summary: str = ""
    markdown: str = ""  # 渲染好的报告（展示给用户）
    raw: dict = field(default_factory=dict)  # 原始 LLM JSON

    @property
    def has_red_lines(self) -> bool:
        """是否有红线项（阻断 F4）。"""
        return bool(self.red_lines_violated) or any(
            it.get("severity") == "red" for it in self.must_fix
        )


# ============================================================
# prompt 渲染
# ============================================================
def _read_common_rules() -> str:
    """读 _common_rules.md 的内容（去掉 jinja 注释行）。"""
    p = Path(__file__).parent.parent / "prompts" / "_common_rules.md"
    if not p.exists():
        return "(common_rules 缺失)"
    text = p.read_text(encoding="utf-8")
    # 去掉首行 jinja 注释
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("{#")]
    return "\n".join(lines).strip()


def render_hrbp_prompt(master: dict[str, Any]) -> str:
    """渲染 hrbp_check.md prompt（注入 master.json + common_rules + 候选人名）。

    Args:
        master: master.json dict。

    Returns:
        渲染后的 prompt 字符串。
    """
    p = Path(__file__).parent.parent / "prompts" / "hrbp_check.md"
    template = p.read_text(encoding="utf-8")
    name = master.get("basics", {}).get("name", "候选人")
    return (
        template
        .replace("{name}", name)
        .replace("{common_rules}", _read_common_rules())
        .replace("{master_json}", json.dumps(master, ensure_ascii=False, indent=2))
    )


# ============================================================
# run_health_check
# ============================================================
def run_health_check(
    master: dict[str, Any],
    llm: Any,
    *,
    logger_obj: Any = None,
) -> HealthReport:
    """调 LLM（model_hrbp）对 master.json 做体检 → HealthReport。

    Args:
        master: master.json dict。
        llm: ``ClaudeClient``。
        logger_obj: loguru logger。

    Returns:
        ``HealthReport``。

    Raises:
        LlmJsonParseError: LLM 输出无法解析。
    """
    log = logger_obj or logger
    prompt = render_hrbp_prompt(master)
    result = llm.invoke(
        prompt,
        expect_json=True,
        purpose="hrbp",
        model=getattr(getattr(llm, "cfg", None), "model_hrbp", None),
    )
    data = result.raw_json
    if not isinstance(data, dict):
        raise LlmJsonParseError(raw=result.text, expected_schema="HealthReport object")

    report = HealthReport(
        must_fix=data.get("must_fix", []) or [],
        suggest_fix=data.get("suggest_fix", []) or [],
        need_user_input=data.get("need_user_input", []) or [],
        keep=data.get("keep", []) or [],
        red_lines_violated=data.get("red_lines_violated", []) or [],
        summary=data.get("summary", ""),
        raw=data,
    )
    report.markdown = _render_report_markdown(report)
    log.info(f"HRBP 体检完成：must_fix={len(report.must_fix)} "
             f"suggest={len(report.suggest_fix)} red_lines={len(report.red_lines_violated)}")
    return report


def _render_report_markdown(report: HealthReport) -> str:
    """把 HealthReport 渲染为 PRD §4.1.6 的 markdown 表格。"""
    lines: list[str] = ["# 简历体检报告"]
    if report.must_fix:
        lines.append("\n## 🔴 必须修正")
        for it in report.must_fix:
            lines.append(
                f"- [ ] {it.get('item', '')}：`{it.get('current', '')}` → `{it.get('should_be', '')}`"
                f"（{it.get('reason', '')}）[{it.get('adr', '')}]"
            )
    if report.suggest_fix:
        lines.append("\n## 🟡 建议修正")
        for it in report.suggest_fix:
            lines.append(
                f"- [ ] {it.get('item', '')}：`{it.get('current', '')}` → `{it.get('should_be', '')}`"
                f"（{it.get('reason', '')}）"
            )
    if report.need_user_input:
        lines.append("\n## ⚠️ 需你补充真实信息")
        for it in report.need_user_input:
            lines.append(f"- [ ] {it.get('item', '')}：{it.get('reason', '')}")
    if report.keep:
        lines.append("\n## ✅ 可保留")
        for k in report.keep:
            lines.append(f"- {k}")
    if report.summary:
        lines.append(f"\n**总评**：{report.summary}")
    if report.red_lines_violated:
        lines.append("\n## 🚫 红线违规（阻断 F4）")
        for r in report.red_lines_violated:
            lines.append(f"- {r}")
    return "\n".join(lines)


# ============================================================
# 展示 + 暂停
# ============================================================
def present_report(report: HealthReport, *, printer: Callable[[str], None] | None = None) -> None:
    """打印体检报告（rich 彩色；测试可注入 printer）。

    Args:
        report: 体检报告。
        printer: 打印函数（默认用 rich 打印 markdown）。
    """
    if printer is not None:
        printer(report.markdown)
        return
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        console = Console()
        console.print(Markdown(report.markdown))
        console.print(
            "\n[dim]请编辑 data/master.json 后回车继续；或输入 'skip' 跳过（红线项不可跳过）。[/]"
        )
    except Exception:
        # rich 不可用时退化纯文本
        print(report.markdown)


def await_user_confirmation(
    report: HealthReport,
    master_path: str,
    *,
    input_fn: Callable[[str], str] = input,
    timeout_sec: int = 0,
    non_interactive_default_accept: bool = False,
    logger_obj: Any = None,
) -> dict[str, Any]:
    """阻塞等用户确认体检。

    用户行为：
      - 编辑 master.json 后回车 → 重新加载 master，校验 must_fix 是否已改 → 通过则 health_check_status=passed
      - 输入 'skip' → 抛 ``HealthCheckFailedError``（红线项不可跳过）
      - 超时（timeout_sec>0）→ 抛 ``HealthCheckFailedError``
      - ``non_interactive_default_accept=True``（CI/--non-interactive）→ 默认接受报告

    Args:
        report: 体检报告。
        master_path: master.json 路径。
        input_fn: 输入函数（测试 mock 用）。
        timeout_sec: 超时秒（0=不超时）。
        non_interactive_default_accept: 非交互模式默认接受。
        logger_obj: loguru logger。

    Returns:
        更新后的 master dict（health_check_status='passed'）。

    Raises:
        HealthCheckFailedError: 用户 skip / 超时 / 红线项未修正。
    """
    log = logger_obj or logger

    # 非交互模式：默认接受报告（标 [假设] A6）
    if non_interactive_default_accept:
        log.info("非交互模式：默认接受体检报告（must_fix 视为已处理）")
        master = _load_master(master_path)
        master["health_check_status"] = "passed"
        _save_master(master_path, master)
        return master

    # 超时模式
    if timeout_sec > 0:
        import signal

        def _handler(*_a: Any) -> None:
            raise HealthCheckFailedError(report_path=master_path, reason="体检确认超时")

        old = signal.signal(signal.SIGALRM, _handler)
        try:
            signal.alarm(timeout_sec)
            answer = input_fn("体检报告已展示。编辑 master.json 后回车继续，或输入 skip：")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    else:
        answer = input_fn("体检报告已展示。编辑 master.json 后回车继续，或输入 skip：")

    answer = (answer or "").strip().lower()
    if answer == "skip":
        if report.has_red_lines:
            raise HealthCheckFailedError(
                report_path=master_path,
                reason="存在红线项，不可 skip（必须修正 must_fix 中的 red 项）",
            )
        raise HealthCheckFailedError(report_path=master_path, reason="用户主动 skip 体检")

    # 用户回车 → 重新加载 master，校验 must_fix
    master = _load_master(master_path)
    uncorrected = validate_fixes(master, report)
    if uncorrected:
        raise HealthCheckFailedError(
            report_path=master_path,
            reason=f"以下 must_fix 项仍未修正：{uncorrected}",
        )
    master["health_check_status"] = "passed"
    _save_master(master_path, master)
    return master


def _load_master(path: str) -> dict[str, Any]:
    """读 master.json。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_master(path: str, master: dict[str, Any]) -> None:
    """写 master.json（pretty）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)


# ============================================================
# validate_fixes
# ============================================================
def validate_fixes(master: dict[str, Any], report: HealthReport) -> list[str]:
    """检查 must_fix 是否真的改了。

    校验项：
    - 工作年限 == constraints.work_years_authoritative（未配置则跳过）
    - 禁忌词扫描（FORBIDDEN_WORDS + constraints.forbidden_words_extra）已替换
    - 学历 == constraints.degree_filter（未配置则跳过）

    Args:
        master: 当前 master dict。
        report: 体检报告。

    Returns:
        仍未修正项列表（空表示全部通过）。
    """
    uncorrected: list[str] = []

    constraints_raw = master.get("constraints", {}) or {}
    # 1) 工作年限（constraints.work_years_authoritative 为 0/None 时跳过）
    basics = master.get("basics", {})
    wy = basics.get("work_years_total")
    authoritative = constraints_raw.get("work_years_authoritative")
    if authoritative and wy is not None and abs(float(wy) - float(authoritative)) > 0.01:
        uncorrected.append(f"工作年限 {wy} → 应为 {authoritative}")

    # 2) 禁忌词扫描（扫 master 文本的字符串值）
    # 注意：constraints.data_standards_locked 是「数据表述标准文档」，会合法地
    # 记录「翻 N 倍 → 稳定增长」这类「原值→标准值」映射；projects 的
    # data_standards_applied 是「已应用的标准」。这些是文档/元数据，不是简历
    # 正文，扫描前必须剔除，否则会把「标准本身」误判为「禁忌词残留」。
    import copy
    scan_master = copy.deepcopy(master)
    constraints = scan_master.get("constraints", {})
    if isinstance(constraints, dict):
        constraints.pop("data_standards_locked", None)
        constraints.pop("forbidden_words_extra", None)
        constraints.pop("work_years_authoritative", None)
    # projects 可能是 list（逐条）或 dict（按 name 索引），统一处理
    projects = scan_master.get("projects", []) or []
    if isinstance(projects, dict):
        projects = list(projects.values())
    for proj in projects:
        if isinstance(proj, dict):
            proj.pop("data_standards_applied", None)
    text_blob = json.dumps(scan_master, ensure_ascii=False)
    # 合并通用禁忌词 + 候选人专属禁忌词（从 constraints.forbidden_words_extra）
    extra = constraints_raw.get("forbidden_words_extra", []) or []
    all_forbidden = list(FORBIDDEN_WORDS) + list(extra)
    for w in all_forbidden:
        if w in text_blob:
            uncorrected.append(f"仍含禁忌词「{w}」")

    # 3) 学历（constraints.degree_filter 为空则跳过）
    degree_filter = constraints_raw.get("degree_filter", "")
    degree = basics.get("degree")
    if degree_filter and degree and degree != degree_filter:
        uncorrected.append(f"学历 {degree} → 应为 {degree_filter}")

    # 4) must_fix 中标 red 的 item（启发式：检查 should_be 是否出现）
    for it in report.must_fix:
        if it.get("severity") != "red":
            continue
        should = it.get("should_be", "")
        if should and should not in text_blob and should not in str(master):
            # 宽松：should_be 没出现不一定是错（可能是描述），记 warning 不阻断
            logger.debug(f"must_fix red 项 should_be 未直接命中：{it.get('item')} → {should}")

    # 5) v3（ADR-0005）：candidate_profile 结构性校验
    # candidate_profile 是下游 matcher/pipeline 通用化的基座，字段不全则 matcher 无法判断
    uncorrected.extend(_validate_candidate_profile(master))

    return uncorrected


def _validate_candidate_profile(master: dict[str, Any]) -> list[str]:
    """校验 master.basics.candidate_profile 完整性（ADR-0005）。

    校验项：
    1. candidate_profile 存在且是 dict
    2. target_role_keywords 非空（≥ 1 个）
    3. role_category 非空
    4. seniority_level ∈ {初级,中级,高级,资深,专家}
    5. work_years_authoritative == basics.work_years_total（防 LLM 篡改工龄）
    6. core_industries 非空（≥ 1 个，含 "通用" 兜底）

    Args:
        master: master dict。

    Returns:
        违规项列表（空表示全部通过）。
    """
    violations: list[str] = []
    basics = master.get("basics") or {}
    cp = basics.get("candidate_profile")
    if not isinstance(cp, dict):
        violations.append("basics.candidate_profile 缺失或非对象（F1.55 补全失败？）")
        return violations

    # 2. target_role_keywords 非空
    trk = cp.get("target_role_keywords")
    if not isinstance(trk, list) or not trk or not all(isinstance(x, str) and x.strip() for x in trk):
        violations.append(f"candidate_profile.target_role_keywords 必须是 3-6 个非空字符串（当前：{trk!r}）")
    elif not (3 <= len(trk) <= 8):
        violations.append(f"candidate_profile.target_role_keywords 数量应在 3-8（当前 {len(trk)} 个：{trk}）")

    # 3. role_category 非空
    rc = cp.get("role_category", "")
    if not isinstance(rc, str) or not rc.strip():
        violations.append("candidate_profile.role_category 不能为空（单一核心职业类别）")

    # 4. seniority_level 枚举校验
    sl = cp.get("seniority_level", "")
    if sl not in ("初级", "中级", "高级", "资深", "专家"):
        violations.append(
            f"candidate_profile.seniority_level 必须 ∈ {{初级,中级,高级,资深,专家}}（当前：{sl!r}）"
        )

    # 5. work_years_authoritative == work_years_total（防 LLM 篡改工龄，ADR-0002）
    wya = cp.get("work_years_authoritative")
    wyt = basics.get("work_years_total")
    if isinstance(wya, (int, float)) and isinstance(wyt, (int, float)):
        if abs(float(wya) - float(wyt)) > 0.01:
            violations.append(
                f"candidate_profile.work_years_authoritative={wya} 与 basics.work_years_total={wyt} 不一致"
                "（ADR-0002 防篡改工龄）"
            )
    elif wya in (None, 0, 0.0):
        violations.append("candidate_profile.work_years_authoritative 不能为空或 0")

    # 6. core_industries 非空
    ci = cp.get("core_industries")
    if not isinstance(ci, list) or not ci or not all(isinstance(x, str) and x.strip() for x in ci):
        violations.append(f"candidate_profile.core_industries 必须是 1-4 个非空字符串（当前：{ci!r}）")

    return violations


# ============================================================
# 闸门：F1.5/F4 调用前强制检查
# ============================================================
def require_health_passed(master: dict[str, Any] | Any, *, report_path: str = "") -> None:
    """闸门：master.health_check_status != 'passed' → 抛 ``HealthCheckFailedError``。

    Args:
        master: master dict 或 Master 模型。
        report_path: 体检报告路径（错误信息用）。

    Raises:
        HealthCheckFailedError: 体检未通过。
    """
    status = None
    if isinstance(master, dict):
        status = master.get("health_check_status")
    else:
        status = getattr(master, "health_check_status", None)
    if status != "passed":
        raise HealthCheckFailedError(
            report_path=report_path,
            reason=f"master.health_check_status={status!r}（必须 'passed'，ADR-0001 闸门）",
        )
