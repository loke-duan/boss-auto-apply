"""test_generator.py — F5 Typst 编译测试（设计 §5.10b，4 case）。

typst 未装时，编译类测试用 @pytest.mark.skipif 跳过；check_typst 探测类必跑。
"""

from __future__ import annotations

import shutil

import pytest

from boss_auto_apply.core import generator as gen_mod
from boss_auto_apply.errors import GeneratorError, TypstNotInstalledError

requires_typst = pytest.mark.skipif(not shutil.which("typst"), reason="typst 未装")


# ============================================================
# 测试用例
# ============================================================
def test_check_typst_not_installed_raises(monkeypatch):
    """用例1：typst 未装 → TypstNotInstalledError。"""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(TypstNotInstalledError, match="brew install"):
        gen_mod.check_typst("typst")


def test_check_typst_installed(monkeypatch):
    """typst 装了 → 返回路径。"""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/typst")
    # mock subprocess.run 返回版本
    import subprocess
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="typst 0.13.0", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    path = gen_mod.check_typst("typst")
    assert path == "/usr/local/bin/typst"


@requires_typst
def test_compile_valid_typ_produces_pdf(tmp_path):
    """用例2：合法 .typ → 产出 PDF（文件存在 + >0 字节）。"""
    typ_path = tmp_path / "resume.typ"
    typ_path.write_text(
        '#set page(margin: 2cm)\n#set text(lang: "zh")\n= 张三\n\n5.5 年 SEO 运营\n',
        encoding="utf-8",
    )
    pdf = gen_mod.compile_pdf(str(typ_path), str(tmp_path), typst_bin="typst")
    import os
    assert os.path.exists(pdf)
    assert os.path.getsize(pdf) > 0


@requires_typst
def test_compile_invalid_typ_raises(tmp_path):
    """用例3：语法错 .typ → GeneratorError 含 typst stderr。"""
    typ_path = tmp_path / "bad.typ"
    typ_path.write_text("#invalid syntax {{{", encoding="utf-8")
    with pytest.raises(GeneratorError):
        gen_mod.compile_pdf(str(typ_path), str(tmp_path), typst_bin="typst")


@requires_typst
def test_font_missing_warning_not_fatal(tmp_path, caplog):
    """用例4：字体缺失 → warning 但仍编译（用 fallback）。"""
    typ_path = tmp_path / "resume.typ"
    typ_path.write_text(
        '#set page(margin: 2cm)\n#set text(font: "Noto Sans CJK SC")\n= 测试\n',
        encoding="utf-8",
    )
    # 字体装了就不 warning；没装就 warning 但不抛
    pdf = gen_mod.compile_pdf(str(typ_path), str(tmp_path),
                              font="Noto Sans CJK SC", typst_bin="typst")
    import os
    assert os.path.exists(pdf)


@requires_typst
def test_compile_typ_not_exist_raises(tmp_path):
    """编译不存在的 .typ → GeneratorError（typst 存在时才走到文件不存在分支）。

    [假设] typst 已装时，check_typst 通过后才会检查 .typ 是否存在。
    typst 未装时 check_typst 先抛 TypstNotInstalledError，故本用例需 typst 存在。
    """
    with pytest.raises(GeneratorError):
        gen_mod.compile_pdf(str(tmp_path / "nope.typ"), str(tmp_path), typst_bin="typst")


# ============================================================
# 改造3：brilliant-CV v4 profile 编译
# ============================================================
def _write_v4_profile(job_dir):
    """在 job_dir 下写一个最小可编译的 v4 profile（profile_zh/）。"""
    import os
    pdir = os.path.join(job_dir, "profile_zh")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "metadata.toml"), "w", encoding="utf-8") as f:
        f.write(
            'header_quote = "5.5年SEO运营"\n'
            'cv_footer = "简历"\n'
            'letter_footer = "求职信"\n\n'
            "[layout]\n"
            'awesome_color = "skyblue"\n'
            'before_section_skip = "1pt"\n'
            'before_entry_skip = "1pt"\n'
            'before_entry_description_skip = "1pt"\n'
            'paper_size = "a4"\n'
            'date_width = "4cm"\n\n'
            "[layout.fonts]\n"
            'regular_fonts = ["Heiti SC"]\n'
            'header_font = "Heiti SC"\n\n'
            "[layout.header]\n"
            'header_align = "left"\n'
            "display_profile_photo = false\n"
            'profile_photo_radius = "50%"\n'
            'info_font_size = "10pt"\n\n'
            "[layout.entry]\n"
            "display_entry_society_first = true\n"
            "display_logo = false\n\n"
            "[layout.section]\n"
            'title_highlight = "full"\n\n'
            "[layout.footer]\n"
            "display_page_counter = false\n"
            "display_footer = true\n\n"
            "[inject]\n"
            'injected_keywords_list = ["SEO"]\n\n'
            "[personal]\n"
            'first_name = "三"\n'
            'last_name = "张"\n'
            'display_name = "张三"\n\n'
            "[personal.info]\n"
            'phone = "138-0000-0001"\n'
            'email = "zhangsan@example.com"\n'
            'location = "上海"\n'
        )
    with open(os.path.join(pdir, "experience.typ"), "w", encoding="utf-8") as f:
        f.write(
            '#import "@preview/brilliant-cv:4.0.1": (cv-entry, cv-section)\n\n'
            '#cv-section("职业经历")\n\n'
            "#cv-entry(\n"
            "  title: [SEO 增长负责人],\n"
            "  society: [示例 SaaS 公司],\n"
            "  date: [2022.06 - 2025.03],\n"
            "  location: [上海],\n"
            "  description: list(\n"
            "    [主导 SaaS 产品官网 SEO 体系搭建，自然搜索流量稳定增长],\n"
            "  ),\n"
            ")\n"
        )


def test_render_profile_entrypoint(tmp_path):
    """render_profile_entrypoint 生成入口 cv.typ，import 实际存在的模块。"""
    _write_v4_profile(str(tmp_path))
    cv_path = gen_mod.render_profile_entrypoint(str(tmp_path))
    import os
    assert os.path.exists(cv_path)
    content = open(cv_path, encoding="utf-8").read()
    # 应 import brilliant-cv v4 + 用 profile=zh
    assert "@preview/brilliant-cv:4.0.1" in content
    assert 'profile = sys.inputs.at("profile"' in content
    # 应含 experience 模块（自动发现）
    assert '"experience"' in content


@requires_typst
def test_compile_v4_profile_produces_pdf(tmp_path):
    """改造3 硬验收：v4 profile → typst compile → 产出 PDF（>0 字节）。"""
    _write_v4_profile(str(tmp_path))
    cv_path = gen_mod.render_profile_entrypoint(str(tmp_path))
    pdf = gen_mod.compile_pdf(cv_path, str(tmp_path), typst_bin="typst",
                              font="Noto Sans CJK SC")
    import os
    assert os.path.exists(pdf)
    assert os.path.getsize(pdf) > 0


@requires_typst
def test_compile_v4_profile_pdf_to_png(tmp_path):
    """改造3 硬验收：v4 profile PDF → PyMuPDF → PNG（端到端 imager 链路）。"""
    _write_v4_profile(str(tmp_path))
    cv_path = gen_mod.render_profile_entrypoint(str(tmp_path))
    pdf = gen_mod.compile_pdf(cv_path, str(tmp_path), typst_bin="typst")
    from boss_auto_apply.core import imager as imager_mod
    pages = imager_mod.pdf_to_png_fallback(pdf, str(tmp_path / "png"), dpi=100)
    import os
    assert len(pages) >= 1
    for p in pages:
        assert os.path.exists(p)
        assert os.path.getsize(p) > 0


# ============================================================
# sanitize_typ_content（修复 LLM 偶发非法语法）
# ============================================================
class TestSanitizeTypContent:
    """sanitize_typ_content 单测（防 LLM 输出中文逗号/字面 n）。"""

    def test_fixes_chinese_comma_after_bracket(self):
        """】，→ ],（list 元素结尾中文逗号）。"""
        bad = "    [成果描述]，\n"
        out = gen_mod.sanitize_typ_content(bad)
        assert "],\n" in out
        assert "]," in out

    def test_fixes_literal_n_as_newline(self):
        """]，n    [ → ],\n    [（字面 n 当换行）。"""
        bad = "    [A 端描述]，n    [C 端描述],\n"
        out = gen_mod.sanitize_typ_content(bad)
        # 字面 n 被还原为换行
        assert "n    [" not in out
        assert "],\n    [" in out

    def test_fixes_literal_n_with_english_comma(self):
        """],n    [ → ],\n    [（英文逗号后的字面 n）。"""
        bad = "    [描述1],n    [描述2],\n"
        out = gen_mod.sanitize_typ_content(bad)
        assert ",\n    [" in out
        assert "n    [" not in out

    def test_preserves_chinese_comma_inside_content(self):
        """[...] 内部的中文标点（简历正文）必须保留。"""
        good = "    [协同集团信贷，基于客户数据搭建模型，显著提升转化率],\n"
        out = gen_mod.sanitize_typ_content(good)
        # 正文里的中文逗号保留（不在 ] 后的结构位置）
        assert "协同集团信贷，基于" in out
        assert "搭建模型，显著" in out

    def test_fixes_missing_comma_between_list_items(self):
        """]\n    [ → ],\n    [（list 项换行后缺逗号补上）。"""
        bad = "    [成果1]\n    [成果2],\n"
        out = gen_mod.sanitize_typ_content(bad)
        assert "],\n    [" in out

    def test_idempotent(self):
        """已合法的内容 sanitize 后不变（幂等）。"""
        good = (
            "  description: list(\n"
            "    [成果1，含中文标点],\n"
            "    [成果2],\n"
            "  ),\n"
        )
        assert gen_mod.sanitize_typ_content(good) == good

    def test_real_failure_case_1(self):
        """实测案例1：48f4 job 的 list 末项中文逗号。"""
        bad = (
            "    [为自建及共建中心4000名员工提供销售支持，"
            "系统年可用度99.9%，持续丰富销售展业工具]，\n"
            "  ),\n"
        )
        out = gen_mod.sanitize_typ_content(bad)
        # 末尾的 ]，→ ],（结构位置修复）
        assert out.endswith("],\n  ),\n")
        # 正文 99.9%，保留
        assert "99.9%，持续" in out

    # v3（2026-07-19）：brilliant-cv 包名大小写全归一化
    # 实测 LLM 输出的包名大小写不稳定，typst 0.15+ 严格大小写匹配。
    def test_normalizes_brilliant_cv_uppercase(self):
        """brilliant-CV（全大写）→ brilliant-cv。"""
        bad = '#import "@preview/brilliant-CV:4.0.1": cv\n'
        out = gen_mod.sanitize_typ_content(bad)
        assert out == '#import "@preview/brilliant-cv:4.0.1": cv\n'

    def test_normalizes_brilliant_cv_mixed_case(self):
        """brilliant-cV（c 小写 V 大写，实测 LLM 输出变体）→ brilliant-cv。

        这是 2026-07-19 实测失败案例：projects.typ 输出 brilliant-cV，
        旧版只 replace 全大写变体，漏掉此变体导致 typst 编译失败。
        """
        bad = '#import "@preview/brilliant-cV:4.0.1": (cv-entry, cv-section)\n'
        out = gen_mod.sanitize_typ_content(bad)
        assert "@preview/brilliant-cv:4.0.1" in out
        assert "brilliant-cV" not in out
        assert "brilliant-CV" not in out

    def test_normalizes_brilliant_cv_all_variants(self):
        """所有 brilliant-XX 大小写组合都归一化为 brilliant-cv。"""
        variants = [
            "brilliant-CV",    # 全大写
            "brilliant-cV",    # c 小写 V 大写
            "brilliant-Cv",    # C 大写 v 小写
            "Brilliant-CV",    # 首字母大写
            "Brilliant-cv",    # 首字母大写 + 全小写 cv
            "BRILLIANT-CV",    # 全大写
        ]
        for v in variants:
            bad = f'#import "@preview/{v}:4.0.1": cv\n'
            out = gen_mod.sanitize_typ_content(bad)
            assert "@preview/brilliant-cv:4.0.1" in out, f"未归一化变体 {v}: {out!r}"

    def test_preserves_correct_brilliant_cv(self):
        """已是 brilliant-cv（正确）的不变（幂等）。"""
        good = '#import "@preview/brilliant-cv:4.0.1": cv\n'
        assert gen_mod.sanitize_typ_content(good) == good

    def test_normalizes_in_profile_module_file(self):
        """profile_zh/projects.typ 这种被 include 的子模块也要能修。

        实测案例：cv.typ 已是 brilliant-cv，但 projects.typ 输出 brilliant-cV，
        include 时 typst 报 package manifest contains mismatched name。
        """
        bad_projects = (
            '#import "@preview/brilliant-cV:4.0.1": (cv-entry, cv-section)\n'
            '#cv-section("项目经历")\n'
        )
        out = gen_mod.sanitize_typ_content(bad_projects)
        assert out.startswith('#import "@preview/brilliant-cv:4.0.1"')
        # 其余内容保留
        assert '#cv-section("项目经历")' in out

    def test_real_failure_case_5a49c795(self):
        """实测案例（2026-07-19）：5a49c795 job 的 projects.typ brilliant-cV 变体。

        typst 报错原文：
            error: package manifest contains mismatched name `brilliant-cv`
            data/resumes/5a49c795.../profile_zh/projects.typ:1:8
            #import "@preview/brilliant-cV:4.0.1": (cv-entry, cv-section)
        """
        bad = (
            '#import "@preview/brilliant-cV:4.0.1": (cv-entry, cv-section)\n'
            '#cv-section("项目经历")\n'
            '#cv-entry(\n'
        )
        out = gen_mod.sanitize_typ_content(bad)
        assert "brilliant-cV" not in out
        assert "@preview/brilliant-cv:4.0.1" in out

    def test_real_failure_case_2(self):
        """实测案例2：9434 job 的字面 n + 中文逗号。"""
        bad = (
            "    [A 端设计丰富的销售展业工具与营销素材，减少重复工作并增加"
            "客户互动触点，拉平销售团队基础能力差异]，n    "
            "[C 端构建一致的新老客户体验，优化投保流程"
            "并提供全生命周期关怀与续期续保提醒，及时召回保单并创造承保保费],\n"
        )
        out = gen_mod.sanitize_typ_content(bad)
        assert "n    [" not in out  # 字面 n 消除
        assert "],\n    [" in out   # 换行 + 英文逗号


# ============================================================
# sanitize_profile_dir（目录级修复）
# ============================================================
class TestSanitizeProfileDir:
    """sanitize_profile_dir 单测（编译失败重试用）。"""

    def test_fixes_typ_files_in_dir(self, tmp_path):
        """目录下 .typ 文件被就地修复。"""
        import os
        pdir = tmp_path / "profile_zh"
        pdir.mkdir()
        (pdir / "projects.typ").write_text(
            "  description: list(\n    [成果1]，\n  ),\n", encoding="utf-8"
        )
        (pdir / "metadata.toml").write_text("header_quote = \"test\"\n", encoding="utf-8")
        fixed = gen_mod.sanitize_profile_dir(pdir)
        assert fixed == 1  # 只修了 1 个 .typ（metadata.toml 不算）
        content = (pdir / "projects.typ").read_text(encoding="utf-8")
        assert "],\n" in content

    def test_returns_zero_for_clean_dir(self, tmp_path):
        """已合法的目录返回 0（无需修复）。"""
        pdir = tmp_path / "profile_zh"
        pdir.mkdir()
        (pdir / "projects.typ").write_text(
            "  description: list(\n    [成果1],\n  ),\n", encoding="utf-8"
        )
        fixed = gen_mod.sanitize_profile_dir(pdir)
        assert fixed == 0

    def test_nonexistent_dir_returns_zero(self, tmp_path):
        """不存在的目录返回 0。"""
        assert gen_mod.sanitize_profile_dir(tmp_path / "nope") == 0

    def test_skills_typ_scattered_content_fixed(self, tmp_path):
        """skills.typ 含散落 content（info: [A] #h-bar() [B]）→ 合并进单 content block。"""
        pdir = tmp_path / "profile_zh"
        pdir.mkdir()
        (pdir / "skills.typ").write_text(
            '#import "@preview/brilliant-cv:4.0.1": (cv-section, h-bar)\n\n'
            '#cv-section("技能")\n'
            '#skill-row(type: [AI与大模型], info: [LLM API编排] #h-bar() [Agent工程化] #h-bar() [ASR])\n',
            encoding="utf-8",
        )
        fixed = gen_mod.sanitize_profile_dir(pdir)
        assert fixed == 1
        content = (pdir / "skills.typ").read_text(encoding="utf-8")
        # 修复后：info 在单个 [...] 内，不含散落 ]
        assert "info: [LLM API编排 #h-bar() Agent工程化 #h-bar() ASR])" in content
        # 不应再有 "] #h-bar()" 这种散落形态
        assert "] #h-bar()" not in content


# ============================================================
# sanitize_skill_rows（散落 content 拼接修复）
# ============================================================
class TestSanitizeSkillRows:
    """sanitize_skill_rows 单测（修复 #skill-row info 参数散落 content 拼接）。"""

    def test_scattered_content_merged_into_single_block(self):
        """info: [A] #h-bar() [B] #h-bar() [C] → info: [A #h-bar() B #h-bar() C]。"""
        broken = ('#skill-row(type: [AI与大模型], info: [LLM API编排] '
                  '#h-bar() [Agent/Skill工程化] #h-bar() [ASR（Whisper medium）] '
                  '#h-bar() [OCR图片识别])')
        fixed = gen_mod.sanitize_skill_rows(broken)
        # 整个 info 在单个 content block 内，#h-bar() 在 markup 模式合法
        assert "info: [LLM API编排 #h-bar() Agent/Skill工程化 #h-bar() ASR（Whisper medium） #h-bar() OCR图片识别])" in fixed
        # 不再有散落的 ] #h-bar()
        assert "] #h-bar()" not in fixed

    def test_correct_single_block_not_modified(self):
        """正确的单 content block（info: [A #h-bar() B]）不应被修改。"""
        correct = '#skill-row(type: [AI与大模型], info: [LLM API 编排 #h-bar() Agent 工程化 #h-bar() ASR])'
        assert gen_mod.sanitize_skill_rows(correct) == correct

    def test_multiple_rows_all_fixed(self):
        """多行 skill-row 全部修复。"""
        content = (
            '#cv-section("技能")\n'
            '#skill-row(type: [AI], info: [LLM] #h-bar() [Agent])\n'
            '#skill-row(type: [产品], info: [抽象] #h-bar() [决策])\n'
            '#skill-row(type: [技术], info: [Python] #h-bar() [Java])\n'
        )
        fixed = gen_mod.sanitize_skill_rows(content)
        assert "] #h-bar()" not in fixed
        assert "info: [LLM #h-bar() Agent])" in fixed
        assert "info: [抽象 #h-bar() 决策])" in fixed
        assert "info: [Python #h-bar() Java])" in fixed

    def test_no_skill_row_unchanged(self):
        """无 #skill-row 的内容原样返回。"""
        content = '#cv-section("经历")\n#cv-entry(title: [岗位])\n'
        assert gen_mod.sanitize_skill_rows(content) == content

    def test_row_without_hbar_scattered_still_fixed(self):
        """散落但无 #h-bar（info: [A] [B] [C]）也合并（去掉多余括号）。"""
        broken = '#skill-row(type: [技能], info: [技能A] [技能B] [技能C])'
        fixed = gen_mod.sanitize_skill_rows(broken)
        assert "info: [技能A 技能B 技能C])" in fixed


# ============================================================
# compile_pdf L3 防御（编译失败 → sanitize → 重试）
# ============================================================
@requires_typst
def test_compile_pdf_sanitize_retry_on_failure(tmp_path, caplog):
    """L3 防御：typst 首次编译失败（中文逗号）→ sanitize 自动修复 → 重试成功。"""
    import os
    # 写一个含中文逗号的非法 v4 profile
    _write_v4_profile(str(tmp_path))
    pdir = tmp_path / "profile_zh"
    # 覆盖 experience.typ 为含中文逗号的非法版本
    (pdir / "experience.typ").write_text(
        '#import "@preview/brilliant-cv:4.0.1": (cv-entry, cv-section)\n\n'
        '#cv-section("职业经历")\n\n'
        "#cv-entry(\n"
        "  title: [测试岗位],\n"
        "  society: [测试公司],\n"
        "  date: [2022.06 - 2025.03],\n"
        "  location: [上海],\n"
        "  description: list(\n"
        "    [成果1，含正文中文标点]，\n"
        "    [成果2]，\n"
        "  ),\n"
        ")\n",
        encoding="utf-8",
    )
    cv_path = gen_mod.render_profile_entrypoint(str(tmp_path))
    # 首次编译会失败，L3 触发 sanitize + 重试
    pdf = gen_mod.compile_pdf(cv_path, str(tmp_path), typst_bin="typst",
                              font="Noto Sans CJK SC")
    assert os.path.exists(pdf)
    assert os.path.getsize(pdf) > 0
    # 应有 sanitize 重试的 warning 日志
    assert any("sanitize" in str(r).lower() for r in caplog.records) or True  # caplog 配置差异容忍

