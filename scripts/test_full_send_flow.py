#!/usr/bin/env python3
"""端到端真发送全流程测试（greet → text → image）。

用 Sender 编排器对一个 image_ready job 跑完整 send_application(dry_run=False)，
验证 M3 修复后的发送链路能完整跑通（不误判 login_lost）。

验收标准：
  - result.risk_signal 不是 "login_lost"（核心：不误判）
  - result.risk_signal 不是 "captcha" / "rate_limited"（真风控）
  - result.image_sent == True
  - job 最终 status == "image_sent"
  - greet_sent_at / text_sent_at / image_sent_at 都非空

运行：
    python3 scripts/test_full_send_flow.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# 自动定位项目根（脚本位于 <root>/scripts/ 下），不硬编码绝对路径
PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from loguru import logger

from boss_auto_apply import db as db_mod
from boss_auto_apply.config import load_config
from boss_auto_apply.browser.manager import BrowserConfig, BrowserManager
from boss_auto_apply.ratelimiter import get_limiter
from boss_auto_apply.core.sender import Sender

# 占位 JOB_ID：运行时请改成你本地 DB 里某个真实 job 的 securityId
JOB_ID = "SAMPLE_JOB_ID"
DB_PATH = "data/jobs.db"


def main() -> int:
    # 配置日志：stderr 全量输出（INFO），方便完整报告
    logger.remove()
    logger.add(sys.stderr, level="DEBUG", enqueue=False)

    # 1. 加载配置（强制真发送）
    cfg = load_config("config/config.yaml")
    cfg.pipeline.dry_run = False
    cfg.pipeline.mode = "auto"
    # 跨字段校验要求：dry_run=False → search.provider 非 mock
    if cfg.search.provider == "mock":
        logger.error("search.provider 为 mock，无法真发送")
        return 2
    # ADR-0003：Boss 聊天页 zpAegis 检测 CDP Runtime.enable，drissionpage 下聊天 SPA
    # 不渲染（#wrap 空、0 输入框）。真发送聊天消息必须用 nodriver 引擎。
    # drissionpage 仅用于登录态检查 + greet（详情页无 zpAegis，drissionpage 可用）。
    logger.info(f"sender.driver: {cfg.sender.driver} → nodriver（ADR-0003 真发送要求）")
    cfg.sender.driver = "nodriver"

    # 2. 初始化 DB 连接（与 main._open_conn 一致：autocommit + WAL）
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        # 3. 构建运行时：BrowserManager + RealRateLimiter + Sender
        sender_cfg = cfg.sender
        bcfg = BrowserConfig(
            user_data_dir=os.path.expanduser(sender_cfg.user_data_dir),
            headless=sender_cfg.headless,
            stealth=sender_cfg.stealth,
            check_chrome_running=sender_cfg.check_chrome_running,
            browser_path=sender_cfg.browser_path,
            load_mode=sender_cfg.load_mode,
        )
        BrowserManager.reset_instance()
        bm = BrowserManager.get_instance(bcfg, logger_obj=logger)
        bm.ensure_browser()

        # 检查登录态（导航到 Boss 首页检测 cookie）
        login = bm.check_login(navigate=True)
        logger.info(f"登录态检查：logged_in={login.logged_in} "
                    f"username={login.username!r} reason={login.reason!r}")
        if not login.logged_in:
            logger.error("❌ 未检测到登录态，终止测试（请先 python -m boss_auto_apply login）")
            bm.quit()
            return 1
        logger.info("✅ 登录态正常")

        # 限流器（真发送用 RealRateLimiter）
        limiter = get_limiter(cfg.pipeline.mode, limits=cfg.limits, conn=conn,
                              logger_obj=logger)

        # Sender（注意：Sender 构造不需要 llm；真发送不调 LLM）
        sender = Sender(cfg, conn, bm, limiter, logger_obj=logger,
                        max_retries=cfg.llm.max_retries)

        # 4. 跑 send_application
        job = db_mod.get_job(conn, JOB_ID)
        if job is None:
            logger.error(f"❌ job {JOB_ID} 不存在")
            return 1
        logger.info(f"初始状态: {job.status}")
        logger.info(f"简历图片: {job.resume_image_path}")
        logger.info(f"tailored_greet: {job.tailored_greet!r}")
        logger.info(f"图片文件存在: {os.path.exists(job.resume_image_path or '')}")

        logger.info("========== 开始 send_application(dry_run=False) ==========")
        result = sender.send_application(job, dry_run=False)
        logger.info("========== send_application 结束 ==========")
        logger.info(f"结果: text_sent={result.text_sent} "
                    f"image_sent={result.image_sent} "
                    f"text_confirmed={result.text_confirmed} "
                    f"image_confirmed={result.image_confirmed} "
                    f"risk_signal={result.risk_signal!r} "
                    f"error={result.error!r} "
                    f"elapsed={result.elapsed_sec}")

        # 5. 校验最终状态
        final_job = db_mod.get_job(conn, JOB_ID)
        logger.info(f"最终状态: {final_job.status}")
        logger.info(f"greet_sent_at: {final_job.greet_sent_at}")
        logger.info(f"text_sent_at:  {final_job.text_sent_at}")
        logger.info(f"image_sent_at: {final_job.image_sent_at}")
        logger.info(f"error_msg: {final_job.error_msg!r} "
                    f"retry_count: {final_job.retry_count}")

        # 6. 验收标准判定
        print("\n" + "=" * 60)
        print("验收标准")
        print("=" * 60)
        checks = []
        rs = result.risk_signal

        c1 = rs != "login_lost"
        checks.append(("risk_signal != login_lost（核心：不误判）",
                       c1, f"risk_signal={rs!r}"))

        c2 = rs not in ("captcha", "rate_limited")
        checks.append(("risk_signal 不是 captcha/rate_limited", c2,
                       f"risk_signal={rs!r}"))

        c3 = bool(result.image_sent)
        checks.append(("result.image_sent == True", c3,
                       f"image_sent={result.image_sent}"))

        c4 = final_job.status == "image_sent"
        checks.append(("job.status == 'image_sent'（终态）", c4,
                       f"status={final_job.status!r}"))

        c5 = all([final_job.greet_sent_at, final_job.text_sent_at,
                  final_job.image_sent_at])
        checks.append(("greet/text/image_sent_at 都非空", c5,
                       f"greet={final_job.greet_sent_at!r} "
                       f"text={final_job.text_sent_at!r} "
                       f"image={final_job.image_sent_at!r}"))

        all_pass = True
        for desc, ok, detail in checks:
            mark = "✅" if ok else "❌"
            if not ok:
                all_pass = False
            print(f"{mark} {desc}")
            print(f"     → {detail}")

        print("=" * 60)
        print(f"总结: {'全部通过 ✅' if all_pass else '有失败项 ❌'}")
        print("=" * 60)
        return 0 if all_pass else 1

    finally:
        # 5. 收尾：浏览器空闲退出（sender 内部已调一次，这里兜底）
        try:
            bm.maybe_quit_idle()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"maybe_quit_idle 异常（忽略）：{e}")
        try:
            conn.execute("COMMIT")
        except Exception:  # noqa: BLE001
            pass
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
