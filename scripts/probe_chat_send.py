#!/usr/bin/env python3
"""诊断：在 Boss 聊天页探查发送机制（Enter vs 发送按钮）。

目的：话术已输入到 contenteditable 但 Enter dispatch 后未发出，需确认真实发送方式。
"""
import asyncio
import nodriver
import os

PROFILE = os.path.expanduser("~/.boss-auto-apply/chrome-profile")

async def main():
    browser = await nodriver.start(
        sandbox=False, headless=False,
        user_data_dir=PROFILE,
        browser_args=["--no-first-run", "--no-default-browser-check", "--lang=zh-CN", "--no-sandbox"],
    )
    # 直接进聊天页
    page = await browser.get("https://www.zhipin.com/web/geek/chat")
    await asyncio.sleep(10)  # 等 SPA 渲染

    print("=== 探查聊天页发送机制 ===")
    # 1. contenteditable 输入框
    info = await page.evaluate("""(() => {
        const ci = document.querySelector('.chat-input[contenteditable="true"]');
        const sendBtns = document.querySelectorAll('[class*="send"], [class*="Send"], .btn-send, .chat-send, .send-btn, .icon-send');
        const allBtns = Array.from(document.querySelectorAll('button, a.btn, [role="button"]')).slice(0, 20);
        return JSON.stringify({
            input_exists: !!ci,
            input_html: ci ? ci.outerHTML.slice(0, 200) : null,
            send_btn_count: sendBtns.length,
            send_btns: Array.from(sendBtns).map(b => ({
                tag: b.tagName, class: b.className, text: (b.innerText||'').slice(0,20)
            })),
            sample_buttons: allBtns.map(b => ({
                tag: b.tagName, class: b.className, text: (b.innerText||'').slice(0,15)
            })),
            wrap_length: document.querySelector('#wrap')?.innerHTML?.length || 0,
        });
    })()""", return_by_value=True)
    if isinstance(info, tuple):
        info = info[0]
    import json
    print(json.dumps(json.loads(info), indent=2, ensure_ascii=False))

    # 2. 试着输入文本看输入框响应
    await page.evaluate("""(() => {
        const ci = document.querySelector('.chat-input[contenteditable="true"]');
        if (ci) { ci.focus(); ci.innerText = '测试话术'; ci.dispatchEvent(new Event('input', {bubbles:true})); }
    })()""", return_by_value=True)
    await asyncio.sleep(1)

    # 3. 看输入文本后，有没有发送按钮变成可用
    info2 = await page.evaluate("""(() => {
        const ci = document.querySelector('.chat-input[contenteditable="true"]');
        const sendBtns = Array.from(document.querySelectorAll('[class*="send"], [class*="Send"]'));
        return JSON.stringify({
            input_text: ci?.innerText,
            send_btns_after_input: sendBtns.map(b => ({
                class: b.className, text: (b.innerText||'').slice(0,20),
                disabled: b.disabled, pointer_events: getComputedStyle(b).pointerEvents
            })),
        });
    })()""", return_by_value=True)
    if isinstance(info2, tuple):
        info2 = info2[0]
    print("\n=== 输入文本后 ===")
    print(json.dumps(json.loads(info2), indent=2, ensure_ascii=False))

    await asyncio.sleep(15)
    browser.stop()

asyncio.run(main())
