#!/usr/bin/env python3
"""验证脚本：nodriver 完整发送流程（greet→text→image），绕过 Sender 编排。

目的：隔离验证 NodriverChatSender 自身的完整能力。
  - 不启动 drissionpage BrowserManager（避免与 nodriver profile 冲突）
  - 直接调 NodriverChatSender.send_full（它自带详情页导航+点沟通+发话术+发图片）
  - 确认 nodriver 通道能完整跑通 text_sent + image_sent

前置条件（ADR-0003 硬约束）：
  必须先关闭所有 Chrome 进程（pkill -9 -f "Google Chrome"），
  否则 nodriver 启动会因 profile 占用而失败。

用法：
  1. 先关 Chrome：pkill -9 -f "Google Chrome"
  2. python scripts/verify_nodriver_full_send.py [job_id]
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from boss_auto_apply.browser.nodriver_sender import NodriverChatSender
from boss_auto_apply.config import load_config
from boss_auto_apply.models import JobRow


def main():
    # 占位默认值：请通过命令行参数传入你本地 DB 里某个真实 job 的 securityId
    job_id = sys.argv[1] if len(sys.argv) > 1 else "SAMPLE_JOB_ID"

    # 注意：不在此检测 Chrome 进程（本脚本自己会启动 nodriver 的 Chrome，会被 pgrep 抓到误报）
    # 运行前请手动确保已 pkill -9 -f "Google Chrome"

    cfg = load_config(os.path.join(ROOT, "config", "config.yaml"))
    sender_cfg = cfg.sender

    # 简历图片绝对路径
    img_rel = f"data/resumes/{job_id}/{job_id}/{job_id}_long.png"
    img_abs = os.path.join(ROOT, img_rel)
    if not os.path.exists(img_abs):
        # 兜底：找目录下任意 png
        import glob
        pngs = glob.glob(os.path.join(ROOT, "data/resumes", job_id, "**/*.png"), recursive=True)
        if pngs:
            img_abs = pngs[0]
        else:
            print(f"❌ 简历图片不存在：{img_abs}")
            sys.exit(1)
    print(f"📋 简历图片：{img_abs}（{os.path.getsize(img_abs)} bytes）")

    job = JobRow(
        job_id=job_id, target_name="验证", city="上海",
        title="AI产品经理", company="验证公司", status="image_ready",
    )

    # 构造 NodriverChatSender（与 Sender 构造时传的参数一致）
    sender = NodriverChatSender(
        user_data_dir=os.path.expanduser(sender_cfg.user_data_dir),
        headless=sender_cfg.headless,
        max_text_len=sender_cfg.chat_text_max_len,
        image_max_kb=sender_cfg.image_max_kb,
        image_width_px=sender_cfg.image_width_px,
        sandbox=False,  # macOS 持久化 profile 必需
    )

    greet_text = f"您好，看到贵司 {job.title} 岗位，希望进一步沟通。"
    print(f"\n👉 话术：{greet_text}")
    print(f"\n🚀 启动 nodriver 完整发送（greet→text→image，会真实投递）...\n")

    result = sender.send_full(job, greet_text, img_abs, dry_run=False, wait_relation_sec=8)

    print("\n" + "=" * 60)
    print("📋 NodriverChatSender.send_full 结果")
    print("=" * 60)
    print(f"  job_id:        {result.job_id}")
    print(f"  text_sent:     {result.text_sent}")
    print(f"  image_sent:    {result.image_sent}")
    print(f"  text_confirmed:{result.text_confirmed}")
    print(f"  image_confirmed:{result.image_confirmed}")
    print(f"  image_path:    {result.image_path}")
    print(f"  error:         {result.error!r}")
    print(f"  risk_signal:   {result.risk_signal!r}")
    print(f"  elapsed_sec:   {result.elapsed_sec}")

    print("\n" + "=" * 60)
    if result.image_sent:
        print("✅✅✅ 完整投递成功！text + image 都已送达 HR")
    elif result.text_sent and not result.image_sent:
        print("⚠️ 话术送达，但图片失败（部分成功，可 resume 重试图片）")
    elif result.error == "chat_page_not_rendered":
        print("❌ 聊天页 SPA 未渲染（可能 zpAegis 检测/nodriver 连接失败）")
    elif result.error:
        print(f"❌ 失败：{result.error}")
    else:
        print(f"ℹ️ 未完成：{result}")
    print("=" * 60)


if __name__ == "__main__":
    main()
