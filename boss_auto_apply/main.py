"""CLI 入口（设计 §5.12）。

子命令：
    preflight / parse / hrbp-check / profile / dry-run / run / resume / status

所有命令开头调 ``preflight.run()``（除 status 可宽松）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from . import db as db_mod
from .config import AppConfig, ensure_data_dirs, load_config
from .core import hrbp_check, parser, profiler, searcher
from .errors import BossAutoError, ConfigValidationError, PreflightError
from .llm import create_llm_client
from .pipeline import Pipeline
from . import preflight as preflight_mod

__all__ = ["main", "build_parser"]


# ============================================================
# CLI 构造
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    """构造 argparse。

    flag 布局：
    - ``--config`` 全局（任何命令前指定配置文件）
    - ``--non-interactive`` / ``--target`` 各子命令（符合 ``cmd --flag`` 习惯）
    - ``--yes`` / ``-y`` 仅 ``run`` 子命令（真发送风险确认）
    """
    ap = argparse.ArgumentParser(
        prog="boss_auto_apply",
        description="BOSS 直聘智能投递助手（M1+M2：解析→体检→画像→搜索→匹配→定制→PDF→PNG）",
    )
    ap.add_argument("--config", default="config/config.yaml", help="config.yaml 路径")
    sub = ap.add_subparsers(dest="command", required=True)

    # 各子命令：通用 flag（--non-interactive / --target），run 额外加 --yes
    interactive_cmds = ("parse", "hrbp-check", "profile", "dry-run", "run", "resume")
    for cmd in ("preflight", "parse", "hrbp-check", "profile",
                "dry-run", "run", "resume", "status", "login"):
        sp = sub.add_parser(cmd, help=f"{cmd} 子命令")
        sp.add_argument("--target", default=None, help="限定单方向")
        if cmd in interactive_cmds:
            sp.add_argument("--non-interactive", action="store_true",
                            help="跳过暂停点（用默认确认）")
        if cmd == "run":
            sp.add_argument("--yes", "-y", action="store_true",
                            help="跳过 run 真发送风险确认（已知晓封号风险）")
    return ap


# ============================================================
# 主入口
# ============================================================
def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。

    Returns:
        进程退出码（0=成功，非 0=失败）。
    """
    ap = build_parser()
    args = ap.parse_args(argv)

    # 配置日志
    logger.remove()
    logger.add(sys.stderr, level="INFO", enqueue=False)

    # 加载配置（preflight 命令除外，可宽松）
    try:
        cfg = load_config(args.config)
    except ConfigValidationError as e:
        logger.error(f"配置加载失败：{e}")
        if args.command == "preflight":
            logger.warning("preflight 命令容忍配置错误，继续弱预检")
            cfg = None  # type: ignore[assignment]
        else:
            return 2

    # 预检（除 status）
    if args.command != "status" and cfg is not None:
        try:
            report = preflight_mod.run_preflight(
                backend=cfg.llm.backend,
                llm_bin=cfg.llm.codex_bin if cfg.llm.backend == "codex" else cfg.llm.claude_bin,
                login_hint=cfg.llm.login_hint,
                typst_bin=cfg.pdf.get("typst_bin", "typst"),
                font_name=cfg.pdf.get("font", "Noto Sans CJK SC"),
                template_dir=cfg.pdf.get("template_dir", "templates/brilliant-cv"),
                resume_path=cfg.paths.get("resume_input", "input/resume.pdf"),
                config_path=args.config,
                data_dir=os.path.dirname(cfg.paths.get("db", "data/jobs.db")) or "data",
                is_dry_run=cfg.pipeline.dry_run,
                skip_login=True,
            )
            # preflight 子命令到此结束（只检查不跑业务）
            if args.command == "preflight":
                # 追加 M3 真发送前置检查（P10-P14）
                try:
                    m3_report = preflight_mod.run_m3_preflight(cfg, skip_login_check=True)
                    # M3 检查非致命（dry-run 不需要），只展示
                except Exception as e:
                    logger.warning(f"M3 预检异常（非致命）：{e}")
                return 0 if report.all_passed else 1
            # 其他命令：致命失败才阻断（typst 缺失等会在具体步骤抛）
            fatal_codes = {r.code for r in report.fatal_failures}
            # P2(claude CLI) 致命 → 直接退出
            if "P2" in fatal_codes:
                logger.error("预检致命失败（claude CLI），无法继续")
                return 3
        except PreflightError as e:
            logger.error(f"预检失败：{e}")
            return 3

    # 分发子命令
    try:
        rc = _dispatch(args, cfg)
        return rc
    except BossAutoError as e:
        logger.error(f"命令失败：{e}")
        return 1
    except KeyboardInterrupt:
        logger.warning("用户中断")
        return 130


def _dispatch(args: argparse.Namespace, cfg: AppConfig) -> int:
    """分发子命令到对应处理函数。"""
    cmd = args.command
    if cmd == "preflight":
        return 0  # 已在 main 处理
    if cmd == "status":
        return _cmd_status(cfg, args)
    if cmd == "parse":
        return _cmd_parse(cfg, args)
    if cmd == "hrbp-check":
        return _cmd_hrbp(cfg, args)
    if cmd == "profile":
        return _cmd_profile(cfg, args)
    if cmd == "dry-run":
        return _cmd_dry_run(cfg, args)
    if cmd == "run":
        return _cmd_run(cfg, args)
    if cmd == "resume":
        return _cmd_resume(cfg, args)
    if cmd == "login":
        return _cmd_login(cfg, args)
    logger.error(f"未知命令：{cmd}")
    return 2


# ============================================================
# 子命令实现
# ============================================================
def _init_runtime(cfg: AppConfig, *, real_send: bool = False) -> tuple:
    """初始化 db + llm + searcher（+ 真发送时构建 Sender/Limiter/Browser）。

    Args:
        cfg: AppConfig。
        real_send: True 表示 run 真发送模式，额外构建 BrowserManager +
            RealRateLimiter + Sender，返回值多两个元素。

    Returns:
        real_send=False: (conn, llm, searcher_obj)
        real_send=True:  (conn, llm, searcher_obj, sender_obj, browser_manager)
    """
    ensure_data_dirs(cfg)
    db_path = cfg.paths.get("db", "data/jobs.db")
    db_mod.init_db(db_path)
    conn = _open_conn(db_path)
    llm = create_llm_client(cfg.llm, cache_conn=conn)

    # BrowserManager：drissionpage 搜索或真发送都需要
    browser_manager = None
    if cfg.search.provider == "drissionpage" or real_send:
        from .browser.manager import BrowserConfig, BrowserManager
        sender_cfg = getattr(cfg, "sender", None)
        bm_cfg = BrowserConfig(
            user_data_dir=getattr(sender_cfg, "user_data_dir", "~/.boss-auto-apply/chrome-profile")
                if sender_cfg else "~/.boss-auto-apply/chrome-profile",
            headless=getattr(sender_cfg, "headless", False) if sender_cfg else False,
            stealth=getattr(sender_cfg, "stealth", True) if sender_cfg else True,
            check_chrome_running=getattr(sender_cfg, "check_chrome_running", True)
                if sender_cfg else True,
            browser_path=getattr(sender_cfg, "browser_path", None)
                if sender_cfg else None,
            load_mode=getattr(sender_cfg, "load_mode", "eager")
                if sender_cfg else "eager",
        )
        BrowserManager.reset_instance()
        browser_manager = BrowserManager.get_instance(bm_cfg, logger_obj=logger)

    searcher_obj = searcher.get_searcher(
        cfg.search.provider,
        fixtures_dir=cfg.search.fixtures_dir,
        browser_manager=browser_manager,
    )

    if not real_send:
        return conn, llm, searcher_obj

    # ---- 真发送：构建 RealRateLimiter + Sender ----
    from .core.sender import Sender
    from .ratelimiter import get_limiter
    limiter = get_limiter(
        cfg.pipeline.mode,
        limits=cfg.limits,
        conn=conn,
        logger_obj=logger,
    )
    sender_obj = Sender(
        cfg, conn, browser_manager, limiter,
        logger_obj=logger,
        max_retries=cfg.llm.max_retries,
    )
    return conn, llm, searcher_obj, sender_obj, browser_manager


def _open_conn(db_path: str) -> Any:
    """开一个 autocommit 长连接供 pipeline 用。

    isolation_level=None（autocommit），与 db.connect 一致；
    pipeline 各步骤的写操作各自管事务。
    """
    import sqlite3
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _cmd_status(cfg: AppConfig, args: argparse.Namespace) -> int:
    """status：打印各状态 job 计数 + 最近 run_log。"""
    db_path = cfg.paths.get("db", "data/jobs.db")
    if not os.path.exists(db_path):
        logger.info("（无 jobs.db，尚未运行过）")
        return 0
    conn = _open_conn(db_path)
    counts = db_mod.count_by_status(conn)
    print("=== Job 状态分布 ===")
    for s, n in sorted(counts.items()):
        print(f"  {s}: {n}")
    runs = db_mod.list_recent_runs(conn, limit=5)
    if runs:
        print("\n=== 最近 5 次运行 ===")
        for r in runs:
            print(f"  {r['started_at'][:19]} mode={r['mode']} "
                  f"planned={r['planned']} ok={r['succeeded']} fail={r['failed']} skip={r['skipped']}")
    return 0


def _cmd_parse(cfg: AppConfig, args: argparse.Namespace) -> int:
    """parse：只跑 F1 → master.json（支持 pdf/docx/md/txt）。"""
    conn, llm, _ = _init_runtime(cfg)
    master_path = cfg.paths.get("master_json", "data/master.json")
    resume_path = cfg.paths.get("resume_input", "input/resume.pdf")
    ext = os.path.splitext(resume_path)[1]
    p = parser.get_parser(ext=ext, logger_obj=logger)
    logger.info(f"简历格式：{ext or '(无扩展名)'}，解析器：{getattr(p, 'name', '?')}")
    md = parser.parse_resume_to_markdown(resume_path, p)
    master = parser.structure_with_llm(md, llm, logger_obj=logger)
    master.setdefault("health_check_status", "pending")
    Path(master_path).parent.mkdir(parents=True, exist_ok=True)
    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)
    logger.info(f"F1 完成：{master_path}")
    # autocommit 连接（isolation_level=None）无活跃事务时 COMMIT 抛 OperationalError。
    # 写操作各自已提交，此处仅防御性收尾，失败忽略。
    try:
        conn.execute("COMMIT")
    except Exception:  # noqa: BLE001 - sqlite3.OperationalError 在 autocommit 下预期
        pass
    return 0


def _cmd_hrbp(cfg: AppConfig, args: argparse.Namespace) -> int:
    """hrbp-check：只跑 F1.6（暂停）。"""
    conn, llm, _ = _init_runtime(cfg)
    master_path = cfg.paths.get("master_json", "data/master.json")
    with open(master_path, encoding="utf-8") as f:
        master = json.load(f)
    report = hrbp_check.run_health_check(master, llm, logger_obj=logger)
    health_path = cfg.paths.get("master_health", "data/master.health.json")
    Path(health_path).parent.mkdir(parents=True, exist_ok=True)
    with open(health_path, "w", encoding="utf-8") as f:
        json.dump({"report": report.raw, "markdown": report.markdown}, f,
                  ensure_ascii=False, indent=2)
    hrbp_check.present_report(report)
    hrbp_check.await_user_confirmation(
        report, master_path, non_interactive_default_accept=args.non_interactive,
    )
    # autocommit 连接（isolation_level=None）无活跃事务时 COMMIT 抛 OperationalError。
    # 写操作各自已提交，此处仅防御性收尾，失败忽略。
    try:
        conn.execute("COMMIT")
    except Exception:  # noqa: BLE001 - sqlite3.OperationalError 在 autocommit 下预期
        pass
    return 0


def _cmd_profile(cfg: AppConfig, args: argparse.Namespace) -> int:
    """profile：只跑 F1.5（暂停）。"""
    conn, llm, _ = _init_runtime(cfg)
    master_path = cfg.paths.get("master_json", "data/master.json")
    with open(master_path, encoding="utf-8") as f:
        master = json.load(f)
    pr = profiler.run_profiler(master, cfg.target_constraints, llm, logger_obj=logger)
    profiler.present_targets(pr.targets)
    targets_path = cfg.paths.get("targets_confirmed", "data/targets.confirmed.yaml")
    profiler.await_target_confirmation(
        pr.targets, targets_path, non_interactive_default_accept=args.non_interactive,
    )
    # autocommit 连接（isolation_level=None）无活跃事务时 COMMIT 抛 OperationalError。
    # 写操作各自已提交，此处仅防御性收尾，失败忽略。
    try:
        conn.execute("COMMIT")
    except Exception:  # noqa: BLE001 - sqlite3.OperationalError 在 autocommit 下预期
        pass
    return 0


def _cmd_dry_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    """dry-run：F1→F6 全链路（全 mock，不触网）。"""
    cfg.pipeline.dry_run = True
    conn, llm, searcher_obj = _init_runtime(cfg)
    pipe = Pipeline(cfg, conn, llm, searcher_obj, non_interactive=args.non_interactive)
    pipe.run_initial()
    counts = pipe.run_batch()
    # autocommit 连接（isolation_level=None）无活跃事务时 COMMIT 抛 OperationalError。
    # 写操作各自已提交，此处仅防御性收尾，失败忽略。
    try:
        conn.execute("COMMIT")
    except Exception:  # noqa: BLE001 - sqlite3.OperationalError 在 autocommit 下预期
        pass
    logger.info(f"dry-run 完成：{counts}")
    return 0


def _confirm_real_send_risk(*, skip: bool) -> bool:
    """run 真发送前风险确认（设计 §16.6）。

    Boss直聘用户协议禁止自动化，真发送有账号风控/封号风险。
    默认要求用户在终端输入 ``y`` 确认；``--yes`` 跳过（已知晓风险）。

    Args:
        skip: ``--yes`` 时为 True，直接返回 True（不交互）。

    Returns:
        True=继续真发送，False=用户拒绝。
    """
    if skip:
        return True
    print(
        "\n⚠️  风险确认\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "即将真实操作 Boss直聘：\n"
        "  • 点击「立即沟通」发起沟通\n"
        "  • 向 HR 发送打招呼话术\n"
        "  • 发送定制简历图片\n"
        "\n"
        "Boss直聘用户协议明确禁止自动化操作，本操作存在\n"
        "账号风控/封号风险，风险由你自行承担。\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        ans = input("已知晓风险，确认继续真发送？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    """run：F1→F7 全链路真发送（M3）。

    与 dry-run 的差异：
    1. ``dry_run=False``，走真实发送链路（greet→text→image）
    2. 强制 M3 预检（P10–P13 致命项：DrissionPage/Chrome/profile/Pillow）
    3. 登录态验证（专用 profile 未登录 → 拒绝执行，提示 ``login`` 命令）
    4. 真发送风险确认（``--yes`` 可跳过）
    5. 构建 RealRateLimiter + BrowserManager + Sender 注入 Pipeline

    数据来源：复用 dry-run 阶段已入库的 image_ready jobs（不重新搜索）。
    """
    # 强制 dry_run=False + mode 非 dry-run（真发送需要 RealRateLimiter）
    cfg.pipeline.dry_run = False
    if cfg.pipeline.mode == "dry-run":
        cfg.pipeline.mode = "auto"
    # 真发送要求 search.provider 非 mock（配置交叉校验已强制）
    if cfg.search.provider == "mock":
        logger.error("run 真发送要求 search.provider 非 mock（boss-cli/drissionpage）。"
                     "当前为 mock —— 无真实岗位数据。")
        logger.info("提示：先用 dry-run 生成 image_ready jobs，再 run 真发送。")
        return 2

    # ---- M3 预检（P10-P13 致命项；P14 登录态由后续 check_login 真检测）----
    logger.info("=== M3 真发送预检 ===")
    try:
        m3_report = preflight_mod.run_m3_preflight(cfg, skip_login_check=True)
    except Exception as e:  # noqa: BLE001
        logger.error(f"M3 预检异常：{e}")
        return 3
    if m3_report.fatal_failures:
        logger.error("M3 预检致命失败，无法真发送。请按提示修复后重试。")
        return 3

    # ---- 构建运行时（含 Sender）----
    from .browser.manager import BrowserManager
    conn, llm, searcher_obj, sender_obj, browser_manager = _init_runtime(cfg, real_send=True)

    # ---- 登录态验证（专用 profile）----
    try:
        login_status = browser_manager.check_login()
    except Exception as e:  # noqa: BLE001
        logger.error(f"登录态检查异常：{e}")
        browser_manager.quit()
        BrowserManager.reset_instance()
        return 3
    if not login_status.logged_in:
        logger.error("未检测到 Boss直聘 登录态。请先执行登录：")
        logger.error("  python -m boss_auto_apply login")
        browser_manager.quit()
        BrowserManager.reset_instance()
        return 3
    logger.info(f"登录态正常（用户={login_status.username or '未知'}）")

    # ---- 风险确认 ----
    if not _confirm_real_send_risk(skip=args.yes):
        logger.warning("用户取消真发送")
        browser_manager.quit()
        BrowserManager.reset_instance()
        return 0

    # ---- 跑流式全链路（含 F7 真发送：采一个投一个）----
    try:
        pipe = Pipeline(cfg, conn, llm, searcher_obj,
                        non_interactive=args.non_interactive,
                        sender=sender_obj)
        counts = pipe.run_streaming()
    finally:
        # 真发送结束后关闭浏览器（释放端口/profile）
        browser_manager.maybe_quit_idle()
        try:
            conn.execute("COMMIT")
        except Exception:  # noqa: BLE001
            pass
    logger.info(f"run 真发送完成：{counts}")
    return 0


def _cmd_resume(cfg: AppConfig, args: argparse.Namespace) -> int:
    """resume：断点续传。"""
    conn, llm, searcher_obj = _init_runtime(cfg)
    pipe = Pipeline(cfg, conn, llm, searcher_obj, non_interactive=args.non_interactive)
    counts = pipe.resume()
    # autocommit 连接（isolation_level=None）无活跃事务时 COMMIT 抛 OperationalError。
    # 写操作各自已提交，此处仅防御性收尾，失败忽略。
    try:
        conn.execute("COMMIT")
    except Exception:  # noqa: BLE001 - sqlite3.OperationalError 在 autocommit 下预期
        pass
    logger.info(f"resume 完成：{counts}")
    return 0


def _cmd_login(cfg: AppConfig, args: argparse.Namespace) -> int:  # noqa: ARG001
    """login：首次扫码登录 Boss 直聘（设计 §2.2 / §16.7）。

    启动 BrowserManager（专用 profile）→ 导航登录页 → 等用户扫码（最多 300s）
    → 检测登录成功 → profile 保存 cookie/stoken → 提示「登录成功，后续 run 复用」。
    """
    from .browser.manager import BrowserConfig, BrowserManager
    sender_cfg = cfg.sender
    bm_cfg = BrowserConfig(
        user_data_dir=sender_cfg.user_data_dir,
        headless=sender_cfg.headless,
        stealth=sender_cfg.stealth,
        check_chrome_running=sender_cfg.check_chrome_running,
        browser_path=sender_cfg.browser_path,
        load_mode=sender_cfg.load_mode,
    )
    # login 命令不复用单例（force_new=True 强制启动新实例）
    BrowserManager.reset_instance()
    bm = BrowserManager.get_instance(bm_cfg, logger_obj=logger)
    try:
        bm.ensure_browser(force_new=True)
        status = bm.wait_for_login(timeout_sec=300)
        if status.logged_in:
            print(f"✅ 登录成功（用户={status.username or '未知'}），profile 已保存到 {bm_cfg.user_data_dir}")
            print("   后续 run/dry-run 复用此 profile，无需重复扫码。")
            return 0
        logger.error("登录未成功（未检测到登录态）")
        return 1
    finally:
        bm.quit()
        BrowserManager.reset_instance()


if __name__ == "__main__":
    raise SystemExit(main())
