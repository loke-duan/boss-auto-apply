#!/usr/bin/env python3
"""诊断：在 Boss 聊天页实测话术发送机制（Enter vs 发送按钮 vs 其他）。

复用 NodriverChatSender 的预启动逻辑（稳定），进聊天页后：
1. dump 输入框 + 发送按钮 DOM
2. 输入文本，看发送按钮状态变化
3. 尝试 Enter 发送，5s 后检查消息流
4. 如果 Enter 没发，尝试点发送按钮
"""
import asyncio
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import nodriver
from boss_auto_apply.browser.nodriver_sender import NodriverChatSender

PROFILE = os.path.expanduser("~/.boss-auto-apply/chrome-profile")

async def dump_state(page, tag):
    """dump 聊天页关键 DOM 状态。"""
    info = await page.evaluate("""(()=> {
        const ci = document.querySelector('.chat-input[contenteditable="true"]');
        const sendBtns = Array.from(document.querySelectorAll('[class*="send"],[class*="Send"],.btn-send,.chat-send'));
        const msgList = document.querySelector('.chat-message-list,.message-list,.chat-content,.chat-msg-list');
        const myMsgs = Array.from(document.querySelectorAll('.chat-message.mine,.message-item.self,.chat-msg.self-message,.message-box.mine'));
        return JSON.stringify({
            input_text: (ci?.innerText||'').slice(0,50),
            input_empty: !(ci?.innerText||'').trim(),
            send_btns: sendBtns.slice(0,5).map(b=>({tag:b.tagName,class:b.className,text:(b.innerText||'').slice(0,15),disabled:b.disabled})),
            msg_list_exists: !!msgList,
            msg_list_len: msgList?.children?.length || 0,
            my_msg_count: myMsgs.length,
            my_msg_texts: myMsgs.slice(-3).map(m=>(m.innerText||'').slice(0,40)),
        });
    })()""")
    # nodriver evaluate 返回值可能是 RemoteObject/str/tuple，统一转 str 再 parse
    s = str(info) if not isinstance(info, str) else info
    d = json.loads(s)
    print(f"\n--- [{tag}] ---")
    print(f"  输入框: empty={d['input_empty']}, text={d['input_text']!r}")
    print(f"  发送按钮: {len(d['send_btns'])} 个")
    for b in d['send_btns']:
        print(f"    {b['tag']}.{b['class']} text={b['text']!r} disabled={b['disabled']}")
    print(f"  消息列表: exists={d['msg_list_exists']}, children={d['msg_list_len']}")
    print(f"  我的消息: {d['my_msg_count']} 条, 末3条={d['my_msg_texts']}")
    return d

async def main():
    sender = NodriverChatSender(user_data_dir=PROFILE, headless=False, sandbox=False)
    uc = sender._get_nodriver()
    browser = await sender._launch_chrome_and_connect(uc)
    try:
        # 直接进聊天页（用之前已建立沟通的 job）
        page = await browser.get("https://www.zhipin.com/web/geek/chat")
        await asyncio.sleep(10)  # 等 SPA

        # 1. 初始状态
        await dump_state(page, "初始")

        # 2. 输入文本
        await page.evaluate("""(()=>{
            const ci=document.querySelector('.chat-input[contenteditable="true"]');
            if(ci){ci.focus();ci.innerText='测试话术123';
            ci.dispatchEvent(new Event('input',{bubbles:true}));}
        })()""", return_by_value=True)
        await asyncio.sleep(1)
        await dump_state(page, "输入文本后（看发送按钮是否启用）")

        # 3. Enter 发送
        await page.evaluate("""(()=>{
            const ci=document.querySelector('.chat-input[contenteditable="true"]');
            if(ci){ci.focus();
            const e=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true});
            ci.dispatchEvent(e);
            const e2=new KeyboardEvent('keyup',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true});
            ci.dispatchEvent(e2);}
        })()""", return_by_value=True)
        print("\n>>> Enter keydown+keyup dispatched")
        await asyncio.sleep(5)
        d = await dump_state(page, "Enter 后 5s（看消息是否发出）")

        # 4. 如果没发，试点击发送按钮
        if d['my_msg_count'] == 0 or not d['input_empty']:
            print("\n>>> Enter 无效，尝试点发送按钮...")
            clicked = await page.evaluate("""()=> {
                const btns = Array.from(document.querySelectorAll('[class*="send"],[class*="Send"]'));
                for(const b of btns){
                    if(!b.disabled && (b.innerText||'').includes('发送')){
                        b.click(); return 'clicked:'+b.className;
                    }
                }
                // 兜底：点任意可点的 send 类按钮
                for(const b of btns){
                    if(!b.disabled){b.click(); return 'clicked:'+b.className;}
                }
                return 'no_btn';
            }""", return_by_value=True)
            if isinstance(clicked, tuple): clicked = clicked[0]
            print(f">>> 点击结果: {clicked}")
            await asyncio.sleep(5)
            await dump_state(page, "点发送按钮后 5s")

        await asyncio.sleep(5)
    finally:
        try: browser.stop()
        except: pass
        sender._kill_chrome()

asyncio.run(main())
