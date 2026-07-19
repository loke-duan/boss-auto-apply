"""F6 PNG（Typst 原生 + PyMuPDF 兜底，设计 §5.10c）。

PDF/typ → PNG。主路径 Typst 原生 ``--format png``；兜底 PyMuPDF。
多页 → Pillow 纵向拼接为长图（Boss 图简历场景）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from ..errors import ImagerError, TypstNotInstalledError
from .generator import check_typst

__all__ = [
    "typst_to_png",
    "pdf_to_png_fallback",
    "stitch_vertical",
    "to_image",
]


def typst_to_png(
    typ_path: str,
    out_dir: str,
    *,
    dpi: int = 200,
    template_dir: str = "templates/brilliant-cv",
    typst_bin: str = "typst",
    logger_obj: Any = None,
) -> list[str]:
    """typst compile --format png --ppi <dpi> → 可能多页。

    [假设] typst 多页 png 命名规则：``{base}_{pageno}.png``（1-based）或 ``{base}.png``（单页）。
    待核实 T6；此处用 glob 兜底捕获所有产出。

    Args:
        typ_path: .typ 源文件。
        out_dir: 输出目录。
        dpi: DPI（typst 用 --ppi）。
        template_dir: 模板目录（含 fonts/）。
        typst_bin: typst 可执行文件。
        logger_obj: loguru logger。

    Returns:
        产出的 png 路径列表（按页序）。

    Raises:
        TypstNotInstalledError: typst 未装。
        ImagerError: typst png 编译失败。
    """
    log = logger_obj or logger
    typst = check_typst(typst_bin)
    if not os.path.exists(typ_path):
        raise ImagerError(f".typ 文件不存在：{typ_path}")

    fonts_dir = os.path.join(template_dir, "fonts")
    font_path_args = ["--font-path", fonts_dir] if os.path.isdir(fonts_dir) else []

    out_dir_abs = os.path.abspath(out_dir)
    os.makedirs(out_dir_abs, exist_ok=True)
    base = Path(typ_path).stem
    # {p} 是 typst CLI 的页码占位符（1-based），多页导出必须带，否则报错。
    png_pattern = os.path.join(out_dir_abs, f"{base}_{{p}}.png")

    cmd = [typst, "compile"] + font_path_args + [
        "--format", "png", "--ppi", str(dpi),
        typ_path, png_pattern,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise ImagerError(f"typst png 编译超时：{typ_path}") from e
    if proc.returncode != 0:
        raise ImagerError(f"typst png 编译失败：{proc.stderr[:1000]}")

    # glob 收集产出（{base}_1.png、{base}_2.png...），按页码数字排序（避免字典序 10<2）
    import re as _re
    produced = sorted(
        Path(out_dir_abs).glob(f"{base}_*.png"),
        key=lambda p: int(_re.search(r"_(\d+)\.png$", p.name).group(1))
        if _re.search(r"_(\d+)\.png$", p.name) else 0,
    )
    if not produced:
        raise ImagerError(f"typst png 未产出文件：{png_pattern}")
    log.info(f"typst_to_png 产出 {len(produced)} 页：{[p.name for p in produced]}")
    return [str(p) for p in produced]


def pdf_to_png_fallback(
    pdf_path: str,
    out_dir: str,
    *,
    dpi: int = 200,
    logger_obj: Any = None,
) -> list[str]:
    """PyMuPDF：``page.get_pixmap(matrix=Matrix(dpi/72,dpi/72))`` → PNG。

    Args:
        pdf_path: PDF 文件路径。
        out_dir: 输出目录。
        dpi: DPI。
        logger_obj: loguru logger。

    Returns:
        产出的 png 路径列表（按页序）。

    Raises:
        ImagerError: PyMuPDF 未装 / PDF 打不开 / 渲染失败。
    """
    log = logger_obj or logger
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise ImagerError(f"PyMuPDF 未安装：{e}") from e

    if not os.path.exists(pdf_path):
        raise ImagerError(f"PDF 不存在：{pdf_path}")

    out_dir_abs = os.path.abspath(out_dir)
    os.makedirs(out_dir_abs, exist_ok=True)
    base = Path(pdf_path).stem

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        raise ImagerError(f"PDF 打不开：{pdf_path} ({e})") from e

    paths: list[str] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix)
            png_path = os.path.join(out_dir_abs, f"{base}_{i + 1}.png")
            pix.save(png_path)
            paths.append(png_path)
        doc.close()
    except Exception as e:
        try:
            doc.close()
        except Exception:
            pass
        raise ImagerError(f"PyMuPDF 渲染失败：{pdf_path} ({e})") from e

    if not paths:
        raise ImagerError(f"PyMuPDF 未产出 png（PDF 可能空）：{pdf_path}")
    log.info(f"pdf_to_png_fallback 产出 {len(paths)} 页")
    return paths


def stitch_vertical(images: list[str], out_path: str, *, gap: int = 0) -> str:
    """Pillow 纵向拼接多页为长图。

    Args:
        images: 待拼接的 png 路径列表。
        out_path: 输出长图路径。
        gap: 页间间隔像素。

    Returns:
        长图绝对路径。

    Raises:
        ImagerError: Pillow 未装 / 图片打不开。
    """
    if not images:
        raise ImagerError("stitch_vertical: images 为空")
    try:
        from PIL import Image
    except ImportError as e:
        raise ImagerError(f"Pillow 未安装：{e}") from e

    imgs = []
    for p in images:
        try:
            imgs.append(Image.open(p).convert("RGB"))
        except Exception as e:
            raise ImagerError(f"图片打不开：{p} ({e})") from e

    width = max(im.width for im in imgs)
    total_h = sum(im.height for im in imgs) + gap * (len(imgs) - 1)
    canvas = Image.new("RGB", (width, total_h), (255, 255, 255))
    y = 0
    for im in imgs:
        canvas.paste(im, (0, y))
        y += im.height + gap

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG")
    return out_path


def to_image(
    job_id: str,
    typ_path: str,
    pdf_path: str | None,
    out_dir: str,
    *,
    dpi: int = 200,
    template_dir: str = "templates/brilliant-cv",
    typst_bin: str = "typst",
    logger_obj: Any = None,
) -> str:
    """优先 typst_to_png；失败/typst 不支持 → pdf_to_png_fallback；多页 → stitch。

    Args:
        job_id: job id（日志用）。
        typ_path: .typ 源文件。
        pdf_path: 已编译的 PDF（兜底用；None 则只走 typst）。
        out_dir: 输出目录。
        dpi: DPI。
        template_dir: 模板目录。
        typst_bin: typst 可执行文件。
        logger_obj: loguru logger。

    Returns:
        单张 png 绝对路径（多页已拼接）。

    Raises:
        ImagerError: typst 和 PyMuPDF 双双失败。
    """
    log = logger_obj or logger
    job_out = os.path.join(out_dir, job_id)
    os.makedirs(job_out, exist_ok=True)

    pages: list[str] = []
    # 1) 优先 typst 原生 png
    try:
        pages = typst_to_png(
            typ_path, job_out, dpi=dpi,
            template_dir=template_dir, typst_bin=typst_bin, logger_obj=log,
        )
    except (TypstNotInstalledError, ImagerError) as e:
        log.warning(f"typst_to_png 失败，走 PyMuPDF 兜底：{e}")
        # 2) PyMuPDF 兜底
        if pdf_path and os.path.exists(pdf_path):
            pages = pdf_to_png_fallback(pdf_path, job_out, dpi=dpi, logger_obj=log)
        else:
            raise ImagerError(
                f"to_image 失败：typst png 不可用且无 PDF 兜底（job={job_id}）"
            ) from e

    # 3) 多页 → 拼接
    if len(pages) == 1:
        return pages[0]
    long_png = os.path.join(job_out, f"{job_id}_long.png")
    return stitch_vertical(pages, long_png)
