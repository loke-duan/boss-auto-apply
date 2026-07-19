"""test_matcher.py — F3 JD 抽取与匹配测试（v3，ADR-0005）。

v3 覆盖：
1. 旧版 score/hard_filter 基础逻辑（向后兼容）
2. v3 新字段：candidate_profile 注入 / category_passed / level_match / business_domain_match_score
3. 兜底：LLM 未输出 business_domain_match_score 时走旧 per_job_keywords 逻辑
4. worth 四要素：category_passed + level_match + hard_filter + score
"""

from __future__ import annotations

import pytest

from boss_auto_apply.core import matcher as matcher_mod
from boss_auto_apply.core.matcher import MatchResult, analyze_jd, compute_jd_signature, filter_jobs, render_matcher_prompt
from boss_auto_apply.errors import LlmJsonParseError


class _FakeJob:
    """测试用 job 替身。"""
    def __init__(self, job_id="j1", jd_full="", skills_required=None, title=""):
        self.job_id = job_id
        self.jd_full = jd_full
        self.skills_required = skills_required or []
        self.title = title


def _matcher_response(
    skills,
    score=0.8,
    worth=True,
    hard=True,
    *,
    category_passed=True,
    level_match="match",
    job_category="产品经理",
    business_domain_match_score=1.0,
    include_bds_field=True,
) -> dict:
    """v3 matcher response 模板。

    include_bds_field=False → 模拟老 LLM 输出（无 business_domain_match_score 字段），
    测试兜底走旧 per_job_keywords 逻辑。
    """
    resp = {
        "skills_required": skills,
        "matched_skills": skills[:-1] if len(skills) > 1 else skills,
        "missing_skills": [],
        "match_score": score,
        "worth_applying": worth,
        "reason": "匹配",
        "hard_filter_passed": hard,
        "category_passed": category_passed,
        "level_match": level_match,
        "job_category": job_category,
        "matched_business_domain": ["保险"] if business_domain_match_score > 0 else [],
    }
    if include_bds_field:
        resp["business_domain_match_score"] = business_domain_match_score
    return resp


# v3 测试用 candidate_profile（产品经理示例）
_PM_PROFILE = {
    "target_role_keywords": ["产品经理", "PM", "Product Manager"],
    "role_category": "产品经理",
    "seniority_level": "资深",
    "work_years_authoritative": 14,
    "core_industries": ["保险", "金融科技"],
    "exclusion_keywords": ["助理", "实习", "销售"],
}


def _pm_master(profile=None):
    """带 candidate_profile 的 master dict。"""
    return {
        "basics": {
            "work_years_total": 14,
            "candidate_profile": profile or _PM_PROFILE,
        },
        "skill_modules": {"product_design": {"summary": "PM"}},
    }


# ============================================================
# 旧版基础逻辑（向后兼容）
# ============================================================
def test_jd_with_seo_high_score(fake_llm):
    """JD 含 SEO/网站运营 → score 高，worth_applying=True。"""
    fake_llm.default_response = _matcher_response(["SEO", "网站运营", "长尾词布局"], score=0.85)
    job = _FakeJob(jd_full="需要 SEO 和网站运营经验", title="SEO 经理")
    master = _pm_master()
    target = {"name": "SEO-上海", "per_job_keywords": []}
    mr = analyze_jd(job, master, target, fake_llm)
    assert mr.match_score >= 0.5
    assert mr.worth_applying is True
    assert mr.jd_signature  # 有签名


def test_jd_signature_same_for_same_skills():
    """两份 JD 技能集相同 → jd_signature 相同（聚类命中）。"""
    skills = ["SEO", "网站运营", "长尾词布局"]
    target = {"name": "SEO-上海"}
    sig1 = compute_jd_signature(skills, target)
    sig2 = compute_jd_signature(list(reversed(skills)), target)
    assert sig1 == sig2
    assert len(sig1) == 12


def test_jd_signature_diff_for_diff_target():
    """不同 target → 不同签名。"""
    skills = ["SEO"]
    sig1 = compute_jd_signature(skills, {"name": "SEO-上海"})
    sig2 = compute_jd_signature(skills, {"name": "私域-上海"})
    assert sig1 != sig2


def test_bad_json_raises(fake_llm):
    """mock LLM 坏 JSON → LlmJsonParseError。"""
    fake_llm.default_response = "not json"
    job = _FakeJob(jd_full="x")
    with pytest.raises(LlmJsonParseError):
        analyze_jd(job, _pm_master(), {"name": "T"}, fake_llm)


def test_filter_jobs_min_score():
    """score < 0.5 → 被 filter_jobs 过滤。"""
    jobs = [_FakeJob(job_id="j_low"), _FakeJob(job_id="j_high")]
    results = {
        "j_low": MatchResult(job_id="j_low", match_score=0.3, worth_applying=True),
        "j_high": MatchResult(job_id="j_high", match_score=0.8, worth_applying=True),
    }
    out = filter_jobs(jobs, results, min_score=0.5)
    ids = {j.job_id for j in out}
    assert "j_high" in ids
    assert "j_low" not in ids


def test_matcher_result_worth_applying_threshold(fake_llm):
    """score == 0.5 边界 → worth_applying（>=0.5）。"""
    fake_llm.default_response = _matcher_response(["SEO"], score=0.5, worth=True, hard=True)
    job = _FakeJob(jd_full="SEO", title="SEO 经理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.worth_applying is True


# ============================================================
# v3 兜底：LLM 未输出 business_domain_match_score → 旧 per_job_keywords 逻辑
# ============================================================
def test_fallback_per_job_keywords_when_no_bds(fake_llm):
    """LLM 未输出 business_domain_match_score 时走旧 AND 硬过滤。

    场景：target.per_job_keywords=['SEO'] 但 JD 无 SEO → hard_filter=False → worth=False
    （测试 v3 兜底分支：matcher.py:has_bds_field=False）
    """
    fake_llm.default_response = _matcher_response(
        ["Java", "Spring"], score=0.9, worth=True,
        include_bds_field=False,  # 模拟老 LLM 输出
    )
    job = _FakeJob(jd_full="Java 后端", title="Java 开发")
    target = {"name": "SEO-上海", "per_job_keywords": ["SEO"]}
    mr = analyze_jd(job, _pm_master(), target, fake_llm)
    assert mr.worth_applying is False
    assert mr.hard_filter_passed is False
    assert "SEO" in mr.missing_skills


# ============================================================
# v3 新维度：category_passed（岗位类别一票否决）
# ============================================================
def test_category_passed_false_blocks_worth(fake_llm):
    """category_passed=false → worth=False（即使 score 高）。"""
    fake_llm.default_response = _matcher_response(
        ["沟通", "办公软件"], score=0.85, worth=True,
        category_passed=False, job_category="业务助理",
    )
    job = _FakeJob(jd_full="业务助理岗", title="业务助理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.category_passed is False
    assert mr.worth_applying is False  # 即使 score=0.85 worth_llm=True


def test_category_passed_true_with_high_score(fake_llm):
    """category_passed=true + score 高 + 业务领域强命中 → worth=True。"""
    fake_llm.default_response = _matcher_response(
        ["PRD", "A/B实验", "保险业务"], score=0.85, worth=True,
        category_passed=True, business_domain_match_score=1.0,
    )
    job = _FakeJob(jd_full="保险产品经理岗", title="保险产品经理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.category_passed is True
    assert mr.worth_applying is True


# ============================================================
# v3 新维度：level_match（防 overqualified）
# ============================================================
def test_level_match_overqualified_blocks_worth(fake_llm):
    """level_match=overqualified → worth=False（即使 score 高）。"""
    fake_llm.default_response = _matcher_response(
        ["数据处理"], score=0.85, worth=True,
        level_match="overqualified",  # 14 年 PM 投业务助理
        category_passed=False, business_domain_match_score=1.0,
    )
    job = _FakeJob(jd_full="业务助理", title="业务助理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.level_match == "overqualified"
    assert mr.worth_applying is False


def test_level_match_unknown_passes(fake_llm):
    """level_match=unknown（JD 无明确级别信号）→ 放行（level_pass=True）。"""
    fake_llm.default_response = _matcher_response(
        ["PRD"], score=0.7, worth=True,
        level_match="unknown",
        category_passed=True, business_domain_match_score=0.8,
    )
    job = _FakeJob(jd_full="产品经理", title="产品经理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.level_match == "unknown"
    # level_pass=True（unknown 视为放行）
    assert mr.worth_applying is True


def test_level_match_underqualified_blocks_worth(fake_llm):
    """level_match=underqualified → worth=False。"""
    fake_llm.default_response = _matcher_response(
        ["PRD"], score=0.7, worth=True,
        level_match="underqualified",
        category_passed=True, business_domain_match_score=0.8,
    )
    job = _FakeJob(jd_full="资深产品经理", title="资深产品经理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.worth_applying is False


def test_invalid_level_match_normalizes_to_unknown(fake_llm):
    """非法 level_match 字符串 → 归一化为 unknown。"""
    fake_llm.default_response = _matcher_response(
        ["PRD"], score=0.7, worth=True,
        level_match="超资深",  # 非法
        category_passed=True, business_domain_match_score=0.8,
    )
    job = _FakeJob(jd_full="产品经理", title="产品经理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.level_match == "unknown"


# ============================================================
# v3 新维度：business_domain_match_score（hard_filter 对齐 score）
# ============================================================
def test_hard_filter_true_when_bds_above_threshold(fake_llm):
    """business_domain_match_score=0.6（>=0.5）→ hard_filter=True。"""
    fake_llm.default_response = _matcher_response(
        ["PRD"], score=0.7, worth=True,
        business_domain_match_score=0.6,
        category_passed=True,
    )
    job = _FakeJob(jd_full="产品经理", title="产品经理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.hard_filter_passed is True


def test_hard_filter_false_when_bds_below_threshold(fake_llm):
    """business_domain_match_score=0.3（<0.5）→ hard_filter=False（即使 score 高）。

    场景：C 端 PM 业务领域不匹配但硬技能强 → 应被 hard_filter 拦下。
    """
    fake_llm.default_response = _matcher_response(
        ["小程序", "PRD", "A/B"], score=0.75, worth=True,  # LLM 想 worth
        business_domain_match_score=0.0,  # 业务不相关
        category_passed=True,
    )
    job = _FakeJob(jd_full="文旅产品经理", title="产品经理")
    mr = analyze_jd(job, _pm_master(), {"name": "T", "per_job_keywords": []}, fake_llm)
    assert mr.hard_filter_passed is False
    assert mr.worth_applying is False  # hard_filter=False 阻断 worth


# ============================================================
# v3：render_matcher_prompt 注入 candidate_profile
# ============================================================
def test_render_matcher_prompt_injects_candidate_profile():
    """render_matcher_prompt 应把 candidate_profile 注入到 prompt 中。"""
    prompt = render_matcher_prompt(
        jd_text="测试 JD",
        skill_modules={"x": {"summary": "y"}},
        job_title="产品经理",
        candidate_profile=_PM_PROFILE,
    )
    # candidate_profile 的内容应出现在 prompt 中
    assert "产品经理" in prompt
    assert "资深" in prompt
    assert "测试 JD" in prompt
    # 旧的硬编码"候选人 14 年产品经理经验"不应再出现（v3 已删除）
    assert "候选人 14 年产品经理经验" not in prompt


def test_render_matcher_prompt_handles_missing_profile():
    """candidate_profile=None 时 prompt 也能正常渲染（空对象兜底）。"""
    prompt = render_matcher_prompt(
        jd_text="JD", skill_modules={}, job_title="x",
        candidate_profile=None,
    )
    assert "JD" in prompt


# ============================================================
# v3：MatchResult 新字段默认值
# ============================================================
def test_match_result_v3_defaults():
    """MatchResult v3 新字段默认值。"""
    mr = MatchResult(job_id="x")
    assert mr.job_category == ""
    assert mr.category_passed is True
    assert mr.level_match == "unknown"
    assert mr.business_domain_match_score == 0.0
