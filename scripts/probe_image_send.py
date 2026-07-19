#!/usr/bin/env python3
"""诊断：图片发送全链路实测。

1. 进聊天页
2. dump 所有 input[type=file]（看 accept 属性，区分图片/文件）
3. 上传图片
4. dump 上传后聊天流状态（img 元素、body 文本）
"""
import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nodriver
from boss_auto_apply.browser.nodriver_sender import NodriverChatSender

PROFILE = os.path.expanduser("~/.boss-auto-apply/chrome-profile")
# 占位 JOB_ID：运行时请改成你本地 DB 里某个真实 job 的 securityId
JOB_ID = "SAMPLE_JOB_ID"
IMG = os.path.expanduser(f"~/Documents/antigravity/boss直聘/data/resumes/{JOB_ID}/{JOB_ID}/{JOB_ID}_long.png")

async def dump(page, tag):
    r = await page.evaluate("""(()=> {
        const files = Array.from(document.querySelectorAll('input[type="file"]'));
        const imgs = Array.from(document.querySelectorAll('img'));
        const msgImgs = Array.from(document.querySelectorAll('.message-item img, .chat-message img, .msg-image, .message-image, [class*="image"] img'));
        return JSON.stringify({
            file_inputs: files.map(f=>({accept:f.accept||'',name:f.name||'',class:f.className||''})),
            file_count: files.length,
            all_img_count: imgs.length,
            msg_img_count: msgImgs.length,
            msg_img_loaded: msgImgs.map(i=>({src:(i.src||'').slice(0,50),nw:i.naturalWidth,loaded:i.naturalWidth>0})).slice(-3),
            body_tail: (document.body.innerText||'').slice(-150),
        });
    })()""")
    s = str(r)
    try: d = json.loads(s)
    except: d = {"raw": s[:300]}
    print(f"\n--- [{tag}] ---")
    print(f"  file_input 数: {d.get('file_count',0)}")
    for f in d.get('file_inputs',[])[:5]:
        print(f"    accept={f.get('accept')!r} name={f.get('name')!r} class={f.get('class')!r}")
    print(f"  全部 img: {d.get('all_img_count',0)}, 消息 img: {d.get('msg_img_count',0)}")
    for m in d.get('msg_img_loaded',[]):
        print(f"    src={m.get('src')!r} naturalWidth={m.get('nw')} loaded={m.get('loaded')}")
    print(f"  body 尾部: ...{d.get('body_tail','')[-100:]!r}")
    return d

async def main():
    sender = NodriverChatSender(user_data_dir=PROFILE, headless=False, sandbox=False)
    uc = sender._get_nodriver()
    browser = await sender._launch_chrome_and_connect(uc)
    try:
        page = await browser.get(f"https://www.zhipin.com/job_detail/{JOB_ID}.html")
        await asyncio.sleep(5)
        btn = await page.find("继续沟通", timeout=10)
        if btn: await btn.click()
        await asyncio.sleep(8)

        print("=== 上传前 ===")
        await dump(page, "上传前")

        # 找 file input
        files = await page.query_selector_all('input[type="file"]')
        print(f"\n找到 {len(files)} 个 file input")
        if not files:
            print("❌ 无 file input"); return

        # 逐个试 accept 属性，找图片那个
        for i, f in enumerate(files):
            acc = await page.evaluate(f'document.querySelectorAll("input[type=file]")[{i}].accept')
            print(f"  [{i}] accept={str(acc)!r}")

        # 上传到第一个（和当前代码一致）
        print(f"\n>>> 上传图片到 file_input[0]...")
        try:
            await files[0].send_file(IMG)
            print(">>> send_file 完成")
        except Exception as e:
            print(f">>> send_file 失败: {e}")

        await asyncio.sleep(5)
        print("\n=== 上传后 5s ===")
        await dump(page, "上传后 5s")

        await asyncio.sleep(10)
        print("\n=== 上传后 15s ===")
        await dump(page, "上传后 15s")

    finally:
        try: browser.stop()
        except: pass
        sender._kill_chrome()

asyncio.run(main())
