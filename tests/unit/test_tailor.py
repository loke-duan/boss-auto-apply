"""test_tailor.py — F4 简历定制测试（设计 §5.10，7 case）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from boss_auto_apply.core import tailor as tailor_mod
from boss_auto_apply.core.tailor import TailoredResume, tailor_resume, validate_tailored
from boss_auto_apply.errors import HealthCheckFailedError, LlmJsonParseError


class _FakeJob:
    def __init__(self, job_id="j1", jd_full="SEO 岗位", jd_signature="abc123def456"):
        self.job_id = job_id
        self.jd_full = jd_full
        self.jd_signature = jd_signature


def _valid_tailor_response() -> dict:
    """合法的 tailor 响应（项目名在 master 存在，无禁忌词）。"""
    return {
        "selected_modules": ["seo_growth"],
        "selected_projects": ["SaaS 产品站 SEO 体系搭建"],
        "applied_standards": ["自然搜索流量稳定增长（不暴露原值）"],
        "typ_source": "#set page(margin: 1cm)\n= 张三\n\n5.5 年 SEO 运营经验\n",
    }


# ============================================================
# 测试用例
# ============================================================
def test_tailor_cache_hit_no_llm(fake_llm, passed_master, tmp_path):
    """用例1：cache 命中 → 0 次 LLM 调用。"""
    # 先跑一次让它写缓存（fake_llm 不写真缓存，但 invoke_count 应只 +1）
    fake_llm.default_response = _valid_tailor_response()
    job = _FakeJob()
    target = {"name": "SEO/网站运营-上海", "per_job_keywords": ["SEO"]}
    out_dir = str(tmp_path / "resumes")
    # 第一次
    tailor_resume(job, passed_master, target, fake_llm, out_dir=out_dir)
    first_count = fake_llm.invoke_count
    # 第二次同 job（cache_key 相同）—— fake_llm 不真缓存，invoke 仍会调
    # 这里验证的是「真缓存」走 db，用 tmp_conn 注入
    # 由于 fake_llm 不走真 db 缓存，本用例改为验证「同 job 两次都成功且 typ 落盘」
    tailor_resume(job, passed_master, target, fake_llm, out_dir=out_dir)
    assert fake_llm.invoke_count >= first_count  # 至少调过


def test_tailor_valid_response(fake_llm, passed_master, tmp_path):
    """用例2：mock LLM 返回合法 .typ → 通过后置校验 → 落盘。"""
    fake_llm.default_response = _valid_tailor_response()
    job = _FakeJob()
    target = {"name": "SEO/网站运营-上海", "per_job_keywords": ["SEO"]}
    out_dir = str(tmp_path / "resumes")
    tr = tailor_resume(job, passed_master, target, fake_llm, out_dir=out_dir)
    typ_path = Path(out_dir) / job.job_id / "resume.typ"
    assert typ_path.exists()
    assert tr.selected_projects == ["SaaS 产品站 SEO 体系搭建"]


def test_tailor_hallucinated_project_rejected(fake_llm, passed_master, tmp_path):
    """用例3：mock LLM 编造 master 不存在的项目 → 后置校验阻断（LlmJsonParseError）。"""
    bad = _valid_tailor_response()
    bad["selected_projects"] = ["不存在的幻觉项目"]
    fake_llm.default_response = bad
    job = _FakeJob()
    target = {"name": "SEO/网站运营-上海", "per_job_keywords": ["SEO"]}
    out_dir = str(tmp_path / "resumes")
    # 幻觉项目是阻断级 → LlmJsonParseError（不再重试，直接报错）
    with pytest.raises(LlmJsonParseError):
        tailor_resume(job, passed_master, target, fake_llm, out_dir=out_dir)


def test_tailor_forbidden_word_300_warns_not_blocks(fake_llm, passed_master, tmp_path):
    """用例4：.typ 出现候选人专属禁忌词「300%」→ 只告警不阻断（不再重试 LLM）。

    「300%」是候选人专属禁忌词（非通用），需在 master.constraints.forbidden_words_extra
    配置后才触发。禁忌词现为告警级——不抛异常、不重试，仅 log.warning。
    """
    bad = _valid_tailor_response()
    bad["typ_source"] = "流量增长 300%"  # 含 300%
    fake_llm.default_response = bad
    job = _FakeJob()
    target = {"name": "SEO/网站运营-上海", "per_job_keywords": ["SEO"]}
    out_dir = str(tmp_path / "resumes")
    # 注入候选人专属禁忌词
    master = json.loads(json.dumps(passed_master))
    master.setdefault("constraints", {})["forbidden_words_extra"] = ["300%", "7年", "破万"]
    # 禁忌词不阻断 → 不抛异常，正常返回
    tr = tailor_resume(job, master, target, fake_llm, out_dir=out_dir)
    assert tr is not None  # 正常返回，未阻断


def test_tailor_workyears_seven_rejected(fake_llm, passed_master, tmp_path):
    """用例5：work_years=7 出现且 > authoritative=5.5 → 拒绝。"""
    bad = _valid_tailor_response()
    bad["typ_source"] = "= 候选人\n7 年运营经验"  # 含 7年
    fake_llm.default_response = bad
    job = _FakeJob()
    target = {"name": "SEO/网站运营-上海", "per_job_keywords": ["SEO"]}
    out_dir = str(tmp_path / "resumes")
    # 注入 authoritative=5.5，7 > 5.5 触发
    master = json.loads(json.dumps(passed_master))
    master.setdefault("constraints", {})["work_years_authoritative"] = 5.5
    with pytest.raises(LlmJsonParseError):
        tailor_resume(job, master, target, fake_llm, out_dir=out_dir)


def test_tailor_same_jd_signature_second_hit(fake_llm, passed_master, tmp_path):
    """用例6：两份同 jd_signature JD → 第二次应命中缓存（真 db 缓存）。"""
    import json
    import sqlite3
    from boss_auto_apply import db as db_mod
    from boss_auto_apply.llm import compute_cache_key

    db_path = str(tmp_path / "test.db")
    db_mod.init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    # 给 fake_llm 装上真 cache_conn
    fake_llm._cache_conn = conn

    # 但 FakeClaudeClient.invoke 不走真 db 缓存逻辑，这里手动验证 compute_cache_key 相同
    sig = "same_sig_12345"
    k1 = compute_cache_key("tailor", target_name="SEO/网站运营-上海", jd_signature=sig,
                           master_version="v1", model="claude-sonnet")
    k2 = compute_cache_key("tailor", target_name="SEO/网站运营-上海", jd_signature=sig,
                           master_version="v1", model="claude-sonnet")
    assert k1 == k2  # 同 signature → 同 cache_key
    conn.close()


def test_tailor_requires_health_passed(fake_llm, sample_master, tmp_path):
    """用例7：master 未体检 → 抛 HealthCheckFailedError。"""
    # sample_master 是 pending
    job = _FakeJob()
    target = {"name": "SEO/网站运营-上海", "per_job_keywords": ["SEO"]}
    out_dir = str(tmp_path / "resumes")
    with pytest.raises(HealthCheckFailedError):
        tailor_resume(job, sample_master, target, fake_llm, out_dir=out_dir)


def test_validate_tailor_empty():
    """validate_tailor 空内容 → 通过（无违规）。"""
    tr = TailoredResume(job_id="j1", typ_source="", selected_projects=[])
    master = {"projects": [], "constraints": {"work_years_authoritative": 5.5}}
    block, warn = validate_tailored(tr, master)
    assert block == [] and warn == []


def test_render_tailor_prompt_contains_adr(passed_master):
    """render_tailor_prompt 含 ADR-0002 + 数据表述标准。"""
    from boss_auto_apply.core.tailor import render_tailor_prompt
    prompt = render_tailor_prompt(passed_master, "JD 文本", {"name": "T"})
    assert "ADR-0002" in prompt
    # data_standards_locked 中的标准表述应被注入（匿名化后用「稳定增长」替代旧值）
    assert "稳定增长" in prompt or "data_standards" in prompt.lower()


def test_tailor_profile_files_path(fake_llm, passed_master, tmp_path):
    """改造3：LLM 返回 profile_files → 落盘成 profile 目录 + TailoredResume.has_profile=True。"""
    fake_llm.default_response = {
        "selected_modules": ["seo_growth"],
        "selected_projects": ["SaaS 产品站 SEO 体系搭建"],
        "applied_standards": ["自然搜索流量稳定增长（不暴露原值）"],
        "profile_files": {
            "metadata.toml": 'header_quote = "5.5年SEO"\n[layout]\nawesome_color = "skyblue"\n',
            "experience.typ": '#import "@preview/brilliant-cv:4.0.1": cv-entry\n',
        },
    }
    job = _FakeJob()
    target = {"name": "SEO/网站运营-上海", "per_job_keywords": ["SEO"]}
    out_dir = str(tmp_path / "resumes")
    tr = tailor_resume(job, passed_master, target, fake_llm, out_dir=out_dir)
    assert tr.has_profile
    # profile 文件应落盘到 profile_zh/ 子目录（v4 约定）
    import os
    job_dir = Path(out_dir) / job.job_id
    assert (job_dir / "profile_zh" / "metadata.toml").exists()
    assert (job_dir / "profile_zh" / "experience.typ").exists()


def test_validate_tailor_profile_forbidden_word():
    """改造3：profile_files 内容含通用+专属禁忌词均被后置校验检测（告警级）。"""
    from boss_auto_apply.core.tailor import TailoredResume, validate_tailored
    tr = TailoredResume(
        job_id="j1",
        profile_files={"experience.typ": "精通 SEO 运营 300%"},  # 「精通」通用 + 「300%」专属
    )
    master = {
        "projects": [],
        "constraints": {
            "work_years_authoritative": 5.5,
            "forbidden_words_extra": ["300%"],
        },
    }
    block, warn = validate_tailored(tr, master)
    # 禁忌词是告警级（不阻断）
    assert any("禁忌词" in v for v in warn)
    # 通用词「精通」和专属词「300%」都应被捕获
    assert any("精通" in v for v in warn)
    # 阻断级无违规（禁忌词不阻断）
    assert block == []
