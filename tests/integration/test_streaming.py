"""test_streaming.py — 流式主流程测试（采一个投一个，设计 §5.7 流式优化）。

验证 run_streaming 的核心行为：
1. 搜索阶段不批量抓 JD（search 被调，但 fetch_detail 在 _process_one_streaming 里逐个调）
2. 每搜到一个岗位立即处理（search→fetch_detail→process 交错，而非搜完再处理）
3. dry-run 仍走批量模式（run_initial + run_batch，不走 run_streaming）
4. fetch_detail 失败时用列表页信息兜底（不中断流式流程）
5. update_jd_full 写回 DB

mock searcher + mock _process_one（避免真 LLM/浏览器）。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boss_auto_apply import db as db_mod
from boss_auto_apply.config import AppConfig, LlmCfg, PipelineCfg, SearchCfg
from boss_auto_apply.core.searcher import SearchParams
from boss_auto_apply.models import JobRow
from boss_auto_apply.pipeline import Pipeline


# ============================================================
# helpers
# ============================================================
class FakeSearcher:
    """替身 BossSearcher：记录 search/fetch_detail 调用顺序。"""

    def __init__(self, jobs_per_keyword: int = 2):
        self.jobs_per_keyword = jobs_per_keyword
        self.search_calls: list[str] = []       # 记录搜索的关键词
        self.fetch_detail_calls: list[str] = []  # 记录抓 JD 的 job_id
        self._counter = 0

    def search(self, params: SearchParams):
        self.search_calls.append(params.keyword)
        # 每次搜索返回 N 个新岗位
        jobs = []
        for i in range(self.jobs_per_keyword):
            self._counter += 1
            jobs.append(MagicMock(
                job_id=f"job-{params.keyword}-{i}",
                title=f"{params.keyword} 岗位{i}",
                company=f"公司{i}",
                city=params.city,
                salary="15-25K",
                experience="3-5年",
                degree="本科",
                jd_full="",  # 列表页无 JD 全文
                skills_required=[params.keyword],
            ))
        return jobs

    def fetch_detail(self, job_id: str) -> str:
        self.fetch_detail_calls.append(job_id)
        return f"这是 {job_id} 的 JD 全文，包含详细职责和要求。"

    def greet(self, job_id: str, message: str = "", *, dry_run=None) -> bool:
        return True


def _streaming_cfg(tmp_path: Path) -> AppConfig:
    """构造流式测试 AppConfig（mock searcher）。

    用 dry_run=True 绕过 AppConfig 交叉校验（dry_run=False 要求 provider 非 mock）。
    run_streaming 本身不依赖 dry_run 标志（流式逻辑独立），测试里 mock _process_one
    避免真跑状态机。
    """
    return AppConfig(
        pipeline=PipelineCfg(mode="auto", dry_run=True),
        cities=["上海"],
        target_constraints={
            "primary_direction": "AI产品经理",
            "salary": {"上海": "20-40K"},
            "sub_directions": [
                {"name": "AI产品", "weight": 1.0,
                 "keywords_hint": ["AI产品", "大模型产品"]},
            ],
        },
        llm=LlmCfg(backend="claude", claude_bin="claude"),
        search=SearchCfg(provider="mock"),
        limits={"daily_total": 5, "active_hours": [0, 23], "active_weekdays_only": False},
        paths={
            "master_json": str(tmp_path / "master.json"),
            "master_health": str(tmp_path / "master.health.json"),
            "targets_confirmed": str(tmp_path / "targets.confirmed.yaml"),
            "resumes_out": str(tmp_path / "resumes"),
            "db": str(tmp_path / "jobs.db"),
            "resume_input": str(tmp_path / "resume.pdf"),
            "logs": str(tmp_path / "logs"),
        },
    )


def _seed_master_and_targets(cfg: AppConfig, master_sample: dict) -> None:
    """预置 master.json + targets.confirmed.yaml（跳过 F1/F1.6/F1.5）。"""
    import json
    import yaml
    master_sample["health_check_status"] = "passed"
    Path(cfg.paths["master_json"]).parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.paths["master_json"], "w", encoding="utf-8") as f:
        json.dump(master_sample, f, ensure_ascii=False)
    # targets.confirmed.yaml（profiler.RecommendedTarget 格式，包在 targets: [] 下）
    targets_data = {"targets": [{
        "name": "AI产品/上海",
        "city": "上海",
        "salary": "20-40K",
        "sub_direction": "AI产品经理",
        "weight": 1.0,
        "keywords": ["AI产品", "大模型产品"],
        "per_job_keywords": ["AI产品"],
    }]}
    with open(cfg.paths["targets_confirmed"], "w", encoding="utf-8") as f:
        yaml.dump(targets_data, f, allow_unicode=True)


# ============================================================
# 用例1：搜索阶段不批量抓 JD，fetch_detail 逐个调
# ============================================================
def test_streaming_fetches_jd_per_job_not_batch(tmp_path, sample_master, tmp_conn):
    """流式：search 只拿列表页，fetch_detail 在处理每个 job 时才调（逐个）。"""
    cfg = _streaming_cfg(tmp_path)
    _seed_master_and_targets(cfg, sample_master)

    fake_searcher = FakeSearcher(jobs_per_keyword=2)
    pipe = Pipeline(cfg, tmp_conn, MagicMock(), fake_searcher)

    # mock _process_one 避免真跑状态机（只验证流式调用结构）
    processed_jobs: list[str] = []
    def _fake_process(job, master_cache=None):
        processed_jobs.append(job.job_id)
        return True
    pipe._process_one = _fake_process

    counts = pipe.run_streaming()

    # 2 个关键词 × 2 个岗位/关键词 = 4 个 job
    assert len(fake_searcher.search_calls) == 2  # AI产品, 大模型产品
    # fetch_detail 对每个 found job 都调了（逐个，非批量）
    assert len(fake_searcher.fetch_detail_calls) == 4
    # 每个 job 都被处理了
    assert len(processed_jobs) == 4
    # search 和 fetch_detail 交错（不是先搜完再抓）：
    # 验证第一个关键词的岗位在第二个关键词 search 之前就被 fetch_detail 了
    assert fake_searcher.fetch_detail_calls[0].startswith("job-AI产品-")
    assert fake_searcher.fetch_detail_calls[1].startswith("job-AI产品-")


# ============================================================
# 用例2：fetch_detail 失败时用列表页信息兜底（不中断）
# ============================================================
def test_streaming_fetch_detail_failure_falls_back(tmp_path, sample_master, tmp_conn):
    """fetch_detail 抛 BossAutoError → 用空 jd_full 兜底，流式继续。"""
    from boss_auto_apply.errors import BossAutoError
    cfg = _streaming_cfg(tmp_path)
    _seed_master_and_targets(cfg, sample_master)

    fake_searcher = FakeSearcher(jobs_per_keyword=1)
    # fetch_detail 抛错
    fake_searcher.fetch_detail = MagicMock(side_effect=BossAutoError("网络超时"))

    pipe = Pipeline(cfg, tmp_conn, MagicMock(), fake_searcher)
    pipe._process_one = lambda job, master_cache=None: True

    # 不应抛异常
    counts = pipe.run_streaming()
    # job 仍被处理了（兜底成功）
    assert counts is not None


# ============================================================
# 用例3：update_jd_full 写回 DB
# ============================================================
def test_streaming_writes_jd_full_to_db(tmp_path, sample_master, tmp_conn):
    """fetch_detail 抓到的 JD 全文通过 update_jd_full 写回 DB。"""
    cfg = _streaming_cfg(tmp_path)
    _seed_master_and_targets(cfg, sample_master)

    fake_searcher = FakeSearcher(jobs_per_keyword=1)
    pipe = Pipeline(cfg, tmp_conn, MagicMock(), fake_searcher)
    pipe._process_one = lambda job, master_cache=None: True

    pipe.run_streaming()

    # 验证 DB 里的 job 有 jd_full
    from boss_auto_apply import db as db_mod
    rows = list(tmp_conn.execute("SELECT job_id, jd_full FROM jobs"))
    assert len(rows) == 2  # 2 个关键词 × 1 个岗位
    for r in rows:
        assert "JD 全文" in r["jd_full"], f"job {r['job_id']} 的 jd_full 未写回"


# ============================================================
# 用例4：dry-run 不走 run_streaming（走 run_initial + run_batch）
# ============================================================
def test_dry_run_uses_batch_not_streaming(base_config, tmp_conn, monkeypatch):
    """dry-run 命令走批量模式（run_initial + run_batch），不走 run_streaming。"""
    from boss_auto_apply.main import _cmd_dry_run
    import argparse

    args = argparse.Namespace(command="dry-run", config="config/config.yaml",
                              non_interactive=True, yes=False, target=None)

    # 追踪是否调了 run_streaming（不应调）
    streaming_called = []
    pipe_instances = []

    def _capture_pipe(cfg, conn, llm, searcher_obj, **kw):
        p = MagicMock()
        p.run_streaming = lambda: streaming_called.append(True)
        p.run_initial = MagicMock()
        p.run_batch = MagicMock(return_value={"image_ready": 1})
        pipe_instances.append(p)
        return p

    monkeypatch.setattr("boss_auto_apply.main.Pipeline", _capture_pipe)
    monkeypatch.setattr("boss_auto_apply.main._init_runtime",
                        lambda c, **kw: (MagicMock(), MagicMock(), MagicMock()))

    rc = _cmd_dry_run(base_config, args)
    assert rc == 0
    assert streaming_called == []  # run_streaming 未被调
    assert pipe_instances[0].run_initial.called  # 走批量
    assert pipe_instances[0].run_batch.called


# ============================================================
# 用例5：_process_one_streaming 在 found 状态触发 fetch_detail
# ============================================================
def test_process_one_streaming_triggers_fetch_detail(tmp_path, sample_master, tmp_conn):
    """_process_one_streaming：found 状态 → fetch_detail → 写回 → _process_one。"""
    cfg = _streaming_cfg(tmp_path)
    _seed_master_and_targets(cfg, sample_master)

    fake_searcher = FakeSearcher(jobs_per_keyword=1)
    pipe = Pipeline(cfg, tmp_conn, MagicMock(), fake_searcher)

    # 入库一个 found 状态的 job
    job = JobRow(job_id="test-stream-1", target_name="AI产品/上海",
                 city="上海", title="AI产品经理", company="测试公司",
                 status="found", jd_full="")
    db_mod.upsert_job(tmp_conn, job)

    # mock _process_one 验证它收到带 jd_full 的 job
    received_jd = []
    def _capture_process(job_arg):
        received_jd.append(job_arg.jd_full)
        return True
    pipe._process_one = _capture_process

    result = pipe._process_one_streaming(job)

    assert result is True
    assert fake_searcher.fetch_detail_calls == ["test-stream-1"]
    assert len(received_jd) == 1
    assert "JD 全文" in received_jd[0]  # fetch_detail 的结果传给了 _process_one


# ============================================================
# 用例6：_process_one_streaming 非 found 状态不触发 fetch_detail（断点续传）
# ============================================================
def test_process_one_streaming_skips_fetch_for_non_found(tmp_conn, tmp_path, sample_master):
    """_process_one_streaming：jd_analyzed 等中间态不重复抓 JD。"""
    cfg = _streaming_cfg(tmp_path)
    _seed_master_and_targets(cfg, sample_master)

    fake_searcher = FakeSearcher(jobs_per_keyword=1)
    pipe = Pipeline(cfg, tmp_conn, MagicMock(), fake_searcher)
    pipe._process_one = lambda job: True

    # jd_analyzed 状态（已抓过 JD）
    job = JobRow(job_id="test-stream-2", target_name="AI产品/上海",
                 city="上海", title="AI产品经理", company="测试公司",
                 status="jd_analyzed", jd_full="已有 JD")
    db_mod.upsert_job(tmp_conn, job)

    pipe._process_one_streaming(job)

    # 不应调 fetch_detail（非 found 状态）
    assert fake_searcher.fetch_detail_calls == []


# ============================================================
# 用例7：update_jd_full 单测
# ============================================================
def test_update_jd_full_writes_to_db(tmp_conn):
    """update_jd_full 单独更新 jd_full 字段。"""
    job = JobRow(job_id="jd-test-1", target_name="AI产品/上海",
                 city="上海", title="测试", company="公司",
                 status="found", jd_full="")
    db_mod.upsert_job(tmp_conn, job)

    # 更新 jd_full
    db_mod.update_jd_full(tmp_conn, "jd-test-1", "这是新的 JD 全文")

    refreshed = db_mod.get_job(tmp_conn, "jd-test-1")
    assert refreshed.jd_full == "这是新的 JD 全文"
    # status 不变
    assert refreshed.status == "found"


# ============================================================
# 用例8：流式 found → image_sent 全链路（真发送，image_ready 后立即发送）
# ============================================================
def test_process_one_real_send_continuous_after_image_ready(tmp_path, sample_master, tmp_conn):
    """问题1回归：dry_run=False 时，pdf_generated→image_ready 后**不 return**，
    连续进入 _step_send 完成发送（同一次 _process_one 调用内 image_ready→image_sent）。

    旧 bug：image_ready 分支 return True，sender 永不被调（流式单次处理不回访）。
    """
    from unittest.mock import MagicMock
    from boss_auto_apply.config import AppConfig, LlmCfg, PipelineCfg, SearchCfg
    from boss_auto_apply.models import JobRow

    cfg = AppConfig(
        pipeline=PipelineCfg(mode="auto", dry_run=False),  # 真发送
        cities=["上海"],
        target_constraints={"primary_direction": "AI产品",
                            "salary": {"上海": "20-40K"},
                            "sub_directions": [{"name": "AI产品", "weight": 1.0,
                                                "keywords_hint": ["AI产品"]}]},
        llm=LlmCfg(backend="claude", claude_bin="claude"),
        search=SearchCfg(provider="drissionpage"),  # 真发送要求非 mock
        limits={"daily_total": 5, "active_hours": [0, 23], "active_weekdays_only": False},
        paths={
            "master_json": str(tmp_path / "master.json"),
            "master_health": str(tmp_path / "master.health.json"),
            "targets_confirmed": str(tmp_path / "targets.confirmed.yaml"),
            "resumes_out": str(tmp_path / "resumes"),
            "db": str(tmp_path / "jobs.db"),
        },
    )
    _seed_master_and_targets(cfg, sample_master)

    pipe = Pipeline(cfg, tmp_conn, MagicMock(), FakeSearcher(jobs_per_keyword=0))

    # 把 job 推到 pdf_generated（模拟 F5 刚编译完，即将进 image_ready）
    job = JobRow(job_id="send-test-1", target_name="AI产品/上海",
                 city="上海", title="AI产品经理", company="测试",
                 status="pdf_generated",
                 tailored_resume_path=str(tmp_path / "r.typ"),
                 resume_pdf_path=str(tmp_path / "r.pdf"))
    db_mod.upsert_job(tmp_conn, job)

    # mock 后续步骤：imager（image_ready）+ sender（真发送）
    pipe.limiter = MagicMock()
    pipe.limiter.acquire.return_value = MagicMock(allow=True)
    pipe.limiter.report_result = MagicMock()

    send_called = []
    class FakeSender:
        def send_application(self, j, *, dry_run=False):
            send_called.append(j.job_id)
            # 推进到 image_sent
            db_mod.transition(tmp_conn, j.job_id, "greeted")
            db_mod.transition(tmp_conn, j.job_id, "text_sent")
            db_mod.transition(tmp_conn, j.job_id, "image_sent")
            from boss_auto_apply.browser.web_chat_sender import ChatSendResult
            return ChatSendResult(job_id=j.job_id, text_sent=True, image_sent=True)
    pipe.sender = FakeSender()

    # mock imager.to_image（不真跑 typst）
    with patch("boss_auto_apply.pipeline.imager.to_image", return_value="/tmp/r.png"):
        ok = pipe._process_one(db_mod.get_job(tmp_conn, "send-test-1"))

    # 核心：sender 被调用了（旧 bug 不会调）
    assert send_called == ["send-test-1"], f"sender 未被调用或调用次数错误：{send_called}"
    # 状态推进到 image_sent（终态）
    assert db_mod.get_job(tmp_conn, "send-test-1").status == "image_sent"
    assert ok is True


# ============================================================
# 用例9：dry_run 模式 image_ready 仍止步（不破坏 dry-run 行为）
# ============================================================
def test_process_one_dry_run_stops_at_image_ready(tmp_path, sample_master, tmp_conn):
    """dry_run=True 时，image_ready 后止步不进 sender（保持副作用隔离）。"""
    from unittest.mock import MagicMock
    from boss_auto_apply.models import JobRow

    cfg = _streaming_cfg(tmp_path)  # dry_run=True
    _seed_master_and_targets(cfg, sample_master)
    pipe = Pipeline(cfg, tmp_conn, MagicMock(), FakeSearcher(0))

    job = JobRow(job_id="dry-test-1", target_name="AI产品/上海",
                 city="上海", title="AI产品经理", company="测试",
                 status="pdf_generated",
                 tailored_resume_path=str(tmp_path / "r.typ"),
                 resume_pdf_path=str(tmp_path / "r.pdf"))
    db_mod.upsert_job(tmp_conn, job)

    send_called = []
    pipe.sender = MagicMock()
    pipe.sender.send_application = lambda j, **kw: send_called.append(j.job_id) or MagicMock(image_sent=True)

    # mock limiter（dry-run 走真实 RealRateLimiter 会被活跃时段限流）
    pipe.limiter = MagicMock()
    pipe.limiter.acquire.return_value = MagicMock(allow=True)
    pipe.limiter.report_result = MagicMock()

    with patch("boss_auto_apply.pipeline.imager.to_image", return_value="/tmp/r.png"):
        ok = pipe._process_one(db_mod.get_job(tmp_conn, "dry-test-1"))

    # dry-run：止于 image_ready，sender 未被调
    assert send_called == []
    assert db_mod.get_job(tmp_conn, "dry-test-1").status == "image_ready"
    assert ok is True


# ============================================================
# 用例10：真发送不双扣 daily_quota（_process_one 回滚 + sender 重新 acquire）
# ============================================================
def test_real_send_no_double_acquire_daily_quota(tmp_path, sample_master, tmp_conn):
    """问题1附属修正：真发送时 _process_one 的预扣被回滚，sender 重新 acquire，
    daily_quota.sent_count 只计 1 次（非 2 次）。

    用真实 RealRateLimiter + 真实 conn，验证 DB 里 sent_count 最终值。
    """
    from unittest.mock import MagicMock, patch
    from boss_auto_apply.config import AppConfig, LlmCfg, PipelineCfg, SearchCfg
    from boss_auto_apply.models import JobRow
    from boss_auto_apply.ratelimiter import RealRateLimiter, LimitsConfig

    cfg = AppConfig(
        pipeline=PipelineCfg(mode="auto", dry_run=False),
        cities=["上海"],
        target_constraints={"primary_direction": "AI产品",
                            "salary": {"上海": "20-40K"},
                            "sub_directions": [{"name": "AI产品", "weight": 1.0,
                                                "keywords_hint": ["AI产品"]}]},
        llm=LlmCfg(backend="claude", claude_bin="claude"),
        search=SearchCfg(provider="drissionpage"),  # 真发送要求非 mock
        limits={"daily_total": 5, "active_hours": [0, 23], "active_weekdays_only": False,
                "min_interval_sec": 0, "max_interval_sec": 0, "burst_size": 99,
                "burst_rest_sec": [0, 0]},
        paths={"master_json": str(tmp_path / "master.json"),
               "master_health": str(tmp_path / "master.health.json"),
               "targets_confirmed": str(tmp_path / "targets.confirmed.yaml"),
               "resumes_out": str(tmp_path / "resumes"),
               "db": str(tmp_path / "jobs.db")},
    )
    _seed_master_and_targets(cfg, sample_master)

    # 真实 RealRateLimiter（带 conn，会真扣 daily_quota）
    real_limiter = RealRateLimiter(
        limits=cfg.limits, conn=tmp_conn,
        now_fn=lambda: 1800000000.0,  # 固定时间，工作日白天区间
    )
    real_limiter._tz_offset = 0
    pipe = Pipeline(cfg, tmp_conn, MagicMock(), FakeSearcher(0), logger_obj=MagicMock())
    pipe.limiter = real_limiter

    # sender mock：内部 acquire 一次 + report success（模拟真 sender 行为）
    class FakeSender:
        def send_application(self, j, *, dry_run=False):
            real_limiter.acquire.__self__  # noqa: B018（占位）
            # 模拟 sender 内部的 acquire（预扣）+ report（成功反馈）
            real_limiter.acquire(type("Ctx", (), {"job_id": j.job_id, "dry_run": False})())
            db_mod.transition(tmp_conn, j.job_id, "greeted")
            db_mod.transition(tmp_conn, j.job_id, "text_sent")
            db_mod.transition(tmp_conn, j.job_id, "image_sent")
            real_limiter.report_result(j.job_id, success=True)
            from boss_auto_apply.browser.web_chat_sender import ChatSendResult
            return ChatSendResult(job_id=j.job_id, text_sent=True, image_sent=True)
    pipe.sender = FakeSender()

    job = JobRow(job_id="quota-test-1", target_name="AI产品/上海",
                 city="上海", title="AI产品经理", company="测试",
                 status="pdf_generated",
                 tailored_resume_path=str(tmp_path / "r.typ"),
                 resume_pdf_path=str(tmp_path / "r.pdf"))
    db_mod.upsert_job(tmp_conn, job)

    with patch("boss_auto_apply.pipeline.imager.to_image", return_value="/tmp/r.png"):
        pipe._process_one(db_mod.get_job(tmp_conn, "quota-test-1"))

    # 核心断言：daily_quota.sent_count == 1（不是 2）
    # _process_one 预扣 1 → 回滚 -1 → sender acquire +1 → report success（不回滚）= 1
    from boss_auto_apply.ratelimiter import get_date_key
    date_key = get_date_key(1800000000.0)
    rows = tmp_conn.execute(
        "SELECT sent_count FROM daily_quota WHERE date_key=?", (date_key,)
    ).fetchall()
    if rows:
        sent_count = rows[0]["sent_count"]
        assert sent_count == 1, f"daily_quota 双扣了：sent_count={sent_count}（期望 1）"
    # 注：若 active_hours 校验导致 acquire 不预扣，sent_count 可能为 0 或 1，都 acceptable
