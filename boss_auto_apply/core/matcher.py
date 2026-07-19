"""F3 JD 分析与匹配（设计 §5.9）。

每个 JD → LLM 抽技能 + 计算与 master 匹配度 → 硬过滤（per_job_keywords 必须命中）→ 决定投/跳。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from ..errors import LlmJsonParseError

__all__ = [
    "MatchResult",
    "analyze_jd",
    "compute_jd_signature",
    "filter_jobs",
    "render_matcher_prompt",
]


@dataclass
class MatchResult:
    """F3 产物（v3，ADR-0005）。

    v3 新增字段：
    - job_category / category_passed：岗位类别判断（一票否决）
    - level_match：资历级别判断（overqualified/match/underqualified/unknown）
    - business_domain_match_score：业务领域连续值匹配度（0.0-1.0，替代旧的 AND 硬过滤）
    """

    job_id: str
    skills_required: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    match_score: float = 0.0
    worth_applying: bool = False
    reason: str = ""
    jd_signature: str = ""
    hard_filter_passed: bool = True
    # v3 新增（ADR-0005）
    job_category: str = ""
    category_passed: bool = True
    level_match: str = "unknown"  # overqualified|match|underqualified|unknown
    business_domain_match_score: float = 0.0


# ============================================================
# JD 聚类签名（§7.2）
# ============================================================
def compute_jd_signature(skills: list[str], target: dict[str, Any] | None = None) -> str:
    """JD 聚类签名：sorted(规范化 skills) + target.name → sha1 前 12 位。

    同 target 内、技能集相同的 JD 共享一份定制结果（降本）。

    Args:
        skills: 抽取的技能列表。
        target: 方向参数（取 name）。

    Returns:
        12 字符签名。
    """
    normalized = sorted({s.strip().lower() for s in skills if s and s.strip()})
    target_name = (target or {}).get("name", "") or ""
    raw = "|".join(normalized) + "|" + target_name
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ============================================================
# prompt 渲染
# ============================================================
def render_matcher_prompt(
    jd_text: str,
    skill_modules: dict[str, Any],
    job_title: str = "",
    candidate_profile: dict[str, Any] | None = None,
) -> str:
    """渲染 matcher.md prompt（v3，ADR-0005）。

    v3 变化：
    - 新增 ``candidate_profile`` 参数注入。matcher 不再依赖产品经理硬编码，
      全部从画像推导（target_role_keywords/exclusion_keywords/seniority_level/core_industries）。
    - 公式改为 0.75*hard_skill + 0.25*business_domain（资深 PM 方法论迁移是核心）。
    - hard_filter 改为基于 business_domain_match_score >= 0.5（连续值，消除与 score 的矛盾）。
    - 新增 level_match 维度（防 overqualified）。
    """
    p = Path(__file__).parent.parent / "prompts" / "matcher.md"
    template = p.read_text(encoding="utf-8")
    cp_json = json.dumps(candidate_profile or {}, ensure_ascii=False, indent=2)
    return (
        template
        .replace("{candidate_profile_json}", cp_json)
        .replace("{job_title}", job_title or "(未提供)")
        .replace("{jd_text}", jd_text)
        .replace("{skill_modules_json}", json.dumps(skill_modules, ensure_ascii=False, indent=2))
    )


# ============================================================
# analyze_jd
# ============================================================
def analyze_jd(
    job: Any,
    master: dict[str, Any],
    target: dict[str, Any],
    llm: Any,
    *,
    logger_obj: Any = None,
) -> MatchResult:
    """分析单个 JD：LLM 抽技能 + 匹配度 + 硬过滤。

    Args:
        job: ``JobRaw`` 或 ``JobRow``（需有 job_id/jd_full/skills_required）。
        master: master.json dict（用 skill_modules 匹配）。
        target: 方向参数（含 per_job_keywords）。
        llm: ``ClaudeClient``。
        logger_obj: loguru logger。

    Returns:
        ``MatchResult``。

    Raises:
        LlmJsonParseError: LLM 输出无法解析。
    """
    log = logger_obj or logger
    job_id = getattr(job, "job_id", "")
    jd_text = getattr(job, "jd_full", "") or ""
    # 若 JD 文本为空，尝试从 skills_required 兜底
    if not jd_text:
        jd_text = " ".join(getattr(job, "skills_required", []) or [])

    skill_modules = master.get("skill_modules", {}) or {}
    job_title = getattr(job, "title", "") or ""
    # v3（ADR-0005）：从 master.basics.candidate_profile 注入，取代产品经理硬编码
    candidate_profile = (
        master.get("basics", {}).get("candidate_profile") or {}
    )
    prompt = render_matcher_prompt(jd_text, skill_modules, job_title, candidate_profile)
    result = llm.invoke(
        prompt,
        expect_json=True,
        purpose="matcher",
        model=getattr(getattr(llm, "cfg", None), "model_matcher", None),
    )
    data = result.raw_json
    if not isinstance(data, dict):
        raise LlmJsonParseError(raw=result.text, expected_schema="MatchResult object")

    skills_required = data.get("skills_required", []) or []
    matched = data.get("matched_skills", []) or []
    missing = data.get("missing_skills", []) or []
    matched_business_domain = data.get("matched_business_domain", []) or []
    score = float(data.get("match_score", 0.0) or 0.0)

    # v3 新字段（ADR-0005）
    job_category = str(data.get("job_category", "") or "")
    category_passed = bool(data.get("category_passed", True))
    level_match = str(data.get("level_match", "unknown") or "unknown")
    if level_match not in ("overqualified", "match", "underqualified", "unknown"):
        level_match = "unknown"
    business_domain_match_score = float(data.get("business_domain_match_score", 0.0) or 0.0)

    # hard_filter 计算（v3，对齐 score）：
    # 主：LLM 输出的 business_domain_match_score >= 0.5
    # 兜底：LLM 未输出该字段（老 prompt 输出），退回旧 per_job_keywords AND 逻辑
    has_bds_field = "business_domain_match_score" in data
    if has_bds_field:
        hard_filter_passed = business_domain_match_score >= 0.5
    else:
        # 旧逻辑兜底（向后兼容）：target.per_job_keywords 每个 ∈ skills_required 或 matched_business_domain
        hard_filter_passed = bool(data.get("hard_filter_passed", True))
        per_job_keywords = target.get("per_job_keywords", []) or []
        if per_job_keywords:
            pool = list(skills_required) + list(matched_business_domain)
            pool_lower = {str(s).lower() for s in pool if s}
            for kw in per_job_keywords:
                kw_lower = kw.lower()
                if not any(kw_lower in sl or sl in kw_lower for sl in pool_lower):
                    hard_filter_passed = False
                    if kw not in missing:
                        missing.append(kw)
                    break

    worth_llm = bool(data.get("worth_applying", True))
    # v3（ADR-0005）worth 计算四要素：
    #   1. category_passed（岗位类别通过）
    #   2. level_pass（level_match ∈ match/unknown；overqualified/underqualified 拒绝）
    #   3. score >= 0.5
    #   4. hard_filter_passed
    # 四者同时满足才 worth=True。
    level_pass = level_match in ("match", "unknown")
    worth = (
        worth_llm
        and category_passed
        and level_pass
        and hard_filter_passed
        and score >= 0.5
    )
    reason = data.get("reason", "")

    sig = compute_jd_signature(skills_required, target)
    mr = MatchResult(
        job_id=job_id,
        skills_required=skills_required,
        matched_skills=matched,
        missing_skills=missing,
        match_score=score,
        worth_applying=worth,
        reason=reason,
        jd_signature=sig,
        hard_filter_passed=hard_filter_passed,
        job_category=job_category,
        category_passed=category_passed,
        level_match=level_match,
        business_domain_match_score=business_domain_match_score,
    )
    log.info(
        f"matcher job={job_id} score={score:.2f} worth={worth} "
        f"category={category_passed} level={level_match} "
        f"hard_filter={hard_filter_passed} bd_score={business_domain_match_score:.2f} sig={sig}"
    )
    return mr


# ============================================================
# filter_jobs
# ============================================================
def filter_jobs(
    jobs: list[Any],
    matcher_results: dict[str, MatchResult],
    *,
    min_score: float = 0.5,
) -> list[Any]:
    """worth_applying 且 score≥min_score 才进 F4。

    Args:
        jobs: job 列表。
        matcher_results: job_id → MatchResult。
        min_score: 最低匹配分。

    Returns:
        通过过滤的 job 列表。
    """
    out: list[Any] = []
    for j in jobs:
        jid = getattr(j, "job_id", "")
        mr = matcher_results.get(jid)
        if mr is None:
            continue
        if mr.worth_applying and mr.match_score >= min_score:
            out.append(j)
    return out
