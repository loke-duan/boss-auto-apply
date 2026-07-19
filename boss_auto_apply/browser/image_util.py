"""图片预处理工具（纯 Pillow，无浏览器依赖）。

抽取自 ``WebChatSender._preprocess_image`` 和 ``NodriverChatSender._preprocess_image``
（两者逻辑完全一致）。集中到模块级纯函数，避免两条发送链路行为分叉。

设计 §7.3：压缩到 <1MB、宽 1080px，输出到临时目录（幂等，原始文件不动）。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..errors import ImagePreprocessError

__all__ = [
    "ImagePreprocessResult",
    "preprocess_resume_image",
]


@dataclass
class ImagePreprocessResult:
    """图片预处理结果（设计 §7.1）。

    与 ``web_chat_sender.ImagePreprocessResult`` 字段一致，
    后者已改为 re-export 本类以保持向后兼容。
    """

    original_path: str
    processed_path: str              # 压缩后的临时文件路径
    size_kb: float                   # 压缩后大小
    width: int                       # 压缩后宽度
    height: int
    changed: bool                    # 是否真的压缩了


def preprocess_resume_image(
    path: str,
    *,
    max_kb: int = 1024,
    width_px: int = 1080,
    tag: str = "resume",
    logger_obj: Any = None,
) -> ImagePreprocessResult:
    """图片预处理（Pillow）：压缩到 <max_kb、宽 <=width_px。

    流程（设计 §7.3）：
    1. Image.open(path)
    2. 若 width > width_px → resize
    3. 转 RGB（PNG 有透明通道时）
    4. 保存为 JPG quality=95 到临时文件 ``/tmp/boss_resume_{tag}.jpg``
    5. 若仍 >max_kb → 降 quality 到 90/85/80 循环直到 <max_kb
    6. 返回 :class:`ImagePreprocessResult`

    幂等：原始文件不动；输出到临时目录。

    Args:
        path: 原始图片路径（PNG/JPG）。
        max_kb: 压缩目标大小上限（KB），默认 1024（1MB）。
        width_px: 宽度上限（px），超过则等比缩放，默认 1080。
        tag: 临时文件标签（用于区分多份简历），默认 ``"resume"``。
        logger_obj: 可选 logger（默认用 loguru 全局 logger）。

    Returns:
        :class:`ImagePreprocessResult`。

    Raises:
        ImagePreprocessError: 图片不存在/损坏/Pillow 不可用/压不到 <max_kb。
    """
    log = logger_obj or logger
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - Pillow 已是依赖
        raise ImagePreprocessError(f"Pillow 不可用：{e}") from e
    if not os.path.exists(path):
        raise ImagePreprocessError(f"图片不存在：{path}")
    original_size_kb = os.path.getsize(path) / 1024.0
    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        raise ImagePreprocessError(f"图片损坏，Image.open 失败：{e}") from e
    # resize
    w, h = img.size
    changed = False
    if w > width_px:
        new_h = int(h * width_px / w)
        img = img.resize((width_px, new_h), Image.LANCZOS)
        w, h = img.size
        changed = True
    # 转 RGB（PNG 透明通道 / 调色板）
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
        changed = True
    # 输出路径
    out_dir = tempfile.gettempdir()
    out_path = os.path.join(out_dir, f"boss_resume_{tag}.jpg")
    # 降 quality 循环直到 < max_kb
    quality = 95
    while True:
        try:
            img.save(out_path, "JPEG", quality=quality)
        except Exception as e:
            raise ImagePreprocessError(f"图片保存失败：{e}") from e
        size_kb = os.path.getsize(out_path) / 1024.0
        if size_kb <= max_kb or quality <= 75:
            break
        quality -= 5
        changed = True
    final_kb = os.path.getsize(out_path) / 1024.0
    if final_kb > max_kb:
        raise ImagePreprocessError(
            f"图片压不到 <{max_kb}KB（最低 quality={quality}，仍 {final_kb:.0f}KB）"
        )
    log.debug(
        f"图片预处理：{path}({original_size_kb:.0f}KB) → {out_path}({final_kb:.0f}KB, "
        f"{w}x{h}, q={quality}, changed={changed})"
    )
    return ImagePreprocessResult(
        original_path=path,
        processed_path=out_path,
        size_kb=final_kb,
        width=w,
        height=h,
        changed=changed,
    )
