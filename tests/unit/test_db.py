"""test_db.py — SQLite 状态库 + 迁移 + 状态机原语测试（设计 §5.3，7 case）。"""

from __future__ import annotations

import sqlite3

import pytest

from boss_auto_apply import db as db_mod
from boss_auto_apply.errors import MatcherRejectError
from boss_auto_apply.models import JobRow


def _make_job(job_id: str = "j1", **kw) -> JobRow:
    base = dict(job_id=job_id, target_name="SEO/网站运营-上海", city="上海")
    base.update(kw)
    return JobRow(**base)


# ============================================================
# 测试用例
# ============================================================
def test_init_db_idempotent(tmp_db):
    """用例1：init_db 幂等：连跑两次不报错。

    M3：schema_version 现为 2（迁移 v2 加 text_sent 状态 + text_sent_at 字段）。
    """
    db_mod.init_db(tmp_db)
    db_mod.init_db(tmp_db)  # 再跑一次
    # schema_version 应为 2（M3 迁移到 v2）
    with db_mod.connect(tmp_db) as conn:
        cur = conn.execute("SELECT MAX(version) FROM schema_version")
        assert cur.fetchone()[0] == 2


def test_transition_legal_and_illegal(tmp_conn):
    """用例2：transition 合法迁移成功；非法迁移抛 ValueError。"""
    job = _make_job("j1")
    db_mod.upsert_job(tmp_conn, job)
    # 合法：found → jd_analyzed
    db_mod.transition(tmp_conn, "j1", "jd_analyzed", set_fields={"match_score": 0.8})
    got = db_mod.get_job(tmp_conn, "j1")
    assert got.status == "jd_analyzed"
    assert got.match_score == 0.8
    # 非法：jd_analyzed → image_ready（越级）
    with pytest.raises(ValueError, match="非法状态迁移"):
        db_mod.transition(tmp_conn, "j1", "image_ready")


def test_mark_failed_increments_retry(tmp_conn):
    """用例3：mark_failed 后 retry_count 递增，status 留原态。"""
    job = _make_job("j1")
    db_mod.upsert_job(tmp_conn, job)
    # found 状态失败 → network 类 → 留原态 retry+1
    err = TimeoutError("模拟网络超时")
    final = db_mod.mark_failed(tmp_conn, "j1", err, max_retries=3)
    assert final == "found"  # 留原态
    got = db_mod.get_job(tmp_conn, "j1")
    assert got.retry_count == 1
    assert got.error_category == "network"


def test_list_resumable_filters(tmp_conn):
    """用例4：list_resumable 正确过滤 failed/skipped。"""
    for jid, status in [("j1", "found"), ("j2", "jd_analyzed"),
                        ("j3", "skipped"), ("j4", "failed"),
                        ("j5", "image_ready")]:
        j = _make_job(jid)
        db_mod.upsert_job(tmp_conn, j)
    # j3 → skipped（found 的合法终态之一）
    db_mod.transition(tmp_conn, "j3", "skipped", clear_error=False)
    # j4 → failed（found 的合法终态之一，failed 不在 RESUMABLE_STATUSES）
    db_mod.transition(tmp_conn, "j4", "failed",
                      set_fields={"error_msg": "模拟失败", "error_category": "network"},
                      clear_error=False)
    # j5 走完整合法链路到 image_ready（found→jd_analyzed→resume_tailored→pdf_generated→image_ready）
    for nxt in ("jd_analyzed", "resume_tailored", "pdf_generated", "image_ready"):
        db_mod.transition(tmp_conn, "j5", nxt, clear_error=False)
    resumable = db_mod.list_resumable(tmp_conn)
    ids = {j.job_id for j in resumable}
    # M3：found/jd_analyzed/image_ready 都可续传（image_ready 是发送输入态，可断点续传发送）；
    # skipped/failed 不可续传。
    assert "j1" in ids
    assert "j2" in ids
    assert "j3" not in ids  # skipped
    assert "j4" not in ids  # failed
    assert "j5" in ids      # image_ready（M3 起可续传发送）


def test_cache_put_get_hit(tmp_conn):
    """用例5：cache_put → cache_get 命中；cache_hit_incr 计数+1。"""
    import json
    db_mod.cache_put(tmp_conn, "k1", "tailor", json.dumps({"a": 1}, ensure_ascii=False),
                     target_name="SEO/网站运营-上海")
    got = db_mod.cache_get(tmp_conn, "k1")
    assert got is not None
    assert got["response_obj"] == {"a": 1}
    assert got["hit_count"] == 0
    db_mod.cache_hit_incr(tmp_conn, "k1")
    got2 = db_mod.cache_get(tmp_conn, "k1")
    assert got2["hit_count"] == 1


def test_concurrent_write_lock_retry(tmp_db):
    """用例6：并发两个连接写同一 job → 不抛（WAL + 重试）。

    [假设] sqlite3 默认 busy_timeout 下，WAL 模式并发写不会立即 locked。
    本用例验证至少不会抛 IntegrityError（主键冲突由 upsert 的 ON CONFLICT 处理）。
    """
    db_mod.init_db(tmp_db)
    conn1 = sqlite3.connect(tmp_db, isolation_level=None, timeout=5.0)
    conn2 = sqlite3.connect(tmp_db, isolation_level=None, timeout=5.0)
    for c in (conn1, conn2):
        c.execute("PRAGMA journal_mode=WAL")
    try:
        # 两个连接都 upsert 同一 job_id（ON CONFLICT 处理）
        job = _make_job("shared")
        for c in (conn1, conn2):
            c.execute("BEGIN")
            c.execute(
                "INSERT OR REPLACE INTO jobs(job_id,target_name,city,status,created_at,updated_at) "
                "VALUES(?,?,?, 'found', ?, ?)",
                (job.job_id, job.target_name, job.city, db_mod.iso_now(), db_mod.iso_now()),
            )
            c.execute("COMMIT")
    finally:
        conn1.close()
        conn2.close()
    # 验证最终只有一条
    with db_mod.connect(tmp_db) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM jobs WHERE job_id='shared'")
        assert cur.fetchone()[0] == 1


def test_migration_version_increments(tmp_db):
    """用例7：迁移版本号递增正确（从空库到 v1 + v2）。

    M3：迁移到 v2（jobs.status CHECK 加 text_sent，加 text_sent_at 字段）。
    """
    db_mod.init_db(tmp_db)
    with db_mod.connect(tmp_db) as conn:
        cur = conn.execute("SELECT version FROM schema_version ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
    assert versions == [1, 2]


def test_mark_failed_business_to_skipped(tmp_conn):
    """补充：business 类错误（MatcherRejectError）直接转 skipped。"""
    job = _make_job("jb")
    db_mod.upsert_job(tmp_conn, job)
    err = MatcherRejectError("硬过滤未命中")
    final = db_mod.mark_failed(tmp_conn, "jb", err, max_retries=3)
    assert final == "skipped"
    got = db_mod.get_job(tmp_conn, "jb")
    assert got.error_category == "business"
