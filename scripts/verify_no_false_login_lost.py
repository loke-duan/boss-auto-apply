#!/usr/bin/env python3
"""验证脚本：修复后真实点击「立即沟通」，确认不再误判 login_lost。

修复内容（2026-07-17）：
  _detect_greet_outcome 的 login_lost 判断从「DOM 选择器 .sign-form」改为「URL 跳转 /web/user」。
  原因：.sign-form 是详情页底部常驻登录推广区块（已登录也在），会误判 → 24h 误熔断。

本脚本真实点击「立即沟通」，跑修复后的 _detect_greet_outcome，验证：
  - 不返回 risk_signal=login_lost（除非 URL 真跳 /web/user）
  - 报告真实 outcome（chat_page / 已发起沟通弹窗 / click_timeout 等）

注意：会真实发起一次沟通（建立沟通关系）。这是必要的端到端验证。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from boss_auto_apply.browser.manager import BrowserConfig, BrowserManager
from boss_auto_apply.browser.web_greeter import WebGreeter
from boss_auto_apply.config import load_config
from boss_auto_apply.models import JobRow


def pick_job_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    import sqlite3
    conn = sqlite3.connect(os.path.join(ROOT, "data", "jobs.db"))
    row = conn.execute(
        "SELECT job_id, title FROM jobs WHERE status='found' LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        print("❌ DB 无 found job，请传 job_id"); sys.exit(1)
    print(f"📋 选取 job：{row[1]} ({row[0]})")
    return row[0]


def main():
    job_id = pick_job_id(sys.argv[1] if len(sys.argv) > 1 else None)
    cfg = load_config(os.path.join(ROOT, "config", "config.yaml"))
    bcfg = BrowserConfig(
        user_data_dir=os.path.expanduser(cfg.sender.user_data_dir), headless=False)

    print("\n🚀 启动浏览器...")
    bm = BrowserManager.get_instance(bcfg)
    bm.ensure_browser()

    print("🔐 检查登录态...")
    login = bm.check_login(navigate=True)
    if not login.logged_in:
        print(f"  ❌ 未登录：{getattr(login, 'reason', '')}")
        print("  请先跑：python -m boss_auto_apply login")
        bm.maybe_quit_idle(); sys.exit(1)
    print(f"  ✅ 已登录")

    job = JobRow(job_id=job_id, target_name="验证", city="上海",
                 title="验证", company="验证", status="image_ready")
    greeter = WebGreeter(bm)

    print(f"\n👉 真实点击「立即沟通」（dry_run=False，会建立沟通关系）...")
    result = greeter.click_and_greet(job, dry_run=False)

    print("\n" + "=" * 60)
    print("📋 GreetResult（修复后）")
    print("=" * 60)
    print(f"  relation_established: {result.relation_established}")
    print(f"  already_friend:       {result.already_friend}")
    print(f"  chat_page_opened:     {result.chat_page_opened}")
    print(f"  boss_default_greet_sent: {result.boss_default_greet_sent}")
    print(f"  risk_signal:          {result.risk_signal!r}")
    print(f"  error:                {result.error!r}")

    print("\n" + "=" * 60)
    # 核心断言
    if result.risk_signal == "login_lost":
        print("❌❌❌ 仍误判 login_lost！修复未生效")
    elif result.risk_signal in ("captcha", "rate_limited"):
        print(f"⚠️ 真风控信号：{result.risk_signal}（非误判，是 Boss 真限制）")
    elif result.relation_established:
        print("✅✅✅ 沟通关系建立成功！未误判 login_lost")
    elif result.error == "click_timeout":
        print("✅ 未误判 login_lost（click_timeout：点击无响应，属 network 类可重试）")
    elif result.error == "button_not_found":
        print("❌ button_not_found（导航/选择器问题，非 login_lost）")
    else:
        print(f"ℹ️ 其他结果：{result.error}（未误判 login_lost）")
    print("=" * 60)

    print("\n浏览器保持 30s 供确认...")
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        pass
    finally:
        bm.maybe_quit_idle()


if __name__ == "__main__":
    main()
