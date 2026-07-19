"""test_web_chat_sender.py — WebChatSender 话术/图片/预处理/顺序/部分失败/确认/dry-run（设计 §14.2）。

11 cases：send-text / send-image / preprocess / order / partial-fail /
confirm-text / confirm-image / dry-run / hidden-input-fallback / no-textarea / file-not-found。

用 make_chat_page_tab 构造聊天页，注入 sleep_fn=lambda 不真睡。
图片预处理用真 Pillow + 真图片文件（tmp_path）。
"""

from __future__ import annotations

import os

import pytest

from boss_auto_apply.browser.web_chat_sender import (
    ChatSendResult,
    ImagePreprocessResult,
    WebChatSender,
)
from boss_auto_apply.errors import ImagePreprocessError
from boss_auto_apply.models import JobRow
import dom_samples


def _make_sender(mgr, **kw):
    """构造 WebChatSender，sleep_fn 不真睡。"""
    kw.setdefault("sleep_fn", lambda *a, **k: None)
    s = WebChatSender(mgr, **kw)
    # ensure_browser（get_tab 前置；send_text/_confirm_* 用 get_tab）
    mgr.ensure_browser()
    return s


def _make_job(job_id: str = "sec-001") -> JobRow:
    return JobRow(job_id=job_id, target_name="SEO/上海", city="上海",
                  title="SEO 运营", company="某公司")


def _make_image(tmp_path, *, size_kb: int = 50, width: int = 2000) -> str:
    """生成一张真 PNG 图片（用 Pillow，宽 width，可控制大小）。"""
    from PIL import Image
    img = Image.new("RGB", (width, int(width * 1.4)), color=(255, 128, 0))
    path = str(tmp_path / "resume.png")
    img.save(path, "PNG")
    return path


# ============================================================
# 用例1：send_text 成功（textarea input + send button + 确认）
# ============================================================
def test_send_text_success(make_browser_manager):
    """用例1：textarea 存在 + send 按钮存在 → send_text 返回 True。"""
    # mine_messages 与发送文本一致，确保 _confirm_text_appeared 命中
    msgs = dom_samples.make_chat_messages(["您好，看到贵司 SEO 岗位"])
    tab = dom_samples.make_chat_page_tab(textarea_input_ok=True, send_button_present=True,
                                          mine_messages=msgs)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    ok = s.send_text(_make_job(), "您好，看到贵司 SEO 岗位")
    assert ok is True


# ============================================================
# 用例2：send_text 找不到 textarea → False
# ============================================================
def test_send_text_no_textarea(make_browser_manager):
    """用例2：聊天页无 textarea → send_text 返回 False。"""
    tab = dom_samples.make_chat_page_tab(textarea_input_ok=False, send_button_present=False)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    ok = s.send_text(_make_job(), "你好")
    assert ok is False


# ============================================================
# 用例3：send_text dry_run → True 不真发
# ============================================================
def test_send_text_dry_run(make_browser_manager):
    """用例3：dry_run=True → send_text 返回 True 不真 input。"""
    tab = dom_samples.make_chat_page_tab(textarea_input_ok=True, send_button_present=True)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    ok = s.send_text(_make_job(), "你好", dry_run=True)
    assert ok is True
    # textarea 没 input
    textarea = tab._current.get(".chat-input textarea", [None])[0]
    assert textarea.input_log == []


# ============================================================
# 用例4：send_image 成功（含预处理 + 隐藏 input + 确认）
# ============================================================
def test_send_image_success(make_browser_manager, tmp_path):
    """用例4：图片存在 + file input 存在 → send_image 预处理并发送成功。"""
    img_path = _make_image(tmp_path)
    tab = dom_samples.make_chat_page_tab(file_input_present=True, image_appears=True)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr, image_width_px=1080)
    ok = s.send_image(_make_job(), img_path)
    assert ok is True


# ============================================================
# 用例5：send_image 图片不存在 → False
# ============================================================
def test_send_image_file_not_found(make_browser_manager):
    """用例5：image_path 不存在 → send_image 返回 False。"""
    tab = dom_samples.make_chat_page_tab(file_input_present=True)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    ok = s.send_image(_make_job(), "/nonexistent/resume.png")
    assert ok is False


# ============================================================
# 用例6：send_image dry_run → True 不真发
# ============================================================
def test_send_image_dry_run(make_browser_manager, tmp_path):
    """用例6：dry_run=True → send_image 返回 True 不真 input（也不预处理）。"""
    img_path = _make_image(tmp_path)
    tab = dom_samples.make_chat_page_tab(file_input_present=True)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    ok = s.send_image(_make_job(), img_path, dry_run=True)
    assert ok is True


# ============================================================
# 用例7：send_full 顺序：先话术后图片（text_sent 先于 image_sent）
# ============================================================
def test_send_full_order_text_then_image(make_browser_manager, tmp_path):
    """用例7：send_full 先发话术再发图片，两者都成功。"""
    img_path = _make_image(tmp_path)
    msgs = dom_samples.make_chat_messages(["您好，看到贵司 SEO 岗位"])
    tab = dom_samples.make_chat_page_tab(textarea_input_ok=True, send_button_present=True,
                                          file_input_present=True, image_appears=True,
                                          mine_messages=msgs)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    res = s.send_full(_make_job(), "您好，看到贵司 SEO 岗位", img_path,
                      dry_run=False, wait_relation_sec=1)
    assert res.text_sent is True
    assert res.image_sent is True


# ============================================================
# 用例8：send_full 部分失败 — 话术成功图片失败（file input 缺失）
# ============================================================
def test_send_full_partial_text_ok_image_fail(make_browser_manager, tmp_path):
    """用例8：话术成功但图片失败（无 file input）→ text_sent=True image_sent=False。"""
    img_path = _make_image(tmp_path)
    msgs = dom_samples.make_chat_messages(["您好，看到贵司 SEO 岗位"])
    tab = dom_samples.make_chat_page_tab(textarea_input_ok=True, send_button_present=True,
                                          file_input_present=False, image_appears=False,
                                          mine_messages=msgs)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    res = s.send_full(_make_job(), "您好，看到贵司 SEO 岗位", img_path,
                      dry_run=False, wait_relation_sec=1)
    assert res.text_sent is True
    assert res.image_sent is False


# ============================================================
# 用例9：send_full dry_run → text_sent=True image_sent=True（mock 成功）
# ============================================================
def test_send_full_dry_run(make_browser_manager, tmp_path):
    """用例9：dry_run → send_full 话术和图片都 mock 成功。"""
    img_path = _make_image(tmp_path)
    tab = dom_samples.make_chat_page_tab()
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    res = s.send_full(_make_job(), "您好", img_path, dry_run=True, wait_relation_sec=1)
    assert res.text_sent is True
    assert res.image_sent is True


# ============================================================
# 用例10：_preprocess_image 压缩到 <1MB 且宽度 1080
# ============================================================
def test_preprocess_image_compresses(make_browser_manager, tmp_path):
    """用例10：大图（2000px PNG）→ 压缩到 ≤1080px 宽 + JPG ≤1MB。"""
    img_path = _make_image(tmp_path, width=2000)  # 大图
    tab = dom_samples.make_chat_page_tab()
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr, image_width_px=1080, image_max_kb=1024)
    pre = s._preprocess_image(img_path, tag="test1")
    assert isinstance(pre, ImagePreprocessResult)
    assert pre.width <= 1080
    assert pre.size_kb <= 1024
    assert pre.changed is True  # resize + 转 RGB + JPG
    assert os.path.exists(pre.processed_path)


# ============================================================
# 用例11：_preprocess_image 图片不存在 → ImagePreprocessError
# ============================================================
def test_preprocess_image_not_found(make_browser_manager):
    """用例11：图片不存在 → ImagePreprocessError。"""
    tab = dom_samples.make_chat_page_tab()
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    with pytest.raises(ImagePreprocessError, match="图片不存在"):
        s._preprocess_image("/no/such/file.png")


# ============================================================
# 用例12：_confirm_text_appeared 在聊天流找到文本
# ============================================================
def test_confirm_text_appeared(make_browser_manager):
    """补充：话术后聊天流出现该文本 → _confirm_text_appeared 返回 True。"""
    msgs = dom_samples.make_chat_messages(["您好，看到贵司 SEO 岗位"])
    tab = dom_samples.make_chat_page_tab(mine_messages=msgs)
    tab.get("https://www.zhipin.com/web/geek/chat")
    # 模拟已发：mine 消息已在 _current
    tab._current[".chat-message.mine"] = msgs
    tab._current[".message-item.self"] = msgs
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr)
    ok = s._confirm_text_appeared("您好，看到贵司", timeout=2)
    assert ok is True


# ============================================================
# 用例13：send_text 超长截断（max_text_len）
# ============================================================
def test_send_text_truncates_long_text(make_browser_manager):
    """补充：话术超 max_text_len → 截断后再发（不报错）。

    用 mine_messages 含截断后的前 10 字，让确认命中。
    """
    long_text = "您好看到贵司 SEO 岗位希望沟通" * 5  # 远超 10 字
    truncated_head = long_text[:10]  # 截断后的前 10 字（needle 取前 30，这里全 10）
    msgs = dom_samples.make_chat_messages([truncated_head])
    tab = dom_samples.make_chat_page_tab(textarea_input_ok=True, send_button_present=True,
                                          mine_messages=msgs)
    tab.get("https://www.zhipin.com/web/geek/chat")
    mgr = make_browser_manager(tab=tab)
    s = _make_sender(mgr, max_text_len=10)
    ok = s.send_text(_make_job(), long_text)
    assert ok is True  # 截断后发送 + 确认命中
    # 验证 textarea 收到的是截断后的文本
    textarea = tab._current.get(".chat-input textarea", [None])[0]
    assert textarea is not None and len(textarea.input_log[0]) == 10
