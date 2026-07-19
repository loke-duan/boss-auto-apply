"""SQLite 状态库 + 迁移 + 状态机原语（设计 §5.3、§4.1、§6.2）。

设计要点：
- 裸 ``sqlite3``，不引 SQLAlchemy ORM（降依赖）。
- 所有时间戳 ISO-8601 UTC（``datetime.now(timezone.utc).isoformat()``）。
- WAL 模式 + 外键开启 + Row dict 返回。
- 迁移走 ``schema_version`` 表，幂等（DDL 全 ``IF NOT EXISTS``）。
- 状态机 ``transition`` 按 §6.2 合法迁移表校验，非法抛 ``ValueError``。
- ``mark_failed`` 用 ``classify_error`` 决定 category；retry_count+1；超限 → skipped。
- ``database is locked`` 重试 3 次（指数退避）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from loguru import logger

from .errors import BossAutoError, classify_error
from .models import JobRow

__all__ = [
    "iso_now",
    "connect",
    "init_db",
    "migrate",
    "upsert_job",
    "get_job",
    "update_jd_full",
    "list_jobs_by_status",
    "list_resumable",
    "transition",
    "mark_skipped",
    "mark_failed",
    "start_run",
    "end_run",
    "get_run",
    "get_or_create_quota",
    "update_quota",
    "get_quota_history",
    "cache_get",
    "cache_put",
    "cache_hit_incr",
    "cache_evict_lru",
    "LEGAL_TRANSITIONS",
    "RESUMABLE_STATUSES",
    "MIGRATIONS",
]


# ============================================================
# 工具
# ============================================================
def iso_now() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（含时区）。"""
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 合法迁移表（设计 §6.2）
# ============================================================
# 注：failed/skipped 处理见 transition 内逻辑（failed 不在此表，由 mark_failed 单独管理）
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "found": {"jd_analyzed", "skipped", "failed"},
    "jd_analyzed": {"resume_tailored", "skipped", "failed"},
    "resume_tailored": {"pdf_generated", "failed"},
    "pdf_generated": {"image_ready", "failed"},
    "image_ready": {"greeted", "failed"},            # M3 WebGreeter 点击沟通
    "greeted": {"text_sent", "failed"},              # M3 新增：话术发送成功
    "text_sent": {"image_sent", "failed"},           # M3 新增：图片发送成功
    "image_sent": set(),  # 终态
    "skipped": set(),  # 终态
    "failed": set(),  # failed 由 mark_failed 管理，不允许 transition 直达
}

# 可断点续传的状态（list_resumable 用）。M3 加 greeted/text_sent：发送中途断点可续。
RESUMABLE_STATUSES = {"found", "jd_analyzed", "resume_tailored", "pdf_generated",
                      "image_ready", "greeted", "text_sent"}


# ============================================================
# 迁移脚本（设计 §4.1）
# ============================================================
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id              TEXT    PRIMARY KEY,
  target_name         TEXT    NOT NULL,
  keyword             TEXT,
  city                TEXT    NOT NULL,
  title               TEXT,
  company             TEXT,
  salary              TEXT,
  experience          TEXT,
  degree              TEXT,
  jd_full             TEXT,
  skills_required     TEXT,
  match_score         REAL    DEFAULT 0,
  match_reason        TEXT,
  jd_signature        TEXT,
  status              TEXT    NOT NULL DEFAULT 'found'
                      CHECK (status IN (
                        'found','jd_analyzed','resume_tailored',
                        'pdf_generated','image_ready',
                        'greeted','image_sent',
                        'skipped','failed'
                      )),
  tailored_resume_path TEXT,
  resume_pdf_path     TEXT,
  resume_image_path   TEXT,
  tailored_greet      TEXT,
  greet_sent_at       TEXT,
  image_sent_at       TEXT,
  error_msg           TEXT,
  error_category      TEXT
                       CHECK (error_category IS NULL
                              OR error_category IN ('network','business','risk','llm')),
  retry_count         INTEGER DEFAULT 0,
  health_checked      INTEGER DEFAULT 0,
  created_at          TEXT    NOT NULL,
  updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status        ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_target_city   ON jobs(target_name, city);
CREATE INDEX IF NOT EXISTS idx_jobs_jd_sig        ON jobs(jd_signature);
CREATE INDEX IF NOT EXISTS idx_jobs_company       ON jobs(company);

CREATE TABLE IF NOT EXISTS run_log (
  run_id      TEXT    PRIMARY KEY,
  started_at  TEXT    NOT NULL,
  ended_at    TEXT,
  mode        TEXT    NOT NULL CHECK (mode IN ('auto','confirm','manual','dry-run')),
  target_name TEXT,
  planned     INTEGER DEFAULT 0,
  succeeded   INTEGER DEFAULT 0,
  failed      INTEGER DEFAULT 0,
  skipped     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_quota (
  date_key         TEXT    PRIMARY KEY,
  sent_count       INTEGER DEFAULT 0,
  risk_events      INTEGER DEFAULT 0,
  cooldown_until   TEXT,
  hard_trip_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_cache (
  cache_key    TEXT    PRIMARY KEY,
  purpose      TEXT    NOT NULL CHECK (purpose IN ('tailor','matcher','profiler','hrbp','greeter')),
  target_name  TEXT,
  jd_signature TEXT,
  model        TEXT,
  response     TEXT    NOT NULL,
  tokens_in    INTEGER,
  tokens_out   INTEGER,
  hit_count    INTEGER DEFAULT 0,
  created_at   TEXT    NOT NULL,
  last_used_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_purpose_sig ON llm_cache(purpose, jd_signature);
CREATE INDEX IF NOT EXISTS idx_cache_last_used   ON llm_cache(last_used_at);
"""

# 未来版本示例（注释占位）
# _SCHEMA_V2 = "ALTER TABLE jobs ADD COLUMN foo TEXT;"

# ============================================================
# 迁移 v2（M3 §11.3）：jobs.status CHECK 加 text_sent + 加 text_sent_at 字段
# ============================================================
# SQLite 不支持直接改 CHECK 约束，用重建表策略：
#   1. 建 jobs_new（status CHECK 含 text_sent，加 text_sent_at 字段）
#   2. INSERT INTO jobs_new SELECT (原字段 + NULL) FROM jobs
#   3. DROP jobs; ALTER TABLE jobs_new RENAME TO jobs;
#   4. 重建索引（同 v1）
# daily_quota 也补 M3 新增列（用 ALTER TABLE ADD COLUMN 幂等探测）。
_SCHEMA_V2 = """
-- jobs 重建（加 text_sent 状态 + text_sent_at 字段）
CREATE TABLE jobs_new (
  job_id              TEXT    PRIMARY KEY,
  target_name         TEXT    NOT NULL,
  keyword             TEXT,
  city                TEXT    NOT NULL,
  title               TEXT,
  company             TEXT,
  salary              TEXT,
  experience          TEXT,
  degree              TEXT,
  jd_full             TEXT,
  skills_required     TEXT,
  match_score         REAL    DEFAULT 0,
  match_reason        TEXT,
  jd_signature        TEXT,
  status              TEXT    NOT NULL DEFAULT 'found'
                      CHECK (status IN (
                        'found','jd_analyzed','resume_tailored',
                        'pdf_generated','image_ready',
                        'greeted','text_sent','image_sent',
                        'skipped','failed'
                      )),
  tailored_resume_path TEXT,
  resume_pdf_path     TEXT,
  resume_image_path   TEXT,
  tailored_greet      TEXT,
  greet_sent_at       TEXT,
  text_sent_at        TEXT,
  image_sent_at       TEXT,
  error_msg           TEXT,
  error_category      TEXT
                       CHECK (error_category IS NULL
                              OR error_category IN ('network','business','risk','llm')),
  retry_count         INTEGER DEFAULT 0,
  health_checked      INTEGER DEFAULT 0,
  created_at          TEXT    NOT NULL,
  updated_at          TEXT    NOT NULL
);

-- 把旧表数据搬过来（旧表无 text_sent_at，用 NULL 占位）
-- 旧表列顺序与 jobs_new 前缀一致（见 v1），用显式列名映射避免顺序依赖
INSERT INTO jobs_new (
  job_id, target_name, keyword, city, title, company,
  salary, experience, degree, jd_full, skills_required,
  match_score, match_reason, jd_signature, status,
  tailored_resume_path, resume_pdf_path, resume_image_path,
  tailored_greet, greet_sent_at, text_sent_at, image_sent_at,
  error_msg, error_category, retry_count, health_checked,
  created_at, updated_at
)
SELECT
  job_id, target_name, keyword, city, title, company,
  salary, experience, degree, jd_full, skills_required,
  match_score, match_reason, jd_signature, status,
  tailored_resume_path, resume_pdf_path, resume_image_path,
  tailored_greet, greet_sent_at, NULL, image_sent_at,
  error_msg, error_category, retry_count, health_checked,
  created_at, updated_at
FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

-- 重建索引（同 v1）
CREATE INDEX IF NOT EXISTS idx_jobs_status        ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_target_city   ON jobs(target_name, city);
CREATE INDEX IF NOT EXISTS idx_jobs_jd_sig        ON jobs(jd_signature);
CREATE INDEX IF NOT EXISTS idx_jobs_company       ON jobs(company);

-- daily_quota 补 M3 新增列（ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS，
-- 用 PRAGMA table_info 探测；这里直接尝试 ADD，已存在列会抛错被吞）
-- [假设] SQLite ALTER TABLE ADD COLUMN 幂等：列已存在时忽略错误。
-- 迁移器 migrate() 对每个 version 的 sql 是 executescript 整体执行，
-- 故把 ALTER 放独立语句，由 _v2_post_migrate 容错执行。
"""

# daily_quota v2 新增列（[假设] ALTER ADD COLUMN 已存在列忽略；由 _try_add_column 容错）
_DAILY_QUOTA_V2_COLUMNS = [
    ("consecutive_run_days", "INTEGER DEFAULT 0"),
    ("last_send_at", "TEXT"),
    ("burst_window", "TEXT"),
    ("forced_mode", "TEXT"),
]

# jobs v3 新增列（ADR-0005 matcher 通用化重构）
# - level_match：资历级别判断（overqualified/match/underqualified/unknown）
# - job_category：识别到的岗位类别（产品经理/Java/销售/...）
# - category_passed：是否通过岗位类别一票否决（0/1）
# - business_domain_match_score：业务领域连续匹配度（0.0-1.0）
# 老数据（迁移前已存在的行）这些列为 NULL/0.0，下游 matcher 重跑时会回填。
_JOBS_V3_COLUMNS = [
    ("level_match", "TEXT"),
    ("job_category", "TEXT"),
    ("category_passed", "INTEGER"),
    ("business_domain_match_score", "REAL DEFAULT 0"),
]


def _try_add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """尝试给表加列；列已存在则忽略（SQLite ALTER ADD 无 IF NOT EXISTS）。

    Args:
        conn: 数据库连接。
        table: 表名。
        column: 列名。
        decl: 列定义（如 ``INTEGER DEFAULT 0``）。
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column in existing:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError:
        # 并发或已存在 → 忽略
        pass


MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
]


def migrate(conn: sqlite3.Connection) -> None:
    """执行所有未应用的迁移（幂等）。

    读 ``schema_version`` 最大版本号，逐个执行 version > current 的迁移脚本，
    每个迁移在独立事务里。

    Args:
        conn: 已连接的 sqlite3 连接。
    """
    # 表可能还没建（首次），用 IF NOT EXISTS 兜底
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    cur = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    current = (row[0] if row else 0) or 0
    applied = 0
    for version, sql in MIGRATIONS:
        if version > current:
            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (version, iso_now()),
                )
                conn.commit()
                applied += 1
                logger.info(f"db 迁移到 v{version}")
                # v2 后置：给 daily_quota 补 M3 新增列（ALTER ADD COLUMN 容错）
                if version == 2:
                    for col, decl in _DAILY_QUOTA_V2_COLUMNS:
                        _try_add_column(conn, "daily_quota", col, decl)
                    conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                raise BossAutoError(f"db 迁移到 v{version} 失败：{e}") from e
    # 幂等兜底：无论版本号如何，确保 daily_quota 列存在（旧库可能 schema_version=1 但缺列）
    for col, decl in _DAILY_QUOTA_V2_COLUMNS:
        _try_add_column(conn, "daily_quota", col, decl)
    # v3 兜底：jobs 表新增 matcher 通用化字段（ADR-0005）
    # 不重建表（CHECK 约束未变），直接 ADD COLUMN，老数据新列为 NULL/0。
    for col, decl in _JOBS_V3_COLUMNS:
        _try_add_column(conn, "jobs", col, decl)
    conn.commit()
    if applied == 0:
        logger.debug(f"db 已是最新版本 v{current}，无迁移")


# ============================================================
# 连接管理
# ============================================================
@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """开启 WAL + 外键 + Row dict 的连接；退出时关闭。

    ``database is locked`` 重试 3 次（指数退避）。

    事务策略：``isolation_level=None``（autocommit 模式）。各写操作（``transition`` /
    ``upsert_job`` / ``cache_put`` 等）用 ``BEGIN IMMEDIATE ... COMMIT`` 显式管理自己的事务；
    这样 ``migrate``（含 ``executescript``，会隐式提交）和 CRUD 共存不冲突，
    也避免嵌套 ``connect`` 时出现「no transaction is active」。

    Args:
        db_path: SQLite 文件路径。

    Yields:
        配置好的 ``sqlite3.Connection``。

    Raises:
        BossAutoError: 连接失败 / 重试耗尽仍 locked。
    """
    conn = sqlite3.Connection  # type: ignore[assignment]
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            conn = sqlite3.connect(
                db_path, isolation_level=None, timeout=10.0, check_same_thread=False
            )
            break
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower():
                time.sleep(0.2 * (2 ** attempt))
                continue
            raise BossAutoError(f"db 连接失败：{db_path} ({e})") from e
    else:
        raise BossAutoError(f"db 连接失败（重试耗尽）：{db_path} ({last_err})")

    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def init_db(db_path: str) -> None:
    """建目录 + connect + migrate（幂等，可重复调用）。

    Args:
        db_path: SQLite 文件路径。
    """
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with connect(db_path) as conn:
        migrate(conn)


# ============================================================
# Jobs CRUD
# ============================================================
def _row_to_job(row: sqlite3.Row) -> JobRow:
    """把 sqlite Row 转 JobRow（skills_required JSON 反序列化）。"""
    d = dict(row)
    skills = d.get("skills_required")
    if isinstance(skills, str) and skills:
        try:
            d["skills_required"] = json.loads(skills)
        except json.JSONDecodeError:
            d["skills_required"] = []
    elif skills is None:
        d["skills_required"] = []
    return JobRow(**d)


def upsert_job(conn: sqlite3.Connection, job: JobRow) -> None:
    """插入或更新一个 job（以 job_id 为主键，幂等）。

    Args:
        conn: 数据库连接。
        job: ``JobRow`` 数据。
    """
    now = iso_now()
    if not job.created_at:
        job.created_at = now
    if not job.updated_at:
        job.updated_at = now
    skills_json = json.dumps(job.skills_required, ensure_ascii=False)
    # category_passed 是 bool，DB 存 INTEGER；None 保持 NULL
    cat_passed = None if job.category_passed is None else int(bool(job.category_passed))
    conn.execute(
        """
        INSERT INTO jobs (
          job_id, target_name, keyword, city, title, company,
          salary, experience, degree, jd_full, skills_required,
          match_score, match_reason, jd_signature, status,
          tailored_resume_path, resume_pdf_path, resume_image_path,
          tailored_greet, greet_sent_at, text_sent_at, image_sent_at,
          error_msg, error_category, retry_count, health_checked,
          created_at, updated_at,
          level_match, job_category, category_passed, business_domain_match_score
        ) VALUES (
          :job_id, :target_name, :keyword, :city, :title, :company,
          :salary, :experience, :degree, :jd_full, :skills_required,
          :match_score, :match_reason, :jd_signature, :status,
          :tailored_resume_path, :resume_pdf_path, :resume_image_path,
          :tailored_greet, :greet_sent_at, :text_sent_at, :image_sent_at,
          :error_msg, :error_category, :retry_count, :health_checked,
          :created_at, :updated_at,
          :level_match, :job_category, :category_passed, :business_domain_match_score
        )
        ON CONFLICT(job_id) DO UPDATE SET
          target_name=excluded.target_name,
          keyword=excluded.keyword,
          city=excluded.city,
          title=excluded.title,
          company=excluded.company,
          salary=excluded.salary,
          experience=excluded.experience,
          degree=excluded.degree,
          jd_full=excluded.jd_full,
          skills_required=excluded.skills_required,
          match_score=excluded.match_score,
          match_reason=excluded.match_reason,
          jd_signature=excluded.jd_signature,
          tailored_resume_path=excluded.tailored_resume_path,
          resume_pdf_path=excluded.resume_pdf_path,
          resume_image_path=excluded.resume_image_path,
          tailored_greet=excluded.tailored_greet,
          health_checked=excluded.health_checked,
          level_match=excluded.level_match,
          job_category=excluded.job_category,
          category_passed=excluded.category_passed,
          business_domain_match_score=excluded.business_domain_match_score,
          updated_at=:updated_at
        """,
        {
            **job.model_dump(),
            "skills_required": skills_json,
            "category_passed": cat_passed,
            "updated_at": now,
        },
    )


def get_job(conn: sqlite3.Connection, job_id: str) -> JobRow | None:
    """按 job_id 取一个 job；不存在返回 None。"""
    cur = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    return _row_to_job(row) if row else None


def update_jd_full(conn: sqlite3.Connection, job_id: str, jd_full: str) -> None:
    """单独更新 jd_full 字段（流式抓 JD 后写回，设计 §5.7）。

    流式模式下，搜索阶段只拿列表页信息（jd_full 暂空），
    在处理单个 job 时才 fetch_detail 抓 JD 全文，抓完用此函数写回 DB。

    Args:
        conn: SQLite 连接。
        job_id: 岗位 ID。
        jd_full: JD 全文。
    """
    conn.execute(
        "UPDATE jobs SET jd_full = ?, updated_at = ? WHERE job_id = ?",
        (jd_full, iso_now(), job_id),
    )


def list_jobs_by_status(
    conn: sqlite3.Connection,
    status: str,
    target_name: str | None = None,
) -> list[JobRow]:
    """按状态列出 job；可选 target_name 过滤。"""
    if target_name:
        cur = conn.execute(
            "SELECT * FROM jobs WHERE status = ? AND target_name = ? ORDER BY created_at",
            (status, target_name),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at", (status,)
        )
    return [_row_to_job(r) for r in cur.fetchall()]


def list_resumable(conn: sqlite3.Connection, target_name: str | None = None) -> list[JobRow]:
    """返回所有可断点续传的 job（status ∈ 待处理态，不含 failed/skipped）。

    Args:
        target_name: 可选，限定方向。

    Returns:
        ``JobRow`` 列表。
    """
    placeholders = ",".join("?" * len(RESUMABLE_STATUSES))
    params: list[Any] = list(RESUMABLE_STATUSES)
    sql = f"SELECT * FROM jobs WHERE status IN ({placeholders})"
    if target_name:
        sql += " AND target_name = ?"
        params.append(target_name)
    sql += " ORDER BY created_at"
    cur = conn.execute(sql, params)
    return [_row_to_job(r) for r in cur.fetchall()]


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """按状态分组计数（status 命令用）。"""
    cur = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status"
    )
    return {r["status"]: r["n"] for r in cur.fetchall()}


# ============================================================
# 状态机原语
# ============================================================
def transition(
    conn: sqlite3.Connection,
    job_id: str,
    to_status: str,
    *,
    set_fields: dict[str, Any] | None = None,
    clear_error: bool = True,
) -> None:
    """原子更新 status + 字段；非法迁移抛 ``ValueError``。

    Args:
        conn: 数据库连接。
        job_id: 目标 job。
        to_status: 目标状态（必须在合法迁移表里）。
        set_fields: 额外要更新的字段（如 match_score/jd_signature 等）。
        clear_error: True 则清空 error_msg/error_category（成功推进时）。

    Raises:
        ValueError: job 不存在 / 迁移非法。
    """
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"transition 失败：job_id 不存在 {job_id!r}")
    frm = job.status
    legal = LEGAL_TRANSITIONS.get(frm, set())
    if to_status not in legal:
        raise ValueError(
            f"非法状态迁移：{frm!r} → {to_status!r}（合法目标：{sorted(legal) or '<终态>'}）"
        )

    fields: dict[str, Any] = {"status": to_status, "updated_at": iso_now()}
    if set_fields:
        fields.update(set_fields)
    if clear_error:
        fields.setdefault("error_msg", None)
        fields.setdefault("error_category", None)

    # 白名单字段（防注入）
    allowed = {
        "status", "updated_at", "error_msg", "error_category",
        "match_score", "match_reason", "jd_signature", "skills_required",
        "tailored_resume_path", "resume_pdf_path", "resume_image_path",
        "tailored_greet", "greet_sent_at", "text_sent_at", "image_sent_at",
        "health_checked", "retry_count",
        # v3（ADR-0005）matcher 通用化字段
        "level_match", "job_category", "category_passed", "business_domain_match_score",
    }
    set_clause = []
    params: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in allowed:
            logger.warning(f"transition 忽略非法字段 {k!r}")
            continue
        # skills_required 要 JSON 序列化
        if k == "skills_required" and isinstance(v, list):
            v = json.dumps(v, ensure_ascii=False)
        set_clause.append(f"{k} = :f_{k}")
        params[f"f_{k}"] = v
    params["job_id"] = job_id
    sql = f"UPDATE jobs SET {', '.join(set_clause)} WHERE job_id = :job_id"
    conn.execute(sql, params)


def mark_skipped(conn: sqlite3.Connection, job_id: str, reason: str) -> None:
    """把 job 标 ``skipped``（业务跳过终态）。

    skipped 是终态，可从 found/jd_analyzed 进入（设计 §6.2）。
    若当前状态不允许直接到 skipped，则先记 error 再尝试。
    """
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"mark_skipped 失败：job_id 不存在 {job_id!r}")
    legal = LEGAL_TRANSITIONS.get(job.status, set())
    if "skipped" in legal:
        transition(
            conn, job_id, "skipped",
            set_fields={"error_msg": reason, "error_category": "business"},
            clear_error=False,
        )
    else:
        # 已是终态或状态不允许直接 skipped，只更新 error 字段
        conn.execute(
            "UPDATE jobs SET error_msg = ?, error_category = 'business', updated_at = ? WHERE job_id = ?",
            (reason, iso_now(), job_id),
        )


def mark_failed(
    conn: sqlite3.Connection,
    job_id: str,
    err: BaseException,
    *,
    max_retries: int = 3,
) -> str:
    """记录失败：classify_error 决定 category；retry_count+1；超限 → skipped。

    设计 §6.2：失败留原态（除重试超限 → skipped）。

    Args:
        conn: 数据库连接。
        job_id: 目标 job。
        err: 触发失败的异常。
        max_retries: 最大重试次数，超过则转 skipped。

    Returns:
        最终状态：``failed``（留原态重试）或 ``skipped``（超限终态）。
    """
    category = classify_error(err)
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"mark_failed 失败：job_id 不存在 {job_id!r}")

    new_retry = job.retry_count + 1
    err_msg = f"{type(err).__name__}: {err}"[:2000]

    # business 类直接跳过（不重试）；或重试超限 → skipped
    final_status = job.status
    if category == "business" or new_retry > max_retries:
        # 转 skipped（若状态允许）
        legal = LEGAL_TRANSITIONS.get(job.status, set())
        if "skipped" in legal:
            transition(
                conn, job_id, "skipped",
                set_fields={
                    "error_msg": err_msg,
                    "error_category": category,
                    "retry_count": new_retry,
                },
                clear_error=False,
            )
            final_status = "skipped"
        else:
            # 状态不允许 skipped，只更新字段
            conn.execute(
                "UPDATE jobs SET error_msg = ?, error_category = ?, retry_count = ?, updated_at = ? WHERE job_id = ?",
                (err_msg, category, new_retry, iso_now(), job_id),
            )
            final_status = job.status
    else:
        # 留原态，retry_count+1
        conn.execute(
            "UPDATE jobs SET error_msg = ?, error_category = ?, retry_count = ?, updated_at = ? WHERE job_id = ?",
            (err_msg, category, new_retry, iso_now(), job_id),
        )
        final_status = job.status
    return final_status


# ============================================================
# run_log
# ============================================================
def start_run(conn: sqlite3.Connection, mode: str, target_name: str | None) -> str:
    """开始一次运行，返回 run_id（uuid4 hex）。"""
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO run_log (run_id, started_at, mode, target_name) VALUES (?, ?, ?, ?)",
        (run_id, iso_now(), mode, target_name),
    )
    return run_id


def end_run(
    conn: sqlite3.Connection,
    run_id: str,
    succeeded: int,
    failed: int,
    skipped: int,
    planned: int | None = None,
) -> None:
    """结束一次运行，写汇总计数。"""
    fields = {"ended_at": iso_now(), "succeeded": succeeded, "failed": failed, "skipped": skipped}
    if planned is not None:
        fields["planned"] = planned
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["run_id"] = run_id
    conn.execute(f"UPDATE run_log SET {set_clause} WHERE run_id = :run_id", fields)


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """取一次运行记录。"""
    cur = conn.execute("SELECT * FROM run_log WHERE run_id = ?", (run_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def list_recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """最近 N 次运行（status 命令用）。"""
    cur = conn.execute(
        "SELECT * FROM run_log ORDER BY started_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in cur.fetchall()]


# ============================================================
# daily_quota
# ============================================================
def get_or_create_quota(conn: sqlite3.Connection, date_key: str) -> dict[str, Any]:
    """取或建当日 quota 记录。"""
    cur = conn.execute("SELECT * FROM daily_quota WHERE date_key = ?", (date_key,))
    row = cur.fetchone()
    if row:
        d = dict(row)
        # 兜底：老记录可能缺 v2 列（迁移前建的），补默认值
        d.setdefault("consecutive_run_days", 0)
        d.setdefault("last_send_at", None)
        d.setdefault("burst_window", None)
        d.setdefault("forced_mode", None)
        return d
    conn.execute(
        "INSERT INTO daily_quota (date_key) VALUES (?)", (date_key,)
    )
    return {"date_key": date_key, "sent_count": 0, "risk_events": 0,
            "cooldown_until": None, "hard_trip_count": 0,
            "consecutive_run_days": 0, "last_send_at": None,
            "burst_window": None, "forced_mode": None}


# daily_quota 字段白名单（防注入）
_QUOTA_FIELDS = {
    "sent_count", "risk_events", "cooldown_until", "hard_trip_count",
    "consecutive_run_days", "last_send_at", "burst_window", "forced_mode",
}


def update_quota(
    conn: sqlite3.Connection,
    date_key: str,
    *,
    set_fields: dict[str, Any] | None = None,
    incr_fields: dict[str, int] | None = None,
) -> dict[str, Any]:
    """更新当日 quota（set 字段 + incr 字段）。幂等建记录。

    Args:
        conn: 数据库连接。
        date_key: 日期键（如 ``2026-07-10``）。
        set_fields: 直接赋值的字段（如 ``last_send_at``/``forced_mode``）。
        incr_fields: 增量字段（如 ``sent_count`` +1）。

    Returns:
        更新后的 quota dict。
    """
    # 确保记录存在
    get_or_create_quota(conn, date_key)
    clauses: list[str] = []
    params: dict[str, Any] = {"date_key": date_key}
    if set_fields:
        for k, v in set_fields.items():
            if k not in _QUOTA_FIELDS:
                logger.warning(f"update_quota 忽略非法字段 {k!r}")
                continue
            clauses.append(f"{k} = :s_{k}")
            params[f"s_{k}"] = v
    if incr_fields:
        for k, delta in incr_fields.items():
            if k not in _QUOTA_FIELDS:
                logger.warning(f"update_quota 忽略非法字段 {k!r}")
                continue
            clauses.append(f"{k} = COALESCE({k}, 0) + :i_{k}")
            params[f"i_{k}"] = int(delta)
    if clauses:
        sql = f"UPDATE daily_quota SET {', '.join(clauses)} WHERE date_key = :date_key"
        conn.execute(sql, params)
    return get_or_create_quota(conn, date_key)


def get_quota_history(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """取最近 N 天的 quota 记录（预热/连投统计用）。按 date_key 升序。"""
    cur = conn.execute(
        "SELECT * FROM daily_quota ORDER BY date_key DESC LIMIT ?", (days,)
    )
    return [dict(r) for r in cur.fetchall()]


# ============================================================
# llm_cache
# ============================================================
def cache_get(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    """按 cache_key 取缓存；命中返回 dict（含 response 反序列化），未命中 None。"""
    cur = conn.execute("SELECT * FROM llm_cache WHERE cache_key = ?", (cache_key,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    # response 是 JSON 串，反序列化
    try:
        d["response_obj"] = json.loads(d["response"])
    except (json.JSONDecodeError, TypeError):
        d["response_obj"] = None
    return d


def cache_put(
    conn: sqlite3.Connection,
    cache_key: str,
    purpose: str,
    response: str,
    *,
    target_name: str | None = None,
    jd_signature: str | None = None,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> None:
    """写缓存（INSERT OR REPLACE）。response 必须是 JSON 串。"""
    now = iso_now()
    conn.execute(
        """
        INSERT OR REPLACE INTO llm_cache
          (cache_key, purpose, target_name, jd_signature, model, response,
           tokens_in, tokens_out, hit_count, created_at, last_used_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (cache_key, purpose, target_name, jd_signature, model, response,
         tokens_in, tokens_out, now, now),
    )


def cache_hit_incr(conn: sqlite3.Connection, cache_key: str) -> None:
    """命中计数 +1，更新 last_used_at。"""
    conn.execute(
        "UPDATE llm_cache SET hit_count = hit_count + 1, last_used_at = ? WHERE cache_key = ?",
        (iso_now(), cache_key),
    )


def cache_evict_lru(conn: sqlite3.Connection, keep: int = 1000) -> int:
    """LRU 清理：保留最近 ``keep`` 条，删旧的。返回删除条数。"""
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM llm_cache"
    )
    total = cur.fetchone()[0]
    if total <= keep:
        return 0
    conn.execute(
        """
        DELETE FROM llm_cache WHERE cache_key IN (
          SELECT cache_key FROM llm_cache ORDER BY last_used_at ASC
          LIMIT ?
        )
        """,
        (total - keep,),
    )
    return total - keep
