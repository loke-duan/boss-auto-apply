"""F5 Typst 编译（设计 §5.10b，改造3：适配 brilliant-CV v4 profile 结构）。

改造3 前：单一 ``.typ`` 文件 → typst compile。
改造3 后：brilliant-CV v4 是**破坏性变更**——profile 目录结构
（``profile_<name>/metadata.toml`` + 各内容模块 ``.typ``）+ 入口 ``cv.typ`` 用
``--input profile=<name>`` 切换。本模块为每个 job 生成入口 ``cv.typ`` + 复用 tailor 落盘的
profile 文件，用 ``typst compile cv.typ --input profile=<name> --root <jobdir>`` 编译。

typst 缺失时，``compile_pdf`` 降级调 ``compile_pdf_fallback``（PyMuPDF），保证 dry-run 闭环不破。
真实投递前必须装 typst（``brew install typst``）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from ..errors import GeneratorError, TypstNotInstalledError

__all__ = [
    "check_typst",
    "compile_pdf",
    "compile_pdf_fallback",
    "render_typ",
    "render_profile_entrypoint",
    "probe_font",
    "sanitize_typ_content",
    "sanitize_skill_rows",
    "sanitize_profile_dir",
]

# 默认 profile 名（profile_zh 风格，CJK）。与 metadata.toml 配合。
DEFAULT_PROFILE_NAME = "zh"


# ============================================================
# Typst 源码 sanitize（修复 LLM 偶发输出的非法语法）
# ============================================================
def sanitize_typ_content(content: str) -> str:
    """修复 LLM 生成 .typ 时的偶发非法语法，保证 typst 能编译。

    已知错误模式（实测）：
    1. ``description: list(...)`` 元素间用**中文逗号** `，` 分隔
       （typst 只认英文 `,`）→ ``]，`` → ``],``
    2. 字面 ``n`` 当换行（LLM 把 ``\\n`` 的反斜杠吃掉）
       → ``]，n    [`` → ``],\\n    [``
    3. list 元素间缺逗号 → ``]\\n    [`` → ``],\\n    [``

    只修复**结构位置**（``]`` 后的元素分隔符），不影响 ``[...]`` content block
    内部的中文标点（简历正文里的中文逗号是合法的，typst 在 content mode 接受）。

    Args:
        content: .typ 源码。

    Returns:
        修复后的 .typ 源码。
    """
    import re as _re
    s = content.replace("\r\n", "\n").replace("\r", "\n")
    # v3（2026-07-19）：typst 0.15+ 严格化包名大小写。
    # 实测 LLM 输出的包名大小写不稳定：同一次输出 3 个 .typ 文件可能出现
    # brilliant-CV / brilliant-cV / Brilliant-CV / BRILLIANT-CV 等变体。
    # 旧版 v2 只 .replace("@preview/brilliant-CV:", ...) 字面替换，
    # 漏掉其他变体导致 typst 报 `package manifest contains mismatched name`。
    # v3 改为正则全归一化（大小写不敏感匹配 brilliant-cv 包名）。
    s = _re.sub(
        r"@preview/[bB][rR][iI][lL][lL][iI][aA][nN][tT]-[cC][vV]:",
        "@preview/brilliant-cv:",
        s,
    )
    # 1. ]， → ],（list 元素结尾的中文逗号 → 英文；含可选空白）
    s = _re.sub(r"\]\s*，", "],", s)
    # 2. ]，n    [ → ],\n    [（字面 n 当换行 + 前置中文逗号）
    #    先把 "，n" / ",n" 这种字面 n（紧跟在标点后、非换行）还原为换行
    s = _re.sub(r"([，,])\s*n(\s*\[)", r",\n\2", s)
    # 3. ]\n    [ → ],\n    [（list 项换行后缺逗号补上；仅当上一行以 ] 结尾）
    #    匹配：] 换行 + 缩进 + [（中间无逗号）→ 补英文逗号
    s = _re.sub(r"\](\s*\n\s*)\[", r"],\1[", s)
    return s


def sanitize_skill_rows(content: str) -> str:
    """修复 skills.typ 里 ``#skill-row(...)`` 调用中 info 参数的散落 content 拼接。

    根因：``#skill-row(...)`` 在代码模式调用，参数列表 ``(...)`` 处于代码模式。
    LLM 偶发输出 ``info: [技能A] #h-bar() [技能B] #h-bar() [技能C]`` ——
    ``]`` 提前闭合 content block，后续 ``#h-bar() [X]`` 散落在参数列表里，
    typst 报 ``the character `#` is not valid in code``（代码模式不接受 ``#`` 入口）。

    代码模式 content 拼接必须用 ``+`` 或全部塞进单个 ``[...]``（markup 模式下 ``#func()`` 合法）。
    本函数采用后者（合并进单个 content block），与正常输出的 ``info: [A #h-bar() B]`` 形态一致。

    判断是否需要修：info 值若出现 ``]``（content block 提前闭合）才修；
    正确形态 ``info: [A #h-bar() B]``（单个 ``[...]`` 不含 ``]``）原样返回。

    Args:
        content: skills.typ 源码。

    Returns:
        修复后的源码（未变化则原样返回）。
    """
    import re as _re

    def _fix_row(m: "_re.Match[str]") -> str:
        prefix = m.group("prefix")      # '#skill-row(' 到 'info:' 的部分
        info_raw = m.group("info")      # info 参数值（到行尾 ')' 前）
        suffix = m.group("suffix")      # ')'
        # 只修含 ']' 的散落形态（正确形态的 info 不含 ']'）
        if "]" not in info_raw:
            return m.group(0)
        # 合并：去掉所有 '[' ']'，保留 '#h-bar()'，多余空白归一为单空格
        cleaned = info_raw.replace("[", " ").replace("]", " ")
        cleaned = _re.sub(r"\s+", " ", cleaned).strip()
        return f"{prefix}[{cleaned}]{suffix}"

    # 匹配单行 #skill-row(...)：prefix 捕获到 'info:'，info 捕获到行尾 ')' 前
    pattern = _re.compile(
        r"(?P<prefix>#skill-row\([^)]*?\binfo:\s*)"   # #skill-row( ... info:
        r"(?P<info>.*?)"                                # info 值（非贪婪，到行尾 ')'）
        r"(?P<suffix>\)\s*)$",                          # 行尾的 ')'
        _re.MULTILINE,
    )
    return pattern.sub(_fix_row, content)


def sanitize_profile_dir(profile_dir: str | Path) -> int:
    """对 profile 目录下所有 .typ 文件就地 sanitize（编译失败重试用）。

    对 skills.typ 额外调 ``sanitize_skill_rows``（修散落 content 拼接）。

    Args:
        profile_dir: ``profile_<name>/`` 目录路径。

    Returns:
        被修复的文件数。
    """
    pdir = Path(profile_dir)
    if not pdir.is_dir():
        return 0
    fixed = 0
    for typ_file in pdir.glob("*.typ"):
        raw = typ_file.read_text(encoding="utf-8")
        sanitized = sanitize_typ_content(raw)
        # skills.typ 额外修散落 content 拼接（#skill-row 的 info 参数）
        if typ_file.name.lower() == "skills.typ":
            sanitized = sanitize_skill_rows(sanitized)
        if sanitized != raw:
            typ_file.write_text(sanitized, encoding="utf-8")
            fixed += 1
    return fixed

# 入口 cv.typ 模板（不引用 assets，避免图片依赖；profile 由 --input 切换）。
# 只 import 实际生成的模块；缺的模块自动跳过。
_CV_ENTRYPOINT_TEMPLATE = """\
// Generated by boss-auto-apply (brilliant-CV v4 entrypoint, no profile photo)
#import "@preview/brilliant-cv:4.0.1": cv

#let profile = sys.inputs.at("profile", default: "{profile}")
#let metadata = toml("profile_" + profile + "/metadata.toml")

#let import-modules(modules) = {{
  for module in modules {{
    include "profile_" + profile + "/" + module + ".typ"
  }}
}}

#show: cv.with(metadata,)

#import-modules((
{modules}
))
"""


def render_profile_entrypoint(
    job_dir: str | Path,
    *,
    profile_name: str = DEFAULT_PROFILE_NAME,
    modules: list[str] | None = None,
) -> str:
    """在 job 目录生成入口 ``cv.typ``（适配 brilliant-CV v4）。

    只 import 实际存在于 ``profile_<name>/`` 的 ``.typ`` 模块（避免缺文件编译错）。

    Args:
        job_dir: job 输出目录（含 ``profile_<name>/`` 子目录）。
        profile_name: profile 名（默认 zh）。
        modules: 显式模块列表；None 则扫 ``profile_<name>/*.typ`` 自动发现。

    Returns:
        生成的 ``cv.typ`` 绝对路径。
    """
    job_dir = Path(job_dir)
    profile_dir = job_dir / f"profile_{profile_name}"
    if modules is None:
        # 自动发现：profile_dir 下除 cv.typ 外的 .typ 文件
        if profile_dir.is_dir():
            discovered = sorted(
                p.stem for p in profile_dir.glob("*.typ")
                if p.stem.lower() not in ("cv", "letter")
            )
        else:
            discovered = []
        modules = discovered or ["experience", "projects", "skills"]
    # 按约定顺序排序（experience/projects/skills 优先，其余按字典序）
    order_pref = ["experience", "professional", "projects", "education",
                  "certificates", "publications", "skills"]
    modules_sorted = sorted(modules, key=lambda m: (
        order_pref.index(m) if m in order_pref else len(order_pref), m
    ))
    modules_block = "\n".join(f'  "{m}",' for m in modules_sorted)
    content = _CV_ENTRYPOINT_TEMPLATE.format(profile=profile_name, modules=modules_block)
    cv_path = job_dir / "cv.typ"
    cv_path.write_text(content, encoding="utf-8")
    return str(cv_path)


def check_typst(typst_bin: str = "typst") -> str:
    """shutil.which + typst --version；缺失抛 ``TypstNotInstalledError``。

    Args:
        typst_bin: typst 可执行文件名或绝对路径。

    Returns:
        typst 可执行文件绝对路径。

    Raises:
        TypstNotInstalledError: typst 未找到。
    """
    path = shutil.which(typst_bin)
    if not path:
        raise TypstNotInstalledError(
            f"typst 未找到（{typst_bin}）。请装：brew install typst"
        )
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode != 0:
            raise TypstNotInstalledError(f"typst --version 失败：{proc.stderr}")
    except subprocess.TimeoutExpired as e:
        raise TypstNotInstalledError(f"typst --version 超时：{e}") from e
    return path


def probe_font(typst_bin: str, font_name: str = "Noto Sans CJK SC") -> bool:
    """探测 typst 能否找到指定字体。

    Args:
        typst_bin: typst 路径。
        font_name: 字体族名。

    Returns:
        True 表示找到。
    """
    path = shutil.which(typst_bin) or typst_bin
    if not os.path.exists(path):
        return False
    try:
        proc = subprocess.run(
            [path, "fonts"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode != 0:
            return False
        return font_name.lower() in (proc.stdout or "").lower()
    except subprocess.TimeoutExpired:
        return False


def compile_pdf(
    typ_path: str,
    out_dir: str,
    *,
    font: str = "Noto Sans CJK SC",
    template_dir: str = "templates/brilliant-cv",
    typst_bin: str = "typst",
    logger_obj: Any = None,
    profile_name: str = DEFAULT_PROFILE_NAME,
) -> str:
    """subprocess: ``typst compile ... <typ> <out>.pdf``。

    改造3：支持两种模式（自动探测）：
    1. **v4 profile 模式**：``typ_path`` 所在目录有 ``profile_<name>/`` 子目录
       （含 metadata.toml + 内容 .typ）+ 入口 ``cv.typ`` → 用
       ``typst compile cv.typ --input profile=<name> --root <jobdir>``。
    2. **旧单文件模式**：``typ_path`` 是独立 .typ → ``typst compile <typ> <pdf>``。

    Args:
        typ_path: 入口 .typ 路径（v4 模式是 ``<jobdir>/cv.typ``；单文件是 .typ 本身）。
        out_dir: 输出目录。
        font: 期望字体族名（探测用）。
        template_dir: 模板目录（含 fonts/）。
        typst_bin: typst 可执行文件。
        profile_name: v4 profile 名（默认 zh）。
        logger_obj: loguru logger。

    Returns:
        PDF 绝对路径。

    Raises:
        TypstNotInstalledError: typst 未装。
        GeneratorError: 编译失败（typst stderr）。
    """
    log = logger_obj or logger
    # typst 探测：缺失则降级 PyMuPDF 兜底（dry-run 闭环不破）
    typst_available = shutil.which(typst_bin) is not None
    if not typst_available:
        log.warning(
            f"typst 未装（{typst_bin}），compile_pdf 降级 PyMuPDF 兜底。"
            "真实投递前请 brew install typst 获得专业排版。"
        )
        return compile_pdf_fallback(typ_path, out_dir, logger_obj=log)
    typst = check_typst(typst_bin)

    if not os.path.exists(typ_path):
        raise GeneratorError(f".typ 文件不存在：{typ_path}")

    # 字体探测（非致命 warning）
    fonts_dir = os.path.join(template_dir, "fonts")
    font_path_args: list[str] = []
    if os.path.isdir(fonts_dir):
        font_path_args = ["--font-path", fonts_dir]
    if not probe_font(typst_bin, font):
        log.warning(f"typst 未找到字体 {font}，将用 fallback 字体（排版可能偏）")

    out_dir_abs = os.path.abspath(out_dir)
    os.makedirs(out_dir_abs, exist_ok=True)
    base = Path(typ_path).stem
    pdf_path = os.path.join(out_dir_abs, f"{base}.pdf")

    # 探测 v4 profile 模式：typ_path 同级有 profile_<name>/ 子目录
    typ_dir = os.path.dirname(os.path.abspath(typ_path))
    profile_dir = os.path.join(typ_dir, f"profile_{profile_name}")
    is_v4 = os.path.isdir(profile_dir) and base.lower() == "cv"

    if is_v4:
        # v4 profile：--input profile=<name> --root <jobdir>
        cmd = [typst, "compile"] + font_path_args + [
            "--input", f"profile={profile_name}",
            "--root", typ_dir,
            typ_path, pdf_path,
        ]
    else:
        # 旧单文件
        cmd = [typst, "compile"] + font_path_args + [typ_path, pdf_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise GeneratorError(f"typst 编译超时（120s）：{typ_path}") from e
    if proc.returncode != 0:
        # L3 防御：编译失败时尝试 sanitize 所有 .typ 后重试一次
        # （修复 LLM 偶发的中文逗号/字面 n 等历史已落盘的非法语法）
        if is_v4:
            fixed = sanitize_profile_dir(profile_dir)
            if fixed > 0:
                log.warning(
                    f"typst 首次编译失败，sanitize 修复 {fixed} 个 .typ 文件后重试：{typ_path}"
                )
                try:
                    proc2 = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                except subprocess.TimeoutExpired as e:
                    raise GeneratorError(f"typst 编译超时（120s 重试）：{typ_path}") from e
                if proc2.returncode == 0:
                    proc = proc2  # 重试成功，用重试结果继续
        if proc.returncode != 0:
            raise GeneratorError(
                f"typst 编译失败：{typ_path}\n--- typst stderr ---\n{proc.stderr[:2000]}"
            )
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
        raise GeneratorError(f"typst 编译未产出有效 PDF：{pdf_path}")
    log.info(f"generator 编译成功（{'v4 profile' if is_v4 else '单文件'}）："
             f"{pdf_path} ({os.path.getsize(pdf_path)} bytes)")
    return pdf_path


def compile_pdf_fallback(
    typ_path: str,
    out_dir: str,
    *,
    logger_obj: Any = None,
) -> str:
    """typst 缺失时的 PyMuPDF 兜底：把 .typ 源文本按行渲染成极简 PDF。

    [假设] 此兜底仅用于 dry-run 闭环演示（让 job 能到 image_ready）。
    真实投递必须装 typst 获得专业排版。PyMuPDF 的 Story/文本流处理 CJK。
    产出的 PDF 内容是 .typ 源码的可读化版本（去 Typst 语法标记），非排版稿。

    Args:
        typ_path: .typ 源文件路径（内容被读取并简化渲染）。
        out_dir: 输出目录。
        logger_obj: loguru logger。

    Returns:
        PDF 绝对路径。

    Raises:
        GeneratorError: PyMuPDF 未装 / .typ 不存在 / 渲染失败。
    """
    log = logger_obj or logger
    if not os.path.exists(typ_path):
        raise GeneratorError(f"fallback：.typ 文件不存在：{typ_path}")
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise GeneratorError(f"fallback：PyMuPDF 未安装：{e}") from e

    raw = Path(typ_path).read_text(encoding="utf-8")
    # 简化：去掉 Typst 函数/标记行，留可读文本行
    readable_lines: list[str] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#") or s.startswith("//"):
            # 保留标题语义（#set/#import 等配置行整体跳过；#set text 等也跳过）
            if s.startswith("#set") or s.startswith("#import") or s.startswith("#show"):
                continue
            # 行内 #set 等
            if "#set" in s:
                continue
        readable_lines.append(ln)
    text = "\n".join(readable_lines) if readable_lines else raw

    out_dir_abs = os.path.abspath(out_dir)
    os.makedirs(out_dir_abs, exist_ok=True)
    base = Path(typ_path).stem
    pdf_path = os.path.join(out_dir_abs, f"{base}.pdf")

    try:
        doc = fitz.open()
        page_w, page_h = 595, 842  # A4
        margin = 56
        font_size = 11
        line_h = font_size + 5
        page = doc.new_page(width=page_w, height=page_h)
        y = margin
        max_y = page_h - margin
        for src_line in text.splitlines():
            # 行内剩余文本换行处理（按可用宽度粗略切分）
            avail = page_w - 2 * margin
            # 粗略按字符数切（CJK 每字宽≈font_size）
            max_chars = max(1, int(avail / (font_size * 0.95)))
            chunks = [src_line[i:i + max_chars] for i in range(0, len(src_line), max_chars)] or [""]
            for chunk in chunks:
                if y + line_h > max_y:
                    page = doc.new_page(width=page_w, height=page_h)
                    y = margin
                # 插入文本（PyMuPDF 自带 CJK 子集字体 fallback）
                try:
                    page.insert_text(
                        (margin, y), chunk,
                        fontname="china-s",  # PyMuPDF 内置 CJK 简体
                        fontsize=font_size,
                    )
                except Exception:
                    # china-s 不可用则退 helv（拉丁），不抛
                    page.insert_text((margin, y), chunk, fontname="helv", fontsize=font_size)
                y += line_h
            # 段落间额外空行
            y += 2
        doc.save(pdf_path)
        doc.close()
    except Exception as e:
        raise GeneratorError(f"fallback：PyMuPDF 渲染失败：{e}") from e

    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
        raise GeneratorError(f"fallback：未产出有效 PDF：{pdf_path}")
    log.warning(
        f"compile_pdf_fallback 产出极简 PDF（typst 未装）：{pdf_path} "
        f"({os.path.getsize(pdf_path)} bytes) —— 真实投递前请装 typst"
    )
    return pdf_path


def render_typ(
    target: dict[str, Any],
    tailored: Any,
    master: dict[str, Any],
    out_path: str,
) -> None:
    """模板变量替换兜底（改造3：优先 v4 profile，其次 typ_source，最后骨架）。

    Args:
        target: 方向参数。
        tailored: ``TailoredResume``（取 profile_files / typ_source）。
        master: master.json dict。
        out_path: 输出 .typ 路径（v4 模式是 ``<jobdir>/cv.typ``；单文件是 resume.typ）。
    """
    out_path_p = Path(out_path)
    out_path_p.parent.mkdir(parents=True, exist_ok=True)

    # 1) v4 profile 模式：tailored.profile_files 已落盘（tailor.py 做），这里只渲染入口 cv.typ
    profile_files = getattr(tailored, "profile_files", None) or {}
    if profile_files:
        job_dir = out_path_p.parent
        # 入口 cv.typ 名固定为 cv.typ（v4 约定）；out_path 可能是 resume.typ，重定向到 cv.typ
        cv_path = job_dir / "cv.typ"
        render_profile_entrypoint(job_dir)
        # 若调用方要的 out_path 不是 cv.typ，也写一份（兼容旧调用）
        if out_path_p.name != "cv.typ":
            out_path_p.write_text(cv_path.read_text(encoding="utf-8"), encoding="utf-8")
        return

    # 2) 若已有 typ_source 直接落盘
    typ_src = getattr(tailored, "typ_source", "") or ""
    if typ_src:
        out_path_p.write_text(typ_src, encoding="utf-8")
        return

    # 3) 兜底：最小骨架（不依赖 brilliant-CV import，保证能编译）
    basics = master.get("basics", {}) or {}
    name = basics.get("name", "候选人")
    headline = basics.get("headline", "")
    projects = tailored.selected_projects or []
    skeleton = f"""#set page(margin: (x: 1.5cm, y: 1.5cm))
#set text(font: ("Noto Sans CJK SC", "Noto Sans CJK SC"), lang: "zh")

= {name}

*{headline}*

== 求职方向
{target.get('name', '')}

== 项目经历
"""
    for p in projects:
        skeleton += f"\n* {p}\n"
    out_path_p.write_text(skeleton, encoding="utf-8")
