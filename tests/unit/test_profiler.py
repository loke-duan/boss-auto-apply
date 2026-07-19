"""test_profiler.py — F1.5 方向推荐测试（v2：LLM 自主推荐，9 case）。

v2 语义变更：
- 方向不再锁定（config sub_directions 降级为 keywords_hint，不强制）。
- validate_targets 校验维度：weight 总和 ≈ 1.0、city 合法、(sub_direction, city) 不重复、sub_direction 非空。
- 删除 SEO/投放专属 case（旧时代的硬编码方向），新增 v2 自主推荐 case。
"""

from __future__ import annotations

import yaml

import pytest

from boss_auto_apply.core import profiler
from boss_auto_apply.errors import HealthCheckFailedError, LlmJsonParseError
from boss_auto_apply.models import RecommendedTarget


def _constraints() -> dict:
    """v2 constraints：salary + degree + sub_directions（keywords_hint，可选池不强制）。"""
    return {
        "primary_direction": "AI产品/金融科技/保险科技产品经理",
        "salary": {"上海": "22-40K", "武汉": "15-25K"},
        "degree": "本科",
        "experience": "10年以上",
        # sub_directions 在 v2 只是 keywords_hint，LLM 可以推荐池外的方向
        "sub_directions": [
            {"name": "AI产品经理", "weight": 0.3, "keywords_hint": ["AI产品", "AIGC"]},
            {"name": "金融科技产品经理", "weight": 0.4, "keywords_hint": ["金融科技", "保险"]},
            {"name": "保险科技产品经理", "weight": 0.3, "keywords_hint": ["保险", "InsurTech"]},
        ],
    }


def _targets_response(
    override_sub: str | None = None,
    override_city: str | None = None,
    override_weight: float | None = None,
    missing_sub_direction: bool = False,
) -> dict:
    """3 个标准 target（AI/金融科技/保险科技 × 上海，weight 0.3/0.4/0.3 = 1.0）。"""
    subs_weights = [("AI产品经理", 0.3), ("金融科技产品经理", 0.4), ("保险科技产品经理", 0.3)]
    targets = []
    for sub, w in subs_weights:
        city = override_city if override_city else "上海"
        actual_sub = override_sub if override_sub else sub
        actual_w = override_weight if override_weight is not None else w
        targets.append({
            "name": f"{actual_sub}-{city}",
            "sub_direction": "" if missing_sub_direction else actual_sub,
            "city": city,
            "keywords": ["测试kw"],
            "per_job_keywords": ["测试"],
            "salary": "22-40K" if city == "上海" else "15-25K",
            "weight": actual_w,
            "reason": f"{sub}方向推荐，候选人 master 有相关项目支撑",
            "interview_safe": True,
        })
    return {"targets": targets, "strategy_note": ""}


# ============================================================
# 测试用例
# ============================================================
def test_profiler_requires_health_passed(fake_llm, sample_master):
    """用例1：master 未体检 → 抛 HealthCheckFailedError（前置闸门）。"""
    with pytest.raises(HealthCheckFailedError):
        profiler.run_profiler(sample_master, _constraints(), fake_llm)


def test_profiler_rejects_invalid_city(fake_llm, passed_master):
    """用例2：city 含非约束城市 → 校验失败（city 必须与薪资带绑定）。"""
    bad = _targets_response(override_city="北京")  # 北京不在 salary
    fake_llm.default_response = bad
    with pytest.raises((ValueError, LlmJsonParseError)):
        profiler.run_profiler(passed_master, _constraints(), fake_llm)


def test_weights_sum_must_be_one(fake_llm, passed_master):
    """用例3：weight 总和 ≠ 1.0 → 校验失败。

    v2 校验维度：weight 总和 ∈ [0.95, 1.05]。三个 target 都给 0.1（总和 0.3）应失败。
    """
    bad = _targets_response(override_weight=0.1)  # 总和 0.3，远低于 0.95
    fake_llm.default_response = bad
    with pytest.raises((ValueError, LlmJsonParseError)):
        profiler.run_profiler(passed_master, _constraints(), fake_llm)


def test_missing_sub_direction_rejected(fake_llm, passed_master):
    """用例4：sub_direction 为空字符串 → 校验失败（必须由 LLM 给出方向名）。"""
    bad = _targets_response(missing_sub_direction=True)
    fake_llm.default_response = bad
    with pytest.raises((ValueError, LlmJsonParseError)):
        profiler.run_profiler(passed_master, _constraints(), fake_llm)


def test_validate_targets_allows_custom_direction(fake_llm):
    """用例5（v2 新增）：LLM 推荐的方向不必 ∈ config sub_directions（自主推荐）。

    给出 config 池外的方向「运营增长产品经理」，只要 weight 总和=1.0 就应通过。
    """
    targets = [
        RecommendedTarget(name="运营增长-上海", sub_direction="运营增长产品经理",
                          city="上海", salary="22-40K", weight=1.0),
    ]
    violations = profiler.validate_targets(targets, _constraints())
    assert not violations, f"自主推荐的方向不应被拒：{violations}"


def test_valid_profiler_three_targets(fake_llm, passed_master):
    """用例6（happy）：mock LLM 返回合法 3 target（AI/金融科技/保险科技）→ run_profiler 成功。"""
    fake_llm.default_response = _targets_response()
    pr = profiler.run_profiler(passed_master, _constraints(), fake_llm)
    assert len(pr.targets) == 3
    subs = {t.sub_direction for t in pr.targets}
    assert subs == {"AI产品经理", "金融科技产品经理", "保险科技产品经理"}


def test_generate_search_targets_count(fake_llm, passed_master):
    """用例7：生成搜索参数 = 3 个 target → 3 套。"""
    fake_llm.default_response = _targets_response()
    pr = profiler.run_profiler(passed_master, _constraints(), fake_llm)
    params = profiler.generate_search_targets(pr.targets)
    assert len(params) == 3


def test_await_confirmation_writes_yaml(fake_llm, passed_master, tmp_path):
    """await_target_confirmation 写 targets.confirmed.yaml。"""
    targets = [RecommendedTarget(name="T", sub_direction="AI产品经理", city="上海",
                                 salary="22-40K", weight=1.0)]
    out = str(tmp_path / "targets.yaml")
    confirmed = profiler.await_target_confirmation(
        targets, out, input_fn=lambda _: "", non_interactive_default_accept=True,
    )
    assert len(confirmed) == 1
    with open(out, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["targets"][0]["name"] == "T"


def test_validate_targets_detects_duplicate(fake_llm):
    """validate_targets 检测重复 (sub_direction, city)。

    注意：v2 校验不锁方向，所以相同 sub_direction 不再算违规，
    但 (sub_direction, city) 重复仍应被检测。
    """
    t = [RecommendedTarget(name="A", sub_direction="AI产品经理", city="上海", weight=0.5),
         RecommendedTarget(name="A2", sub_direction="AI产品经理", city="上海", weight=0.5)]
    violations = profiler.validate_targets(t, _constraints())
    assert any("重复" in v for v in violations)
