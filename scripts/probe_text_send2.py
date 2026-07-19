#!/usr/bin/env python3
"""诊断 v2：用 send_keys 输入（和真实代码一致），正确处理 nodriver 返回值。"""
import asyncio, os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import nodriver
from boss_auto_apply.browser.nodriver_sender import NodriverChatSender

PROFILE = os.path.expanduser("~/.boss-auto-apply/chrome-profile")

async def js(page, expr):
    """执行 JS 并取纯值（处理 RemoteObject）。"""
    r = await page.evaluate(expr)
    if r is None: return None
    s = str(r)
    # RemoteObject 的 str 是 repr，含 'type_' 等；纯值是直接字符串
    if 'RemoteObject' in s or 'deep_serialized' in s:
        return s  # 异常，原样返回便于排查
    return s

async def dump(page, tag):
    info = await page.evaluate('JSON.stringify({it:document.querySelector(\".chat-input[contenteditable=\\\"true\\\"]\")?.innerText?.slice(0,50), btns:Array.from(document.querySelectorAll(\"[class*=send],[class*=Send]\")).slice(0,5).map(b=>b.className+\"|\"+(b.innerText||\"\").slice(0,10)), mine:Array.from(document.querySelectorAll(\".message-box.mine,.chat-message.mine,.message-item.self\")).slice(-3).map(m=>(m.innerText||\"\").slice(0,30))})')
    s = str(info)
    try:
        d = json.loads(s)
    except Exception:
        d = {"raw": s[:200]}
    print(f"[{tag}] input={d.get('it')!r} btns={d.get('btns')} mine={d.get('mine')}")
    return d

async def main():
    sender = NodriverChatSender(user_data_dir=PROFILE, headless=False, sandbox=False)
    uc = sender._get_nodriver()
    browser = await sender._launch_chrome_and_connect(uc)
    try:
        page = await browser.get("https://www.zhipin.com/web/geek/chat")
        await asyncio.sleep(10)
        await dump(page, "初始")

        # 用 send_keys（和真实代码一致）
        ci = await page.query_selector('.chat-input[contenteditable="true"]')
        if not ci:
            print("❌ 输入框未找到")
            return
        await ci.click()
        await asyncio.sleep(0.5)
        await ci.send_keys("测试话术ABC")
        print(">>> send_keys 完成")
        await asyncio.sleep(1)
        await dump(page, "send_keys 后")

        # Enter 发送（keydown + keyup）
        await page.evaluate("""(()=>{
            const ci=document.querySelector('.chat-input[contenteditable="true"]');
            if(ci){ci.focus();
            ['keydown','keypress','keyup'].forEach(t=>{
                ci.dispatchEvent(new KeyboardEvent(t,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));
            });}
        })()""")
        print(">>> Enter dispatched")
        await asyncio.sleep(5)
        d = await dump(page, "Enter 后 5s")

        if not d.get('mine'):
            print(">>> Enter 无效，试 send_keys Enter")
            await ci.click()
            await ci.send_keys('\n')
            await asyncio.sleep(5)
            await dump(page, "send_keys(Enter) 后")

        await asyncio.sleep(5)
    finally:
        try: browser.stop()
        except: pass
        sender._kill_chrome()

asyncio.run(main())
