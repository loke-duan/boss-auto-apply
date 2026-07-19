#!/usr/bin/env python3
"""诊断脚本：真实点击「立即沟通」并记录点击后页面状态，排查 login_lost 误判。

目的（排查 24h 硬熔断 risk_signal=login_lost 根因）：
  _detect_greet_outcome 用 risk.login_page_redirect 选择器（.login-box 等，verified=False）
  检测登录态丢失。怀疑是误判——点击后 Boss 页面出现某个含 login class 的元素被错误匹配。

本脚本做的事：
  1. 启动浏览器 + 确认登录态
  2. 导航到一个真实岗位详情页
  3. 点击「立即沟通」按钮（真点击）
  4. 立即探查点击后的页面：
     - 当前 URL（是否跳转 chat 页 / 登录页）
     - risk.login_page_redirect 4 个选择器各自是否命中
     - 页面上所有含 login/sign/user 文本的元素
     - 是否出现验证码/操作频繁 toast/已是好友弹窗
  5. 不进熔断，只记录（这是诊断脚本，不走 Sender）

用法：python scripts/verify_greet_outcome.py [job_id]
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from boss_auto_apply.browser.manager import BrowserConfig, BrowserManager
from boss_auto_apply.browser import selectors as sel
from boss_auto_apply.config import load_config


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
        print("❌ DB 里没有 found 状态的 job，请显式传 job_id")
        sys.exit(1)
    print(f"📋 自动选取 job：{row[1]} (job_id={row[0]})")
    return row[0]


def probe_post_click(tab) -> dict:
    """点击后探查页面状态，返回诊断结果。"""
    result = {}

    # 1) 当前 URL
    try:
        cur_url = tab.url or ""
    except Exception:
        cur_url = ""
    result["url"] = cur_url
    print(f"\n📍 点击后 URL：{cur_url}")
    if "/web/geek/chat" in cur_url:
        print("  ✅ 跳转聊天页 → relation_established=True")
        result["outcome"] = "chat_page"
    elif "/user" in cur_url or "login" in cur_url.lower():
        print("  ⚠️ URL 含 login/user → 可能真登录态丢失")
        result["outcome"] = "login_redirect"
    else:
        print("  ℹ️ URL 未跳转（可能弹窗/无反应）")

    # 2) risk.login_page_redirect 4 个选择器逐个测
    print("\n--- risk.login_page_redirect 选择器逐个命中情况 ---")
    login_selectors = [".login-box", ".sign-form", "#wrap.login-page", ".user-login"]
    login_hits = []
    for s in login_selectors:
        try:
            el = tab.ele(f"css:{s}", timeout=1)
            hit = bool(el)
            if hit:
                try:
                    txt = (el.text or "").strip()[:40]
                except Exception:
                    txt = ""
                # 看这个元素的可见性/位置
                try:
                    tag = el.tag
                except Exception:
                    tag = "?"
                print(f"  ❌ 命中：{s}  tag={tag} text={txt!r}")
                login_hits.append({"selector": s, "tag": tag, "text": txt})
            else:
                print(f"  ✅ 未命中：{s}")
        except Exception as e:
            print(f"  ⚠️ 异常：{s} → {e}")
    result["login_selector_hits"] = login_hits

    # 3) 暴力探查含 login/sign/user 文本的元素
    print("\n--- 页面上含 login/sign/user 关键词的元素 ---")
    kw_hits = []
    for kw in ["登录", "登陆", "login", "sign", "注册", "user-info", "user-login"]:
        try:
            els = tab.eles(f"text:{kw}", timeout=1)
            for el in els[:2]:
                try:
                    tag = el.tag
                except Exception:
                    tag = "?"
                try:
                    cls = el.attr("class") or ""
                except Exception:
                    cls = ""
                try:
                    txt = (el.text or "").strip()[:40]
                except Exception:
                    txt = ""
                # 判断是否可见（offsetParent 非空表示可见）
                try:
                    visible = el.states.is_displayed if hasattr(el, "states") else "?"
                except Exception:
                    visible = "?"
                info = f"kw={kw} tag={tag} class={cls!r} text={txt!r} visible={visible}"
                print(f"  {info}")
                kw_hits.append(info)
        except Exception:
            pass
    result["keyword_hits"] = kw_hits

    # 4) 其它风控信号
    print("\n--- 其它风控信号 ---")
    for name in ["risk.captcha", "risk.rate_limited_toast", "risk.already_friend_dialog"]:
        el = sel.find_element(tab, name, timeout=1)
        if el:
            try:
                txt = (el.text or "").strip()[:50]
            except Exception:
                txt = ""
            print(f"  ❌ 命中 {name}: text={txt!r}")
        else:
            print(f"  ✅ 未命中 {name}")

    # 5) body 文本里找「已发起沟通/等待对方回复/已是好友」
    print("\n--- body 文本匹配（已发起沟通等）---")
    try:
        body = tab.ele("css:body", timeout=1)
        body_text = (body.text or "") if body else ""
    except Exception:
        body_text = ""
    for kw in ["已发起沟通", "等待对方回复", "已发送打招呼", "已经是好友", "操作频繁", "请稍后再试"]:
        if kw in body_text:
            print(f"  ✅ body 含「{kw}」")
        # 只打印存在的

    return result


def main():
    explicit_id = sys.argv[1] if len(sys.argv) > 1 else None
    job_id = pick_job_id(explicit_id)

    cfg = load_config(os.path.join(ROOT, "config", "config.yaml"))
    sender_cfg = cfg.sender
    user_data_dir = getattr(sender_cfg, "user_data_dir", None) or "~/.boss-auto-apply/chrome-profile"
    bcfg = BrowserConfig(user_data_dir=os.path.expanduser(user_data_dir), headless=False)

    print(f"\n🚀 启动浏览器（profile={user_data_dir}）...")
    bm = BrowserManager.get_instance(bcfg)
    tab = bm.ensure_browser()

    print("🔐 检查登录态...")
    try:
        login = bm.check_login(navigate=True)
        if not login.logged_in:
            print(f"  ❌ 未登录！原因：{getattr(login, 'reason', '')}")
            bm.maybe_quit_idle()
            sys.exit(1)
        print(f"  ✅ 已登录（用户={login.username or '未知'}）")
    except Exception as e:
        print(f"  ⚠️ 登录态检查异常：{e}")

    # 导航详情页
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    print(f"\n🌐 导航：{url}")
    tab.get(url)
    time.sleep(3)

    # 定位并点击「立即沟通」
    print("\n🔍 定位「立即沟通」按钮...")
    btn = sel.find_element(tab, "detail.greet_button", timeout=5)
    if not btn:
        print("  ❌ 找不到「立即沟通」按钮")
        bm.maybe_quit_idle()
        sys.exit(1)
    try:
        btn_text = btn.text
    except Exception:
        btn_text = "?"
    print(f"  ✅ 找到按钮（text={btn_text!r}），即将点击...")

    # 真点击
    try:
        btn.click()
        print("  ✅ 已点击")
    except Exception as e:
        print(f"  ❌ 点击失败：{e}")
        bm.maybe_quit_idle()
        sys.exit(1)

    # 等响应
    time.sleep(2)

    # 探查点击后状态
    print("\n" + "=" * 60)
    print("🔬 点击后页面状态诊断")
    print("=" * 60)
    probe_post_click(tab)

    print("\n" + "=" * 60)
    print("✅ 诊断完成（浏览器保持 30s 供肉眼确认）")
    print("=" * 60)
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        bm.maybe_quit_idle()


if __name__ == "__main__":
    main()
