"""test_searcher.py — F2 搜索器测试（设计 §5.8，7 case）。"""

from __future__ import annotations

import json

import pytest

from boss_auto_apply.core import searcher as searcher_mod
from boss_auto_apply.core.searcher import (
    BossCliSearcher,
    JobRaw,
    MockBossSearcher,
    SearchParams,
    dedupe_jobs,
    get_searcher,
)
from boss_auto_apply.errors import SearcherError


# ============================================================
# MockBossSearcher
# ============================================================
def test_mock_search_keyword_filter(fixtures_dir):
    """用例1：Mock searcher 按 keyword 过滤 fixtures 正确。"""
    s = MockBossSearcher(fixtures_dir)
    params = SearchParams(keyword="SEO", city="上海", limit=10)
    results = s.search(params)
    assert len(results) > 0
    assert all(r.city == "上海" for r in results)
    # title 应含 SEO 或 jd_full 含
    assert any("SEO" in (r.title + r.jd_full) for r in results)


def test_mock_search_limit_truncation(fixtures_dir):
    """用例2：limit 截断生效。"""
    s = MockBossSearcher(fixtures_dir)
    params = SearchParams(keyword="", city="上海", limit=2)
    results = s.search(params)
    assert len(results) <= 2


def test_mock_fetch_detail(fixtures_dir):
    """用例7：fetch_detail 正常返回 jd_full。"""
    s = MockBossSearcher(fixtures_dir)
    jd = s.fetch_detail("mock-seo-sh-001")
    assert "SEO" in jd


def test_mock_fetch_detail_not_found(fixtures_dir):
    """fetch_detail 不存在的 job_id → SearcherError。"""
    s = MockBossSearcher(fixtures_dir)
    with pytest.raises(SearcherError):
        s.fetch_detail("no-such-id")


def test_mock_greet_returns_true(fixtures_dir):
    """Mock greet 永远成功（dry-run）。"""
    s = MockBossSearcher(fixtures_dir)
    assert s.greet("any", "hi") is True


# ============================================================
# 去重 / 黑名单
# ============================================================
def test_dedupe_by_job_id_and_company():
    """用例3/4：重复 search 同 params → 去重；dedupe_by_company 同公司留一条。"""
    jobs = [
        JobRaw(job_id="j1", title="SEO", company="A", city="上海"),
        JobRaw(job_id="j1", title="SEO", company="A", city="上海"),  # 重复 job_id
        JobRaw(job_id="j2", title="SEO2", company="A", city="上海"),  # 同公司
        JobRaw(job_id="j3", title="私域", company="B", city="上海"),
    ]
    # by_company=True，A 公司留一条（j1，因 score 默认 0 取第一个）
    out = dedupe_jobs(jobs, by_company=True)
    companies = {j.company for j in out}
    assert companies == {"A", "B"}
    # A 公司只一条
    a_jobs = [j for j in out if j.company == "A"]
    assert len(a_jobs) == 1


def test_exclude_companies():
    """用例5：exclude_companies 生效。"""
    jobs = [
        JobRaw(job_id="j1", title="SEO", company="BlackCo", city="上海"),
        JobRaw(job_id="j2", title="SEO", company="GoodCo", city="上海"),
    ]
    out = dedupe_jobs(jobs, exclude_companies=["BlackCo"])
    assert all(j.company != "BlackCo" for j in out)
    assert len(out) == 1


# ============================================================
# get_searcher 降级
# ============================================================
def test_get_searcher_boss_cli_fallback_to_mock(fixtures_dir):
    """用例6：get_searcher('boss-cli') 在 boss-cli 未装时 → 降级 mock（不抛错）。"""
    s = get_searcher("boss-cli", fixtures_dir=fixtures_dir, bin_path="boss-not-installed-xyz")
    assert isinstance(s, MockBossSearcher)


def test_get_searcher_mock(fixtures_dir):
    """get_searcher('mock') 返回 MockBossSearcher。"""
    s = get_searcher("mock", fixtures_dir=fixtures_dir)
    assert isinstance(s, MockBossSearcher)


def test_get_searcher_unknown_raises():
    """未知 provider 抛 ValueError。"""
    with pytest.raises(ValueError):
        get_searcher("unknown")


# ============================================================
# BossCliSearcher（改造2：jackwener/boss-cli，greet 可用）
# ============================================================
def test_boss_cli_greet_dry_run_returns_true():
    """BossCliSearcher.greet 在 dry_run=True 时返回 True（不真调 boss）。"""
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type("P", (), {"stdout": json.dumps({"ok": True, "data": {}}), "stderr": "", "returncode": 0})()
    s = BossCliSearcher(bin_path="boss", dry_run=True, run_fn=fake_run)
    assert s.greet("j1", "hi") is True
    assert calls == []  # dry_run 不真调 subprocess


def test_boss_cli_greet_non_dry_run_calls_boss(monkeypatch):
    """BossCliSearcher.greet 在 dry_run=False 时真调 `boss greet <id> --json`。"""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/boss")
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type("P", (), {"stdout": json.dumps({"ok": True, "data": {"sent": True}}), "stderr": "", "returncode": 0})()
    s = BossCliSearcher(bin_path="boss", dry_run=False, run_fn=fake_run)
    assert s.greet("sec-id-123", "你好") is True
    assert len(calls) == 1
    assert calls[0][0] == "boss"
    assert calls[0][1] == "greet"
    assert calls[0][2] == "sec-id-123"
    assert "--json" in calls[0]


def test_boss_cli_search_bin_not_found():
    """boss 命令未装 → search 抛 SearcherError。"""
    s = BossCliSearcher(bin_path="boss-not-installed-xyz")
    with pytest.raises(SearcherError, match="未找到"):
        s.search(SearchParams(keyword="SEO", city="上海"))


def test_boss_cli_parse_envelope(monkeypatch):
    """BossCliSearcher 正确解析 boss 信封（jackwener 驼峰字段 + --json）。"""
    envelope = {"ok": True, "data": [
        {"securityId": "s1", "jobName": "SEO", "brandName": "Co", "city": "上海",
         "salary": "12-18K", "jd_full": "JD"},
    ]}
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type("P", (), {"stdout": json.dumps(envelope), "stderr": "", "returncode": 0})()
    s = BossCliSearcher(bin_path="boss", run_fn=fake_run)
    # 跳过 _check_bin：monkeypatch which
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/boss")
    results = s.search(SearchParams(keyword="SEO", city="上海"))
    assert len(results) == 1
    assert results[0].job_id == "s1"
    assert results[0].title == "SEO"
    assert results[0].company == "Co"
    # cmd 含 --json（jackwener 契约）
    assert "--json" in calls[0]
    assert calls[0][0] == "boss"
    assert calls[0][1] == "search"


def test_boss_cli_envelope_not_ok_raises(monkeypatch):
    """boss 信封 ok=False（not_authenticated）→ 抛 SearcherError。"""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/boss")
    envelope = {"ok": False, "error": {"code": "not_authenticated", "message": "未登录"}}
    def fake_run(cmd, **kw):
        return type("P", (), {"stdout": json.dumps(envelope), "stderr": "", "returncode": 0})()
    s = BossCliSearcher(bin_path="boss", run_fn=fake_run)
    with pytest.raises(SearcherError, match="未登录"):
        s.search(SearchParams(keyword="SEO", city="上海"))
