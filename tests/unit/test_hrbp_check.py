"""test_hrbp_check.py — F1.6 HRBP 体检测试（设计 §5.6，8 case，mock llm）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_auto_apply.core import hrbp_check
from boss_auto_apply.errors import HealthCheckFailedError, LlmJsonParseError


def _standard_report() -> dict:
    """标准体检报告（LLM 返回）。"""
    return {
        "must_fix": [
            {"item": "工作年限写 7 年", "current": "7年", "should_be": "5.5年",
             "reason": "可验证经历仅 5.5 年", "adr": "G1", "severity": "red"},
        ],
        "suggest_fix": [
            {"item": "流量翻 N 倍", "current": "翻N倍", "should_be": "稳定增长",
             "reason": "基数不明", "adr": "ADR-0002", "severity": "yellow"},
        ],
        "need_user_input": [
            {"item": "自由职业期客户详情", "reason": "空窗期需填充", "severity": "warn"},
        ],
        "keep": ["注册转化率显著提升（已加行业基准）"],
        "red_lines_violated": [],
        "summary": "主要风险是工作年限与数据基数",
    }


# ============================================================
# 测试用例
# ============================================================
def test_run_health_check_parses_4_sections(fake_llm, sample_master):
    """用例1：mock LLM 返回标准报告 → run_health_check 正确解析 4 段。"""
    fake_llm.default_response = _standard_report()
    report = hrbp_check.run_health_check(sample_master, fake_llm)
    assert len(report.must_fix) == 1
    assert len(report.suggest_fix) == 1
    assert len(report.need_user_input) == 1
    assert report.keep == ["注册转化率显著提升（已加行业基准）"]
    assert "工作年限" in report.markdown


def test_must_fix_contains_workyears_when_seven(fake_llm):
    """用例2：master work_years=7 → 报告 must_fix 含年限项（由 LLM 判断，mock 模拟）。"""
    master = {"basics": {"name": "x", "work_years_total": 7}, "health_check_status": "pending",
              "constraints": {"work_years_authoritative": 5.5}}
    fake_llm.default_response = {
        "must_fix": [{"item": "工作年限 7 年", "current": "7", "should_be": "5.5",
                      "reason": "可验证仅 5.5", "severity": "red"}],
        "suggest_fix": [], "need_user_input": [], "keep": [],
        "red_lines_violated": [], "summary": "",
    }
    report = hrbp_check.run_health_check(master, fake_llm)
    assert any("工作年限" in it["item"] for it in report.must_fix)


def test_must_fix_contains_forbidden_words(fake_llm):
    """用例3：master 含「精通」「全链路」→ must_fix 含禁忌词。"""
    master = {"basics": {"name": "x", "work_years_total": 5.5, "degree": "大专"},
              "health_check_status": "pending",
              "skill_modules": {"a": {"summary": "精通全链路运营"}}}
    fake_llm.default_response = {
        "must_fix": [{"item": "含禁忌词 精通", "current": "精通", "should_be": "主导",
                      "reason": "面试陷阱", "severity": "red"}],
        "suggest_fix": [], "need_user_input": [], "keep": [],
        "red_lines_violated": ["精通"], "summary": "",
    }
    report = hrbp_check.run_health_check(master, fake_llm)
    assert report.has_red_lines


def test_validate_fixes_returns_empty_after_correction(sample_master):
    """用例4：用户修正年限后 → validate_fixes 返回空 → status=passed。"""
    report = hrbp_check.HealthReport(raw={})
    # sample_master 已是 5.5，无禁忌词，degree=本科
    uncorrected = hrbp_check.validate_fixes(sample_master, report)
    assert uncorrected == []


def test_validate_fixes_detects_workyears():
    """validate_fixes 检测 work_years != 5.5。"""
    master = {"basics": {"work_years_total": 7, "degree": "大专"},
              "constraints": {"work_years_authoritative": 5.5}}
    report = hrbp_check.HealthReport(raw={})
    uncorrected = hrbp_check.validate_fixes(master, report)
    assert any("工作年限" in u for u in uncorrected)


def test_validate_fixes_detects_forbidden_word():
    """validate_fixes 检测禁忌词。"""
    master = {"basics": {"work_years_total": 5.5, "degree": "大专"},
              "skill_modules": {"a": {"summary": "精通 SEO"}}, "constraints": {}}
    report = hrbp_check.HealthReport(raw={})
    uncorrected = hrbp_check.validate_fixes(master, report)
    assert any("精通" in u for u in uncorrected)


def test_skip_raises_when_red_lines(sample_master, tmp_path):
    """用例5/6：用户输入 skip 且有红线 → 抛 HealthCheckFailedError；红线不可 skip。"""
    report = hrbp_check.HealthReport(
        must_fix=[{"severity": "red", "item": "x"}],
        red_lines_violated=["x"],
        raw={},
    )
    report.markdown = "md"
    master_path = str(tmp_path / "master.json")
    Path(master_path).write_text(json.dumps(sample_master), encoding="utf-8")
    with pytest.raises(HealthCheckFailedError, match="红线"):
        hrbp_check.await_user_confirmation(
            report, master_path, input_fn=lambda _: "skip",
        )


def test_skip_no_red_lines_raises(sample_master, tmp_path):
    """无红线时 skip 也抛（用户主动放弃）。"""
    report = hrbp_check.HealthReport(raw={})
    report.markdown = "md"
    master_path = str(tmp_path / "master.json")
    Path(master_path).write_text(json.dumps(sample_master), encoding="utf-8")
    with pytest.raises(HealthCheckFailedError, match="skip"):
        hrbp_check.await_user_confirmation(report, master_path, input_fn=lambda _: "skip")


def test_bad_json_raises(fake_llm, sample_master):
    """用例7：mock LLM 返回坏 JSON → 抛 LlmJsonParseError。"""
    fake_llm.default_response = "not json"
    with pytest.raises(LlmJsonParseError):
        hrbp_check.run_health_check(sample_master, fake_llm)


def test_markdown_contains_adr_references(fake_llm, sample_master):
    """用例8：报告 markdown 含 ADR-0001/0002 引用。"""
    fake_llm.default_response = _standard_report()
    report = hrbp_check.run_health_check(sample_master, fake_llm)
    # 渲染的 markdown 应含 ADR（来自 suggest_fix 的 adr 字段 或 common_rules 注入的 prompt）
    # must_fix/suggest_fix 的 adr 字段会出现在 markdown
    assert "ADR-0002" in report.markdown or "G1" in report.markdown


def test_require_health_passed_gate(sample_master):
    """闸门：未体检 → 抛 HealthCheckFailedError。"""
    # sample_master 是 pending
    with pytest.raises(HealthCheckFailedError):
        hrbp_check.require_health_passed(sample_master)


def test_non_interactive_accepts(sample_master, tmp_path):
    """非交互模式默认接受 → status=passed。"""
    report = hrbp_check.HealthReport(raw={})
    report.markdown = "md"
    master_path = str(tmp_path / "master.json")
    Path(master_path).write_text(json.dumps(sample_master), encoding="utf-8")
    result = hrbp_check.await_user_confirmation(
        report, master_path, non_interactive_default_accept=True,
    )
    assert result["health_check_status"] == "passed"
