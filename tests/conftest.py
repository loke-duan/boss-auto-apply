"""共享 fixtures（设计 §12.1）。

提供：
- mock claude subprocess（FakeClaudeClient，替代 ClaudeClient 真调用）
- mock boss 命令（MockBossSearcher 已在 core 里，fixtures_dir 指向 tests/fixtures）
- tmp db（tmp_path 临时库）
- sample master（读 tests/fixtures/master.sample.json）
- sample JD（读 tests/fixtures/jd.sample.json）
- 基础 AppConfig（dry-run）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# 把项目根加入 sys.path，便于 `import boss_auto_apply`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES_DIR = ROOT / "tests" / "fixtures"


# ============================================================
# sample 数据
# ============================================================
@pytest.fixture
def fixtures_dir() -> str:
    """fixtures 目录绝对路径。"""
    return str(FIXTURES_DIR)


@pytest.fixture
def sample_master() -> dict[str, Any]:
    """读 master.sample.json（设计 §4.2，已体检 pending）。"""
    with open(FIXTURES_DIR / "master.sample.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def passed_master(sample_master: dict[str, Any]) -> dict[str, Any]:
    """体检通过的 master（health_check_status='passed'）。"""
    m = json.loads(json.dumps(sample_master))  # deep copy
    m["health_check_status"] = "passed"
    return m


@pytest.fixture
def sample_jds() -> list[dict[str, Any]]:
    """读 jd.sample.json 的 data[]。"""
    with open(FIXTURES_DIR / "jd.sample.json", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("data", []) if isinstance(data, dict) else data


@pytest.fixture
def sample_resume_pdf() -> str:
    """测试用简历 PDF 路径。"""
    return str(FIXTURES_DIR / "resume.sample.pdf")


# ============================================================
# tmp db
# ============================================================
@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    """临时 SQLite 文件路径。"""
    return str(tmp_path / "test.db")


@pytest.fixture
def tmp_conn(tmp_db: str):
    """初始化并返回一个 autocommit 连接（测试结束关闭）。

    用 isolation_level=None（autocommit），与 db.connect 一致；
    写操作各自用 BEGIN IMMEDIATE/COMMIT 管事务。
    """
    from boss_auto_apply import db as db_mod
    db_mod.init_db(tmp_db)
    import sqlite3
    conn = sqlite3.connect(tmp_db, isolation_level=None, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()


# ============================================================
# FakeClaudeClient（mock ClaudeClient）
# ============================================================
class FakeClaudeClient:
    """替身 ClaudeClient：不真调 subprocess，按预置响应返回。

    用法：
        fake = FakeClaudeClient()
        fake.responses[("hrbp",)] = {...}      # 按 purpose 预置
        fake.responses["default"] = {...}       # 默认兜底
        fake.call_log.append(...)               # 记录调用
    """

    def __init__(self, *, login_ok: bool = True) -> None:
        self.login_ok = login_ok
        self.responses: dict[tuple, Any] = {}
        self.default_response: dict[str, Any] | str = {"text": "ok"}
        self.call_log: list[dict[str, Any]] = []
        self.invoke_count = 0
        # 伪装 cfg，让 core 模块 getattr(llm, 'cfg').model_* 能取到
        class _FakeCfg:
            backend = "claude"
            claude_bin = "claude"
            model_tailor = "sonnet"
            model_profiler = "sonnet"
            model_hrbp = "sonnet"
            model_matcher = "haiku"
            model_greeter = "haiku"
            cache = True
        self.cfg = _FakeCfg()

    def check_login(self) -> None:
        if not self.login_ok:
            from boss_auto_apply.errors import ClaudeLoginRequiredError
            raise ClaudeLoginRequiredError("claude auth login")

    def invoke(self, prompt: str, *, model: str | None = None,
               attachments: list[str] | None = None, expect_json: bool = False,
               json_schema: str | None = None, purpose: str = "generic",
               cache_key: str | None = None, cwd: str | None = None,
               timeout_sec: int | None = None) -> Any:
        from boss_auto_apply.llm import LlmResult
        self.invoke_count += 1
        self.call_log.append({
            "purpose": purpose, "model": model, "expect_json": expect_json,
            "cache_key": cache_key, "prompt_snippet": prompt[:80],
        })

        # 缓存命中（cache_conn 模拟）
        resp = self.responses.get((purpose,)) 
        if resp is None:
            resp = self.default_response

        # 支持 callable 响应（按 prompt 动态生成）
        if callable(resp):
            resp = resp(prompt=prompt, purpose=purpose)

        if isinstance(resp, dict):
            return LlmResult(text=json.dumps(resp, ensure_ascii=False), raw_json=resp,
                             model_used=model or "fake")
        # 异常
        if isinstance(resp, Exception):
            raise resp
        return LlmResult(text=str(resp), raw_json=None, model_used=model or "fake")


@pytest.fixture
def fake_llm() -> FakeClaudeClient:
    """未登录会抛错的 FakeClaudeClient（默认登录 OK）。"""
    return FakeClaudeClient(login_ok=True)


@pytest.fixture
def fake_llm_with_cache(tmp_conn) -> FakeClaudeClient:
    """带 DB 缓存的 FakeClaudeClient（cache_get/cache_put 走真 DB）。"""
    fake = FakeClaudeClient(login_ok=True)
    # 注入真 cache_conn，让 llm.py 的缓存逻辑生效（但 invoke 已被 fake 替换）
    # 这里我们让 fake 自己模拟缓存命中计数
    fake._cache_conn = tmp_conn
    return fake


# ============================================================
# FakeCompletedProcess（mock subprocess.run）
# ============================================================
def make_completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    """构造 CompletedProcess（mock subprocess.run 返回值）。"""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def make_proc():
    """构造 CompletedProcess 的工厂 fixture。"""
    return make_completed


# ============================================================
# 基础 AppConfig
# ============================================================
@pytest.fixture
def base_config(tmp_path: Path) -> Any:
    """基础 dry-run AppConfig（tmp_path 下的路径）。"""
    from boss_auto_apply.config import AppConfig, PipelineCfg, LlmCfg, SearchCfg
    cfg = AppConfig(
        pipeline=PipelineCfg(mode="dry-run", dry_run=True),
        cities=["上海", "武汉"],
        target_constraints={
            "primary_direction": "流量增长/SEO",
            "salary": {"上海": "12-18K", "武汉": "8-12K"},
            "degree": "大专",
            "experience": "3-5年",
            "sub_directions": [
                {"name": "SEO/网站运营", "weight": 0.5, "keywords_hint": ["SEO", "网站运营"]},
                {"name": "私域运营", "weight": 0.3, "keywords_hint": ["私域运营"]},
                {"name": "投放运营", "weight": 0.2, "keywords_hint": ["信息流"], "warning": "需转型叙事"},
            ],
        },
        llm=LlmCfg(backend="claude", claude_bin="claude"),
        limits={"daily_total": 15, "active_hours": [9, 18]},
        pdf={"template": "brilliant-cv", "font": "Noto Sans CJK SC", "typst_bin": "typst",
             "template_dir": "templates/brilliant-cv", "png_dpi": 200},
        paths={
            "resume_input": str(FIXTURES_DIR / "resume.sample.pdf"),
            "master_json": str(tmp_path / "master.json"),
            "master_health": str(tmp_path / "master.health.json"),
            "targets_confirmed": str(tmp_path / "targets.confirmed.yaml"),
            "resumes_out": str(tmp_path / "resumes"),
            "db": str(tmp_path / "jobs.db"),
            "logs": str(tmp_path / "logs"),
        },
        search=SearchCfg(provider="mock", fixtures_dir=str(FIXTURES_DIR), per_target_limit=20),
    )
    return cfg


# ============================================================
# typst 可用性探测（skipif）
# ============================================================
def _typst_available() -> bool:
    return shutil.which("typst") is not None


requires_typst = pytest.mark.skipif(
    not _typst_available(), reason="typst 未装，跳过编译类测试"
)


# ============================================================
# M3 Mock DrissionPage fixtures（设计 §14.2）
# ============================================================
# 加载 tests/fixtures/dom_samples.py（该目录无 __init__.py；用唯一模块名避免与
# site-packages 的 tests 包冲突）
import importlib.util as _ilu

_dom_path = FIXTURES_DIR / "dom_samples.py"
if "dom_samples" not in sys.modules:
    if _dom_path.exists():
        _spec = _ilu.spec_from_file_location("dom_samples", _dom_path)
        if _spec is not None and _spec.loader is not None:
            dom_samples = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(dom_samples)
            sys.modules["dom_samples"] = dom_samples
    else:
        dom_samples = None
else:
    dom_samples = sys.modules["dom_samples"]


class FakeChromiumOptions:
    """替身 DrissionPage.ChromiumOptions（记录所有 set_* 调用，便于断言）。"""

    def __init__(self) -> None:
        self.user_data_path: str | None = None
        self.headless_flag: bool | None = None
        self.arguments: list[str] = []
        self.user_agent: str | None = None
        self.browser_path: str | None = None
        self.load_mode: str | None = None
        self._auto_port = False

    def set_user_data_path(self, path: str) -> "FakeChromiumOptions":
        self.user_data_path = path
        return self

    def headless(self, flag: bool = True) -> "FakeChromiumOptions":
        self.headless_flag = flag
        return self

    def auto_port(self) -> "FakeChromiumOptions":
        self._auto_port = True
        return self

    def set_argument(self, arg: str) -> "FakeChromiumOptions":
        self.arguments.append(arg)
        return self

    def set_browser_path(self, path: str) -> "FakeChromiumOptions":
        self.browser_path = path
        return self

    def set_user_agent(self, ua: str) -> "FakeChromiumOptions":
        self.user_agent = ua
        return self

    def set_load_mode(self, mode: str) -> "FakeChromiumOptions":
        self.load_mode = mode
        return self


class FakeChromium:
    """替身 DrissionPage.Chromium（持有 tab，模拟浏览器进程）。"""

    def __init__(self, tab: Any | None = None) -> None:
        self._tab = tab or dom_samples.MockTab()
        self.quit_count = 0
        self.new_tab_count = 0
        self._alive = True

        class _FakeProc:
            def poll(self_inner) -> int | None:  # noqa: N805
                return None if self._alive else 0
        self.process = _FakeProc()

    @property
    def latest_tab(self) -> Any:
        return self._tab

    def new_tab(self, url: str | None = None) -> Any:
        self.new_tab_count += 1
        if url:
            self._tab.get(url)
        return self._tab

    def quit(self) -> None:
        self.quit_count += 1
        self._alive = False


@pytest.fixture
def mock_tab():
    """空的 MockTab（测试按需 register_page / set_cookies）。"""
    return dom_samples.MockTab()


@pytest.fixture
def mock_browser(mock_tab):
    """FakeChromium 实例（持有 mock_tab）。"""
    return FakeChromium(tab=mock_tab)


@pytest.fixture
def make_browser_manager(mock_tab):
    """构造 BrowserManager 的工厂（每个测试可自定义 tab/options/chromium_cls）。

    用法：
        mgr = make_browser_manager(tab=my_tab, chromium_cls=MyChrome)
        tab = mgr.ensure_browser()

    自动 reset_instance（测试隔离）。
    """
    from boss_auto_apply.browser.manager import BrowserConfig, BrowserManager
    BrowserManager.reset_instance()

    def _factory(
        *,
        cfg: BrowserConfig | None = None,
        tab: Any | None = None,
        chromium_cls: Any | None = None,
        options: FakeChromiumOptions | None = None,
    ) -> BrowserManager:
        _tab = tab or mock_tab
        _opts = options or FakeChromiumOptions()
        if chromium_cls is None:
            class _DefaultChrome(FakeChromium):
                def __init__(self, co: Any) -> None:  # noqa: ARG002
                    super().__init__(tab=_tab)
            chromium_cls = _DefaultChrome
        return BrowserManager(
            cfg or BrowserConfig(check_chrome_running=False),
            chromium_cls=chromium_cls,
            options_cls=lambda: _opts,
        )
    yield _factory
    BrowserManager.reset_instance()
