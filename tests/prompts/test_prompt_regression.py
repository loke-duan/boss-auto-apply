"""test_prompt_regression.py — prompt 模板渲染快照测试（设计 §12.5，4 case）。

验证 prompt 模板渲染含关键约束文本，防回归。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_auto_apply.core.hrbp_check import render_hrbp_prompt
from boss_auto_apply.core.matcher import render_matcher_prompt
from boss_auto_apply.core.profiler import render_profiler_prompt
from boss_auto_apply.core.tailor import render_tailor_prompt

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "boss_auto_apply" / "prompts"


# ============================================================
# 测试用例
# ============================================================
def test_hrbp_prompt_contains_master_and_keywords(sample_master):
    """用例1：hrbp_check.md 渲染含 master 关键字段（张三/SEO/5.5）。"""
    prompt = render_hrbp_prompt(sample_master)
    assert "张三" in prompt
    assert "SEO" in prompt
    assert "5.5" in prompt
    # 含体检 6 项清单
    assert "工作年限诚信" in prompt
    assert "禁忌词扫描" in prompt


def test_profiler_prompt_contains_primary_direction(sample_master):
    """用例2：profiler.md 含 config 注入的主方向 + 子方向约束。"""
    constraints = {
        "primary_direction": "AI产品/金融科技产品经理",
        "salary": {"上海": "22-40K"},
        "sub_directions": [{"name": "AI产品经理", "weight": 0.4}],
    }
    prompt = render_profiler_prompt(sample_master, constraints)
    # config 的 primary_direction 应被注入 prompt（通过 target_constraints JSON）
    assert "AI产品/金融科技产品经理" in prompt
    assert "AI产品经理" in prompt
    assert "22-40K" in prompt


def test_tailor_prompt_contains_adr_and_standards(sample_master):
    """用例3：tailor.md 含 ADR-0002 三规则 + 4 条标准表述。"""
    prompt = render_tailor_prompt(sample_master, "JD 文本", {"name": "T"})
    # ADR-0002
    assert "ADR-0002" in prompt
    # 三规则关键词
    assert "可信" in prompt
    assert "可验证" in prompt
    assert "有基数参照" in prompt
    # 4 条标准表述（匿名化后用通用表述，与新 master.data_standards_locked 对齐）
    assert "稳定增长" in prompt
    assert "排名持续上升" in prompt
    assert "注册转化率显著提升" in prompt
    assert "线索流转闭环" in prompt
    # 禁忌词黑名单
    assert "精通" in prompt


def test_all_prompt_files_exist():
    """用例4：5 个 prompt 文件都存在（防误删）。"""
    for name in ("hrbp_check.md", "profiler.md", "tailor.md", "matcher.md", "_common_rules.md"):
        assert (PROMPTS_DIR / name).exists(), f"prompt 文件缺失：{name}"


def test_matcher_prompt_renders(sample_master):
    """matcher.md 渲染含 JD + skill_modules。"""
    prompt = render_matcher_prompt("需要 SEO 经验", sample_master.get("skill_modules", {}))
    assert "SEO" in prompt
    assert "match_score" in prompt or "匹配度" in prompt


def test_common_rules_contains_both_adrs():
    """_common_rules.md 含 ADR-0001 + ADR-0002。"""
    content = (PROMPTS_DIR / "_common_rules.md").read_text(encoding="utf-8")
    assert "ADR-0001" in content
    assert "ADR-0002" in content
