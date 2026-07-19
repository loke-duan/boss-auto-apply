#!/usr/bin/env python3
"""生成虚构的 sample 简历 PDF（与 master.sample.json 对齐的虚构样例）。

输出：tests/fixtures/resume.sample.pdf
内容：虚构人物「张三」，SaaS/工具类 SEO 增长方向，本科市场营销
      （test_parser.py 断言 `"SEO" in md or "运营" in md`）。
字体：STHeiti（macOS 系统自带，reportlab 可注册 .ttc）。
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

OUTPUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "resume.sample.pdf"
FONT_PATH = "/System/Library/Fonts/STHeiti Medium.ttc"


def _register_font() -> str:
    """注册中文 TrueType 字体，返回 fontname。"""
    pdfmetrics.registerFont(TTFont("STHeiti", FONT_PATH, subfontIndex=0))
    return "STHeiti"


def _style(name: str, size: int, leading: int, space_after: int = 4) -> ParagraphStyle:
    return ParagraphStyle(
        name=name, fontName="STHeiti", fontSize=size, leading=leading,
        spaceAfter=space_after, textColor="#111111",
    )


def build() -> None:
    font = _register_font()
    assert font == "STHeiti"

    doc = SimpleDocTemplate(
        str(OUTPUT), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title="张三 - 简历（虚构样例）", author="张三",
    )
    h1 = _style("h1", 20, 26, space_after=2)
    h2 = _style("h2", 13, 18, space_after=3)
    body = _style("body", 10, 15, space_after=2)
    bullet = _style("bullet", 10, 15, space_after=2)

    story: list = []

    # 头部
    story.append(Paragraph("张三", h1))
    story.append(Paragraph(
        "SEO 增长专家 / SaaS 内容运营　·　"
        "138-0000-0001　·　zhangsan@example.com　·　意向城市：上海、武汉",
        _style("meta", 10, 14, space_after=8),
    ))

    # 个人优势
    story.append(Paragraph("个人优势", h2))
    for line in [
        "· 5.5 年 SaaS/工具类产品 SEO 增长经验：精通技术 SEO + 内容 SEO 双轮驱动。",
        "· 0→1 搭站经验：主导 SaaS 产品官网 SEO 体系从 0 搭建，自然搜索流量稳定增长。",
        "· 数据驱动决策：市场营销科班背景，熟练运用 Ahrefs、Search Console、SQL。",
        "· 跨部门协作力：协同产研/销售搭建线索流转闭环。",
    ]:
        story.append(Paragraph(line, bullet))
    story.append(Spacer(1, 6))

    # 核心技能
    story.append(Paragraph("核心技能 & 工具", h2))
    story.append(Paragraph("· SEO 增长：技术 SEO / 内容 SEO / 长尾词布局", bullet))
    story.append(Paragraph("· 内容运营：技术博客 / 开发者社区 / 选题 SOP", bullet))
    story.append(Paragraph("· 数据分析：漏斗分析 / A/B 测试 / ROI 归因", bullet))
    story.append(Paragraph("· 工具软件：Ahrefs / Semrush / Search Console / SQL", bullet))
    story.append(Spacer(1, 6))

    # 工作经历
    story.append(Paragraph("工作经历", h2))
    story.append(Paragraph("示例 SaaS 公司 · SEO 增长负责人 · 2022.06 – 2025.03", body))
    for line in [
        "· 主导 SaaS 产品官网 SEO 体系从 0 搭建，自然搜索流量实现稳定增长。",
        "· 技术 SEO + 内容 SEO 双轮驱动，核心关键词排名持续上升。",
        "· 搭建搜索流量监控看板，周维度归因迭代内容策略。",
    ]:
        story.append(Paragraph(line, bullet))
    story.append(Spacer(1, 4))

    story.append(Paragraph("示例工具类公司 · 内容运营 · 2020.05 – 2022.05", body))
    for line in [
        "· 搭建技术博客 + 开发者社区内容矩阵，长尾词布局带动自然流量。",
        "· 建立内容选题 SOP，结合搜索意图与产品特性产出高转化内容。",
        "· 协同产研团队产出技术文档，提升产品文档的自然搜索可见度。",
    ]:
        story.append(Paragraph(line, bullet))
    story.append(Spacer(1, 4))

    story.append(Paragraph("示例互联网公司 · 增长运营专员 · 2019.07 – 2020.04", body))
    for line in [
        "· 协助搭建搜索流量监控体系，定位增长机会关键词。",
        "· 执行内容发布 SOP 与 SEO 操作规范，提升内容收录效率。",
    ]:
        story.append(Paragraph(line, bullet))
    story.append(Spacer(1, 6))

    # 项目经历
    story.append(Paragraph("项目经历", h2))
    story.append(Paragraph("SaaS 产品站 SEO 体系搭建", body))
    story.append(Paragraph(
        "· 主导 SaaS 产品官网 SEO 体系从 0 搭建，沉淀可复用的 SaaS SEO 方法论。",
        bullet))
    story.append(Paragraph("开发者社区内容矩阵", body))
    story.append(Paragraph(
        "· 搭建技术博客 + 开发者社区内容矩阵，长尾词布局带动自然流量稳定增长。",
        bullet))
    story.append(Spacer(1, 6))

    # 教育背景
    story.append(Paragraph("教育背景", h2))
    story.append(Paragraph(
        "示例理工大学 · 本科 · 市场营销 · 2015.09 – 2019.06", body))

    doc.build(story)
    print(f"✅ 已生成虚构样例简历：{OUTPUT}（{OUTPUT.stat().st_size} bytes）")


if __name__ == "__main__":
    build()
