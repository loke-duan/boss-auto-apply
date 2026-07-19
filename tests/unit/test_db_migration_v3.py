"""test_db_migration_v3.py — ADR-0005 jobs 表 v3 新增列测试。

覆盖：
1. 全新 DB 初始化后 jobs 表有 v3 新列（level_match/job_category/category_passed/business_domain_match_score）
2. 老 DB（v2 schema）迁移后 v3 列被加上，老数据保留
3. upsert_job 写入 v3 字段、读取时类型转换正确
4. transition 能更新 v3 字段（白名单已加）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from boss_auto_apply import db as db_mod
from boss_auto_apply.models import JobRow


# ============================================================
# 列存在性
# ============================================================
def test_v3_columns_exist_on_fresh_db(tmp_path: Path):
    """全新 DB 应有 v3 新列（init_db + migrate 兜底加列）。"""
    db_path = str(tmp_path / "test.db")
    db_mod.init_db(db_path)
    with db_mod.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "level_match" in cols
    assert "job_category" in cols
    assert "category_passed" in cols
    assert "business_domain_match_score" in cols


def test_v3_columns_added_to_old_db(tmp_path: Path):
    """模拟老 DB（只有 v2 列），跑 migrate 后 v3 列被加上。"""
    db_path = str(tmp_path / "old.db")
    # 手动建一个 v2 schema 的 jobs 表（无 v3 列）
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (2, '2026-07-18');
        CREATE TABLE jobs (
          job_id TEXT PRIMARY KEY,
          target_name TEXT NOT NULL,
          city TEXT NOT NULL,
          title TEXT,
          status TEXT NOT NULL DEFAULT 'found',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        INSERT INTO jobs (job_id, target_name, city, title, status, created_at, updated_at)
          VALUES ('old1', 'T', '上海', '老岗位', 'skipped', '2026-07-18', '2026-07-18');
    """)
    conn.commit()
    conn.close()

    # 跑 migrate（应在 v2 基础上加 v3 列）
    with db_mod.connect(db_path) as conn:
        db_mod.migrate(conn)

    with db_mod.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        # v3 列已加上
        assert "level_match" in cols
        assert "job_category" in cols
        assert "category_passed" in cols
        assert "business_domain_match_score" in cols
        # 老数据保留
        row = conn.execute("SELECT job_id, title, status FROM jobs WHERE job_id='old1'").fetchone()
        assert row is not None
        assert row["title"] == "老岗位"
        assert row["status"] == "skipped"


# ============================================================
# upsert_job 写入 + 读取 v3 字段
# ============================================================
def test_upsert_writes_v3_fields(tmp_path: Path):
    """upsert_job 能写入 v3 字段。"""
    db_path = str(tmp_path / "test.db")
    db_mod.init_db(db_path)
    with db_mod.connect(db_path) as conn:
        job = JobRow(
            job_id="j1", target_name="T", city="上海", title="产品经理",
            status="found",
            level_match="match",
            job_category="产品经理",
            category_passed=True,
            business_domain_match_score=0.85,
        )
        db_mod.upsert_job(conn, job)
        # 读回
        loaded = db_mod.get_job(conn, "j1")
        assert loaded is not None
        assert loaded.level_match == "match"
        assert loaded.job_category == "产品经理"
        assert loaded.category_passed is True  # bool 类型转换
        assert loaded.business_domain_match_score == 0.85


def test_upsert_handles_null_v3_fields(tmp_path: Path):
    """upsert_job 处理 v3 字段为 None（老数据兼容）。"""
    db_path = str(tmp_path / "test.db")
    db_mod.init_db(db_path)
    with db_mod.connect(db_path) as conn:
        job = JobRow(
            job_id="j2", target_name="T", city="上海",
            status="found",
            # 不设 v3 字段，全为 None
        )
        db_mod.upsert_job(conn, job)
        loaded = db_mod.get_job(conn, "j2")
        assert loaded is not None
        assert loaded.level_match is None
        assert loaded.job_category is None
        assert loaded.category_passed is None
        assert loaded.business_domain_match_score is None


def test_category_passed_roundtrip(tmp_path: Path):
    """category_passed True/False/None 三态正确存取。"""
    db_path = str(tmp_path / "test.db")
    db_mod.init_db(db_path)
    with db_mod.connect(db_path) as conn:
        for jid, val in [("t", True), ("f", False), ("n", None)]:
            job = JobRow(
                job_id=jid, target_name="T", city="上海",
                status="found", category_passed=val,
            )
            db_mod.upsert_job(conn, job)
        assert db_mod.get_job(conn, "t").category_passed is True
        assert db_mod.get_job(conn, "f").category_passed is False
        assert db_mod.get_job(conn, "n").category_passed is None


# ============================================================
# transition 白名单含 v3 字段
# ============================================================
def test_transition_updates_v3_fields(tmp_path: Path):
    """transition 能更新 v3 字段（白名单已加）。"""
    db_path = str(tmp_path / "test.db")
    db_mod.init_db(db_path)
    with db_mod.connect(db_path) as conn:
        # 先插一个 found 状态
        job = JobRow(job_id="j3", target_name="T", city="上海", status="found")
        db_mod.upsert_job(conn, job)
        # transition 到 jd_analyzed，带 v3 字段
        db_mod.transition(conn, "j3", "jd_analyzed", set_fields={
            "match_score": 0.85,
            "level_match": "match",
            "job_category": "产品经理",
            "category_passed": True,
            "business_domain_match_score": 0.9,
        })
        loaded = db_mod.get_job(conn, "j3")
        assert loaded.status == "jd_analyzed"
        assert loaded.match_score == 0.85
        assert loaded.level_match == "match"
        assert loaded.job_category == "产品经理"
        assert loaded.category_passed is True
        assert loaded.business_domain_match_score == 0.9


# ============================================================
# 迁移幂等性
# ============================================================
def test_migrate_idempotent_with_v3(tmp_path: Path):
    """多次调用 migrate 不会报错（幂等）。"""
    db_path = str(tmp_path / "test.db")
    db_mod.init_db(db_path)
    # 再次 migrate
    with db_mod.connect(db_path) as conn:
        db_mod.migrate(conn)  # 不抛
    # v3 列还在
    with db_mod.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "level_match" in cols
