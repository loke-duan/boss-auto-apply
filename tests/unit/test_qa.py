"""test_qa.py — 简历存疑点审查 + 交互问答测试（需求2，10+ case）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from boss_auto_apply.core import qa as qa_mod
from boss_auto_apply.core.qa import ResumeQuestion, apply_answers_to_master, await_qa_answers, present_questions, review_resume


# ============================================================
# Fake LLM
# ============================================================
class FakeLLM:
    """模拟 LLM 客户端。"""

    def __init__(self, response: Any = None):
        self.default_response = response
        self.cfg = type("Cfg", (), {"model_hrbp": "sonnet"})()

    def invoke(self, prompt, *, expect_json=False, purpose="", model=None):
        class Result:
            def __init__(self, resp):
                if isinstance(resp, (dict, list)):
                    self.raw_json = resp
                    self.text = json.dumps(resp, ensure_ascii=False)
                else:
                    self.raw_json = None
                    self.text = str(resp)
        return Result(self.default_response)


# ============================================================
# review_resume 测试
# ============================================================
def test_review_resume_returns_questions():
    """review_resume → LLM 返回问题清单。"""
    fake = FakeLLM([
        {"category": "gap", "field_path": "experiences[1].period",
         "question": "2020年有空窗？", "suggestion": "如实说明", "severity": "block"},
        {"category": "data_missing", "field_path": "projects[0].highlights[0]",
         "question": "数据缺失？", "suggestion": "补充量化", "severity": "warn"},
    ])
    master = {"basics": {"name": "张三"}}
    questions = review_resume(master, fake)
    assert len(questions) == 2
    # block 应排在前
    assert questions[0].severity == "block"
    assert questions[1].severity == "warn"


def test_review_resume_empty_list():
    """review_resume → LLM 返回空数组（无存疑点）。"""
    fake = FakeLLM([])
    questions = review_resume({"basics": {"name": "张三"}}, fake)
    assert questions == []


def test_review_resume_llm_failure_returns_empty():
    """review_resume → LLM 抛异常时返回空列表（不阻断流程）。"""
    class FailingLLM(FakeLLM):
        def invoke(self, *a, **kw):
            raise RuntimeError("LLM 不可用")
    questions = review_resume({"basics": {}}, FailingLLM())
    assert questions == []


def test_review_resume_llm_returns_dict_with_questions():
    """review_resume → LLM 返回 {"questions": [...]} 也能解析。"""
    fake = FakeLLM({"questions": [
        {"category": "skill_vague", "field_path": "skill_modules.a",
         "question": "技能模糊？", "suggestion": "补充工具", "severity": "warn"},
    ]})
    questions = review_resume({"basics": {}}, fake)
    assert len(questions) == 1
    assert questions[0].category == "skill_vague"


def test_review_resume_sorts_block_first():
    """review_resume → block 问题排在 warn 之前。"""
    fake = FakeLLM([
        {"category": "other", "severity": "warn", "question": "q1", "field_path": "a", "suggestion": "s"},
        {"category": "gap", "severity": "block", "question": "q2", "field_path": "b", "suggestion": "s"},
        {"category": "data_missing", "severity": "warn", "question": "q3", "field_path": "c", "suggestion": "s"},
    ])
    questions = review_resume({"basics": {}}, fake)
    assert questions[0].severity == "block"
    assert questions[1].severity == "warn"


# ============================================================
# present_questions 测试
# ============================================================
def test_present_questions_empty():
    """present_questions 空列表 → 打印通过消息。"""
    printed = []
    present_questions([], printer=printed.append)
    assert any("通过" in p for p in printed)


def test_present_questions_with_items():
    """present_questions 有问题 → 打印每个问题。"""
    qs = [
        ResumeQuestion(category="gap", field_path="exp[0]", question="空窗？",
                       suggestion="如实", severity="block"),
    ]
    printed = []
    present_questions(qs, printer=printed.append)
    combined = "\n".join(printed)
    assert "空窗" in combined
    assert "block" in combined


# ============================================================
# await_qa_answers 测试
# ============================================================
def test_await_qa_answers_non_interactive():
    """non_interactive 模式 → block 标记 NEEDS_REVIEW，warn 跳过。"""
    qs = [
        ResumeQuestion(category="gap", field_path="f1", question="q1",
                       suggestion="s", severity="block"),
        ResumeQuestion(category="other", field_path="f2", question="q2",
                       suggestion="s", severity="warn"),
    ]
    answers = await_qa_answers(qs, "master.json", non_interactive_default_accept=True)
    assert answers["f1"] == "NEEDS_REVIEW"
    # warn 级在 non_interactive 下不记录
    assert "f2" not in answers


def test_await_qa_answers_interactive():
    """交互模式 → input_fn 收集回答。"""
    qs = [
        ResumeQuestion(category="gap", field_path="f1", question="空窗？",
                       suggestion="如实", severity="block"),
        ResumeQuestion(category="other", field_path="f2", question="次要？",
                       suggestion="可选", severity="warn"),
    ]
    # input_fn 依次返回 "自由职业" 和 ""（warn 跳过）
    responses = iter(["自由职业", ""])
    answers = await_qa_answers(qs, "master.json", input_fn=lambda _: next(responses))
    assert answers["f1"] == "自由职业"
    assert answers["f2"] == ""


def test_await_qa_answers_block_requires_answer():
    """block 级空输入 → 重问直到有回答。"""
    qs = [
        ResumeQuestion(category="gap", field_path="f1", question="必答",
                       suggestion="s", severity="block"),
    ]
    responses = iter(["", "", "最终回答"])
    answers = await_qa_answers(qs, "master.json", input_fn=lambda _: next(responses))
    assert answers["f1"] == "最终回答"


def test_await_qa_answers_empty_questions():
    """空问题列表 → 空回答。"""
    answers = await_qa_answers([], "master.json", input_fn=lambda _: "")
    assert answers == {}


# ============================================================
# apply_answers_to_master 测试
# ============================================================
def test_apply_answers_adds_qa_log():
    """apply_answers → master 新增 qa_log 字段。"""
    master = {"basics": {"name": "张三"}}
    answers = {"f1": "自由职业", "f2": ""}
    result = apply_answers_to_master(master, answers)
    assert result["qa_log"] == answers
    # 原始字段不变
    assert result["basics"]["name"] == "张三"


def test_apply_answers_empty_no_change():
    """空回答 → master 不变（不新增 qa_log）。"""
    master = {"basics": {"name": "张三"}}
    result = apply_answers_to_master(master, {})
    assert "qa_log" not in result
