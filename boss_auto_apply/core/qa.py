"""qa.py — 简历存疑点审查 + CLI 交互问答（需求2）。

在简历解析（F1）后、体检（F1.6）前插入：LLM 审查 master.json 识别存疑点
（经历断层/数据缺失/技能模糊/信息不一致），CLI 逐条提问并附建议方向。
用户回答写回 master.json 的 qa_log 字段。

设计原则（复用现有模式）：
- ``input_fn`` 注入（与 hrbp_check/profiler 一致，便于测试 mock）。
- rich 渲染 + print 退化（与 present_report 一致）。
- ``non_interactive_default_accept`` 跳过（与 await_user_confirmation 一致）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Literal

from loguru import logger
from pydantic import BaseModel, Field

from ..errors import LlmJsonParseError
from .hrbp_check import _read_common_rules

__all__ = [
    "ResumeQuestion",
    "review_resume",
    "present_questions",
    "await_qa_answers",
    "apply_answers_to_master",
]


class ResumeQuestion(BaseModel):
    """单个简历存疑问题。"""

    category: Literal["gap", "data_missing", "skill_vague", "inconsistency", "other"] = "other"
    field_path: str = ""
    question: str = ""
    suggestion: str = ""
    severity: Literal["block", "warn"] = "warn"

    model_config = {"extra": "allow"}


def _render_qa_prompt(master: dict[str, Any]) -> str:
    """渲染 qa_review.md prompt（注入 master + common_rules）。"""
    p = Path(__file__).parent.parent / "prompts" / "qa_review.md"
    template = p.read_text(encoding="utf-8")
    # 过滤 jinja 注释行（{# ... #}）
    lines = [line for line in template.splitlines() if not line.strip().startswith("{#")]
    template_clean = "\n".join(lines)
    return (
        template_clean
        .replace("{common_rules}", _read_common_rules())
        .replace("{master_json}", json.dumps(master, ensure_ascii=False, indent=2))
    )


def review_resume(
    master: dict[str, Any],
    llm: Any,
    *,
    logger_obj: Any = None,
) -> list[ResumeQuestion]:
    """LLM 审查 master.json，生成存疑问题清单。

    Args:
        master: master.json dict。
        llm: LLM 客户端（ClaudeClient）。
        logger_obj: loguru logger。

    Returns:
        问题清单（按 severity 降序：block 在前）。空列表表示无存疑点。
    """
    log = logger_obj or logger
    prompt = _render_qa_prompt(master)
    try:
        result = llm.invoke(
            prompt,
            expect_json=True,
            purpose="hrbp",  # 复用 hrbp 模型（强，审查需要推理）
            model=getattr(getattr(llm, "cfg", None), "model_hrbp", None),
        )
    except Exception as e:
        log.warning(f"[qa] LLM 审查失败，跳过问答：{e}")
        return []
    data = result.raw_json
    # LLM 可能返回 dict（带 questions 键）或 list
    if isinstance(data, dict):
        data = data.get("questions", data.get("data", []))
    if not isinstance(data, list):
        log.warning(f"[qa] LLM 返回非 list，跳过问答：{type(data).__name__}")
        return []
    questions: list[ResumeQuestion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            questions.append(ResumeQuestion(**item))
        except Exception as e:
            log.debug(f"[qa] 跳过无法解析的问题项：{item} ({e})")
    # block 在前
    questions.sort(key=lambda q: 0 if q.severity == "block" else 1)
    log.info(f"[qa] 审查完成：{len(questions)} 个问题（block={sum(1 for q in questions if q.severity=='block')}, warn={sum(1 for q in questions if q.severity=='warn')}）")
    return questions


def present_questions(
    questions: list[ResumeQuestion],
    *,
    printer: Callable[[str], None] | None = None,
) -> None:
    """打印问题清单（rich 彩色；测试可注入 printer）。

    Args:
        questions: 问题清单。
        printer: 打印函数（默认用 rich 打印 markdown）。
    """
    if not questions:
        msg = "✅ 简历审查通过，无存疑点。"
        if printer is not None:
            printer(msg)
        else:
            print(msg)
        return
    md_lines = ["# 📋 简历存疑点审查\n"]
    for i, q in enumerate(questions, 1):
        icon = "🔴" if q.severity == "block" else "🟡"
        md_lines.append(f"## {icon} 问题 {i}（{q.category}，{q.severity}）")
        md_lines.append(f"**位置**：`{q.field_path}`\n")
        md_lines.append(f"**提问**：{q.question}\n")
        md_lines.append(f"**建议**：{q.suggestion}\n")
    md = "\n".join(md_lines)
    if printer is not None:
        printer(md)
        return
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        console = Console()
        console.print(Markdown(md))
        console.print(
            "\n[dim]请逐条回答（block 级必须回答，warn 级可回车跳过）。[/]"
        )
    except Exception:
        print(md)


def await_qa_answers(
    questions: list[ResumeQuestion],
    master_path: str,
    *,
    input_fn: Callable[[str], str] = input,
    non_interactive_default_accept: bool = False,
    logger_obj: Any = None,
) -> dict[str, str]:
    """CLI 逐条提问，返回 field_path → answer 映射。

    block 级问题必须回答（空输入重问）；warn 级可回车跳过。
    non_interactive 模式：自动跳过（warn 空，block 标记 'NEEDS_REVIEW'）。

    Args:
        questions: 问题清单。
        master_path: master.json 路径（用于日志提示）。
        input_fn: 输入函数（默认 input）。
        non_interactive_default_accept: True 则跳过所有提问（CI/--non-interactive）。
        logger_obj: loguru logger。

    Returns:
        field_path → answer 映射。
    """
    log = logger_obj or logger
    if non_interactive_default_accept:
        log.info("[qa] --non-interactive 模式，跳过问答（block 标记 NEEDS_REVIEW）")
        answers: dict[str, str] = {}
        for q in questions:
            if q.severity == "block":
                answers[q.field_path or q.question] = "NEEDS_REVIEW"
        return answers
    if not questions:
        return {}
    answers = {}
    print(f"\n简历解析完成，发现 {len(questions)} 个存疑点，请逐条回答：")
    print(f"（block 级必须回答，warn 级可直接回车跳过）\n")
    for i, q in enumerate(questions, 1):
        icon = "🔴" if q.severity == "block" else "🟡"
        print(f"{icon} 问题 {i}/{len(questions)}（{q.category}）")
        print(f"  位置：{q.field_path}")
        print(f"  提问：{q.question}")
        print(f"  建议：{q.suggestion}")
        while True:
            answer = input_fn(f"  你的回答> ").strip()
            if answer:
                break
            if q.severity == "warn":
                break  # warn 级可空答跳过
            print("  ⚠ block 级问题必须回答，请输入后回车。")
        key = q.field_path or q.question
        answers[key] = answer
        print()
    log.info(f"[qa] 用户回答了 {sum(1 for v in answers.values() if v)} / {len(questions)} 个问题")
    return answers


def apply_answers_to_master(
    master: dict[str, Any],
    answers: dict[str, str],
) -> dict[str, Any]:
    """将用户回答写入 master.json 的 qa_log 字段。

    master.json 的 Pydantic 模型用 ``extra="allow"``，新增 qa_log 字段兼容。
    回答仅记录（不自动修改原始字段），后续 LLM 定制时可参考。

    Args:
        master: master.json dict。
        answers: field_path → answer 映射。

    Returns:
        更新后的 master dict（新增 qa_log 字段）。
    """
    if not answers:
        return master
    master["qa_log"] = answers
    return master
