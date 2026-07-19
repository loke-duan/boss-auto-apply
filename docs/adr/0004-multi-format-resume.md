# ADR-0004: 多格式简历解析（PDF/DOCX/MD/TXT）

**日期**：2026-07-13
**状态**：已采纳

## 背景

项目最初只支持 PDF 格式简历输入（`input/resume.pdf`），通过 DoclingParser（主）+ PymupdfRuleParser（兜底）双引擎解析。

用户需求（开源化迭代需求1）要求支持更多格式：
- **PDF**（原有）：格式还原最好，但用户可能没有 PDF 源文件
- **DOCX**（Word）：最常见的简历格式，用户易编辑
- **Markdown**：开发者和技术人员常用
- **TXT**：最通用的纯文本格式

原 `get_parser(prefer)` 只按 `"docling"`/`"pymupdf"` 字符串选解析器，不看文件扩展名。`ResumeParser.parse(pdf_path)` 形参名也硬编码为 `pdf_path`。

## 决策

扩展 `parser.py` 的解析器 Protocol + 工厂模式，新增 2 个解析器 + 按扩展名分发：

### 新增解析器

| 解析器 | 格式 | 实现 | 降级 |
|--------|------|------|------|
| `TextRuleParser` | .md / .txt | 纯 Python，读取文本 + 锚点标题识别 | 无（无外部依赖） |
| `PythonDocxParser` | .docx | python-docx 按段落样式识别标题 | 无（python-docx 是依赖） |

### 扩展 `get_parser` 为按扩展名分发

```python
def get_parser(prefer="auto", *, ext=None, logger_obj=None) -> ResumeParser:
    if ext:
        if ext == ".pdf":  return docling → pymupdf
        if ext == ".docx": return docling → python-docx
        if ext in (".md", ".txt"): return TextRuleParser
        else: raise ParserError("不支持的格式")
    # 无 ext 时按 prefer（向后兼容）
```

### 形参通用化

- `ResumeParser.parse(pdf_path)` → `parse(file_path)`
- `parse_resume_to_markdown(pdf_path, parser)` → `parse_resume_to_markdown(file_path, parser)`
- 错误信息 "PDF 不存在" → "简历文件不存在"

## 测量对比

| 格式 | 解析器 | 依赖 | 标题识别 | 测试覆盖 |
|------|--------|------|----------|----------|
| PDF | DoclingParser | docling（重） | 字号 + 结构 | ✅ 9 case |
| PDF | PymupdfRuleParser | PyMuPDF | 字号 ≥12 或锚点 | ✅ |
| DOCX | DoclingParser | docling | 原生结构 | ✅ ext 分发 |
| DOCX | PythonDocxParser | python-docx | 段落样式名 | ✅ ext 分发 |
| MD | TextRuleParser | 无 | `#`/`##` 已有 | ✅ 3 case |
| TXT | TextRuleParser | 无 | `_SECTION_ANCHORS` 锚点 | ✅ 2 case |

## 后果

**正面**：
- 用户可用 4 种格式提交简历，降低使用门槛
- 开源后适用面更广（不只 PDF 用户）
- 纯文本格式（md/txt）无外部依赖，最低成本可用

**负面**：
- 新增 `python-docx` 依赖（requirements.txt 已加）
- 不同格式解析质量可能有差异（PDF > DOCX > MD > TXT），但 LLM 结构化步骤会补齐

**向后兼容**：
- `get_parser(prefer="docling")` 仍可用（无 ext 时走旧逻辑）
- `parse(file_path)` 形参名变了但位置不变，调用方无需改
