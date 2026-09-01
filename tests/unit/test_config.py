"""test_config.py — 配置加载 + 校验测试（设计 §5.2，6 case）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from boss_auto_apply.config import load_config
from boss_auto_apply.errors import ConfigValidationError


# ============================================================
# 辅助
# ============================================================
def _write_config(tmp_path: Path, data: dict, schema: dict | None = None) -> tuple[str, str]:
    """写 config.yaml + schema，返回 (config_path, schema_path)。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    if schema is None:
        # 用项目自带的 schema
        schema_path = str(Path(__file__).resolve().parent.parent.parent / "config" / "config.schema.json")
    else:
        schema_path = str(tmp_path / "schema.json")
        Path(schema_path).write_text(json.dumps(schema), encoding="utf-8")
    return str(cfg_path), schema_path


def _base_valid_config() -> dict:
    """最小合法 config。"""
    return {
        "pipeline": {
            "mode": "dry-run", "dry_run": True, "require_health_check": True,
            "require_target_confirmation": True, "dedupe_by_company": True,
            "exclude_companies": [], "require_jd_match": True, "skip_greeted": True,
        },
        "cities": ["上海", "武汉"],
        "targets": [],
        "target_constraints": {
            "primary_direction": "流量增长/SEO",
            "salary": {"上海": "12-18K", "武汉": "8-12K"},
            "degree": "大专",
            "experience": "3-5年",
            "sub_directions": [
                {"name": "SEO/网站运营", "weight": 0.5, "keywords_hint": ["SEO"]},
                {"name": "私域运营", "weight": 0.3, "keywords_hint": ["私域"]},
                {"name": "投放运营", "weight": 0.2, "keywords_hint": ["信息流"]},
            ],
        },
        "search": {"provider": "mock", "fixtures_dir": "tests/fixtures", "per_target_limit": 20},
        "llm": {
            "backend": "claude", "claude_bin": "claude",
            "model_tailor": "sonnet", "model_profiler": "sonnet",
            "model_hrbp": "sonnet", "model_matcher": "haiku",
            "model_greeter": "haiku",
        },
        "limits": {"daily_total": 15, "active_hours": [9, 18]},
        "pdf": {"template": "brilliant-cv", "font": "Noto Sans CJK SC", "typst_bin": "typst"},
        "paths": {
            "db": "data/jobs.db", "master_json": "data/master.json",
            "resume_input": "input/resume.pdf",
        },
    }


# ============================================================
# 测试用例
# ============================================================
def test_minimal_valid_config(tmp_path):
    """用例1：最小合法 config 通过。"""
    cfg_path, schema_path = _write_config(tmp_path, _base_valid_config())
    cfg = load_config(cfg_path, schema_path)
    assert cfg.pipeline.mode == "dry-run"
    assert cfg.cities == ["上海", "武汉"]
    assert cfg.llm.backend == "claude"
    assert cfg.llm.claude_bin == "claude"


def test_require_health_check_false_rejected(tmp_path):
    """用例2：require_health_check=False → 校验失败（ADR-0001）。"""
    data = _base_valid_config()
    data["pipeline"]["require_health_check"] = False
    cfg_path, schema_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigValidationError):
        load_config(cfg_path, schema_path)


def test_auto_mode_with_mock_search_rejected(tmp_path):
    """用例3：mode=auto 但 search.provider=mock → 失败（交叉校验）。"""
    data = _base_valid_config()
    data["pipeline"]["mode"] = "auto"
    data["pipeline"]["dry_run"] = False
    data["search"]["provider"] = "mock"
    cfg_path, schema_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigValidationError):
        load_config(cfg_path, schema_path)


def test_city_missing_salary_rejected(tmp_path):
    """用例4：cities 含薪资带缺失的城市 → 失败。"""
    data = _base_valid_config()
    data["cities"] = ["上海", "北京"]  # 北京不在 salary
    cfg_path, schema_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigValidationError):
        load_config(cfg_path, schema_path)


def test_missing_model_tailor_rejected(tmp_path):
    """用例5：缺 llm.model_tailor → 失败且错误信息可读。"""
    data = _base_valid_config()
    del data["llm"]["model_tailor"]
    cfg_path, schema_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigValidationError) as ei:
        load_config(cfg_path, schema_path)
    # 错误信息提到 model_tailor 或 required
    assert "model_tailor" in str(ei.value) or "required" in str(ei.value).lower()


def test_file_not_found(tmp_path):
    """用例6：YAML 文件不存在 → ConfigValidationError。"""
    with pytest.raises(ConfigValidationError):
        load_config(str(tmp_path / "nope.yaml"))


def test_codex_backend_config_accepted(tmp_path):
    """Codex 后端可选，且可使用 CLI 默认模型。"""
    data = _base_valid_config()
    data["llm"].update({"backend": "codex", "codex_bin": "codex", "codex_model": None})
    cfg_path, schema_path = _write_config(tmp_path, data)
    cfg = load_config(cfg_path, schema_path)
    assert cfg.llm.backend == "codex"
    assert cfg.llm.codex_bin == "codex"
    assert cfg.llm.codex_model is None


def test_active_hours_24_accepted_for_all_day(tmp_path):
    """用例7：active_hours=[0, 24] 全天配置通过 schema（maximum 已放宽到 24）。

    回归：曾因 schema maximum=23 导致 [0,24] 被拒，无法配置 24 小时运行。
    代码语义 end_h 是排他上界（hour >= end_h 拦截），全天需 [0, 24]。
    """
    data = _base_valid_config()
    data["limits"]["active_hours"] = [0, 24]
    cfg_path, schema_path = _write_config(tmp_path, data)
    cfg = load_config(cfg_path, schema_path)  # 不抛即通过
    assert cfg.limits["active_hours"] == [0, 24]


def test_active_hours_25_rejected(tmp_path):
    """用例8：active_hours 上限 25 仍被拒（schema 上限是 24，不是无限放宽）。"""
    data = _base_valid_config()
    data["limits"]["active_hours"] = [0, 25]
    cfg_path, schema_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigValidationError):
        load_config(cfg_path, schema_path)
