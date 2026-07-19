"""test_cli_run_m3.py — CLI ``run`` 命令真发送路径测试（设计 §16.6）。

验证 M3 接通层（main.py _cmd_run）的正确性：
1. mock 搜索被拒（run 要求 provider 非 mock）
2. M3 预检致命失败 → 拒绝执行
3. 未登录 → 拒绝执行并提示 login
4. --yes 跳过风险确认 + 全链路真发送成功
5. 用户拒绝风险确认 → 友好退出
6. dry-run 仍走旧路径（不构建 sender）
7. _confirm_real_send_risk 交互逻辑

不触网：monkeypatch 替换 preflight / BrowserManager / Sender / Pipeline。
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from boss_auto_apply import main as cli_main
from boss_auto_apply.config import (AppConfig, LlmCfg, PipelineCfg, SearchCfg,
                                     SenderCfg)
from boss_auto_apply.browser.manager import LoginStatus
from boss_auto_apply.errors import BossAutoError


# ============================================================
# helpers
# ============================================================
def _make_args(**kw) -> argparse.Namespace:
    """构造 run 命令的 args。"""
    defaults = dict(command="run", config="config/config.yaml",
                    non_interactive=False, yes=False, target=None)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _real_send_cfg(**overrides) -> AppConfig:
    """构造真发送 AppConfig（provider=drissionpage, dry_run=False 由 _cmd_run 强制）。"""
    cfg = AppConfig(
        pipeline=PipelineCfg(mode="auto", dry_run=True),
        cities=["上海"],
        target_constraints={"salary": {"上海": "12-18K"}},
        llm=LlmCfg(backend="claude", claude_bin="claude"),
        search=SearchCfg(provider="drissionpage"),
        sender=SenderCfg(check_chrome_running=False),
        limits={"daily_total": 5},
        paths={},
    )
    return cfg


class _FakePreflightReport:
    """替身 PreflightReport（fatal_failures 空表示通过）。"""
    def __init__(self, fatal_failures=None):
        self.fatal_failures = fatal_failures or []


class _FakeBrowserManager:
    """替身 BrowserManager（不真启动浏览器）。"""
    def __init__(self, *, logged_in=True):
        self._logged_in = logged_in
        self.quit_count = 0
        self.idle_quit_count = 0

    def check_login(self, *, navigate=True) -> LoginStatus:
        return LoginStatus(logged_in=self._logged_in, has_stoken=self._logged_in,
                           has_wt2=self._logged_in, has_zp_at=self._logged_in,
                           username="测试用户" if self._logged_in else None)

    def quit(self) -> None:
        self.quit_count += 1

    def maybe_quit_idle(self) -> None:
        self.idle_quit_count += 1


# ============================================================
# 用例1：provider=mock → 拒绝（rc=2）
# ============================================================
def test_run_rejects_mock_provider(base_config):
    """run 真发送要求 search.provider 非 mock；mock 被拒（rc=2）。"""
    args = _make_args()
    rc = cli_main._cmd_run(base_config, args)
    assert rc == 2
    # base_config 的 search.provider == "mock"
    assert base_config.search.provider == "mock"


# ============================================================
# 用例2：M3 预检致命失败 → 拒绝（rc=3）
# ============================================================
def test_run_preflight_fatal_rejects(monkeypatch):
    """M3 预检致命失败 → 拒绝执行（rc=3）。"""
    cfg = _real_send_cfg()
    args = _make_args()

    fatal_result = MagicMock()
    fatal_result.fatal_failures = [MagicMock(code="P10", name="DrissionPage")]
    monkeypatch.setattr(cli_main.preflight_mod, "run_m3_preflight",
                        lambda *a, **kw: fatal_result)

    rc = cli_main._cmd_run(cfg, args)
    assert rc == 3


# ============================================================
# 用例3：未登录 → 拒绝（rc=3）+ 提示 login
# ============================================================
def test_run_not_logged_in_rejects(monkeypatch):
    """专用 profile 未登录 → 拒绝执行（rc=3），提示 login 命令。"""
    cfg = _real_send_cfg()
    args = _make_args()

    # 预检通过
    monkeypatch.setattr(cli_main.preflight_mod, "run_m3_preflight",
                        lambda *a, **kw: _FakePreflightReport())
    # 运行时构建（mock 掉真实依赖）
    fake_browser = _FakeBrowserManager(logged_in=False)
    monkeypatch.setattr(cli_main, "_init_runtime",
                        lambda c, **kw: (MagicMock(), MagicMock(), MagicMock(),
                                         MagicMock(), fake_browser))
    rc = cli_main._cmd_run(cfg, args)
    assert rc == 3
    # 浏览器被关闭
    assert fake_browser.quit_count == 1


# ============================================================
# 用例4：--yes 跳过风险确认 + 全链路真发送成功（rc=0）
# ============================================================
def test_run_yes_flag_skips_confirm_and_sends(monkeypatch):
    """--yes 跳过风险确认；Pipeline 全链路真发送完成（rc=0）。"""
    cfg = _real_send_cfg()
    args = _make_args(yes=True)

    monkeypatch.setattr(cli_main.preflight_mod, "run_m3_preflight",
                        lambda *a, **kw: _FakePreflightReport())

    fake_browser = _FakeBrowserManager(logged_in=True)
    fake_sender = MagicMock()
    fake_conn = MagicMock()
    monkeypatch.setattr(cli_main, "_init_runtime",
                        lambda c, **kw: (fake_conn, MagicMock(), MagicMock(),
                                         fake_sender, fake_browser))

    # mock Pipeline（不真跑状态机）
    pipe_instance = MagicMock()
    pipe_instance.run_streaming.return_value = {"image_sent": 1}
    monkeypatch.setattr(cli_main, "Pipeline",
                        lambda *a, **kw: pipe_instance)

    rc = cli_main._cmd_run(cfg, args)
    assert rc == 0
    # run_streaming 被调（流式真发送），不是 run_initial + run_batch
    assert pipe_instance.run_streaming.called
    assert not pipe_instance.run_initial.called  # dry-run 才走 run_initial
    # dry_run 被强制关闭
    assert cfg.pipeline.dry_run is False


# ============================================================
# 用例5：用户拒绝风险确认 → 友好退出（rc=0）
# ============================================================
def test_run_user_declines_risk_confirm(monkeypatch):
    """用户在风险确认输入 N → 友好退出（rc=0），不执行发送。"""
    cfg = _real_send_cfg()
    args = _make_args(yes=False)

    monkeypatch.setattr(cli_main.preflight_mod, "run_m3_preflight",
                        lambda *a, **kw: _FakePreflightReport())

    fake_browser = _FakeBrowserManager(logged_in=True)
    monkeypatch.setattr(cli_main, "_init_runtime",
                        lambda c, **kw: (MagicMock(), MagicMock(), MagicMock(),
                                         MagicMock(), fake_browser))

    # 模拟用户输入 N
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "N")

    # Pipeline 不应被调用
    pipe_called = []
    def _no_pipe(*a, **kw):
        pipe_called.append(True)
        return MagicMock()
    monkeypatch.setattr(cli_main, "Pipeline", _no_pipe)

    rc = cli_main._cmd_run(cfg, args)
    assert rc == 0
    assert pipe_called == []  # Pipeline 未被构造
    assert fake_browser.quit_count == 1


# ============================================================
# 用例6：_confirm_real_send_risk 交互逻辑
# ============================================================
def test_confirm_risk_skip_returns_true():
    """--yes（skip=True）直接返回 True（不交互）。"""
    assert cli_main._confirm_real_send_risk(skip=True) is True


@pytest.mark.parametrize("user_input,expected", [
    ("y", True), ("Y", True), ("yes", True), ("YES", True),
    ("n", False), ("N", False), ("", False), ("no", False),
])
def test_confirm_risk_interactive(monkeypatch, user_input, expected):
    """用户输入 y/yes → True；其他 → False。"""
    monkeypatch.setattr("builtins.input", lambda *a, **kw: user_input)
    assert cli_main._confirm_real_send_risk(skip=False) is expected


def test_confirm_risk_eof_returns_false(monkeypatch):
    """EOFError（非交互环境无 stdin）→ False（安全拒绝）。"""
    def _raise_eof(*a, **kw):
        raise EOFError
    monkeypatch.setattr("builtins.input", _raise_eof)
    assert cli_main._confirm_real_send_risk(skip=False) is False


# ============================================================
# 用例7：dry-run 仍走旧路径（不构建 sender，sender=None）
# ============================================================
def test_dry_run_does_not_build_sender(monkeypatch, base_config):
    """dry-run 命令不构建 Sender（sender=None），止于 image_ready。"""
    args = _make_args(command="dry-run")

    # 追踪 _init_runtime 调用（不应 real_send=True）
    init_calls = []
    real_init = cli_main._init_runtime

    def _track_init(cfg, *, real_send=False):
        init_calls.append(real_send)
        return real_init(cfg, real_send=real_send)
    monkeypatch.setattr(cli_main, "_init_runtime", _track_init)

    # mock Pipeline 避免真跑
    pipe_instance = MagicMock()
    pipe_instance.run_batch.return_value = {"image_ready": 1}
    monkeypatch.setattr(cli_main, "Pipeline",
                        lambda *a, **kw: pipe_instance)

    rc = cli_main._cmd_dry_run(base_config, args)
    assert rc == 0
    assert init_calls == [False]  # real_send=False
    assert base_config.pipeline.dry_run is True


# ============================================================
# 用例8：run 强制 mode=auto（避免 get_limiter 返回 DryRunLimiter）
# ============================================================
def test_run_forces_mode_auto(monkeypatch):
    """run 把 mode=dry-run 强制改为 auto（确保 RealRateLimiter）。"""
    cfg = _real_send_cfg()
    cfg.pipeline.mode = "dry-run"  # 模拟配置默认 dry-run
    args = _make_args(yes=True)

    monkeypatch.setattr(cli_main.preflight_mod, "run_m3_preflight",
                        lambda *a, **kw: _FakePreflightReport())
    fake_browser = _FakeBrowserManager(logged_in=True)
    monkeypatch.setattr(cli_main, "_init_runtime",
                        lambda c, **kw: (MagicMock(), MagicMock(), MagicMock(),
                                         MagicMock(), fake_browser))
    monkeypatch.setattr(cli_main, "Pipeline",
                        lambda *a, **kw: MagicMock())

    cli_main._cmd_run(cfg, args)
    assert cfg.pipeline.mode == "auto"
    assert cfg.pipeline.dry_run is False
