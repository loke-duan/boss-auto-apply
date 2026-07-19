"""F1.5 简历画像与方向推荐（设计 §5.7）。

基于**体检通过**的 master.json，由 LLM 自主推荐 target 列表 → 暂停等确认 → 写 targets.confirmed.yaml。

v2 设计（开源化）：
- **方向完全由 LLM 自主推荐**——从 master 的 work experiences + projects + skill_modules
  中识别候选人核心行业经验（如保险/金融科技/AI），输出对应方向的 target。
- 不再硬编码任何方向（旧版 SEO/私域/投放 + weight 0.5/0.3/0.2 已废弃）。
- config.yaml 的 sub_directions 降级为 keywords_hint（可选池，辅助 LLM 但不强制）。
- 校验维度（validate_targets）：weight 总和 ≈ 1.0、(sub_direction, city) 无重复、
  city ∈ constraints.salary、sub_direction 非空。
- 用户 review 确认环节（present_targets → await_target_confirmation）保留，
  落盘前必须用户回车确认。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from loguru import logger

from ..errors import HealthCheckFailedError, LlmJsonParseError
from ..models import RecommendedTarget
from .hrbp_check import _read_common_rules, require_health_passed

__all__ = [
    "run_profiler",
    "validate_targets",
    "present_targets",
    "await_target_confirmation",
    "generate_search_targets",
    "render_profiler_prompt",
]


@dataclass
class ProfilerResult:
    """F1.5 产物。"""

    targets: list[RecommendedTarget] = field(default_factory=list)
    strategy_note: str = ""
    raw: dict = field(default_factory=dict)


# ============================================================
# prompt 渲染
# ============================================================
def render_profiler_prompt(master: dict[str, Any], constraints: dict[str, Any]) -> str:
    """渲染 profiler.md prompt（注入 master + constraints + common_rules）。"""
    p = Path(__file__).parent.parent / "prompts" / "profiler.md"
    template = p.read_text(encoding="utf-8")
    return (
        template
        .replace("{common_rules}", _read_common_rules())
        .replace("{master_json}", json.dumps(master, ensure_ascii=False, indent=2))
        .replace("{constraints_json}", json.dumps(constraints, ensure_ascii=False, indent=2))
    )


# ============================================================
# run_profiler
# ============================================================
def run_profiler(
    master: dict[str, Any],
    constraints: dict[str, Any],
    llm: Any,
    *,
    retry_on_constraint_violation: bool = True,
    logger_obj: Any = None,
) -> ProfilerResult:
    """基于体检通过的 master，在硬约束下推荐 target 列表。

    前置：master.health_check_status 必须 == 'passed'，否则抛 ``HealthCheckFailedError``。

    Args:
        master: master.json dict。
        constraints: target_constraints dict。
        llm: ``ClaudeClient``。
        retry_on_constraint_violation: 主方向不合规时是否重试 1 次。
        logger_obj: loguru logger。

    Returns:
        ``ProfilerResult``。

    Raises:
        HealthCheckFailedError: master 未体检。
        LlmJsonParseError: LLM 输出无法解析。
        ValueError: 主方向不合规且重试后仍不合规。
    """
    log = logger_obj or logger
    require_health_passed(master, report_path="（profiler 前置）")

    prompt = render_profiler_prompt(master, constraints)
    result = llm.invoke(
        prompt,
        expect_json=True,
        purpose="profiler",
        model=getattr(getattr(llm, "cfg", None), "model_profiler", None),
    )
    data = result.raw_json
    if not isinstance(data, dict) or "targets" not in data:
        raise LlmJsonParseError(raw=result.text, expected_schema="profiler {targets:[...]}")

    targets_dicts = data.get("targets", []) or []
    targets = [RecommendedTarget(**_coerce_target(t, constraints)) for t in targets_dicts]
    pr = ProfilerResult(targets=targets, strategy_note=data.get("strategy_note", ""), raw=data)

    # 强制约束校验
    violations = validate_targets(pr.targets, constraints)
    if violations:
        if retry_on_constraint_violation:
            log.warning(f"profiler 约束违规，重试 1 次：{violations}")
            result2 = llm.invoke(
                prompt + "\n\n注意：上次输出违规：" + "; ".join(violations),
                expect_json=True,
                purpose="profiler",
                model=getattr(getattr(llm, "cfg", None), "model_profiler", None),
            )
            data2 = result2.raw_json or {}
            targets2 = [RecommendedTarget(**_coerce_target(t, constraints)) for t in (data2.get("targets", []) or [])]
            pr.targets = targets2
            pr.strategy_note = data2.get("strategy_note", "")
            violations2 = validate_targets(pr.targets, constraints)
            if violations2:
                raise ValueError(f"profiler 重试后仍违规：{violations2}")
        else:
            raise ValueError(f"profiler 约束违规：{violations}")

    log.info(f"profiler 完成：{len(pr.targets)} 个 target")
    return pr


def _coerce_target(t: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]:
    """补全 target 字段（city/weight 缺失时从 constraints 取）。"""
    t = dict(t)
    city = t.get("city", "")
    salary_map = constraints.get("salary", {}) or {}
    if not t.get("salary"):
        t["salary"] = salary_map.get(city, "")
    # weight 从 sub_directions 取（按 sub_direction 匹配）
    if "weight" not in t or t.get("weight") in (None, 0):
        for sd in constraints.get("sub_directions", []) or []:
            if sd.get("name") == t.get("sub_direction"):
                t["weight"] = sd.get("weight", 0.0)
                break
    return t


# ============================================================
# 约束校验
# ============================================================
def validate_targets(targets: list[RecommendedTarget], constraints: dict[str, Any]) -> list[str]:
    """方向自洽性校验（v2：放开方向锁定，校验维度从「必须 ∈ config 池」改为结构性约束）。

    v2 校验维度（开源化后保留的硬约束）：
    1. **方向非空**：sub_direction 必须是非空字符串（自洽性，不限定具体值）。
    2. **城市合法**：city 必须 ∈ constraints.salary.keys()（薪资带与城市绑定，是物理约束）。
    3. **权重总和 ≈ 1.0**：所有 target weight 之和 ∈ [0.95, 1.05]（容差 0.05）。
    4. **无重复**：(sub_direction, city) 不应重复。

    Args:
        targets: 推荐的 target 列表。
        constraints: target_constraints。

    Returns:
        违规项列表（空表示全部通过）。
    """
    violations: list[str] = []
    allowed_cities = set(constraints.get("salary", {}).keys())

    # 1. 方向非空（sub_direction 必须是非空字符串）
    for t in targets:
        if not t.sub_direction or not str(t.sub_direction).strip():
            violations.append(f"target {t.name!r} 缺 sub_direction（必须由 LLM 给出方向名）")

    # 2. city 必须在 constraints.salary
    for t in targets:
        if allowed_cities and t.city not in allowed_cities:
            violations.append(f"target {t.name!r} 的 city {t.city!r} 不在允许城市 {sorted(allowed_cities)}")

    # 3. 权重总和 ≈ 1.0（容差 0.05）
    total_weight = sum(float(t.weight or 0) for t in targets)
    if targets and abs(total_weight - 1.0) > 0.05:
        violations.append(
            f"权重总和 {total_weight:.3f} 不在 [0.95, 1.05] 区间（应 ≈ 1.0）"
        )

    # 4. (sub_direction, city) 不应重复
    seen = set()
    for t in targets:
        key = (t.sub_direction, t.city)
        if key in seen:
            violations.append(f"target {t.name!r} 重复 (sub_direction={t.sub_direction}, city={t.city})")
        seen.add(key)

    return violations


# ============================================================
# 展示 + 暂停
# ============================================================
def present_targets(targets: list[RecommendedTarget], *, printer: Callable[[str], None] | None = None) -> None:
    """打印推荐方向（测试可注入 printer）。"""
    lines = ["# F1.5 推荐求职方向（请确认/修改后继续）"]
    for t in targets:
        safe = "✅" if t.interview_safe else "⚠️"
        lines.append(
            f"\n## {safe} {t.name}（{t.city}·{t.salary}·权重{t.weight}）\n"
            f"- 关键词：{t.keywords}\n"
            f"- 硬过滤：{t.per_job_keywords}\n"
            f"- 理由：{t.reason}"
        )
    text = "\n".join(lines)
    if printer is not None:
        printer(text)
        return
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        Console().print(Markdown(text))
    except Exception:
        print(text)


def await_target_confirmation(
    targets: list[RecommendedTarget],
    output_path: str,
    *,
    input_fn: Callable[[str], str] = input,
    non_interactive_default_accept: bool = False,
    logger_obj: Any = None,
) -> list[RecommendedTarget]:
    """写 targets.confirmed.yaml → 等用户编辑后回车 → 重载校验返回。

    Args:
        targets: 推荐的 target 列表。
        output_path: targets.confirmed.yaml 路径。
        input_fn: 输入函数。
        non_interactive_default_accept: 非交互模式默认接受。
        logger_obj: loguru logger。

    Returns:
        确认后的 target 列表。
    """
    log = logger_obj or logger
    # 先写推荐结果
    data = {
        "targets": [t.model_dump() for t in targets],
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    if non_interactive_default_accept:
        log.info("非交互模式：默认接受推荐方向")
        return targets

    input_fn("推荐方向已写入 targets.confirmed.yaml。编辑后回车继续：")
    # 重载
    with open(output_path, encoding="utf-8") as f:
        reloaded = yaml.safe_load(f) or {}
    confirmed = [RecommendedTarget(**t) for t in reloaded.get("targets", [])]
    return confirmed


def generate_search_targets(confirmed: list[RecommendedTarget]) -> list[dict[str, Any]]:
    """把 confirmed 转成 searcher 需要的搜索参数（每方向×每城）。

    Args:
        confirmed: 确认的 target 列表。

    Returns:
        搜索参数 dict 列表（每条一个 (sub_direction, city) 组合）。
    """
    out: list[dict[str, Any]] = []
    for t in confirmed:
        out.append({
            "name": t.name,
            "sub_direction": t.sub_direction,
            "city": t.city,
            "keywords": list(t.keywords),
            "per_job_keywords": list(t.per_job_keywords),
            "salary": t.salary,
            "weight": t.weight,
        })
    return out
