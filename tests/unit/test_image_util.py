"""test_image_util.py — 图片预处理共享函数测试（需求4：冗余清理，5+ case）。"""

from __future__ import annotations

import os

import pytest

from boss_auto_apply.browser.image_util import ImagePreprocessResult, preprocess_resume_image
from boss_auto_apply.errors import ImagePreprocessError


@pytest.fixture
def sample_png(tmp_path):
    """创建测试用 PNG 图片（100x100 红色）。"""
    from PIL import Image
    img = Image.new("RGB", (100, 100), color=(255, 0, 0))
    path = tmp_path / "test.png"
    img.save(str(path))
    return str(path)


@pytest.fixture
def large_png(tmp_path):
    """创建需要压缩的大 PNG（2000x2000）。"""
    from PIL import Image
    img = Image.new("RGB", (2000, 2000), color=(0, 128, 255))
    path = tmp_path / "large.png"
    img.save(str(path))
    return str(path)


def test_preprocess_small_image_passes_through(sample_png):
    """小图片 → 预处理成功，可能不压缩（changed 取决于模式）。"""
    result = preprocess_resume_image(sample_png, max_kb=1024, width_px=1080)
    assert isinstance(result, ImagePreprocessResult)
    assert os.path.exists(result.processed_path)
    assert result.size_kb <= 1024
    assert result.width <= 1080


def test_preprocess_large_image_resizes(large_png):
    """大图片 → 宽度被缩放到 ≤1080。"""
    result = preprocess_resume_image(large_png, max_kb=1024, width_px=1080)
    assert result.width <= 1080
    assert result.size_kb <= 1024
    assert result.changed is True


def test_preprocess_not_found_raises():
    """文件不存在 → ImagePreprocessError。"""
    with pytest.raises(ImagePreprocessError, match="不存在"):
        preprocess_resume_image("/tmp/__no_such_image__.png")


def test_preprocess_corrupt_file_raises(tmp_path):
    """损坏文件 → ImagePreprocessError。"""
    bad = tmp_path / "corrupt.png"
    bad.write_bytes(b"not an image at all")
    with pytest.raises(ImagePreprocessError, match="损坏"):
        preprocess_resume_image(str(bad))


def test_preprocess_custom_tag(sample_png):
    """自定义 tag → 输出文件名含 tag。"""
    result = preprocess_resume_image(sample_png, tag="custom_tag_123")
    assert "custom_tag_123" in result.processed_path


def test_preprocess_idempotent(sample_png):
    """幂等：原始文件不被修改。"""
    original_size = os.path.getsize(sample_png)
    preprocess_resume_image(sample_png)
    preprocess_resume_image(sample_png)
    assert os.path.getsize(sample_png) == original_size


def test_preprocess_accepts_logger(sample_png):
    """接受 logger_obj 参数（兼容调用方注入）。"""
    class FakeLog:
        def __init__(self):
            self.debugs = []
        def debug(self, msg):
            self.debugs.append(msg)
    log = FakeLog()
    preprocess_resume_image(sample_png, logger_obj=log)
    assert len(log.debugs) >= 1
