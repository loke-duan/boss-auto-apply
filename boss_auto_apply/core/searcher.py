"""F2 岗位搜索：抽象 + boss-cli + Mock（设计 §5.8，改造2：换 jackwener/boss-cli）。

关键：
- 改造2：从 can4hou6joeng4/boss-agent-cli 换成 **jackwener/boss-cli**
  （PyPI 包名 ``kabi-boss-cli``，命令名 ``boss``，Apache-2.0）。
- jackwener/boss-cli 的 ``greet`` 可用（``boss greet <securityId>``，内置 1.5s 防风控延迟），
  故 ``BossCliSearcher.greet()`` 不再 ``raise NotImplementedError``：dry_run=True 返回 mock True，
  dry_run=False（M3+）真调 ``boss greet``。
- 注意：jackwener greet 只能发文字，**图片简历仍需 DrissionPage**（M3）。
- 输出契约：``--json`` flag，信封 ``{ok, schema_version, data}`` / error ``{code, message}``。
- JobRaw 字段映射（jackwener schema，驼峰）：``securityId → job_id``, ``jobName → title``,
  ``brandName → company``。同时兼容下划线风格（旧 fixture / mock）。
- M1+M2 默认 Mock（不依赖真 boss 命令）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from ..errors import SearcherError

__all__ = [
    "BossSearcher",
    "SearchParams",
    "JobRaw",
    "MockBossSearcher",
    "BossCliSearcher",
    "get_searcher",
    "dedupe_jobs",
    "BOSS_ENVELOPE_OK_KEY",
]


@runtime_checkable
class BossSearcher(Protocol):
    """搜索器协议。"""

    def search(self, params: SearchParams) -> list["JobRaw"]:
        """按关键词/城市/薪资搜索，返回岗位元数据列表。"""
        ...

    def fetch_detail(self, job_id: str) -> str:
        """拉单个岗位完整 JD 文本。"""
        ...

    def greet(self, job_id: str, message: str = "", *, dry_run: bool | None = None) -> bool:
        """M3 才真发；M1+M2 默认实现返回 True（dry-run）。"""
        ...


# ============================================================
# 数据结构
# ============================================================
@dataclass
class SearchParams:
    """搜索参数。"""

    keyword: str
    city: str
    salary: str = ""
    experience: str = ""
    degree: str = ""
    per_job_keywords: list[str] = field(default_factory=list)
    limit: int = 20
    target_name: str = ""        # 所属方向（写 jobs.target_name）
    sub_direction: str = ""
    weight: float = 0.0
    # WebSearcher（DrissionPage）专用字段，mock/boss-cli 不用
    page: int = 1                 # 起始页码
    scroll_count: int = 3         # 无限滚动次数（加载更多岗位）
    per_page_limit: int = 20      # 每次搜索最多抓多少条


@dataclass
class JobRaw:
    """搜回的单个岗位元数据（写库前的中间态）。"""

    job_id: str
    title: str
    company: str
    city: str
    salary: str = ""
    experience: str = ""
    degree: str = ""
    jd_full: str = ""
    jd_url: str | None = None
    skills_required: list[str] = field(default_factory=list)


BOSS_ENVELOPE_OK_KEY = "ok"


# ============================================================
# MockBossSearcher（默认，M1+M2 dry-run）
# ============================================================
class MockBossSearcher:
    """从 fixtures/jd.sample.json 读预置 JD。

    用于 M1+M2 dry-run，不依赖 boss-cli、不触网。
    """

    name = "mock"

    def __init__(self, fixtures_dir: str = "tests/fixtures", *, logger_obj: Any = None) -> None:
        self.log = logger_obj or logger
        self.fixtures_dir = fixtures_dir
        self._cache: list[dict[str, Any]] | None = None

    def _load_fixtures(self) -> list[dict[str, Any]]:
        """惰性加载 fixtures/jd.sample.json。"""
        if self._cache is not None:
            return self._cache
        path = Path(self.fixtures_dir) / "jd.sample.json"
        if not path.exists():
            raise SearcherError(f"Mock fixtures 不存在：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SearcherError(f"Mock fixtures JSON 损坏：{path} ({e})") from e
        # 兼容两种结构：list 或 {"data":[...]} 信封
        if isinstance(data, list):
            self._cache = data
        elif isinstance(data, dict):
            self._cache = data.get("data", []) if isinstance(data.get("data"), list) else []
        else:
            self._cache = []
        self.log.debug(f"Mock 加载 {len(self._cache)} 条 JD fixtures")
        return self._cache

    def search(self, params: SearchParams) -> list[JobRaw]:
        """按 keyword/city 过滤 fixtures，返回前 ``params.limit`` 条。

        Args:
            params: 搜索参数。

        Returns:
            ``JobRaw`` 列表。
        """
        fixtures = self._load_fixtures()
        keyword = params.keyword.strip()
        results: list[JobRaw] = []
        for item in fixtures:
            # city 必须匹配
            if params.city and item.get("city") != params.city:
                continue
            # keyword 命中：title 或 keywords 任一含（兼容驼峰/下划线）
            title = item.get("jobName") or item.get("job_name") or item.get("title") or ""
            item_kws = (item.get("skillsRequired") or item.get("skills_required")
                        or item.get("skills") or item.get("keywords") or [])
            haystack = title + " " + " ".join(item_kws) + " " + (
                item.get("jd_full") or item.get("jobDescription") or "")
            if keyword and keyword.lower() not in haystack.lower():
                continue
            results.append(_fixture_to_jobraw(item))
            if len(results) >= params.limit:
                break
        self.log.info(f"Mock search keyword={keyword!r} city={params.city!r} → {len(results)} 条")
        return results

    def fetch_detail(self, job_id: str) -> str:
        """从 fixtures 读 jd_full。"""
        fixtures = self._load_fixtures()
        for item in fixtures:
            jid = str(item.get("securityId") or item.get("security_id")
                      or item.get("job_id") or "")
            if jid == job_id:
                return item.get("jd_full") or item.get("jobDescription") or ""
        raise SearcherError(f"Mock fetch_detail 失败：job_id 不存在 {job_id!r}")

    def greet(self, job_id: str, message: str) -> bool:  # noqa: ARG002
        """Mock：永远成功（dry-run）。"""
        self.log.debug(f"Mock greet（dry-run，不真发）：{job_id}")
        return True


def _fixture_to_jobraw(item: dict[str, Any]) -> JobRaw:
    """把 boss 信封 data[] 一条或 fixture dict 转 JobRaw。

    改造2 jackwener/boss-cli 字段映射（驼峰，优先）+ 兼容旧下划线风格：
    - securityId / security_id → job_id
    - jobName / job_name → title
    - brandName / brand_name → company
    """
    job_id = str(
        item.get("securityId") or item.get("security_id")
        or item.get("job_id") or item.get("id") or ""
    )
    skills = item.get("skills_required") or item.get("skills") or item.get("skillsRequired") or []
    return JobRaw(
        job_id=job_id,
        title=item.get("jobName") or item.get("job_name") or item.get("title") or "",
        company=item.get("brandName") or item.get("brand_name") or item.get("company") or "",
        city=item.get("city") or "",
        salary=item.get("salary") or "",
        experience=item.get("experience") or item.get("jobExperience") or item.get("job_experience") or "",
        degree=item.get("degree") or item.get("jobDegree") or item.get("job_degree") or "",
        jd_full=item.get("jd_full") or item.get("jobDescription") or item.get("job_description") or "",
        jd_url=item.get("jd_url") or item.get("jdUrl"),
        skills_required=list(skills) if isinstance(skills, list) else [],
    )


# ============================================================
# BossCliSearcher（真实现，jackwener/boss-cli，M3 才用）
# ============================================================
class BossCliSearcher:
    """subprocess 调 ``boss`` 命令（来自 PyPI 包 ``kabi-boss-cli``，仓库 jackwener/boss-cli）。

    ⚠️ **M3 降级为备用**（设计 §16.1 / §1.2）：实测当前 Boss 反爬下不可用——
    ``boss-cli`` 的 ``client.py`` 不把 ``__zp_stoken__`` 注入请求 header，
    ``boss search``/``greet``/``me`` 全报「登录失效 code=7/200404」。
    **M3 主路径改用 :class:`~boss_auto_apply.browser.web_searcher.WebSearcher`**（DrissionPage），
    ``greet`` 主路径改用 :class:`~boss_auto_apply.browser.web_greeter.WebGreeter`（点击沟通）。

    输出契约（``--json`` flag）：
        成功：``{"ok": true, "schema_version": "...", "data": [...]}``
        失败：``{"ok": false, "error": {"code": "...", "message": "..."}}``

    jackwener 错误码：``not_authenticated`` / ``rate_limited`` / ``invalid_params``
    / ``api_error`` / ``unknown_error``。

    dry_run 副作用隔离：``greet(dry_run=True)`` 不真调，返回 mock True。
    """

    name = "boss-cli"

    def __init__(
        self,
        bin_path: str = "boss",
        *,
        dry_run: bool = True,
        run_fn: Any = None,
        logger_obj: Any = None,
    ) -> None:
        self.bin_path = bin_path
        self.dry_run = dry_run
        self._run_fn = run_fn or subprocess.run
        self.log = logger_obj or logger

    def _check_bin(self) -> None:
        if not shutil.which(self.bin_path):
            raise SearcherError(
                f"boss 命令未找到（{self.bin_path}）。"
                "请先安装：uv tool install kabi-boss-cli && boss login；"
                "或改用 search.provider=mock 跑 dry-run。"
            )

    def search(self, params: SearchParams) -> list[JobRaw]:
        """subprocess: ``boss search "{keyword}" --city {city} --salary ... --json``。

        解析 stdout JSON 信封的 data[] → JobRaw（jackwener 驼峰字段）。
        """
        self._check_bin()
        cmd = [
            self.bin_path, "search", params.keyword,
            "--city", params.city,
        ]
        if params.salary:
            cmd += ["--salary", params.salary]
        if params.experience:
            cmd += ["--exp", params.experience]
        if params.degree:
            cmd += ["--degree", params.degree]
        if params.limit:
            cmd += ["--n", str(params.limit)]
        cmd += ["--json"]

        envelope = self._run_envelope(cmd)
        if not envelope.get(BOSS_ENVELOPE_OK_KEY):
            err = envelope.get("error") or {}
            self._raise_envelope_error(err)
        data = envelope.get("data") or []
        return [_fixture_to_jobraw(it) for it in data]

    def fetch_detail(self, security_id: str) -> str:
        """subprocess: ``boss detail {security_id} --json`` → data.jd_full / jobDescription。"""
        self._check_bin()
        cmd = [self.bin_path, "detail", security_id, "--json"]
        envelope = self._run_envelope(cmd)
        if not envelope.get(BOSS_ENVELOPE_OK_KEY):
            err = envelope.get("error") or {}
            self._raise_envelope_error(err)
        data = envelope.get("data") or {}
        return data.get("jd_full") or data.get("jobDescription") or data.get("job_description") or ""

    def greet(self, job_id: str, message: str = "", *, dry_run: bool | None = None) -> bool:
        """jackwener/boss-cli 的 greet（**M3 降级备用，当前反爬下不可用**）。

        ⚠️ 实测 ``boss greet`` 实为 ``add_friend``（建立沟通关系），**不能发话术**；
        且因 stoken header 缺陷，当前 Boss 反爬下报 code=7/200404 登录失效。
        **M3 主路径用 :class:`~boss_auto_apply.browser.web_greeter.WebGreeter`**（点击沟通）。

        Args:
            job_id: 岗位 securityId。
            message: 打招呼文字（jackwener greet 只能发文字；图片简历走 DrissionPage M3）。
            dry_run: 显式指定 dry-run；None 则用构造时的 self.dry_run。

        Returns:
            True 表示发送成功（dry_run 时是 mock True）。

        Note:
            dry_run=True（M1+M2 默认）不真调，保持副作用隔离；
            dry_run=False（M3+）才真调 boss greet（但当前反爬下会失败，建议用 WebGreeter）。
        """
        is_dry = self.dry_run if dry_run is None else dry_run
        if is_dry:
            self.log.debug(f"greet（dry-run，不真发）：{job_id}")
            return True
        self._check_bin()
        cmd = [self.bin_path, "greet", job_id]
        if message:
            cmd += ["--message", message]
        cmd += ["--json"]
        envelope = self._run_envelope(cmd)
        if not envelope.get(BOSS_ENVELOPE_OK_KEY):
            err = envelope.get("error") or {}
            self._raise_envelope_error(err)
        self.log.info(f"boss greet 真发送成功：{job_id}（jackwener，文字）")
        return True

    # ---- 内部 ----
    def _run_envelope(self, cmd: list[str]) -> dict[str, Any]:
        """跑一次 boss 命令，解析 JSON 信封。"""
        try:
            proc = self._run_fn(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as e:
            raise SearcherError(f"boss 命令超时：{' '.join(cmd)}") from e
        out = (proc.stdout or "").strip()
        if not out:
            raise SearcherError(f"boss 命令无输出：{' '.join(cmd)}（stderr={proc.stderr[:200]}）")
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError as e:
            raise SearcherError(f"boss 输出非 JSON：{out[:200]}") from e
        return envelope

    def _raise_envelope_error(self, err: dict[str, Any]) -> None:
        """按 jackwener boss 错误码分类抛错。"""
        code = err.get("code") or err.get("error_code") or "UNKNOWN"
        msg = err.get("message") or err.get("msg") or str(err)
        # jackwener: not_authenticated
        if code in ("not_authenticated", "AUTH_REQUIRED", "AUTH_EXPIRED"):
            raise SearcherError(f"boss 未登录或登录过期：{msg}。请跑 `boss login`。")
        # jackwener: rate_limited
        if code in ("rate_limited", "RATE_LIMITED"):
            raise SearcherError(f"boss 限流：{msg}。稍后重试。")
        if code in ("invalid_params", "INVALID_PARAMS"):
            raise SearcherError(f"boss 参数错误：{msg}。")
        if code in ("ACCOUNT_RISK",):
            raise SearcherError(
                f"boss 账号风控：{msg}。M3 用 DrissionPage 通道处理，不重试。"
            )
        raise SearcherError(f"boss 命令失败 [{code}]：{msg}")


# ============================================================
# 工厂 + 去重
# ============================================================
def get_searcher(provider: str, **kwargs: Any) -> BossSearcher:
    """工厂：mock / boss-cli / drissionpage。

    - ``mock``：M1+M2 默认，读 fixtures（dry-run）。
    - ``boss-cli``：备用（当前 Boss 反爬下 stoken header 缺陷不可用，降级 mock）。
    - ``drissionpage``：**M3+ 主路径**（WebSearcher，浏览器网页搜索）。

    boss-cli 未装时不抛错，降级 mock 并 warning（设计测试用例 6）。
    drissionpage 需传 browser_manager 关键字参数。

    Args:
        provider: ``"mock"`` / ``"boss-cli"`` / ``"drissionpage"``。
        **kwargs: 各 searcher 构造参数；drissionpage 需 ``browser_manager``。

    Returns:
        ``BossSearcher`` 实例。
    """
    log = kwargs.get("logger_obj") or logger
    if provider == "mock":
        return MockBossSearcher(kwargs.get("fixtures_dir", "tests/fixtures"), logger_obj=log)
    if provider == "boss-cli":
        # 探测 boss 命令是否装了
        if not shutil.which(kwargs.get("bin_path", "boss")):
            log.warning("boss-cli 未装，降级 MockBossSearcher（dry-run 可跑）")
            return MockBossSearcher(kwargs.get("fixtures_dir", "tests/fixtures"), logger_obj=log)
        return BossCliSearcher(kwargs.get("bin_path", "boss"), logger_obj=log)
    if provider == "drissionpage":
        # M3+ 主路径：WebSearcher（设计 §16.1）
        from ..browser.web_searcher import WebSearcher
        bm = kwargs.get("browser_manager")
        if bm is None:
            raise ValueError("drissionpage searcher 需要 browser_manager 关键字参数")
        return WebSearcher(bm, logger_obj=log)  # type: ignore[return-value]
    raise ValueError(f"未知 searcher provider：{provider}")


def dedupe_jobs(
    jobs: list[JobRaw],
    *,
    by_company: bool = False,
    exclude_companies: list[str] | None = None,
    seen_job_ids: set[str] | None = None,
    score_by_job_id: dict[str, float] | None = None,
) -> list[JobRaw]:
    """去重/黑名单过滤（pipeline 层调 searcher 后处理）。

    - 按 job_id 去重（DB 主键天然）。
    - ``by_company=True`` → 同公司只留 match_score 最高的一岗。
    - ``exclude_companies`` → 过滤。

    Args:
        jobs: 待过滤的 job 列表。
        by_company: 是否同公司去重。
        exclude_companies: 排除公司名单。
        seen_job_ids: 已入库的 job_id 集合（这些直接跳过）。
        score_by_job_id: job_id → match_score（by_company 时选最高分）。

    Returns:
        过滤后的 job 列表。
    """
    exclude = set(exclude_companies or [])
    seen = seen_job_ids or set()
    scores = score_by_job_id or {}

    out: list[JobRaw] = []
    seen_id_locally: set[str] = set()
    best_by_company: dict[str, JobRaw] = {}

    for j in jobs:
        if j.job_id in seen or j.job_id in seen_id_locally:
            continue
        if j.company in exclude:
            continue
        seen_id_locally.add(j.job_id)
        if by_company and j.company:
            existing = best_by_company.get(j.company)
            if existing is None or scores.get(j.job_id, 0) > scores.get(existing.job_id, 0):
                if existing is not None:
                    # 把之前留的从 out 移除
                    out = [x for x in out if x.job_id != existing.job_id]
                best_by_company[j.company] = j
                out.append(j)
        else:
            out.append(j)
    return out
