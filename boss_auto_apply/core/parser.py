"""F1 简历解析：Docling 主 + PyMuPDF 兜底 dual-parser（设计 §5.4）。

数据流：
    resume.pdf --Docling/Pymupdf--> raw_markdown
              --LLM(structure prompt)--> master.json(dict)
              --Pydantic 校验--> data/master.json（health_check_status=pending）

dual-parser 决策（§5.4）：
- ``get_parser("docling")`` 先试 ``import docling``；``ImportError`` → 自动切 ``PymupdfRuleParser`` + warning。
- PyMuPDF 兜底规则：``page.get_text("dict")`` 拿字号识别标题，按固定锚点切分。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from ..errors import LlmJsonParseError, ParserError
from ..models import Master

__all__ = [
    "ResumeParser",
    "DoclingParser",
    "PymupdfRuleParser",
    "TextRuleParser",
    "PythonDocxParser",
    "SUPPORTED_EXTENSIONS",
    "get_parser",
    "parse_resume_to_markdown",
    "structure_with_llm",
    "render_parser_prompt",
    "derive_candidate_profile",
    "MASTER_STRUCTURE_SCHEMA",
]


@runtime_checkable
class ResumeParser(Protocol):
    """解析器协议。"""

    def parse(self, file_path: str) -> str:
        """返回 Markdown/JSON 文本（未结构化），交给 LLM 整理成 master.json。"""
        ...


# ============================================================
# 数据诚信规则（ADR-0002，注入 F1 prompt，从源头保证 master.json 诚信）
# ============================================================
# 通用数据诚信规则（ADR-0002），适用于所有求职者。
# 求职者专属的标准表述映射应从 config 读取，不在此硬编码。
DATA_INTEGRITY_RULES = """**数据诚信硬规则（ADR-0002，通用，适用于所有求职者）**

任何量化数据保留必须**同时满足**：①可信（能说清来源）②可验证（背调可佐证）③有基数参照
（单独的百分比/倍数必须配基数或参照系，否则用「绝对值+结果」或「定性描述」替代）。

不满足时的降级方案（按优先级）：
- 用「绝对值+结果」替代「无基数百分比」（如「提升X%」→「提升至日均N+」）
- 用「过程价值」替代「小体量结果」（如「营收破万」→「从0到1变现闭环」）
- 用「数量+体系」替代「反向金额」（如小额赞助 → 「对接N+品牌商户+招商SOP」）
- 用「定性描述」替代「占位符」：简历中的 X、*、□ 等占位符必须替换为定性描述
  （如「转化率至X‰」→「通过模型评分与断点转化策略，显著提升转化率」）

**占位符处理（重要）**：简历中常见 X、*、□ 等隐私占位符遮挡真实数据。
这些占位符**不得原样保留**到 master.json，必须替换为定性描述（说明做了什么、产生了什么价值）。

**禁忌词扫描（通用，非求职者专属）**：
- 「精通」→ 替换为「熟练掌握」或「深度实践」（「精通」会被面试官当作深度追问靶子）
- 其他过度承诺词（如「完美」「极致」「100%」）→ 替换为可验证的表述

**工作年限**：work_years_total 等于简历中声明的、可验证的工作经历区间之和。
若简历声明的年限与实际经历区间不匹配，以可验证经历为准并在 work_years_note 标注差异。"""


# ============================================================
# master 结构化输出的 JSON schema 描述（注入 LLM prompt）
# ============================================================
MASTER_STRUCTURE_SCHEMA = """{
  "schema_version": "1.0",
  "basics": {
    "name": "str",
    "phone": "str", "email": "str", "cities": ["str"],
    "headline": "str",
    "work_years_total": "float (必须等于可验证经历区间之和)",
    "work_years_note": "str",
    "degree": "str", "major": "str", "school": "str", "edu_period": "str",
    "candidate_profile": {
      "target_role_keywords": ["str (3-6 个，BOSS 搜索用目标岗位关键词)"],
      "role_category": "str (单一核心职业类别，如 产品经理/Java 开发/销售总监)",
      "seniority_level": "str (枚举：初级|中级|高级|资深|专家)",
      "work_years_authoritative": "float (必须 == basics.work_years_total)",
      "core_industries": ["str (1-4 个，候选人核心行业经验)"],
      "exclusion_keywords": ["str (明确排除的方向词，如 助理/实习/销售)"]
    }
  },
  "skill_modules": {"<key>": {"summary": "str", "bullet_points": ["str"], "tools": ["str"]}},
  "projects": [{"name": "str", "period": "str", "company": "str", "stack": ["str"],
                "role": "str", "highlights": ["str"], "interview_safe": true,
                "data_standards_applied": ["str"]}],
  "experiences": [{"company": "str", "position": "str", "period": "str", "highlights": ["str"]}],
  "constraints": {"primary_direction": "str (从 config.target_constraints 同步)",
                  "salary_band": {"<城市>": "<薪资带，从 config 同步>"},
                  "degree_filter": "str (从简历 basics.degree 提取)",
                  "work_years_authoritative": "float (从可验证经历计算)",
                  "data_standards_locked": ["str"],
                  "forbidden_words_extra": ["str (候选人专属禁忌词，从 config 注入)"]},
  "health_check_status": "pending"
}

【关键：projects 必须充分拆分】
- 不要把所有项目塞进 experiences.highlights 就完事。
- **每段工作经历中，凡是有可量化成果/独立交付物的子项目，都必须拆成独立 project**，放进 projects 数组。
- 例：某段工作经历中的「核心业务系统搭建」是独立项目（有可量化成果）→ 拆成 project「（业务化命名）」。
- projects.name 用业务化命名（如「XX系统从0到1搭建」「XX平台架构重构」），不要用岗位名。
- projects.period/company 与对应 experience 对齐；highlights 保留该项目的核心成果 bullet。
- experiences 仍保留（公司-岗位-时段），但 highlights 可简化为「见 projects 对应项」引用，避免重复。
- 预期应拆出至少 2-4 个 projects（每段重要工作经历里的核心交付物独立成 project）。

【关键：candidate_profile 必填】
- basics.candidate_profile 是下游 matcher/pipeline 通用化的基座（ADR-0005）。
- 必须严格按 schema 推导 6 个子字段，禁止留空。
- 推导方法论见 prompts/parser.md「候选人画像推导方法论」章节。"""


# ============================================================
# DoclingParser（主，import 失败抛 ParserError）
# ============================================================
class DoclingParser:
    """主解析器。构造时尝试 import docling；失败抛 ``ParserError``。

    生产环境由 ``get_parser`` 捕获并降级。
    """

    name = "docling"

    def __init__(self) -> None:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
        except ImportError as e:  # docling 未装
            raise ParserError(
                f"docling 未安装（{e}）。请用 venv 3.12 装，或降级 PymupdfRuleParser。"
            ) from e
        self._Converter = DocumentConverter  # 延迟到 parse 实例化（重）

    def parse(self, file_path: str) -> str:
        """解析 PDF/DOCX → Docling Markdown（docling 原生支持多格式）。

        Args:
            file_path: 简历文件路径（PDF/DOCX）。

        Returns:
            Markdown 文本。

        Raises:
            ParserError: 文件不存在 / 解析失败。
        """
        if not os.path.exists(file_path):
            raise ParserError(f"简历文件不存在：{file_path}")
        try:
            conv = self._Converter()
            doc = conv.convert(file_path)
            md = doc.document.export_to_markdown()
            return md or ""
        except Exception as e:
            raise ParserError(f"docling 解析失败：{file_path} ({e})") from e


# ============================================================
# PymupdfRuleParser（兜底）
# ============================================================
# 固定锚点：出现这些字样的行视为段落标题
_SECTION_ANCHORS = (
    "个人优势", "核心技能", "技能", "工作经历", "项目经历", "项目经验",
    "教育背景", "自我评价", "联系方式", "工作经历",
)
# 视为大标题的字号阈值（简历通常正文 9-11，标题 ≥12）
_TITLE_SIZE_THRESHOLD = 12.0


@dataclass
class _TextSpan:
    text: str
    size: float


class PymupdfRuleParser:
    """兜底解析器：PyMuPDF 抽文本 + 规则切分 → 半结构化 Markdown。

    规则：
    - ``page.get_text("dict")`` 拿字号。
    - 大字号（≥阈值）或命中 ``_SECTION_ANCHORS`` 的行视为段落标题（加 ``## ``）。
    - 其余作为正文 bullet。
    """

    name = "pymupdf"

    def parse(self, file_path: str) -> str:
        """解析 PDF → 半结构化 Markdown。

        Args:
            file_path: PDF 文件路径。

        Returns:
            Markdown 文本。

        Raises:
            ParserError: 文件不存在 / 加密损坏。
        """
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise ParserError(f"PyMuPDF 未安装：{e}") from e

        if not os.path.exists(file_path):
            raise ParserError(f"简历文件不存在：{file_path}")

        try:
            doc = fitz.open(file_path)
        except Exception as e:
            raise ParserError(f"PDF 无法打开（可能加密/损坏）：{file_path} ({e})") from e

        if doc.needs_pass:
            doc.close()
            raise ParserError(f"PDF 加密：{file_path}")

        lines: list[str] = []
        try:
            for page in doc:
                d = page.get_text("dict")
                for block in d.get("blocks", []):
                    if block.get("type", 0) != 0:  # 0=文本
                        continue
                    for line in block.get("lines", []):
                        spans_data = [
                            _TextSpan(span.get("text", ""), round(span.get("size", 0), 1))
                            for span in line.get("spans", [])
                            if span.get("text", "").strip()
                        ]
                        if not spans_data:
                            continue
                        text = "".join(s.text for s in spans_data).strip()
                        max_size = max(s.size for s in spans_data)
                        if not text:
                            continue
                        # 标题判定：字号大 或 命中锚点
                        is_title = max_size >= _TITLE_SIZE_THRESHOLD or any(
                            a in text for a in _SECTION_ANCHORS
                        )
                        if is_title and len(text) < 30:
                            lines.append(f"## {text}")
                        else:
                            lines.append(text)
            doc.close()
        except Exception as e:
            try:
                doc.close()
            except Exception:
                pass
            raise ParserError(f"PyMuPDF 解析失败：{file_path} ({e})") from e

        return "\n".join(lines)


# ============================================================
# TextRuleParser（纯文本 .md/.txt，无外部依赖）
# ============================================================
class TextRuleParser:
    """纯文本解析器（.md/.txt）：直接读取文本，做轻量标题识别。

    Markdown 文件本身已有 ``#``/``##`` 标题结构，直接返回。
    TXT 文件无结构，用 ``_SECTION_ANCHORS`` 识别标题行并加 ``## `` 前缀。

    无降级需要（纯 Python，无外部依赖）。
    """

    name = "text"

    def parse(self, file_path: str) -> str:
        """读取 .md/.txt → Markdown 文本。

        Args:
            file_path: .md 或 .txt 文件路径。

        Returns:
            Markdown 文本。

        Raises:
            ParserError: 文件不存在 / 编码错误。
        """
        if not os.path.exists(file_path):
            raise ParserError(f"简历文件不存在：{file_path}")
        try:
            # 尝试 UTF-8，失败则尝试 GBK（Windows 中文简历常见）
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            try:
                with open(file_path, encoding="gbk") as f:
                    content = f.read()
            except Exception as e:
                raise ParserError(f"文本文件编码无法识别：{file_path} ({e})") from e
        except Exception as e:
            raise ParserError(f"文本文件读取失败：{file_path} ({e})") from e

        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".md":
            # Markdown 已有结构，直接返回
            return content
        # TXT：为命中锚点的行加 ## 前缀（轻量结构化）
        lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                lines.append("")
                continue
            is_title = any(a in stripped for a in _SECTION_ANCHORS) and len(stripped) < 30
            if is_title:
                lines.append(f"## {stripped}")
            else:
                lines.append(stripped)
        return "\n".join(lines)


# ============================================================
# PythonDocxParser（.docx 兜底，docling 装不上时用 python-docx）
# ============================================================
class PythonDocxParser:
    """DOCX 兜底解析器：用 python-docx 按段落样式识别标题。

    规则：
    - ``paragraph.style.name`` 含 ``Heading``/``Title`` → 视为标题（加 ``## ``）。
    - 其余段落作为正文。
    - 表格内容按行拼接为 bullet。
    """

    name = "python-docx"

    def parse(self, file_path: str) -> str:
        """解析 .docx → 半结构化 Markdown。

        Args:
            file_path: .docx 文件路径。

        Returns:
            Markdown 文本。

        Raises:
            ParserError: 文件不存在 / python-docx 未装 / 解析失败。
        """
        try:
            from docx import Document  # type: ignore[import-not-found]
        except ImportError as e:
            raise ParserError(
                f"python-docx 未安装（pip install python-docx）：{e}"
            ) from e
        if not os.path.exists(file_path):
            raise ParserError(f"简历文件不存在：{file_path}")
        try:
            doc = Document(file_path)
        except Exception as e:
            raise ParserError(f"DOCX 无法打开（可能损坏）：{file_path} ({e})") from e

        lines: list[str] = []
        try:
            for para in doc.paragraphs:
                text = (para.text or "").strip()
                if not text:
                    continue
                style_name = ""
                try:
                    style_name = para.style.name if para.style else ""
                except Exception:
                    pass
                is_title = (
                    ("Heading" in style_name or "Title" in style_name)
                    or any(a in text for a in _SECTION_ANCHORS)
                ) and len(text) < 30
                if is_title:
                    lines.append(f"## {text}")
                else:
                    lines.append(text)
            # 表格内容
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
        except Exception as e:
            raise ParserError(f"python-docx 解析失败：{file_path} ({e})") from e

        return "\n".join(lines)


# ============================================================
# 工厂（按扩展名分发 + prefer 降级）
# ============================================================
# 支持的简历格式
SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".md", ".txt")


def get_parser(
    prefer: str = "auto",
    *,
    ext: str | None = None,
    logger_obj: Any = None,
) -> ResumeParser:
    """工厂：按文件扩展名选择解析器，支持降级。

    分发逻辑（ext 优先级 > prefer）：
    - ``.pdf``：docling（主）→ pymupdf（兜底）
    - ``.docx``：docling（主）→ python-docx（兜底）
    - ``.md``/``.txt``：TextRuleParser（纯文本，无降级需要）
    - 无 ext：按 prefer（``"docling"``/``"pymupdf"``/``"text"``）

    Args:
        prefer: 默认 ``"auto"``（按 ext 分发）；或 ``"docling"``/``"pymupdf"``/``"text"``。
        ext: 文件扩展名（如 ``".pdf"``）。设置时按扩展名分发。
        logger_obj: loguru logger。

    Returns:
        解析器实例。

    Raises:
        ParserError: 不支持的扩展名。
    """
    log = logger_obj or logger

    # 按 ext 分发
    if ext:
        ext_lower = ext.lower()
        if ext_lower == ".pdf":
            return _get_pdf_parser(prefer, log)
        if ext_lower == ".docx":
            return _get_docx_parser(prefer, log)
        if ext_lower in (".md", ".txt"):
            return TextRuleParser()
        raise ParserError(
            f"不支持的简历格式：{ext}（支持 {'/'.join(SUPPORTED_EXTENSIONS)}）"
        )

    # 无 ext，按 prefer
    if prefer in ("auto", "docling"):
        return _get_pdf_parser("docling", log)
    if prefer == "pymupdf":
        return PymupdfRuleParser()
    if prefer == "text":
        return TextRuleParser()
    if prefer == "python-docx":
        return PythonDocxParser()
    raise ValueError(f"未知 parser 类型：{prefer}")


def _get_pdf_parser(prefer: str, log: Any) -> ResumeParser:
    """PDF 解析器：docling 主 → pymupdf 兜底。"""
    if prefer == "pymupdf":
        return PymupdfRuleParser()
    try:
        return DoclingParser()
    except ParserError as e:
        log.warning(f"docling 不可用，降级 PymupdfRuleParser：{e}")
        return PymupdfRuleParser()


def _get_docx_parser(prefer: str, log: Any) -> ResumeParser:
    """DOCX 解析器：docling 主 → python-docx 兜底。"""
    if prefer == "python-docx":
        return PythonDocxParser()
    try:
        return DoclingParser()
    except ParserError as e:
        log.warning(f"docling 不可用，降级 PythonDocxParser：{e}")
        return PythonDocxParser()


def parse_resume_to_markdown(file_path: str, parser: ResumeParser) -> str:
    """用给定 parser 解析简历文件 → Markdown。

    Args:
        file_path: 简历文件路径（PDF/DOCX/MD/TXT）。
        parser: 解析器实例。

    Returns:
        Markdown 文本。

    Raises:
        FileNotFoundError: 文件不存在。
        ParserError: 解析失败。
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"简历文件不存在：{file_path}")
    raw = parser.parse(file_path)
    # 清洗图标/字体残渣：解析时图标字符常变 \x00 等控制字符，
    # 带进 prompt 会让 subprocess（claude CLI）报 ValueError: embedded null byte。
    # 保留 \n\t，去掉其余 C0 控制字符 + Unicode 替换字符。
    import re as _re
    cleaned = _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\uFFFD]", "", raw)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


# ============================================================
# LLM 结构化
# ============================================================
def render_parser_prompt(markdown: str) -> str:
    """渲染 parser.md prompt（注入 DATA_INTEGRITY_RULES + schema + markdown）。

    v3（2026-07-19，ADR-0005）：F1 prompt 从 parser.py 内联抽到 prompts/parser.md，
    与其他 stage（profiler/matcher/hrbp_check）保持一致。
    新增「候选人画像推导方法论」章节，让 LLM 在 F1 一次推导 candidate_profile，
    下游 matcher/pipeline/greet 通用复用。
    """
    p = Path(__file__).parent.parent / "prompts" / "parser.md"
    template = p.read_text(encoding="utf-8")
    return (
        template
        .replace("{data_integrity_rules}", DATA_INTEGRITY_RULES)
        .replace("{master_schema}", MASTER_STRUCTURE_SCHEMA)
        .replace("{markdown}", markdown)
    )


def structure_with_llm(markdown: str, llm: Any, *, logger_obj: Any = None) -> dict[str, Any]:
    """调 LLM 把 Markdown 整理成 master.json dict。

    Prompt 指令：严格基于文本抽取，禁止补充/编造；空字段填 null。
    返回前用 Pydantic 校验；失败抛 ``LlmJsonParseError``。

    Args:
        markdown: 解析后的 Markdown。
        llm: ``ClaudeClient``（或任何有 invoke 方法的对象）。
        logger_obj: loguru logger。

    Returns:
        master.json dict（``health_check_status='pending'``）。

    Raises:
        LlmJsonParseError: LLM 输出无法解析为合法 master。
    """
    log = logger_obj or logger
    prompt = render_parser_prompt(markdown)
    result = llm.invoke(
        prompt,
        expect_json=True,
        json_schema=MASTER_STRUCTURE_SCHEMA,
        purpose="profiler",  # 复用 profiler 模型（强）
        model=getattr(getattr(llm, "cfg", None), "model_profiler", None),
    )
    data = result.raw_json
    if not isinstance(data, dict):
        raise LlmJsonParseError(raw=result.text, expected_schema="master.json object")
    # 强制 pending
    data.setdefault("health_check_status", "pending")
    # 强制 candidate_profile.work_years_authoritative == work_years_total（防 LLM 篡改工龄）
    _sync_work_years_authoritative(data)
    # Pydantic 校验（宽松：extra allow）
    try:
        Master.model_validate(data)
    except Exception as e:
        log.warning(f"master.json Pydantic 校验宽松失败（仍返回 dict）：{e}")
    return data


def _sync_work_years_authoritative(data: dict[str, Any]) -> None:
    """强制 candidate_profile.work_years_authoritative == basics.work_years_total。

    LLM 偶尔会在 candidate_profile 里给不同的工龄值，必须对齐。
    """
    basics = data.get("basics") or {}
    cp = basics.get("candidate_profile")
    if not isinstance(cp, dict):
        return
    wyt = basics.get("work_years_total")
    if isinstance(wyt, (int, float)) and wyt > 0:
        cp["work_years_authoritative"] = float(wyt)


# ============================================================
# F1.55 候选人画像补全（ADR-0005）
# ============================================================
def derive_candidate_profile(
    master: dict[str, Any], llm: Any, *, logger_obj: Any = None,
) -> dict[str, Any]:
    """F1.55：当 master.basics.candidate_profile 缺失或字段不全时，调 LLM 补全。

    用于兼容老 master.json（v3 之前没有 candidate_profile 字段）。
    位于体检之后、profiler 之前。

    补全后写回 master dict（in-place 修改 basics.candidate_profile），
    并强制 ``work_years_authoritative == work_years_total``。

    Args:
        master: master.json dict（会被 in-place 修改）。
        llm: ``ClaudeClient``。
        logger_obj: loguru logger。

    Returns:
        补全后的 master dict（与入参同对象）。
    """
    log = logger_obj or logger
    basics = master.get("basics") or {}
    cp = basics.get("candidate_profile")
    if _is_candidate_profile_complete(cp):
        log.info("candidate_profile 已完整，跳过 F1.55")
        return master

    log.info("=== F1.55 候选人画像补全（candidate_profile 缺失/不全）===")
    # 复用 parser.md 的方法论，输入简历素材（headline/projects/skill_modules）让 LLM 推导
    # 简化输入：只给推导必需的字段，避免 prompt 过长
    summary_for_llm = {
        "basics": {
            "headline": basics.get("headline", ""),
            "work_years_total": basics.get("work_years_total", 0),
            "work_years_note": basics.get("work_years_note", ""),
            "degree": basics.get("degree", ""),
            "major": basics.get("major", ""),
        },
        "skill_modules": master.get("skill_modules", {}),
        "projects": [
            {"name": p.get("name", ""), "role": p.get("role", ""),
             "company": p.get("company", "")}
            for p in master.get("projects", [])
        ],
        "experiences": [
            {"company": e.get("company", ""), "position": e.get("position", "")}
            for e in master.get("experiences", [])
        ],
    }
    prompt = (
        "# 候选人画像推导（ADR-0005）\n"
        "下面的 JSON 是已结构化的简历母库摘要。请推导出 candidate_profile 6 个字段。\n\n"
        "## 推导方法论\n"
        "- **target_role_keywords**：必须是**岗位词**（描述职业身份），不是业务方向词/技能词/跨职能词。\n"
        "  - ✅ 正确：产品经理候选人 → [\"产品经理\",\"PM\",\"Product Manager\",\"高级产品经理\",\"产品负责人\",\"产品总监\"]\n"
        "  - ✅ 正确：Java 候选人 → [\"Java 开发\",\"Java 工程师\",\"后端开发\",\"高级开发工程师\"]\n"
        "  - ❌ 错误：\"金融科技产品经理\" / \"AI 产品经理\" / \"SCRM/CRM 产品\"（这些是业务方向，放 core_industries）\n"
        "  - ❌ 错误：\"项目经理\"（这是另一个职业） / \"产品运营\"（这是运营岗）\n"
        "  - ❌ 错误：\"SQL\" / \"PRD\" / \"保险\"（这些是技能/行业，不是岗位词）\n"
        "  - 来源：headline + projects.role（取通用岗位词，去掉业务修饰）\n"
        "  - 必须 3-6 个，中英文 + 缩写全覆盖\n"
        "- **role_category**：target_role_keywords[0]，必须是单一通用岗位词（不带方向修饰）\n"
        "  - ✅ \"产品经理\" / \"Java 开发\" / \"销售总监\"\n"
        "  - ❌ \"金融科技产品经理\"（带方向修饰）\n"
        "- seniority_level：工龄 < 3 → 初级；3-5 → 中级；5-8 → 高级；8-12 → 资深；≥ 12 → 专家\n"
        "- work_years_authoritative：必须 == basics.work_years_total\n"
        "- core_industries：projects.company 行业 + skill_modules.business_domain，1-4 个\n"
        "- exclusion_keywords：资深/专家必排除「助理/实习/管培生/学徒」；"
        "若 role_category 是管理/产品/技术，再排除「销售/经纪人/文员」（除非本身是销售）\n\n"
        f"## 简历摘要 JSON\n```json\n{json.dumps(summary_for_llm, ensure_ascii=False, indent=2)}\n```\n\n"
        "## 输出格式（严格 JSON，只含 candidate_profile 对象）\n"
        "```json\n{{\"target_role_keywords\":[...],\"role_category\":\"...\","
        "\"seniority_level\":\"...\",\"work_years_authoritative\":0.0,"
        "\"core_industries\":[...],\"exclusion_keywords\":[...]}}\n```\n"
    )
    result = llm.invoke(
        prompt,
        expect_json=True,
        purpose="profiler",
        model=getattr(getattr(llm, "cfg", None), "model_profiler", None),
    )
    cp_data = result.raw_json
    if not isinstance(cp_data, dict):
        log.warning(f"F1.55 LLM 输出非 dict，candidate_profile 补全失败：{result.text[:200]}")
        return master
    # 强制工龄对齐
    wyt = basics.get("work_years_total")
    if isinstance(wyt, (int, float)) and wyt > 0:
        cp_data["work_years_authoritative"] = float(wyt)
    basics["candidate_profile"] = cp_data
    master["basics"] = basics
    log.info(
        f"F1.55 完成：role_category={cp_data.get('role_category')!r} "
        f"seniority={cp_data.get('seniority_level')!r} "
        f"target_keywords={cp_data.get('target_role_keywords')}"
    )
    return master


def _is_candidate_profile_complete(cp: Any) -> bool:
    """candidate_profile 6 个字段是否都非空。"""
    if not isinstance(cp, dict):
        return False
    required_keys = (
        "target_role_keywords", "role_category", "seniority_level",
        "work_years_authoritative", "core_industries",
    )
    for k in required_keys:
        v = cp.get(k)
        if isinstance(v, list):
            if not v:
                return False
        elif isinstance(v, str):
            if not v.strip():
                return False
        elif v in (None, 0, 0.0):
            return False
    return True
