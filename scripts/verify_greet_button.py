#!/usr/bin/env python3
"""验证脚本：确认 /job_detail/{id}.html 详情页能加载出「立即沟通」按钮。

目的（修复 button_not_found 根因前的实测验证）：
  web_greeter._navigate_to_job 当前用 job.job_id 当 securityId 拼 URL（错误）。
  本脚本验证：改用 /job_detail/{job_id}.html（fetch_detail 同款 URL）后，
  Boss JS 是否会渲染出「立即沟通」按钮。

做的事（只读，不点击）：
  1. 用专用 Chrome profile 启动浏览器
  2. 从 DB 取一个 found 状态的 job_id
  3. 导航 /job_detail/{job_id}.html
  4. 打印重定向后的最终 URL（看 Boss 是否生成 securityId）
  5. 探查页面上所有含「沟通/聊天/联系」文本的按钮，打印 class/tag/text
  6. 用当前 selectors.py 的 detail.greet_button / continue_chat_button 试定位

用法：python scripts/verify_greet_button.py [job_id]
不传 job_id 则从 DB 自动取一个 found 状态的。
"""
from __future__ import annotations

import os
import sys
import time

# 项目根加入 sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from boss_auto_apply.browser.manager import BrowserConfig, BrowserManager
from boss_auto_apply.config import load_config
from boss_auto_apply.browser import selectors as sel


def pick_job_id(explicit: str | None) -> str:
    """从 DB 取一个 found 状态的 job_id，或用显式指定的。"""
    if explicit:
        return explicit
    import sqlite3
    conn = sqlite3.connect(os.path.join(ROOT, "data", "jobs.db"))
    row = conn.execute(
        "SELECT job_id, title FROM jobs WHERE status='found' LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        print("❌ DB 里没有 found 状态的 job，请显式传 job_id")
        sys.exit(1)
    print(f"📋 自动选取 job：{row[1]} (job_id={row[0]})")
    return row[0]


def probe_buttons(tab) -> None:
    """探查页面上所有可能的沟通按钮，打印真实 DOM 结构。"""
    print("\n" + "=" * 60)
    print("🔍 探查页面上的沟通类按钮（只读，不点击）")
    print("=" * 60)

    # 1) 用当前 selectors 试定位
    print("\n--- 用 selectors.py 现有选择器试定位 ---")
    greet_btn = sel.find_element(tab, "detail.greet_button", timeout=3)
    print(f"  detail.greet_button: {'✅ 找到' if greet_btn else '❌ 未找到'}")

    cont_btn = sel.find_element(tab, "detail.continue_chat_button", timeout=3)
    print(f"  detail.continue_chat_button: {'✅ 找到' if cont_btn else '❌ 未找到'}")

    # 2) 暴力探查：找所有含「沟通/聊天/联系/聊一聊」文本的可点击元素
    print("\n--- 暴力探查：含沟通关键词的按钮 ---")
    keywords = ["立即沟通", "继续沟通", "沟通", "聊一聊", "聊天", "联系", "发送"]
    found_any = False
    for kw in keywords:
        try:
            # 用文本定位（DrissionPage 语法）
            els = tab.eles(f"text:{kw}", timeout=1)
            for el in els[:3]:  # 每个关键词最多打 3 个
                found_any = True
                # 尝试提取 class / tag / 文本
                try:
                    tag = el.tag
                except Exception:
                    tag = "?"
                try:
                    cls = el.attr("class") or ""
                except Exception:
                    cls = ""
                try:
                    txt = (el.text or "").strip()[:30]
                except Exception:
                    txt = ""
                # 尝试构造 css 选择器路径
                try:
                    # DrissionPage 的 loc 会给定位字符串
                    loc = str(getattr(el, "loc", getattr(el, "_loc", "")))[:60]
                except Exception:
                    loc = ""
                print(f"  [{kw}] tag={tag} class={cls!r} text={txt!r} loc={loc}")
        except Exception as e:
            print(f"  [{kw}] 查询异常：{e}")

    if not found_any:
        print("  ⚠️ 未找到任何含沟通关键词的按钮")

    # 3) 看详情页主体容器是否存在（确认页面真的加载了）
    print("\n--- 详情页主体容器探测 ---")
    containers = [
        ".job-detail-container", ".job-detail", ".job-box",
        ".detail-op", ".job-sec-text", ".info-primary",
    ]
    for c in containers:
        try:
            el = tab.ele(f"css:{c}", timeout=1)
            print(f"  {c}: {'✅ 存在' if el else '❌ 不存在'}")
        except Exception:
            print(f"  {c}: 查询异常")


def dry_run_greet(bm, job_id: str) -> None:
    """用修复后的 WebGreeter 跑 dry_run greet，验证完整定位链路。

    dry_run=True 时 WebGreeter.click_and_greet 会在点击前返回，
    但会先走 _navigate_to_job + _locate_greet_button（验证这两步）。
    """
    from boss_auto_apply.browser.web_greeter import WebGreeter
    from boss_auto_apply.models import JobRow

    print("\n" + "=" * 60)
    print(f"🧪 用修复后的 WebGreeter 跑 dry_run greet（job={job_id}）")
    print("=" * 60)

    job = JobRow(
        job_id=job_id, target_name="验证", city="上海",
        title="验证岗位", company="验证公司", status="image_ready",
    )
    greeter = WebGreeter(bm)
    result = greeter.click_and_greet(job, dry_run=True)

    print(f"\n  GreetResult: {result}")
    print(f"  error: {result.error!r}")
    print(f"  dry_run: {result.dry_run}")
    # dry_run 模式下，click_and_greet 在 _locate_greet_button 成功后返回 error='dry_run'
    # 如果 error='button_not_found' 说明按钮仍找不到（修复失败）
    # 如果 error='dry_run' 说明 _navigate_to_job + _locate_greet_button 都成功了
    if result.error == "dry_run":
        print("\n  ✅✅✅ 修复成功！_navigate_to_job + _locate_greet_button 完整链路通过")
        print("     （dry_run 不真点击，但按钮定位成功，真发送时就能点）")
    elif result.error == "button_not_found":
        print("\n  ❌ 仍 button_not_found，修复未生效")
    elif result.relation_established and result.already_friend:
        print("\n  ✅ 该 job 已是好友（显示「继续沟通」），沟通关系已建立")
    else:
        print(f"\n  ⚠️ 其他结果：{result.error}")


def main():
    explicit_id = sys.argv[1] if len(sys.argv) > 1 else None
    job_id = pick_job_id(explicit_id)

    # 加载 config（复用 run 命令的浏览器配置）
    cfg = load_config(os.path.join(ROOT, "config", "config.yaml"))

    # 从 config.sender 读 profile 路径（与 BrowserManager 一致）
    sender_cfg = cfg.sender
    user_data_dir = getattr(sender_cfg, "user_data_dir", None) or "~/.boss-auto-apply/chrome-profile"
    headless = getattr(sender_cfg, "headless", False)
    bcfg = BrowserConfig(
        user_data_dir=os.path.expanduser(user_data_dir),
        headless=headless,
        check_chrome_running=True,
    )

    print(f"\n🚀 启动浏览器（profile={user_data_dir}）...")
    bm = BrowserManager.get_instance(bcfg)
    tab = bm.ensure_browser()

    # 先确认登录态（navigate=True 会导航到 job-recommend 触发 cookie 加载）
    print("🔐 检查登录态...")
    try:
        login = bm.check_login(navigate=True)
        if login.logged_in:
            print(f"  ✅ 已登录（用户={login.username or '未知'}）")
        else:
            print(f"  ❌ 未登录！请先跑：python -m boss_auto_apply login")
            print(f"     （原因：{getattr(login, 'reason', '') or '未知'}）")
            bm.maybe_quit_idle()
            sys.exit(1)
    except Exception as e:
        print(f"  ⚠️ 登录态检查异常（继续）：{e}")

    # 导航到 /job_detail/{job_id}.html
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    print(f"\n🌐 导航：{url}")
    try:
        tab.get(url)
    except Exception as e:
        print(f"  ❌ 导航失败：{e}")
        bm.maybe_quit_idle()
        sys.exit(1)

    # 等页面加载（详情页 JS 渲染较慢）
    print("⏳ 等待页面加载（3s）...")
    time.sleep(3)

    # 打印最终 URL（看 Boss 是否重定向到 job-detail?securityId=...）
    try:
        final_url = tab.url or ""
    except Exception:
        final_url = ""
    print(f"📍 最终 URL：{final_url}")
    if "securityId" in final_url:
        print("  ✅ Boss JS 生成了 securityId（说明详情页正常加载）")
    elif "/job_detail/" in final_url:
        print("  ℹ️ URL 仍是 /job_detail/ 路径（未重定向，但页面可能已渲染）")
    else:
        print(f"  ⚠️ URL 既无 securityId 也非 /job_detail/，可能被重定向到别处")

    # 探查按钮
    probe_buttons(tab)

    # 用修复后的 WebGreeter 跑 dry_run greet（验证完整定位链路）
    dry_run_greet(bm, job_id)

    print("\n" + "=" * 60)
    print("✅ 验证完成（浏览器保持打开 30s 供你肉眼确认，可 Ctrl+C 提前退出）")
    print("=" * 60)
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        bm.maybe_quit_idle()


if __name__ == "__main__":
    main()
