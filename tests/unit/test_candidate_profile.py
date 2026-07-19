"""test_candidate_profile.py — ADR-0005 候选人画像测试。

覆盖：
1. CandidateProfile Pydantic 模型校验（seniority 枚举、字段必填）
2. _sync_work_years_authoritative 强制工龄对齐
3. _is_candidate_profile_complete 完整性判断
4. derive_candidate_profile（F1.55）补全流程
5. hrbp_check._validate_candidate_profile 闸门校验
"""

from __future__ import annotations

import pytest

from boss_auto_apply.core import hrbp_check, parser
from boss_auto_apply.models import Basics, CandidateProfile


# ============================================================
# CandidateProfile Pydantic 模型
# ============================================================
class TestCandidateProfileModel:
    """CandidateProfile 模型校验。"""

    def test_valid_candidate_profile(self):
        """完整合法的 candidate_profile 应通过。"""
        cp = CandidateProfile(
            target_role_keywords=["产品经理", "PM", "Product Manager"],
            role_category="产品经理",
            seniority_level="资深",
            work_years_authoritative=14.0,
            core_industries=["保险", "金融科技"],
            exclusion_keywords=["助理", "实习", "销售"],
        )
        assert cp.role_category == "产品经理"
        assert cp.seniority_level == "资深"
        assert len(cp.target_role_keywords) == 3

    def test_seniority_level_enum(self):
        """seniority_level 必须是 5 档枚举之一。"""
        valid_levels = ("初级", "中级", "高级", "资深", "专家")
        for lvl in valid_levels:
            cp = CandidateProfile(seniority_level=lvl)
            assert cp.seniority_level == lvl

    def test_seniority_level_invalid(self):
        """非法 seniority_level 应抛 ValidationError。"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CandidateProfile(seniority_level="超资深")

    def test_exclusion_keywords_default_empty(self):
        """exclusion_keywords 默认空列表。"""
        cp = CandidateProfile()
        assert cp.exclusion_keywords == []

    def test_basics_optional_candidate_profile(self):
        """Basics.candidate_profile 是 Optional，老 master.json 兼容。"""
        b = Basics(name="张三", work_years_total=5)
        assert b.candidate_profile is None

    def test_basics_with_candidate_profile(self):
        """Basics 带 candidate_profile 时能正常装载。"""
        cp = CandidateProfile(
            target_role_keywords=["运营"],
            role_category="运营",
            seniority_level="中级",
            work_years_authoritative=5,
            core_industries=["互联网"],
        )
        b = Basics(name="张三", work_years_total=5, candidate_profile=cp)
        assert b.candidate_profile is not None
        assert b.candidate_profile.role_category == "运营"


# ============================================================
# _sync_work_years_authoritative（防 LLM 篡改工龄）
# ============================================================
class TestSyncWorkYears:
    """structure_with_llm 内部的工龄对齐逻辑。"""

    def test_sync_overrides_llm_value(self):
        """LLM 给的 work_years_authoritative 必须被 basics.work_years_total 覆盖。"""
        data = {
            "basics": {
                "work_years_total": 14,
                "candidate_profile": {
                    "work_years_authoritative": 99,  # LLM 篡改
                },
            }
        }
        parser._sync_work_years_authoritative(data)
        assert data["basics"]["candidate_profile"]["work_years_authoritative"] == 14.0

    def test_sync_skips_when_no_work_years_total(self):
        """basics.work_years_total 缺失时不强改。"""
        data = {"basics": {"candidate_profile": {"work_years_authoritative": 5}}}
        parser._sync_work_years_authoritative(data)
        assert data["basics"]["candidate_profile"]["work_years_authoritative"] == 5

    def test_sync_skips_when_no_candidate_profile(self):
        """无 candidate_profile 时静默跳过。"""
        data = {"basics": {"work_years_total": 10}}
        parser._sync_work_years_authoritative(data)  # 不抛
        assert "candidate_profile" not in data["basics"]


# ============================================================
# _is_candidate_profile_complete
# ============================================================
class TestIsComplete:
    """完整性判断。"""

    def test_complete_profile(self):
        """6 个字段都非空 → 完整。"""
        cp = {
            "target_role_keywords": ["PM"],
            "role_category": "产品经理",
            "seniority_level": "资深",
            "work_years_authoritative": 14,
            "core_industries": ["金融"],
        }
        assert parser._is_candidate_profile_complete(cp) is True

    def test_missing_target_keywords(self):
        """target_role_keywords 空 → 不完整。"""
        cp = {
            "target_role_keywords": [],
            "role_category": "产品经理",
            "seniority_level": "资深",
            "work_years_authoritative": 14,
            "core_industries": ["金融"],
        }
        assert parser._is_candidate_profile_complete(cp) is False

    def test_missing_role_category(self):
        """role_category 空 → 不完整。"""
        cp = {
            "target_role_keywords": ["PM"],
            "role_category": "",
            "seniority_level": "资深",
            "work_years_authoritative": 14,
            "core_industries": ["金融"],
        }
        assert parser._is_candidate_profile_complete(cp) is False

    def test_work_years_zero(self):
        """work_years_authoritative=0 → 不完整。"""
        cp = {
            "target_role_keywords": ["PM"],
            "role_category": "产品经理",
            "seniority_level": "资深",
            "work_years_authoritative": 0,
            "core_industries": ["金融"],
        }
        assert parser._is_candidate_profile_complete(cp) is False

    def test_none_input(self):
        """None 输入 → 不完整。"""
        assert parser._is_candidate_profile_complete(None) is False

    def test_non_dict_input(self):
        """非 dict 输入 → 不完整。"""
        assert parser._is_candidate_profile_complete(["bad"]) is False


# ============================================================
# derive_candidate_profile（F1.55 补全）
# ============================================================
class TestDeriveCandidateProfile:
    """F1.55 候选人画像补全流程。"""

    def test_skip_when_already_complete(self, fake_llm):
        """已有完整 candidate_profile 时跳过。"""
        master = {
            "basics": {
                "work_years_total": 14,
                "candidate_profile": {
                    "target_role_keywords": ["PM"],
                    "role_category": "产品经理",
                    "seniority_level": "资深",
                    "work_years_authoritative": 14,
                    "core_industries": ["金融"],
                },
            },
        }
        parser.derive_candidate_profile(master, fake_llm)
        # 未调用 LLM
        assert fake_llm.invoke_count == 0

    def test_derive_when_missing(self, fake_llm):
        """candidate_profile 缺失时调 LLM 补全。"""
        fake_llm.default_response = {
            "target_role_keywords": ["产品经理", "PM", "Product Manager"],
            "role_category": "产品经理",
            "seniority_level": "资深",
            "work_years_authoritative": 14,
            "core_industries": ["保险", "金融科技"],
            "exclusion_keywords": ["助理", "实习"],
        }
        master = {
            "basics": {"work_years_total": 14, "headline": "产品经理 | 14年"},
            "skill_modules": {},
            "projects": [],
        }
        parser.derive_candidate_profile(master, fake_llm)
        assert fake_llm.invoke_count == 1
        cp = master["basics"]["candidate_profile"]
        assert cp["role_category"] == "产品经理"
        assert cp["seniority_level"] == "资深"

    def test_derive_forces_work_years_alignment(self, fake_llm):
        """补全时强制 work_years_authoritative == work_years_total。"""
        fake_llm.default_response = {
            "target_role_keywords": ["PM"],
            "role_category": "产品经理",
            "seniority_level": "资深",
            "work_years_authoritative": 99,  # LLM 篡改
            "core_industries": ["金融"],
        }
        master = {"basics": {"work_years_total": 12, "headline": "PM"}}
        parser.derive_candidate_profile(master, fake_llm)
        assert master["basics"]["candidate_profile"]["work_years_authoritative"] == 12.0

    def test_derive_handles_bad_llm_output(self, fake_llm):
        """LLM 返回非 dict 时静默失败（保持原 master 不变）。"""
        fake_llm.default_response = "not json"
        master = {"basics": {"work_years_total": 10}}
        result = parser.derive_candidate_profile(master, fake_llm)
        # 返回原 master 对象，candidate_profile 未设
        assert "candidate_profile" not in result.get("basics", {})


# ============================================================
# hrbp_check._validate_candidate_profile（闸门校验）
# ============================================================
class TestHrbpValidateCandidateProfile:
    """体检闸门对 candidate_profile 的结构性校验。"""

    def test_valid_profile_no_violations(self):
        """合法 candidate_profile 无违规。"""
        master = {
            "basics": {
                "work_years_total": 14,
                "candidate_profile": {
                    "target_role_keywords": ["产品经理", "PM", "Product Manager"],
                    "role_category": "产品经理",
                    "seniority_level": "资深",
                    "work_years_authoritative": 14,
                    "core_industries": ["保险", "金融科技"],
                },
            },
        }
        assert hrbp_check._validate_candidate_profile(master) == []

    def test_missing_profile(self):
        """candidate_profile 缺失。"""
        master = {"basics": {"work_years_total": 14}}
        violations = hrbp_check._validate_candidate_profile(master)
        assert any("candidate_profile 缺失" in v for v in violations)

    def test_target_keywords_too_few(self):
        """target_role_keywords 少于 3 个。"""
        master = {
            "basics": {
                "work_years_total": 14,
                "candidate_profile": {
                    "target_role_keywords": ["PM"],  # 仅 1 个
                    "role_category": "产品经理",
                    "seniority_level": "资深",
                    "work_years_authoritative": 14,
                    "core_industries": ["金融"],
                },
            },
        }
        violations = hrbp_check._validate_candidate_profile(master)
        assert any("target_role_keywords 数量" in v for v in violations)

    def test_invalid_seniority(self):
        """seniority_level 非法枚举。"""
        master = {
            "basics": {
                "work_years_total": 14,
                "candidate_profile": {
                    "target_role_keywords": ["PM", "产品经理", "Product Manager"],
                    "role_category": "产品经理",
                    "seniority_level": "超资深",
                    "work_years_authoritative": 14,
                    "core_industries": ["金融"],
                },
            },
        }
        violations = hrbp_check._validate_candidate_profile(master)
        assert any("seniority_level" in v for v in violations)

    def test_work_years_mismatch(self):
        """work_years_authoritative 与 work_years_total 不一致。"""
        master = {
            "basics": {
                "work_years_total": 14,
                "candidate_profile": {
                    "target_role_keywords": ["PM", "产品经理", "Product Manager"],
                    "role_category": "产品经理",
                    "seniority_level": "资深",
                    "work_years_authoritative": 5,  # 不一致
                    "core_industries": ["金融"],
                },
            },
        }
        violations = hrbp_check._validate_candidate_profile(master)
        assert any("不一致" in v for v in violations)
