"""test_parser.py — F1 解析 dual-parser 测试（设计 §5.4，7 case）。"""

from __future__ import annotations

import os

import pytest

from boss_auto_apply.core import parser as parser_mod
from boss_auto_apply.errors import LlmJsonParseError, ParserError


# ============================================================
# 测试用例
# ============================================================
def test_pymupdf_parser_contains_keywords(sample_resume_pdf):
    """用例2（先用兜底）：PymupdfParser 解析简历 PDF → markdown 含关键词。"""
    p = parser_mod.PymupdfRuleParser()
    md = p.parse(sample_resume_pdf)
    # fixture resume.sample.pdf 是虚构样例（张三/SEO 运营），验证解析器能提取关键信息
    assert len(md) > 50  # 解析出足够文本
    assert "SEO" in md or "运营" in md


def test_get_parser_docling_fallback_to_pymupdf():
    """用例3：get_parser('docling') 在 docling 不可用时返回 PymupdfParser + warning。"""
    p = parser_mod.get_parser("docling")
    # docling 在 3.13 装不上，应降级
    assert p.name == "pymupdf"


def test_get_parser_pymupdf():
    """get_parser('pymupdf') 直接返回 PymupdfParser。"""
    p = parser_mod.get_parser("pymupdf")
    assert p.name == "pymupdf"


def test_pdf_not_found_raises():
    """用例4：PDF 不存在 → FileNotFoundError。"""
    p = parser_mod.PymupdfRuleParser()
    with pytest.raises(ParserError, match="不存在"):
        p.parse("/tmp/__no_such_file__.pdf")


def test_corrupt_pdf_raises(tmp_path):
    """用例5：损坏 PDF → ParserError。"""
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf %PDF- broken")
    p = parser_mod.PymupdfRuleParser()
    with pytest.raises(ParserError):
        p.parse(str(bad))


def test_structure_with_llm_success(fake_llm, sample_master):
    """用例6：LLM 整理后 master 通过；故意喂坏 JSON → LlmJsonParseError。"""
    fake_llm.default_response = sample_master
    md = "## 个人优势\n张三，SEO 运营"
    result = parser_mod.structure_with_llm(md, fake_llm)
    assert result["basics"]["name"] == "张三"
    assert result["health_check_status"] == "pending"


def test_structure_with_llm_bad_json(fake_llm):
    """LLM 返回非 dict → LlmJsonParseError。"""
    # 让 fake 返回 raw_json=None（text 是乱码）
    fake_llm.default_response = "这不是 JSON"
    # FakeClaudeClient 对 str 返回 raw_json=None
    md = "## 简历"
    with pytest.raises(LlmJsonParseError):
        parser_mod.structure_with_llm(md, fake_llm)


def test_work_years_correct(fake_llm, sample_master):
    """用例7：工作年限字段 == 5.5（体检前置数据正确性）。"""
    fake_llm.default_response = sample_master
    result = parser_mod.structure_with_llm("md", fake_llm)
    assert result["basics"]["work_years_total"] == 5.5


def test_parse_resume_to_markdown_not_found():
    """parse_resume_to_markdown PDF 不存在 → FileNotFoundError。"""
    p = parser_mod.PymupdfRuleParser()
    with pytest.raises(FileNotFoundError):
        parser_mod.parse_resume_to_markdown("/tmp/__nope__.pdf", p)


# ============================================================
# 多格式解析测试（需求1：pdf/docx/md/txt）
# ============================================================
def test_text_parser_md_direct_return(tmp_path):
    """TextRuleParser 解析 .md → 直接返回内容（Markdown 已有结构）。"""
    md_file = tmp_path / "resume.md"
    md_file.write_text("# 张三\n\n## 工作经历\n- 后端工程师 3 年\n", encoding="utf-8")
    p = parser_mod.TextRuleParser()
    result = p.parse(str(md_file))
    assert "# 张三" in result
    assert "## 工作经历" in result
    assert "后端工程师" in result


def test_text_parser_txt_adds_headers(tmp_path):
    """TextRuleParser 解析 .txt → 命中锚点的行加 ## 前缀。"""
    txt_file = tmp_path / "resume.txt"
    txt_file.write_text(
        "张三的简历\n工作经历\n后端工程师 2020-2023\n项目经历\nXX系统重构\n",
        encoding="utf-8",
    )
    p = parser_mod.TextRuleParser()
    result = p.parse(str(txt_file))
    assert "## 工作经历" in result
    assert "## 项目经历" in result
    # 非锚点行不加 ##
    assert "## 张三" not in result


def test_text_parser_gbk_fallback(tmp_path):
    """TextRuleParser GBK 编码文件 → 自动降级读取。"""
    txt_file = tmp_path / "resume_gbk.txt"
    txt_file.write_bytes("张三\n工作经历\n后端工程师\n".encode("gbk"))
    p = parser_mod.TextRuleParser()
    result = p.parse(str(txt_file))
    assert "张三" in result
    assert "## 工作经历" in result


def test_text_parser_not_found():
    """TextRuleParser 文件不存在 → ParserError。"""
    p = parser_mod.TextRuleParser()
    with pytest.raises(ParserError, match="不存在"):
        p.parse("/tmp/__no_such_text__.txt")


def test_get_parser_ext_pdf():
    """get_parser(ext='.pdf') → docling 或 pymupdf。"""
    p = parser_mod.get_parser(ext=".pdf")
    assert p.name in ("docling", "pymupdf")


def test_get_parser_ext_md():
    """get_parser(ext='.md') → TextRuleParser。"""
    p = parser_mod.get_parser(ext=".md")
    assert p.name == "text"


def test_get_parser_ext_txt():
    """get_parser(ext='.txt') → TextRuleParser。"""
    p = parser_mod.get_parser(ext=".txt")
    assert p.name == "text"


def test_get_parser_ext_docx():
    """get_parser(ext='.docx') → docling 或 python-docx。"""
    p = parser_mod.get_parser(ext=".docx")
    assert p.name in ("docling", "python-docx")


def test_get_parser_unsupported_ext():
    """get_parser(ext='.xls') → ParserError（不支持格式）。"""
    with pytest.raises(ParserError, match="不支持"):
        parser_mod.get_parser(ext=".xls")


def test_get_parser_uppercase_ext():
    """get_parser(ext='.PDF') → 大写扩展名也能识别。"""
    p = parser_mod.get_parser(ext=".PDF")
    assert p.name in ("docling", "pymupdf")


def test_parse_resume_to_markdown_md(tmp_path):
    """parse_resume_to_markdown 解析 .md 文件 → 清洗后返回。"""
    md_file = tmp_path / "resume.md"
    md_file.write_text("# 张三\n\n## 技能\nPython, Go\n", encoding="utf-8")
    p = parser_mod.TextRuleParser()
    result = parser_mod.parse_resume_to_markdown(str(md_file), p)
    assert "# 张三" in result
    assert "Python" in result


def test_supported_extensions_constant():
    """SUPPORTED_EXTENSIONS 包含 4 种格式。"""
    assert ".pdf" in parser_mod.SUPPORTED_EXTENSIONS
    assert ".docx" in parser_mod.SUPPORTED_EXTENSIONS
    assert ".md" in parser_mod.SUPPORTED_EXTENSIONS
    assert ".txt" in parser_mod.SUPPORTED_EXTENSIONS
