"""test_imager.py — F6 PNG 测试（设计 §5.10c，5 case）。

typst 未装时走 PyMuPDF 兜底路径（必跑）；typst 路径用 @skipif。
"""

from __future__ import annotations

import os
import shutil

import pytest

from boss_auto_apply.core import imager as imager_mod
from boss_auto_apply.errors import ImagerError

requires_typst = pytest.mark.skipif(not shutil.which("typst"), reason="typst 未装")


@pytest.fixture
def sample_pdf(sample_resume_pdf):
    """用测试简历 PDF 做渲染输入。"""
    return sample_resume_pdf


# ============================================================
# 测试用例
# ============================================================
def test_pdf_to_png_fallback_single_or_multi(sample_pdf, tmp_path):
    """用例2/3：PyMuPDF 兜底 → 产出 png（多页→多张）。"""
    pages = imager_mod.pdf_to_png_fallback(sample_pdf, str(tmp_path), dpi=100)
    assert len(pages) >= 1
    for p in pages:
        assert os.path.exists(p)
        assert os.path.getsize(p) > 0


def test_pdf_to_png_not_found_raises(tmp_path):
    """PDF 不存在 → ImagerError。"""
    with pytest.raises(ImagerError):
        imager_mod.pdf_to_png_fallback(str(tmp_path / "nope.pdf"), str(tmp_path))


def test_stitch_vertical(sample_pdf, tmp_path):
    """用例2：多页 → stitch 后 1 张长图。"""
    pages = imager_mod.pdf_to_png_fallback(sample_pdf, str(tmp_path), dpi=80)
    if len(pages) >= 2:
        long_png = str(tmp_path / "long.png")
        out = imager_mod.stitch_vertical(pages, long_png)
        assert os.path.exists(out)
        from PIL import Image
        im = Image.open(out)
        assert im.width > 0 and im.height > 0
    else:
        pytest.skip("简历只有 1 页，无法测多页拼接")


def test_stitch_empty_raises():
    """stitch 空 list → ImagerError。"""
    with pytest.raises(ImagerError):
        imager_mod.stitch_vertical([], "/tmp/x.png")


def test_png_file_size_reasonable(sample_pdf, tmp_path):
    """用例4：PNG 文件合理大小（DPI=100 不至于过大）。"""
    pages = imager_mod.pdf_to_png_fallback(sample_pdf, str(tmp_path), dpi=100)
    for p in pages:
        size_kb = os.path.getsize(p) / 1024
        # DPI=100 单页 PNG 一般 < 1MB；放宽到 5MB 防环境差异
        assert size_kb < 5 * 1024


def test_png_openable_with_pil(sample_pdf, tmp_path):
    """用例5：输出 png 能被 PIL 打开。"""
    pages = imager_mod.pdf_to_png_fallback(sample_pdf, str(tmp_path), dpi=100)
    from PIL import Image
    for p in pages:
        im = Image.open(p)
        assert im.width > 0
        # 宽度大致合理（A4 @ 100dpi ≈ 827）
        assert 400 < im.width < 3000


@requires_typst
def test_typst_to_png(tmp_path):
    """用例1：typst_to_png 单页 → png（typst 装了才跑）。"""
    typ_path = tmp_path / "r.typ"
    typ_path.write_text('#set page(margin: 2cm)\n= 测试\n内容\n', encoding="utf-8")
    pages = imager_mod.typst_to_png(str(typ_path), str(tmp_path), dpi=100, typst_bin="typst")
    assert len(pages) >= 1
    for p in pages:
        assert os.path.exists(p)


@requires_typst
def test_typst_to_png_multi_page(tmp_path):
    """用例2：typst_to_png 多页 → 带页码的 png（修复 {p} 模板缺失 bug）。

    回归：typst CLI 多页导出要求输出路径含 {p}，否则报
    "cannot export multiple images without a page number template"。
    """
    # 构造 3 页文档（用 pagebreak 分页）
    typ_path = tmp_path / "multi.typ"
    typ_path.write_text(
        '#set page(margin: 2cm)\n'
        '= 第一页\n内容A\n'
        '#pagebreak()\n'
        '= 第二页\n内容B\n'
        '#pagebreak()\n'
        '= 第三页\n内容C\n',
        encoding="utf-8",
    )
    pages = imager_mod.typst_to_png(str(typ_path), str(tmp_path), dpi=100, typst_bin="typst")
    assert len(pages) == 3
    # 文件名应含页码（_1/_2/_3）
    import os
    names = [os.path.basename(p) for p in pages]
    assert any("_1" in n for n in names), f"页码命名错误：{names}"
    assert any("_3" in n for n in names), f"页码命名错误：{names}"
    # 按数字排序（cv_2 在 cv_10 前）
    assert pages[0] < pages[1] < pages[2] or "_1" in names[0]


def test_to_image_falls_back_to_pymupdf(sample_pdf, tmp_path):
    """typst 不存在时 to_image 走 PyMuPDF 兜底（typ_path 也给个假的）。"""
    # 构造一个 typ_path 不存在的场景 → 应抛 ImagerError（因 typ 和 pdf 都需要）
    # 但若 pdf 存在，应走 pdf 兜底
    typ_path = str(tmp_path / "fake.typ")  # 不存在
    pages_or_path = imager_mod.to_image(
        "job1", typ_path, sample_pdf, str(tmp_path / "out"),
        dpi=80, typst_bin="typst-not-installed-xyz",
    )
    assert os.path.exists(pages_or_path)
