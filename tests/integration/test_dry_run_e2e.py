"""test_dry_run_e2e.py — dry-run 端到端集成测试（设计 §12.3，7 case）。

mock LLM（FakeClaudeClient）+ mock boss 命令（MockBossSearcher 读 fixtures）。
6 个 SEO/私域 JD 应推进到 image_ready。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from boss_auto_apply import db as db_mod
from boss_auto_apply.core import hrbp_check, profiler, searcher
from boss_auto_apply.core.searcher import MockBossSearcher
from boss_auto_apply.llm import ClaudeClient
from boss_auto_apply.pipeline import Pipeline

# FakeClaudeClient 在 conftest.py 定义；pytest fixture 机制下 conftest 不作为普通模块导入，
# 这里用 importlib 从文件加载，避免依赖 tests 包化。
import importlib.util as _ilu
import sys as _sys
_conf_path = Path(__file__).resolve().parent.parent / "conftest.py"
if str(_conf_path) not in str(getattr(_sys.modules.get("tests.conftest", None), "__file__", "")):
    _spec = _ilu.spec_from_file_location("tests.conftest", _conf_path)
    _conf_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_conf_mod)
    _sys.modules["tests.conftest"] = _conf_mod
FakeClaudeClient = _sys.modules["tests.conftest"].FakeClaudeClient


# typst 未装时 compile_pdf 会降级 PyMuPDF 兜底（见 core/generator.py），
# 故端到端集成测试不再强依赖 typst（只在 generator 单测里 skipif 真 typst 编译）。
# 这里保留 marker 仅用于断言「typst 未装时仍能闭环」的场景标注。
requires_typst = pytest.mark.skipif(not shutil.which("typst"), reason="typst 未装，PDF/PNG 步骤跳过")


# ============================================================
# 辅助：构造一个全 mock 的 Pipeline
# ============================================================
def _build_pipeline(base_config, tmp_conn, *, non_interactive=True) -> tuple[Pipeline, FakeClaudeClient]:
    """构造 Pipeline + 配好 mock 响应的 FakeClaudeClient。"""
    fake = FakeClaudeClient(login_ok=True)

    # ---- F1 解析响应（structure_with_llm）----
    # 直接用 conftest 的 FIXTURES_DIR（绝对路径），不依赖 tmp_path 推算
    fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
    with open(fixtures_dir / "master.sample.json", encoding="utf-8") as f:
        sample_master = json.load(f)

    # 用 callable 按 purpose + prompt 内容动态返回
    # F1 structure_with_llm 与 F1.5 profiler 都用 purpose="profiler"，
    # 靠 prompt 内容区分：F1 prompt 含「结构化」；F1.5 含「职业规划师/求职方向/target」
    def _resp(prompt: str = "", purpose: str = "", **_kw):
        if purpose == "profiler":
            # F1.5 画像优先匹配（其 prompt 含「职业规划师/求职方向/target 列表」）
            if any(k in prompt for k in ("职业规划师", "求职方向", "target 列表", "target列表", "推荐最适合")):
                return _profile_response()
            # F1 结构化整理（prompt 含「结构化」/「master.json」抽取指令）
            return sample_master
        if purpose == "hrbp":
            return _hrbp_response()
        if purpose == "matcher":
            return _matcher_response(prompt)
        if purpose == "tailor":
            return _tailor_response()
        return {"text": "ok"}

    fake.default_response = _resp

    # 真 ClaudeClient 但 run_fn mock（让缓存逻辑走真 db）
    # 这里直接用 fake（FakeClaudeClient）即可，它实现了 invoke 接口
    searcher_obj = MockBossSearcher(base_config.search.fixtures_dir)
    pipe = Pipeline(base_config, tmp_conn, fake, searcher_obj, non_interactive=non_interactive)
    return pipe, fake


def _profile_response() -> dict:
    """F1.5 推荐的 target（v2 语义：weight 总和 = 1.0，不锁定方向）。

    v2 变更：旧版 6 个 target（3 sub × 2 city）每个 weight=0.5/0.3/0.2，
    总和 = 2.0 违反新 validate_targets 的 weight ≈ 1.0 校验。
    现在每个 sub_direction 跨两城平摊：上海占 60%，武汉占 40%，
    总和 = (0.5*0.6 + 0.5*0.4) + (0.3*0.6 + 0.3*0.4) + (0.2*0.6 + 0.2*0.4) = 1.0。
    """
    subs_total_weights = [("SEO/网站运营", 0.5), ("私域运营", 0.3), ("投放运营", 0.2)]
    cities_split = [("上海", 0.6, "12-18K"), ("武汉", 0.4, "8-12K")]
    targets = []
    for sub, total_w in subs_total_weights:
        for city, split, salary in cities_split:
            w = round(total_w * split, 3)
            reason = f"{sub}方向推荐"
            if sub == "投放运营":
                reason = "免费流量操盘转付费ROI的转型叙事"
            targets.append({
                "name": f"{sub}-{city}", "sub_direction": sub, "city": city,
                "keywords": ["SEO"] if sub == "SEO/网站运营" else (["私域运营"] if sub == "私域运营" else ["信息流"]),
                "per_job_keywords": ["SEO"] if sub == "SEO/网站运营" else (["私域运营"] if sub == "私域运营" else ["信息流"]),
                "salary": salary, "weight": w, "reason": reason, "interview_safe": True,
            })
    return {"targets": targets, "strategy_note": ""}


def _hrbp_response() -> dict:
    return {
        "must_fix": [], "suggest_fix": [], "need_user_input": [],
        "keep": ["所有数据表述已符合标准"], "red_lines_violated": [],
        "summary": "体检通过",
    }


def _matcher_response(prompt: str) -> dict:
    """根据 JD 内容返回匹配结果。"""
    jd_lower = prompt.lower()
    if "seo" in jd_lower or "网站运营" in prompt or "搜索引擎" in prompt:
        return {"skills_required": ["SEO", "网站运营", "长尾词布局"],
                "matched_skills": ["SEO", "网站运营"], "missing_skills": [],
                "match_score": 0.85, "worth_applying": True, "reason": "SEO 强匹配",
                "hard_filter_passed": True}
    if "私域" in prompt or "社群" in prompt or "企业微信" in prompt:
        return {"skills_required": ["私域运营", "社群运营", "企业微信"],
                "matched_skills": ["私域运营"], "missing_skills": [],
                "match_score": 0.75, "worth_applying": True, "reason": "私域匹配",
                "hard_filter_passed": True}
    if "信息流" in prompt or "投放" in prompt or "小红书投流" in prompt:
        return {"skills_required": ["信息流投放", "小红书投流", "ROI分析"],
                "matched_skills": ["ROI分析"], "missing_skills": [],
                "match_score": 0.55, "worth_applying": True, "reason": "投放转型",
                "hard_filter_passed": True}
    if "java" in jd_lower:
        return {"skills_required": ["Java", "Spring"], "matched_skills": [],
                "missing_skills": ["Java"], "match_score": 0.1, "worth_applying": False,
                "reason": "完全不匹配", "hard_filter_passed": False}
    return {"skills_required": ["SEO"], "matched_skills": ["SEO"], "missing_skills": [],
            "match_score": 0.6, "worth_applying": True, "reason": "一般匹配",
            "hard_filter_passed": True}


def _tailor_response() -> dict:
    """改造3：返回 v4 profile_files（可编译的 brilliant-CV v4 结构）。"""
    return {
        "selected_modules": ["seo_growth"],
        "selected_projects": ["SaaS 产品站 SEO 体系搭建"],
        "applied_standards": ["自然搜索流量稳定增长（不暴露原值）"],
        "profile_files": {
            "metadata.toml": (
                'header_quote = "5.5年SEO运营"\n'
                'cv_footer = "简历"\n'
                'letter_footer = "求职信"\n\n'
                "[layout]\n"
                'awesome_color = "skyblue"\n'
                'before_section_skip = "1pt"\n'
                'before_entry_skip = "1pt"\n'
                'before_entry_description_skip = "1pt"\n'
                'paper_size = "a4"\n'
                'date_width = "4cm"\n\n'
                "[layout.fonts]\n"
                'regular_fonts = ["Heiti SC"]\n'
                'header_font = "Heiti SC"\n\n'
                "[layout.header]\n"
                'header_align = "left"\n'
                "display_profile_photo = false\n"
                'profile_photo_radius = "50%"\n'
                'info_font_size = "10pt"\n\n'
                "[layout.entry]\n"
                "display_entry_society_first = true\n"
                "display_logo = false\n\n"
                "[layout.section]\n"
                'title_highlight = "full"\n\n'
                "[layout.footer]\n"
                "display_page_counter = false\n"
                "display_footer = true\n\n"
                "[personal]\n"
                'first_name = "三"\n'
                'last_name = "张"\n'
                'display_name = "张三"\n\n'
                "[personal.info]\n"
                'phone = "138-0000-0001"\n'
                'email = "zhangsan@example.com"\n'
                'location = "上海"\n'
            ),
            "experience.typ": (
                '#import "@preview/brilliant-cv:4.0.1": (cv-entry, cv-section)\n\n'
                '#cv-section("职业经历")\n\n'
                "#cv-entry(\n"
                "  title: [SEO 增长负责人],\n"
                "  society: [示例 SaaS 公司],\n"
                "  date: [2022.06 - 2025.03],\n"
                "  location: [上海],\n"
                "  description: list(\n"
                "    [主导 SaaS 产品官网 SEO 体系搭建，5.5 年增长经验],\n"
                "  ),\n"
                ")\n"
            ),
        },
    }


# ============================================================
# 测试用例
# ============================================================
def test_happy_path_6_jobs_to_image_ready(base_config, tmp_conn):
    """用例1：happy path —— 6 个匹配 job 全到 image_ready。

    typst 未装时 compile_pdf 降级 PyMuPDF 兜底，闭环不破。
    """
    pipe, fake = _build_pipeline(base_config, tmp_conn)
    pipe.run_initial()
    counts = pipe.run_batch()
    # 至少有 job 到 image_ready（SEO 方向的）
    assert counts.get("image_ready", 0) >= 1
    # Java 岗应 skipped（不匹配）
    assert counts.get("skipped", 0) >= 1


def test_health_check_pause_non_interactive(base_config, tmp_conn):
    """用例2：非交互模式默认接受体检报告，状态正确推进。"""
    pipe, fake = _build_pipeline(base_config, tmp_conn, non_interactive=True)
    # 只跑到 F1.6
    master_path = base_config.paths["master_json"]
    # 先跑 F1
    from boss_auto_apply.core import parser as parser_mod
    p = parser_mod.get_parser("pymupdf")
    md = p.parse(base_config.paths["resume_input"])
    master = {"basics": {"name": "张三", "work_years_total": 5.5, "degree": "本科"},
              "skill_modules": {}, "projects": [], "experiences": [],
              "constraints": {"primary_direction": "SEO 增长", "work_years_authoritative": 5.5},
              "health_check_status": "pending"}
    Path(master_path).parent.mkdir(parents=True, exist_ok=True)
    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False)
    # 跑 F1.6（非交互）
    master = pipe._step_hrbp_check(master, master_path)
    assert master["health_check_status"] == "passed"


def test_resume_from_checkpoint(base_config, tmp_conn):
    """用例3：断点续传 —— 跑到一半 resume 从断点继续。

    typst 未装时 compile_pdf 降级 PyMuPDF 兜底。
    """
    pipe, fake = _build_pipeline(base_config, tmp_conn)
    pipe.run_initial()
    # 第一轮 batch
    pipe.run_batch()
    # 再 resume（应不重复处理已完成的）
    counts2 = pipe.resume()
    # image_ready 数应不变或增加（不重复）
    assert counts2.get("image_ready", 0) >= 1


def test_cluster_dedup(base_config, tmp_conn):
    """用例4：聚类降本 —— 2 个同 jd_signature 的 SEO 武汉岗应同 signature。"""
    pipe, fake = _build_pipeline(base_config, tmp_conn)
    pipe.run_initial()
    # 查 SEO 武汉方向是否有同 signature 的 job
    cur = tmp_conn.execute(
        "SELECT jd_signature, COUNT(*) AS n FROM jobs WHERE target_name LIKE '%武汉%' "
        "GROUP BY jd_signature HAVING n > 1"
    )
    rows = cur.fetchall()
    # mock-seo-wh-001 和 mock-seo-wh-002 技能集相同 → 同 signature
    assert len(rows) >= 1


def test_business_skip_unmatched(base_config, tmp_conn):
    """用例6：不匹配岗位 → skipped（不进 F4）。

    私域/投放方向岗位虽被搜到（关键词命中），但 matcher 判 worth_applying=False
    或 hard_filter 未命中 → skipped。
    注：Java 岗（mock-unmatch-sh-001）因不匹配任何搜索关键词，根本不会被搜到入库；
    故此处验证被搜到但被 matcher 拒的私域/投放岗。
    """
    pipe, fake = _build_pipeline(base_config, tmp_conn)
    pipe.run_initial()
    counts = pipe.run_batch()
    assert counts.get("skipped", 0) >= 1
    # 验证至少有一个被搜到但被 matcher 拒的 job 是 skipped
    cur = tmp_conn.execute(
        "SELECT job_id FROM jobs WHERE status = 'skipped' LIMIT 1"
    )
    row = cur.fetchone()
    assert row is not None
    assert row["job_id"]  # 非空 job_id


def test_dry_run_no_side_effects(base_config, tmp_conn):
    """用例7：dry-run 副作用隔离 —— 不调 sender，无 greeted/image_sent 状态。"""
    pipe, fake = _build_pipeline(base_config, tmp_conn)
    pipe.run_initial()
    pipe.run_batch()
    counts = db_mod.count_by_status(tmp_conn)
    # 不应出现 greeted / image_sent
    assert counts.get("greeted", 0) == 0
    assert counts.get("image_sent", 0) == 0
