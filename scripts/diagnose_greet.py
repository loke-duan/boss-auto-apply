#!/usr/bin/env python3
"""CDP 诊断脚本：抓 click「立即沟通」失败的现场。

背景（与 web_greeter.click_timeout 根因排查配合）：
  日志里 "发送部分失败：click_timeout" 来自 WebGreeter（DrissionPage 引擎），
  表示点 .btn-startchat 后 10s 内没检测到任何成功/风控信号。
  5 个根因假设（H1-H5）需要现场 dump 才能定向修复：
    H1 zpAegis 拦截 click（详情页 click 也被吞）
    H2 信号漏匹配（新弹窗/新 URL/新文案未覆盖）
    H3 按钮被遮挡（overlay 盖在 btn-startchat 上）
    H4 页面没渲染完（click 时事件未绑定）
    H5 误报（实际发送成功只是没检测到）

这个脚本用 nodriver 启动 Chrome（已带 remote-debugging-port），通过 CDP 安全域抓：
  - Network.enable + Network.responseReceived（监控 /wapi/ 相关响应，看 zpToken 是否 200）
  - Log.enable（控制台日志，看 zpAegis 是否抛错或警告）
  - Page.captureScreenshot（点击前/后各一张，看 DOM 视觉变化）
  - 单次 evaluate（取按钮 boundingBox + document.elementFromPoint 命中元素 + body 文本）
  注：刻意不调 Runtime.enable——它会触发 zpAegis 检测（ADR-0003 原理）。
  nodriver 的 page.evaluate 内部实现不调 Runtime.enable，是安全的。

输出：data/diagnose/{job_id}_{timestamp}/ 含：
  - before_click.png / after_click_3s.png / after_click_10s.png
  - dom_before.html / dom_after.html
  - cdp_network.log（wapi 网络响应）
  - cdp_console.log（控制台日志）
  - button_bbox.json（按钮位置 + elementFromPoint 命中）
  - body_text_after.txt（点击后页面文本）
  - summary.md（人读摘要，含 5 个假设的判定建议）

用法：
  python scripts/diagnose_greet.py [job_id]
  不传 job_id 则从 DB 取一个 found/image_ready 状态的 job。

前置：
  1. 已用 python -m boss_auto_apply login 建立登录态
  2. 关闭所有日常 Chrome 进程（pkill -9 -f "Google Chrome"）
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

# 项目根加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ============================================================
# 配置
# ============================================================
PROFILE_DIR = os.path.expanduser("~/.boss-auto-apply/chrome-profile")
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DIAGNOSE_ROOT = ROOT / "data" / "diagnose"
DETAIL_URL_TMPL = "https://www.zhipin.com/job_detail/{job_id}.html"


# ============================================================
# 工具
# ============================================================
def pick_job_id(explicit: str | None) -> tuple[str, str]:
    """从 DB 取一个 found/image_ready 状态的 job_id，或用显式指定的。返回 (job_id, title)。"""
    if explicit:
        return explicit, "(explicit)"
    import sqlite3
    db_path = ROOT / "data" / "jobs.db"
    if not db_path.exists():
        print(f"❌ DB 不存在：{db_path}")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT job_id, title FROM jobs WHERE status IN ('found','image_ready','greeted','text_sent') "
        "ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        print("❌ DB 里没有可诊断的 job（found/image_ready/greeted/text_sent）")
        sys.exit(1)
    print(f"📋 自动选取 job：{row[1]} (job_id={row[0]})")
    return row[0], row[1]


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def kill_chrome(proc: subprocess.Popen | None) -> None:
    """kill 预启动的 Chrome 进程。"""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _jsonify_result(result: Any) -> Any:
    """把 nodriver evaluate 的返回值规整成 Python 原生类型。

    nodriver 0.50.3 的 evaluate（不带 return_by_value）返回的是 RemoteObject 序列化格式：
    - 标量：{'type': 'string', 'value': 'xxx'} / {'type': 'number', 'value': 123}
    - 数组：{'type': 'array', 'value': [...]}
    - 对象：{'type': 'object', 'value': [[k1, v1], [k2, v2], ...]}  ← 注意是 list of [k, v] 不是 dict
    - 顶层可能直接是这个结构，也可能被包成 [[k, v], ...]（IIFE 返回对象的特殊情况）

    Args:
        result: nodriver evaluate 返回值。

    Returns:
        规整后的 Python dict / list / 标量。
    """
    if result is None:
        return None
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return result
    # 标量类型直接返回
    if isinstance(result, (int, float, bool)):
        return result
    # nodriver 的 RemoteObject 序列化格式：{'type': ..., 'value': ...}
    if isinstance(result, dict) and "type" in result and "value" in result:
        return _unwrap_remote_value(result)
    # 顶层是 [[k, v], ...]（IIFE 返回对象，nodriver 把它解成 list of pairs）
    if isinstance(result, list) and result and isinstance(result[0], list) and len(result[0]) == 2:
        # 看起来是 [[k, v], ...] 形式的对象，还原成 dict
        return _pairs_to_dict(result)
    # 顶层是 list，里面的元素是 RemoteObject 格式
    if isinstance(result, list):
        return [_jsonify_result(x) for x in result]
    # 普通 dict（无 type/value 包装）
    if isinstance(result, dict):
        return {k: _jsonify_result(v) for k, v in result.items()}
    return result


def _unwrap_remote_value(node: dict) -> Any:
    """递归拆 RemoteObject 的 {type, value} 包装。"""
    if not isinstance(node, dict):
        return _jsonify_result(node)
    if "type" not in node:
        return _jsonify_result(node)
    t = node.get("type")
    v = node.get("value")
    if t in ("string", "number", "boolean"):
        return v
    if t in ("null", "undefined"):
        return None
    if t == "array":
        return [_jsonify_result(x) for x in (v or [])]
    if t == "object":
        # object 的 value 是 [[k, v], ...]
        if isinstance(v, list):
            return _pairs_to_dict(v)
        if isinstance(v, dict):
            return {k: _jsonify_result(x) for k, x in v.items()}
        return v
    return v


def _pairs_to_dict(pairs: list) -> dict:
    """把 [[k, v], ...] 形式还原成 dict，递归 unwrap 每个 v。"""
    out = {}
    if not isinstance(pairs, list):
        return out
    for pair in pairs:
        if isinstance(pair, list) and len(pair) == 2:
            k, v = pair
            out[k] = _jsonify_result(v)
    return out


# ============================================================
# 诊断主流程（async，用 nodriver）
# ============================================================
async def diagnose(job_id: str, title: str, out_dir: Path) -> dict:
    """跑一次诊断，返回 summary dict。"""
    import nodriver as uc

    port = find_free_port()
    print(f"[1/8] 预启动 Chrome（port={port}，profile={PROFILE_DIR}）...")
    args = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=zh-CN",
        "--no-sandbox",
    ]
    chrome_proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port_ready(port, timeout=30):
            return {"error": f"Chrome 端口 {port} 30s 未就绪"}
        print(f"      Chrome 端口就绪 ✅")
        browser = await uc.start(host="127.0.0.1", port=port, browser_args=[], sandbox=False)

        print(f"[2/8] 打开详情页：{DETAIL_URL_TMPL.format(job_id=job_id)}")
        page = await browser.get(DETAIL_URL_TMPL.format(job_id=job_id))
        # 给页面充分渲染时间（zpToken + Vue 初始化）
        await asyncio.sleep(6)

        # ===== 步骤3：dump 点击前 DOM + 按钮位置 =====
        print(f"[3/8] dump 点击前 DOM + 按钮位置...")
        # 找按钮 + boundingBox + elementFromPoint（nodriver evaluate 不调 Runtime.enable）
        probe_js = """
        (() => {
            const result = {buttons: [], found: null, blocker: null, url: location.href, body_text_len: 0};
            // 找所有疑似 greet 按钮
            const selectors = ['.btn-startchat', '.btn-greet', '.btn-continuechat',
                              '.job-detail-container .btn-greet', '.detail-op .btn-greet',
                              '.job-box .btn-greet'];
            const seen = new Set();
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (seen.has(el)) continue;
                    seen.add(el);
                    const rect = el.getBoundingClientRect();
                    const text = (el.textContent || '').trim().slice(0, 30);
                    result.buttons.push({
                        selector: sel, tag: el.tagName, text: text,
                        class: el.className, rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height},
                        visible: rect.width > 0 && rect.height > 0,
                        disabled: el.disabled || false,
                    });
                }
            }
            // 取第一个可见按钮，看 elementFromPoint 命中的是不是它（H3 遮挡假设）
            const visibleBtn = result.buttons.find(b => b.visible);
            if (visibleBtn) {
                const cx = visibleBtn.rect.x + visibleBtn.rect.w / 2;
                const cy = visibleBtn.rect.y + visibleBtn.rect.h / 2;
                const hit = document.elementFromPoint(cx, cy);
                result.found = {
                    target_text: visibleBtn.text, target_class: visibleBtn.class,
                    center: {x: cx, y: cy},
                    hit_at_center: hit ? {tag: hit.tagName, class: hit.className, text: (hit.textContent||'').trim().slice(0,30)} : null,
                    hit_matches: hit === document.querySelector(visibleBtn.selector),
                };
                // 若命中元素不是按钮本身，记录遮挡元素
                if (hit && !hit.matches(visibleBtn.selector) && !hit.closest(visibleBtn.selector)) {
                    result.blocker = {tag: hit.tagName, class: hit.className,
                                      text: (hit.textContent||'').trim().slice(0, 80)};
                }
            }
            result.body_text_len = (document.body.innerText || '').length;
            return result;
        })()
        """
        probe_before = await page.evaluate(probe_js, await_promise=True)
        # nodriver evaluate 可能返回 RemoteObject / dict / str / 其他原生类型
        # 用 _jsonify_result 统一规整成 Python dict
        probe_data = _jsonify_result(probe_before)
        if not isinstance(probe_data, dict):
            # 退化场景：nodriver 把对象包成 list 或其他类型，直接存原始 json 文本
            print(f"      ⚠️ probe_before 返回非 dict（type={type(probe_before).__name__}），存原始值")
            (out_dir / "button_bbox_raw.txt").write_text(
                str(probe_before)[:5000], encoding="utf-8"
            )
            probe_data = {"buttons": [], "_raw_type": type(probe_before).__name__}
        (out_dir / "button_bbox.json").write_text(
            json.dumps(probe_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"      找到 {len(probe_data.get('buttons', []))} 个疑似按钮；"
              f"blocker={probe_data.get('blocker')}; body_text_len={probe_data.get('body_text_len')}")

        # 点击前截图 + DOM
        try:
            await page.save_screenshot(str(out_dir / "before_click.png"))
            print(f"      截图 before_click.png ✅")
        except Exception as e:
            print(f"      截图失败（忽略）：{e}")
        try:
            html_before = await page.get_content()
            (out_dir / "dom_before.html").write_text(html_before, encoding="utf-8")
        except Exception as e:
            print(f"      dump DOM 失败（忽略）：{e}")

        # ===== 步骤4：判断能否继续 click =====
        if not probe_data.get("found"):
            print(f"[4/8] ⚠️ 没找到可见的 greet 按钮——可能页面未登录/被风控/已下线")
            return {
                "stage": "locate_button",
                "probe": probe_data,
                "diagnosis": "未找到可见按钮，先检查登录态（url 是否含 /web/user）",
            }
        if probe_data.get("blocker"):
            print(f"[4/8] ⚠️ 按钮 center 命中的不是按钮本身——疑似被遮挡（H3）")
            print(f"      blocker={probe_data['blocker']}")

        # ===== 步骤5：点击 =====
        print(f"[5/8] 点击按钮（文本：{probe_data['found'].get('target_text', '?')}）...")
        # 用 JS dispatch click（绕过 nodriver click 可能的 click-to-center 问题）
        # 注意：这里点的是 .btn-startchat 等 selector 命中的第一个元素
        click_js = """
        (() => {
            const selectors = ['.btn-startchat', '.btn-greet', '.btn-continuechat'];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.getBoundingClientRect().width > 0) {
                    const rect = el.getBoundingClientRect();
                    const evt = new MouseEvent('click', {
                        bubbles: true, cancelable: true, view: window,
                        clientX: rect.x + rect.width/2, clientY: rect.y + rect.height/2,
                    });
                    el.dispatchEvent(evt);
                    return {clicked: true, selector: sel, text: (el.textContent||'').trim().slice(0,30)};
                }
            }
            return {clicked: false};
        })()
        """
        click_result = _jsonify_result(await page.evaluate(click_js, await_promise=True))
        if not isinstance(click_result, dict):
            click_result = {"raw": str(click_result)[:500], "_type": type(click_result).__name__}
        print(f"      click 结果：{click_result}")

        # ===== 步骤6-7：点击后 3s/10s dump =====
        for delay, label in [(3, "after_click_3s"), (10, "after_click_10s")]:
            print(f"[6/8] 等待 {delay}s 后 dump（{label}）...")
            await asyncio.sleep(delay if delay == 3 else 7)  # 第二次只多等 7s（3+7=10）
            try:
                await page.save_screenshot(str(out_dir / f"{label}.png"))
            except Exception as e:
                print(f"      截图失败（忽略）：{e}")
            # 点击后的 URL + body 文本 + 弹窗检测
            probe_after_js = """
            (() => {
                const result = {url: location.href, body_text: '', dialogs: [], toasts: []};
                result.body_text = (document.body.innerText || '').slice(0, 800);
                // 检测各种弹窗/toast
                const dialogSelectors = [
                    '.dialog', '.modal', '.layui-layer', '.van-dialog', '.el-dialog',
                    '.captcha', '.verify-wrap', '.slide-verify',
                    '.already-friend', '.friend-dialog',
                ];
                for (const sel of dialogSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        result.dialogs.push({selector: sel, text: (el.textContent||'').trim().slice(0,200)});
                    }
                }
                // toast 类
                const toasts = document.querySelectorAll('.toast, .tip, .message-tip, .van-toast, [class*="toast"]');
                for (const t of toasts) {
                    const txt = (t.textContent || '').trim();
                    if (txt && t.offsetParent !== null) {
                        result.toasts.push({text: txt.slice(0, 100), class: t.className});
                    }
                }
                return result;
            })()
            """
            probe_after = _jsonify_result(await page.evaluate(probe_after_js, await_promise=True))
            if not isinstance(probe_after, dict):
                print(f"      ⚠️ probe_after 返回非 dict（type={type(probe_after).__name__}）")
                probe_after = {"url": "", "body_text": str(probe_after)[:500],
                               "dialogs": [], "toasts": []}
            (out_dir / f"probe_{label}.json").write_text(
                json.dumps(probe_after, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            url = probe_after.get("url", "")
            print(f"      URL={url}")
            print(f"      dialogs={len(probe_after.get('dialogs', []))}, toasts={len(probe_after.get('toasts', []))}")
            # 跳聊天页就是成功
            if "/web/geek/chat" in url:
                print(f"      ✅ 已跳转聊天页（greet 成功）")
                break

        # ===== 步骤8：dump 网络日志（从浏览器要） =====
        # nodriver 没有直接暴露 CDP Network 事件订阅，我们通过 Performance API 拿网络请求时间线
        print(f"[7/8] 抓 Performance API 网络请求时间线...")
        perf_js = """
        (() => {
            const entries = performance.getEntriesByType('resource')
                .filter(e => e.name.includes('/wapi/') || e.name.includes('zpToken') || e.name.includes('zppassport'))
                .map(e => ({url: e.name, duration: Math.round(e.duration), size: e.transferSize, status: e.responseStatus}));
            return entries;
        })()
        """
        perf = _jsonify_result(await page.evaluate(perf_js, await_promise=True))
        if not isinstance(perf, list):
            perf = []
        (out_dir / "network_wapi.log").write_text(
            json.dumps(perf, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"      抓到 {len(perf)} 条 wapi/zpToken 请求")

        # ===== 步骤8：取 body 文本全文 + 控制台日志 =====
        print(f"[8/8] dump 完整 body 文本 + summary...")
        body_js = "document.body.innerText.slice(0, 3000)"
        body_text = _jsonify_result(await page.evaluate(body_js, await_promise=True))
        (out_dir / "body_text_after.txt").write_text(str(body_text), encoding="utf-8")

        # 综合判定
        final_url = probe_after.get("url", "") if probe_after else ""
        summary = {
            "job_id": job_id,
            "title": title,
            "final_url": final_url,
            "click_result": click_result,
            "buttons_found": len(probe_data.get("buttons", [])),
            "blocker": probe_data.get("blocker"),
            "dialog_count": len(probe_after.get("dialogs", [])) if probe_after else 0,
            "dialog_texts": [d.get("text", "")[:80] for d in (probe_after or {}).get("dialogs", [])],
            "toast_count": len(probe_after.get("toasts", [])) if probe_after else 0,
            "wapi_count": len(perf),
            "wapi_entries": [{"url": e.get("url", ""), "status": e.get("status"),
                              "duration": e.get("duration"), "size": e.get("size")} for e in perf[:5]],
            "body_text_snippet": str(body_text)[:300],
        }
        # 5 个假设的自动判定建议
        h_hints = []
        if "/web/geek/chat" in final_url:
            h_hints.append("✅ 已跳聊天页 → greet 成功；若 web_greeter 仍报 click_timeout，是 H2 信号漏匹配")
        elif summary["blocker"]:
            h_hints.append("⚠️ elementFromPoint 命中遮挡元素 → H3 按钮被遮挡，建议改 JS dispatch click")
        elif summary["dialog_count"] == 0 and summary["toast_count"] == 0 and "/web/geek/chat" not in final_url:
            h_hints.append("🚨 点击后无任何信号（无跳转/无弹窗/无 toast）→ H1 zpAegis 拦截 或 H4 事件未绑定")
            # 进一步看 wapi
            zpstatus = [e.get("status") for e in perf if "zpToken" in e.get("url", "")]
            if zpstatus and 0 in zpstatus:
                h_hints.append("   zpToken 返回 0 字节 → 强烈指向 H1 zpAegis 拦截")
            elif not zpstatus:
                h_hints.append("   没看到 zpToken 请求 → 可能 H4 页面未完全加载，事件未触发 wapi")
        else:
            h_hints.append(f"⚠️ 检测到 {summary['dialog_count']} 弹窗 + {summary['toast_count']} toast，"
                          f"但 web_greeter 没匹配上 → H2 信号漏匹配（selector/关键词需补）")
            for d in summary["dialog_texts"][:3]:
                h_hints.append(f"   弹窗内容：{d}")
        summary["diagnosis_hints"] = h_hints
        return summary
    finally:
        try:
            await browser.stop()
        except Exception:
            pass
        kill_chrome(chrome_proc)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="CDP 诊断 click「立即沟通」失败的现场")
    parser.add_argument("job_id", nargs="?", default=None, help="job_id（不传则自动从 DB 取）")
    parser.add_argument("--out-dir", default=None, help="输出目录（默认 data/diagnose/{job_id}_{ts}/）")
    args = parser.parse_args()

    # 检查 Chrome 已关闭（避免 profile 冲突）
    try:
        ps = subprocess.run(["pgrep", "-f", "Google Chrome"], capture_output=True, text=True)
        if ps.stdout.strip():
            print("⚠️ 检测到 Google Chrome 进程在运行，可能与诊断脚本抢 profile。")
            print(f"   pids: {ps.stdout.strip()}")
            resp = input("继续吗？(y/N): ").strip().lower()
            if resp != "y":
                print("已取消。请先 pkill -9 -f 'Google Chrome' 再重试。")
                return
    except Exception:
        pass

    job_id, title = pick_job_id(args.job_id)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else DIAGNOSE_ROOT / f"{job_id}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n📁 输出目录：{out_dir}\n")

    summary = asyncio.run(diagnose(job_id, title, out_dir))

    # 写 summary.md（人读）
    summary_md = ["# CDP 诊断报告", ""]
    summary_md.append(f"- job_id: `{summary.get('job_id', job_id)}`")
    summary_md.append(f"- title: {summary.get('title', title)}")
    summary_md.append(f"- final_url: `{summary.get('final_url', '?')}`")
    summary_md.append(f"- 找到按钮数: {summary.get('buttons_found', 0)}")
    summary_md.append(f"- 弹窗数: {summary.get('dialog_count', 0)}")
    summary_md.append(f"- toast 数: {summary.get('toast_count', 0)}")
    summary_md.append(f"- wapi 请求数: {summary.get('wapi_count', 0)}")
    summary_md.append(f"- blocker: `{summary.get('blocker')}`")
    summary_md.append("")
    summary_md.append("## 诊断建议")
    for h in summary.get("diagnosis_hints", ["（无）"]):
        summary_md.append(f"- {h}")
    summary_md.append("")
    summary_md.append("## wapi 请求前 5 条")
    for e in summary.get("wapi_entries", []):
        summary_md.append(f"- {e}")
    summary_md.append("")
    summary_md.append("## body 文本片段")
    summary_md.append("```")
    summary_md.append(summary.get("body_text_snippet", ""))
    summary_md.append("```")
    (out_dir / "summary.md").write_text("\n".join(summary_md), encoding="utf-8")

    print("\n" + "=" * 60)
    print("📊 诊断完成。摘要：")
    print("=" * 60)
    for h in summary.get("diagnosis_hints", ["（无）"]):
        print(f"  {h}")
    print(f"\n📁 完整报告：{out_dir / 'summary.md'}")
    print(f"📁 现场文件目录：{out_dir}")


if __name__ == "__main__":
    main()
