"""test_tailor_greet.py — F7 个性化招呼话术生成测试（v2 新增，9 case）。

测试 generate_greet_text + pick_projects_for_greet + render_greet_prompt。
"""

from __future__ import annotations

import pytest

from boss_auto_apply.core import tailor as tailor_mod
from boss_auto_apply.core.tailor import (
    generate_greet_text,
    pick_projects_for_greet,
    render_greet_prompt,
)


class _FakeJob:
    """JobRow 替身（只需 generate_greet_text 用到的字段）。"""
    def __init__(self, job_id="j1", title="保险产品经理", company="某保司",
                 skills_required=None):
        self.job_id = job_id
        self.title = title
        self.company = company
        self.skills_required = skills_required or ["保险", "投保"]


# ============================================================
# pick_projects_for_greet（4 case）
# ============================================================
def test_pick_projects_by_keyword_match():
    """用例1：按 per_job_keywords 命中数排序，返回 top-N。"""
    master = {
        "projects": [
            {"name": "保险产品矩阵", "description": "保险产品对接与投保漏斗", "skill_modules": []},
            {"name": "CRM 系统", "description": "客户关系管理", "skill_modules": []},
            {"name": "某保司投保系统", "description": "保单与签单保费", "skill_modules": []},
            {"name": "AI Agent 平台", "description": "LLM 应用", "skill_modules": []},
        ]
    }
    picked = pick_projects_for_greet(master, ["保险", "投保"], top_n=2)
    names = [p["name"] for p in picked]
    assert "保险产品矩阵" in names  # 命中 "保险" + "投保" 两个关键词
    assert "某保司投保系统" in names  # 命中 "投保"
    assert "CRM 系统" not in names
    assert "AI Agent 平台" not in names


def test_pick_projects_empty_keywords():
    """用例2：per_job_keywords 为空 → 返回空列表（不报错）。"""
    master = {"projects": [{"name": "X", "description": "y"}]}
    assert pick_projects_for_greet(master, []) == []
    assert pick_projects_for_greet(master, None) == []


def test_pick_projects_no_match():
    """用例3：关键词都不命中 → 返回空列表。"""
    master = {"projects": [{"name": "CRM", "description": "客户管理"}]}
    picked = pick_projects_for_greet(master, ["保险", "区块链"], top_n=2)
    assert picked == []


def test_pick_projects_top_n_limit():
    """用例4：top_n 限制返回数量（按命中数降序）。"""
    master = {
        "projects": [
            {"name": f"保险项目{i}", "description": "保险相关", "skill_modules": []}
            for i in range(5)
        ]
    }
    picked = pick_projects_for_greet(master, ["保险"], top_n=2)
    assert len(picked) == 2


# ============================================================
# render_greet_prompt（1 case）
# ============================================================
def test_render_greet_prompt_injects_all_fields():
    """用例5：render_greet_prompt 注入 job/master/projects/target/max_len。"""
    master = {"basics": {"name": "测试"}, "projects": [], "health_check_status": "passed"}
    projects = [{"name": "保险产品矩阵", "description": "投保漏斗优化。"}]
    prompt = render_greet_prompt(
        master, job_title="保险产品经理", job_company="某保司",
        jd_skills=["保险", "投保"], matched_projects=projects,
        target={"name": "保险科技产品经理", "per_job_keywords": ["保险"]},
        max_len=200,
    )
    assert "保险产品经理" in prompt
    assert "某保司" in prompt
    assert "保险产品矩阵" in prompt
    assert "投保漏斗优化" in prompt
    assert "200" in prompt  # max_len 占位符


# ============================================================
# generate_greet_text（4 case）
# ============================================================
def test_generate_greet_text_happy(fake_llm, passed_master):
    """用例6（happy）：LLM 返回正常招呼 → 返回文本，去 markdown 标记。"""
    fake_llm.responses[("greeter",)] = "看到贵司保险产品经理岗位，我在某公司做过保险产品矩阵与投保漏斗优化，希望进一步交流。"
    greet = generate_greet_text(
        _FakeJob(), passed_master,
        {"name": "保险科技产品经理", "per_job_keywords": ["保险"]},
        fake_llm,
    )
    assert greet is not None
    assert "保险产品经理" in greet
    assert "某公司" in greet


def test_generate_greet_text_strips_markdown(fake_llm, passed_master):
    """用例7：LLM 返回带 ```markdown 标记 → 去除。"""
    fake_llm.responses[("greeter",)] = "```\n看到贵司 XX 岗位，希望交流。\n```"
    greet = generate_greet_text(
        _FakeJob(title="XX"), passed_master,
        {"name": "T", "per_job_keywords": []},
        fake_llm,
    )
    assert greet is not None
    assert "```" not in greet
    assert "看到贵司" in greet


def test_generate_greet_text_forbidden_word_returns_none(fake_llm, passed_master):
    """用例8：LLM 输出含禁忌词「精通」→ 返回 None（走兜底）。"""
    fake_llm.responses[("greeter",)] = "看到贵司岗位，我精通保险业务，希望交流。"
    greet = generate_greet_text(
        _FakeJob(), passed_master,
        {"name": "T", "per_job_keywords": ["保险"]},
        fake_llm,
    )
    assert greet is None


def test_generate_greet_text_llm_exception_returns_none(fake_llm, passed_master):
    """用例9：LLM 抛异常 → 返回 None（兜底），不抛错。"""
    fake_llm.responses[("greeter",)] = RuntimeError("claude subprocess failed")
    greet = generate_greet_text(
        _FakeJob(), passed_master,
        {"name": "T", "per_job_keywords": ["保险"]},
        fake_llm,
    )
    assert greet is None


def test_generate_greet_text_truncates_overlong(fake_llm, passed_master):
    """用例10：超长 → 截断到最后一个句号。"""
    # 构造一段超长招呼（>100 字，无句号直到结尾）
    long_text = "看到贵司保险产品经理岗位" + "我在保险行业有很多经验" * 10 + "。希望交流。"
    fake_llm.responses[("greeter",)] = long_text
    greet = generate_greet_text(
        _FakeJob(), passed_master,
        {"name": "T", "per_job_keywords": ["保险"]},
        fake_llm,
        max_len=100,
    )
    assert greet is not None
    assert len(greet) <= 100


def test_generate_greet_text_empty_response_returns_none(fake_llm, passed_master):
    """用例11：LLM 返回空字符串 → 返回 None。"""
    fake_llm.responses[("greeter",)] = ""
    greet = generate_greet_text(
        _FakeJob(), passed_master,
        {"name": "T", "per_job_keywords": ["保险"]},
        fake_llm,
    )
    assert greet is None


def test_generate_greet_text_master_not_passed_returns_none(fake_llm, sample_master):
    """用例12：master 未体检 → 返回 None（不抛错）。"""
    # sample_master 是 pending
    greet = generate_greet_text(
        _FakeJob(), sample_master,
        {"name": "T", "per_job_keywords": ["保险"]},
        fake_llm,
    )
    assert greet is None
