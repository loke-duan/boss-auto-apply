"""test_cli_args.py — CLI argparse 解析层测试（防 flag 位置回归）。

回归背景：M3 接通时误把 ``--non-interactive`` / ``--yes`` 从子命令挪到全局，
导致 ``cmd --flag``（项目文档和用户习惯的用法）报 unrecognized arguments。
本测试锁定 flag 必须在子命令上解析。

正确布局：
- ``--config`` 全局
- ``--non-interactive`` / ``--target`` 各子命令（cmd --flag）
- ``--yes`` / ``-y`` 仅 run 子命令
"""

from __future__ import annotations

import pytest

from boss_auto_apply.main import build_parser


def _parse(argv: list[str]):
    """解析 argv，失败抛 SystemExit。"""
    return build_parser().parse_args(argv)


# ============================================================
# --config 是全局 flag（任何位置）
# ============================================================
def test_config_is_global_flag():
    """--config 必须在子命令前可用（全局）。"""
    a = _parse(["--config", "x.yaml", "status"])
    assert a.config == "x.yaml"


def test_config_default():
    """--config 默认值 config/config.yaml。"""
    a = _parse(["status"])
    assert a.config == "config/config.yaml"


# ============================================================
# --non-interactive 必须在子命令上（cmd --flag）
# ============================================================
@pytest.mark.parametrize("cmd", ["parse", "hrbp-check", "profile",
                                  "dry-run", "run", "resume"])
def test_non_interactive_on_subcommand(cmd):
    """cmd --non-interactive 必须可解析（用户习惯用法）。"""
    a = _parse([cmd, "--non-interactive"])
    assert a.command == cmd
    assert a.non_interactive is True


def test_non_interactive_global_position_rejected():
    """--non-interactive 放全局位置（子命令前）应被拒绝。

    回归保护：M3 接通时误设为全局，现已修复为子命令 flag。
    """
    with pytest.raises(SystemExit):
        _parse(["--non-interactive", "dry-run"])


def test_non_interactive_default_false():
    """不加 flag 时 non_interactive 默认 False。"""
    a = _parse(["dry-run"])
    assert a.non_interactive is False


# ============================================================
# --yes / -y 仅 run 子命令
# ============================================================
def test_yes_on_run_subcommand():
    """run --yes 必须可解析。"""
    a = _parse(["run", "--yes"])
    assert a.command == "run"
    assert a.yes is True


def test_short_y_on_run_subcommand():
    """run -y（短格式）必须可解析。"""
    a = _parse(["run", "-y"])
    assert a.yes is True


def test_yes_default_false_on_run():
    """run 不加 --yes 时 yes=False。"""
    a = _parse(["run"])
    assert a.yes is False


def test_yes_global_position_rejected():
    """--yes 放全局位置（子命令前）应被拒绝。"""
    with pytest.raises(SystemExit):
        _parse(["--yes", "run"])


def test_yes_only_on_run():
    """--yes 只在 run 子命令可用，其他命令应拒绝。"""
    with pytest.raises(SystemExit):
        _parse(["dry-run", "--yes"])


# ============================================================
# --target 各子命令都有
# ============================================================
@pytest.mark.parametrize("cmd", ["parse", "dry-run", "run", "resume"])
def test_target_on_subcommands(cmd):
    """cmd --target xxx 必须可解析。"""
    a = _parse([cmd, "--target", "SEO"])
    assert a.target == "SEO"


def test_target_default_none():
    """不加 --target 时 target=None。"""
    a = _parse(["status"])
    assert a.target is None


# ============================================================
# 组合用法
# ============================================================
def test_run_with_all_flags():
    """run --non-interactive --yes --target SEO 全组合可解析。"""
    a = _parse(["run", "--non-interactive", "--yes", "--target", "SEO"])
    assert a.command == "run"
    assert a.non_interactive is True
    assert a.yes is True
    assert a.target == "SEO"


def test_dry_run_with_config_and_non_interactive():
    """--config 全局 + dry-run 子命令 --non-interactive。"""
    a = _parse(["--config", "my.yaml", "dry-run", "--non-interactive"])
    assert a.config == "my.yaml"
    assert a.command == "dry-run"
    assert a.non_interactive is True
