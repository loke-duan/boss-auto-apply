#!/usr/bin/env python3
"""测试 4 种 contenteditable 写入方式，找出对 Boss 聊天页有效的。"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nodriver
from boss_auto_apply.browser.nodriver_sender import NodriverChatSender

PROFILE = os.path.expanduser("~/.boss-auto-apply/chrome-profile")
TEXT = "测试话术XYZ"

async def read_input(page):
    r = await page.evaluate('document.querySelector(".chat-input[contenteditable=\\"true\\"]")?.innerText || "<null>"')
    return str(r)

async def try_method(page, method):
    ci = await page.query_selector('.chat-input[contenteditable="true"]')
    if not ci:
        print(f"  [{method}] 输入框未找到"); return
    # 先清空
    await page.evaluate('(()=>{const c=document.querySelector(".chat-input[contenteditable=\\"true\\"]");if(c){c.innerHTML="";c.focus();}})()')
    await asyncio.sleep(0.3)

    if method == "send_keys":
        await ci.click(); await asyncio.sleep(0.2)
        await ci.send_keys(TEXT)
    elif method == "execCommand":
        await page.evaluate(f"""(()=>{{const c=document.querySelector('.chat-input[contenteditable="true"]');
            c.focus(); document.execCommand('selectAll'); document.execCommand('insertText', false, {repr(TEXT)}); }})()""")
    elif method == "innerText+input":
        await page.evaluate(f"""(()=>{{const c=document.querySelector('.chat-input[contenteditable="true"]');
            c.focus(); c.innerText={repr(TEXT)};
            c.dispatchEvent(new InputEvent('input',{{bubbles:true,data:{repr(TEXT)},inputType:'insertText'}})); }})()""")
    elif method == "textContent":
        await page.evaluate(f"""(()=>{{const c=document.querySelector('.chat-input[contenteditable="true"]');
            c.focus(); c.textContent={repr(TEXT)};
            c.dispatchEvent(new Event('input',{{bubbles:true}})); }})()""")

    await asyncio.sleep(0.5)
    val = await read_input(page)
    print(f"  [{method}] 输入框={val!r} {'✅' if TEXT in val else '❌'}")

async def main():
    sender = NodriverChatSender(user_data_dir=PROFILE, headless=False, sandbox=False)
    uc = sender._get_nodriver()
    browser = await sender._launch_chrome_and_connect(uc)
    try:
        # 用完整流程进聊天页（点继续沟通）
        from boss_auto_apply.models import JobRow
        # 运行时请把 SAMPLE_JOB_ID 换成你本地 DB 里某个真实 job 的 securityId
        page = await browser.get("https://www.zhipin.com/job_detail/SAMPLE_JOB_ID.html")
        await asyncio.sleep(5)
        # 点继续沟通
        btn = await page.find("继续沟通", timeout=10)
        if btn: await btn.click()
        await asyncio.sleep(8)  # 等 SPA

        print("=== 测试 4 种写入方式 ===")
        for m in ["send_keys", "execCommand", "innerText+input", "textContent"]:
            await try_method(page, m)
    finally:
        try: browser.stop()
        except: pass
        sender._kill_chrome()

asyncio.run(main())
