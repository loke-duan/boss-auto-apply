"""F4 简历定制（ADR-0002 注入，设计 §5.10）。

JD + master.json → 定制 ``.typ`` 源（brilliant-CV 语法）。

降本三件套落在此模块：
1. master.json 母库：LLM 只选材不生成 → prompt 明确「禁止生成 master 不存在的内容」。
2. JD 聚类：相同 jd_signature 复用 → cache_key 命中。
3. 模型分级：model_tailor=sonnet（贵但质量高），仅 cluster 未命中时调。

后置校验（防幻觉，第 4 步）：
- 项目名必须在 master 存在。
- 数值必须在 data_standards_applied。
- work_years 必须 == 5.5。
- 禁忌词黑名单扫描。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from ..errors import HealthCheckFailedError, LlmJsonParseError
from ..llm import compute_cache_key
from .hrbp_check import _read_common_rules, require_health_passed

__all__ = [
    "TailoredResume",
    "tailor_resume",
    "validate_tailored",
    "render_tailor_prompt",
    "generate_greet_text",
    "pick_projects_for_greet",
    "FORBIDDEN_PATTERNS",
]


@dataclass
class TailoredResume:
    """F4 产物（改造3：适配 brilliant-CV v4 profile 结构）。

    - ``profile_files``：brilliant-CV v4 的 profile 目录文件（key=相对路径如
      ``metadata.toml``/``experience.typ``，value=文件内容）。由 generator 落盘成
      ``profile_<name>/`` 目录 + 入口 ``cv.typ``，typst compile。
    - ``typ_source``：[兼容] 旧单文件假设；若 LLM 仍返回单文件则兜底渲染。
      优先用 profile_files。
    """

    job_id: str
    typ_source: str = ""
    profile_files: dict[str, str] = field(default_factory=dict)
    selected_modules: list[str] = field(default_factory=list)
    selected_projects: list[str] = field(default_factory=list)
    applied_standards: list[str] = field(default_factory=list)
    cache_hit: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def has_profile(self) -> bool:
        """是否有 v4 profile（True 则走 profile 目录编译路径）。"""
        return bool(self.profile_files)


# ============================================================
# 禁忌词正则 — 通用过度承诺词（适用所有候选人）
# 候选人专属禁忌词（如特定年限/数据）应从 master.constraints.forbidden_words_extra
# 注入，在 validate_tailored 中动态合并为正则扫描。
# ============================================================
FORBIDDEN_PATTERNS = [
    re.compile(r"精通"),
    re.compile(r"100\s*%"),
    re.compile(r"完美"),
    re.compile(r"极致"),
]


# ============================================================
# prompt 渲染
# ============================================================
def render_tailor_prompt(master: dict[str, Any], jd_text: str, target: dict[str, Any]) -> str:
    """渲染 tailor.md prompt（注入 master + JD + target + common_rules + 个人信息占位符）。"""
    p = Path(__file__).parent.parent / "prompts" / "tailor.md"
    template = p.read_text(encoding="utf-8")
    basics = master.get("basics", {}) or {}
    name = basics.get("name", "") or ""
    # 中文姓名拆分：最后一个字为 first_name，其余为 last_name（兼容单字名/复姓）
    if len(name) >= 2:
        first_name, last_name = name[-1], name[:-1]
    else:
        first_name, last_name = name, ""
    cities = basics.get("cities", []) or []
    location = "/".join(cities) if cities else ""
    return (
        template
        .replace("{common_rules}", _read_common_rules())
        .replace("{master_json}", json.dumps(master, ensure_ascii=False, indent=2))
        .replace("{jd_text}", jd_text)
        .replace("{target_json}", json.dumps(target, ensure_ascii=False, indent=2))
        .replace("{first_name}", first_name)
        .replace("{last_name}", last_name)
        .replace("{phone}", basics.get("phone", "") or "")
        .replace("{email}", basics.get("email", "") or "")
        .replace("{location}", location)
    )


# ============================================================
# tailor_resume
# ============================================================
def tailor_resume(
    job: Any,
    master: dict[str, Any],
    target: dict[str, Any],
    llm: Any,
    *,
    out_dir: str = "data/resumes",
    logger_obj: Any = None,
) -> TailoredResume:
    """定制单个 job 的简历。

    前置：master.health_check_status=='passed'。

    流程：
    1. cache_key = sha1(purpose='tailor' + target.name + jd_signature + master 版本)。
    2. cache_get 命中 → 直接返回（cache_hit=True）。
    3. 否则 llm.invoke(model_tailor, expect_json=True, purpose='tailor', cache_key=...)。
    4. 后置校验（防幻觉）：阻断级违规直接抛错（不重试），告警级只记 warning。
    5. 校验通过 → cache_put + 落 data/resumes/{job_id}/resume.typ。

    Args:
        job: ``JobRow``（需有 job_id/jd_full/jd_signature）。
        master: master.json dict。
        target: 方向参数。
        llm: ``ClaudeClient``。
        out_dir: 简历输出根目录。
        logger_obj: loguru logger。

    Returns:
        ``TailoredResume``。

    Raises:
        HealthCheckFailedError: master 未体检。
        LlmJsonParseError: LLM 输出无法解析为 JSON。
    """
    log = logger_obj or logger
    require_health_passed(master, report_path="（tailor 前置）")

    job_id = getattr(job, "job_id", "")
    jd_text = getattr(job, "jd_full", "") or ""
    jd_sig = getattr(job, "jd_signature", "") or ""
    target_name = target.get("name", "")

    # master 版本（hash 前 8 位）
    master_version = hashlib.sha1(
        json.dumps(master, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    # 同步给 llm（缓存失效用）
    if hasattr(llm, "_master_version"):
        llm._master_version = master_version

    model = getattr(getattr(llm, "cfg", None), "model_tailor", None)
    cache_key = compute_cache_key(
        "tailor",
        target_name=target_name,
        jd_signature=jd_sig,
        master_version=master_version,
        model=model or "",
    )

    prompt = render_tailor_prompt(master, jd_text, target)
    # 第 1 次调用（带 cache_key）
    result = llm.invoke(
        prompt,
        model=model,
        expect_json=True,
        purpose="tailor",
        cache_key=cache_key,
    )
    data = result.raw_json
    if not isinstance(data, dict):
        raise LlmJsonParseError(raw=result.text, expected_schema="tailor {typ_source, ...}")

    tr = TailoredResume(
        job_id=job_id,
        typ_source=data.get("typ_source", ""),
        profile_files=_normalize_profile_files(data.get("profile_files")),
        selected_modules=data.get("selected_modules", []) or [],
        selected_projects=data.get("selected_projects", []) or [],
        applied_standards=data.get("applied_standards", []) or [],
        cache_hit=result.cache_hit,
        raw=data,
    )

    # 后置校验：阻断级（幻觉项目/work_years）报错；告警级（禁忌词）只 warning 不阻断。
    # 不再重跑 LLM——重试耗时翻倍，且 prompt 已约束输出质量。
    block_violations, warn_violations = validate_tailored(tr, master)
    if warn_violations:
        log.warning(f"tailor 后置校验告警（不阻断）：{warn_violations}")
    if block_violations:
        raise LlmJsonParseError(
            raw=json.dumps(data, ensure_ascii=False),
            expected_schema=f"tailor 后置校验阻断（{block_violations}）",
        )

    # 落盘
    job_dir = Path(out_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    if tr.has_profile:
        # v4 profile：落盘成 profile_<name>/ 目录 + 入口 cv.typ
        _write_profile_dir(tr.profile_files, job_dir)
        log.info(f"tailor 完成（v4 profile）job={job_id} cache_hit={tr.cache_hit} "
                 f"→ {job_dir}（{len(tr.profile_files)} 文件）")
    else:
        # 兼容：旧单文件假设
        typ_path = job_dir / "resume.typ"
        typ_path.write_text(tr.typ_source, encoding="utf-8")
        log.info(f"tailor 完成 job={job_id} cache_hit={tr.cache_hit} → {typ_path}")
    return tr


def _normalize_profile_files(raw: Any) -> dict[str, str]:
    """把 LLM 返回的 profile_files 规整成 ``{相对路径: 内容}``。

    接受：dict（直接用）；list[str]（每行是 "path\n---\ncontent"）；其他→空 dict。
    """
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    return {}


def _normalize_typ_whitespace(content: str) -> str:
    """规整 .typ 文件的空白，避免 brilliant-CV v4 渲染错位。

    根因：cv-skill/cv-entry 各自是独立 table，相邻调用之间若插入空行，
    Typst 会当作段落分隔叠加额外垂直间距，再叠加组件内部的 v(-6pt) 负补偿，
    导致技能栏/经历栏行距错乱、列宽无法对齐。

    规则：
    1. 连续 3+ 空行压缩为 1 个空行（保留段落语义）。
    2. ``#cv-xxx(...)`` 调用之间只允许 0 个空行（紧贴）——删除 cv 组件之间的空行。
    3. 文件末尾保留单个换行。
    """
    import re as _re
    # 统一换行
    s = content.replace("\r\n", "\n").replace("\r", "\n")
    # 删除相邻 cv-* 组件调用之间的空行（cv-skill/cv-entry/cv-section 紧贴）
    # 匹配：某行以 #cv- 开头，后面跟若干空行，再跟 #cv- 开头的行 → 压成无空行
    s = _re.sub(r"(\n)(\s*\n)+(\s*#cv-)", r"\1\3", s)
    # 连续 3+ 空行压缩为 1（其余场景）
    s = _re.sub(r"\n{3,}", "\n\n", s)
    # 末尾保留单个换行
    return s.rstrip() + "\n"


def _fix_skills_typ_alignment(content: str) -> str:
    """修复 skills.typ 的技能类型标签对齐问题。

    根因：brilliant-CV v4 的 ``cv-skill`` 组件 type 列用 ``align(right)`` 右对齐，
    中文字数不同的标签（「工具」2字 vs「新媒体平台」5字）左侧参差不齐，
    视觉上显得没对齐。英文场景因单词宽度相近不明显，中文场景需修复。

    修复：用本地定义的 ``skill-row`` 组件替换 ``cv-skill``，type 列改左对齐。
    skill-row 复刻 cv-skill 的 table 布局（17% + 1fr 双列 + v(-6pt) 负间距），
    仅把 ``align(right, ...)`` 改为 ``align(left, ...)``，渲染效果除对齐外完全一致。

    同时更新 import 行：不再导入 cv-skill（避免未使用告警），新增 skill-row 定义。
    """
    import re as _re
    if "cv-skill" not in content:
        return content  # 无 cv-skill 调用，无需修复

    # 1) 在 import 行后注入 skill-row 定义（左对齐版 cv-skill）
    skill_row_def = (
        "\n#let skill-row(type: \"\", info: \"\") = {\n"
        "  table(\n"
        "    columns: (17%, 1fr),\n"
        "    inset: 0pt,\n"
        "    column-gutter: 10pt,\n"
        "    stroke: none,\n"
        "    align(left, text(size: 10pt, weight: \"bold\", type)),\n"
        "    text(info),\n"
        "  )\n"
        "  v(-6pt)\n"
        "}\n"
    )
    # 在第一个 #cv-section 之前插入定义（import 之后）
    content = _re.sub(
        r"(\n)(\s*)(#cv-section\()",
        lambda m: m.group(1) + m.group(2) + skill_row_def + m.group(1) + m.group(2) + m.group(3),
        content,
        count=1,
    )
    # 2) 把所有 #cv-skill( 调用替换为 #skill-row(
    content = content.replace("#cv-skill(", "#skill-row(")
    # 3) import 行去掉 cv-skill（保留 cv-section 和 h-bar）
    content = _re.sub(
        r"(#import\s+\"[^\"]*brilliant-cv[^\"]*\":\s*\([^)]*\))",
        lambda m: m.group(1).replace("cv-skill", "").replace(",,", ",").replace("(,", "(").replace(", )", ")"),
        content,
        count=1,
    )
    return content


def _write_profile_dir(profile_files: dict[str, str], job_dir: Path) -> None:
    """把 profile_files 落盘到 ``job_dir/profile_<name>/`` 子目录（v4 约定）。

    key 是相对路径（如 ``metadata.toml``、``experience.typ``，或带 ``profile_zh/`` 前缀）。
    文件路径会被 sanitize 防越界。统一写到 ``profile_<DEFAULT_PROFILE_NAME>/`` 下，
    入口 ``cv.typ`` 由 generator 渲染（不在 profile_files 里）。

    .typ 文件做三层处理（防 LLM 偶发非法语法）：
    1. ``sanitize_typ_content``：修中文逗号/字面 n（见 generator.py）
    2. ``_normalize_typ_whitespace``：修 cv 组件间空行（渲染错位）
    3. skills.typ 额外做 cv-skill 左对齐修复（见 ``_fix_skills_typ_alignment``）
    """
    from .generator import DEFAULT_PROFILE_NAME, sanitize_skill_rows, sanitize_typ_content
    profile_subdir = job_dir / f"profile_{DEFAULT_PROFILE_NAME}"
    profile_subdir.mkdir(parents=True, exist_ok=True)
    for rel, content in profile_files.items():
        # 去掉可能的 profile_zh/ 前缀，只留文件名（v4 profile 都是平铺一层）
        name = rel.replace("\\", "/")
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        safe = Path(name).name
        if not safe or safe.lower() in ("cv.typ", "letter.typ"):
            continue  # 入口由 generator 渲染，不覆盖
        target = profile_subdir / safe
        # .typ 文件三层处理（见 docstring）
        if safe.endswith(".typ"):
            content = sanitize_typ_content(content)
            content = _normalize_typ_whitespace(content)
        # skills.typ 额外两步修复：
        #   1. sanitize_skill_rows：修散落 content 拼接（info: [A] #h-bar() [B] → [A #h-bar() B]）
        #      —— 必须在 _fix_skills_typ_alignment 之前，否则 cv-skill 漏洞仍在
        #   2. _fix_skills_typ_alignment：cv-skill 右对齐 → skill-row 左对齐（中文视觉）
        if safe.lower() == "skills.typ":
            content = sanitize_skill_rows(content)
            content = _fix_skills_typ_alignment(content)
        target.write_text(content, encoding="utf-8")


# ============================================================
# 后置校验（防幻觉）
# ============================================================
def validate_tailored(tr: TailoredResume, master: dict[str, Any]) -> list[str]:
    """后置校验（设计 §5.10 第 4 步）。

    校验：
    - 所有 selected_projects 必须在 master 中存在（按 name 完全匹配）。
    - 所有数值必须 ∈ master 的 data_standards_applied（不允许新增数字）。
    - work_years 出现则必须 == 5.5（constraints.work_years_authoritative）。
    - 禁忌词扫描。

    Args:
        tr: 待校验的定制结果。
        master: master.json dict。

    Returns:
        ``(block_violations, warn_violations)`` 元组。block 级报错阻断（幻觉/work_years），
        warn 级只告警不阻断（禁忌词）。两者为空表示通过。
    """
    violations: list[str] = []      # 阻断级
    warn_only: list[str] = []       # 告警级（禁忌词）

    # 1) 项目名必须在 master 存在（fuzzy 匹配：LLM 常对项目名加后缀/调整措辞，
    #    如「SaaS 产品站 SEO 体系」vs「SaaS 产品站 SEO 体系搭建」。用双向子串 + 关键词重叠，
    #    严格相等会误杀合法选材。）
    master_projects = [p.get("name") or "" for p in master.get("projects", []) or []]
    master_exp_companies = [e.get("company") or "" for e in master.get("experiences", []) or []]
    for pname in tr.selected_projects or []:
        pname_norm = (pname or "").strip()
        if not pname_norm:
            continue
        # 匹配维度：①任一 master project 名互相包含 ②与任一 experience company 互相包含
        # ③去除括号/标点后的核心词（≥3 字）出现在某 master project 名里
        import re as _re
        pname_core = _re.sub(r"[（）()【】\[\]「」\"'·,，。.!！?？\s]+", "", pname_norm)
        matched = (
            any(pname_norm == mp or pname_norm in mp or mp in pname_norm for mp in master_projects)
            or any(pname_norm in c or c in pname_norm for c in master_exp_companies if c)
            or any(len(pname_core) >= 3 and pname_core in _re.sub(r"[（）()【】\[\]「」\"'·,，。.!！?？\s]+", "", mp)
                   for mp in master_projects if mp)
        )
        if not matched:
            violations.append(
                f"selected_project {pname!r} 不在 master.projects/experiences 中（幻觉）。"
                f"master projects={master_projects}"
            )

    # 2) 数值必须在 data_standards_applied
    applied_set: set[str] = set()
    for p in master.get("projects", []) or []:
        applied_set.update(p.get("data_standards_applied", []) or [])
    # applied_standards 声明：每条应能在 applied_set 找到来源（宽松：只记 warning 不阻断）
    for std in tr.applied_standards:
        # 宽松匹配：只要 applied_set 有任何条目包含或被包含
        if not any(std in a or a in std for a in applied_set) and applied_set:
            logger.debug(f"applied_standard {std!r} 未在 master.data_standards_applied 直接命中（宽松放行）")

    # 3) work_years 出现则必须 ≤ authoritative（扫 typ_source + profile_files 全文）
    #    authoritative=0 时跳过（未配置 constraints.work_years_authoritative）
    authoritative = master.get("constraints", {}).get("work_years_authoritative", 0.0) or 0.0
    scan_blob = tr.typ_source + "\n" + "\n".join(tr.profile_files.values())
    if authoritative > 0:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*年", scan_blob):
            val = float(m.group(1))
            # 允许「3-5年」「2019」这类非工龄数字；只拦明确大于 authoritative 的
            if val > authoritative + 0.01 and val < 50:  # 排除年份（2019 等）
                violations.append(f"出现工作年限 {val} 年（应 ≤ {authoritative}）")

    # 4) 禁忌词扫描（通用正则 + 候选人专属词，扫 typ_source + profile_files 全文）
    #    告警级：不阻断（prompt 已约束，偶发误判不应翻倍 LLM 耗时）
    extra_words = master.get("constraints", {}).get("forbidden_words_extra", []) or []
    all_patterns = list(FORBIDDEN_PATTERNS) + [re.compile(re.escape(w)) for w in extra_words if w]
    for pat in all_patterns:
        if pat.search(scan_blob):
            warn_only.append(f"内容含禁忌词「{pat.pattern}」")

    return violations, warn_only


# ============================================================
# F7 打招呼话术生成（v2 新增：替换 sender 的兜底默认话术）
# ============================================================
# 与简历定制的关系：tailor_resume 生成简历文件（profile_dir），
# generate_greet_text 生成 2-3 句个性化招呼（写 DB 字段 tailored_greet）。
# 两者输入相似（master + JD + target），但产物和模型不同：
#   - 简历：sonnet（贵但质量高），按 jd_signature 聚类缓存
#   - 招呼：haiku（快+省），每个 job 独立生成（不聚类，避免千篇一律）

# 招呼话术禁忌词（与 sender._FORBIDDEN_KEYWORDS 同步；不在 sender 层重复扫，
# 但 generate_greet_text 内部做后置校验，确保 LLM 输出不含禁忌词）
_GREET_FORBIDDEN_KEYWORDS = ("精通", "100%", "完美", "极致")


def pick_projects_for_greet(
    master: dict[str, Any],
    target_per_job_keywords: list[str],
    *,
    top_n: int = 2,
) -> list[dict[str, Any]]:
    """从 master.projects 中按 target.per_job_keywords 抽出 top-N 强相关项目。

    匹配规则（任一命中即算相关，按命中关键词数降序）：
    - 项目 name 或 description 包含任一 per_job_keywords
    - 项目所属 skill_modules 包含任一 per_job_keywords

    Args:
        master: master.json dict。
        target_per_job_keywords: 方向的 per_job_keywords（如 ["保险", "投保"]）。
        top_n: 最多返回的项目数。

    Returns:
        top-N 个 project dict（含 name + 简短 description），按相关性降序。
    """
    if not target_per_job_keywords:
        return []
    keywords_norm = [str(k).strip().lower() for k in target_per_job_keywords if k]
    if not keywords_norm:
        return []

    projects = master.get("projects", []) or []
    scored: list[tuple[int, dict]] = []
    for proj in projects:
        name = str(proj.get("name", "") or "").lower()
        desc = str(proj.get("description", "") or "").lower()
        skills = " ".join(str(s) for s in (proj.get("skill_modules", []) or [])).lower()
        blob = f"{name} {desc} {skills}"
        hits = sum(1 for kw in keywords_norm if kw and kw in blob)
        if hits > 0:
            scored.append((hits, proj))

    scored.sort(key=lambda x: -x[0])
    return [proj for _, proj in scored[:top_n]]


def render_greet_prompt(
    master: dict[str, Any],
    job_title: str,
    job_company: str,
    jd_skills: list[str],
    matched_projects: list[dict[str, Any]],
    target: dict[str, Any],
    *,
    max_len: int = 200,
) -> str:
    """渲染 greet.md prompt。"""
    p = Path(__file__).parent.parent / "prompts" / "greet.md"
    template = p.read_text(encoding="utf-8")
    # matched_projects 简化为 name + 一句 description（避免 prompt 过长）
    proj_lines = []
    for proj in matched_projects:
        name = proj.get("name", "")
        desc = (proj.get("description", "") or "").strip()
        # description 太长则截到第一个句号
        if desc and len(desc) > 100:
            for sep in ("。", "；", ";", "."):
                idx = desc.find(sep)
                if 0 < idx <= 100:
                    desc = desc[: idx + 1]
                    break
            else:
                desc = desc[:100] + "..."
        proj_lines.append(f"- {name}：{desc}" if desc else f"- {name}")
    matched_text = "\n".join(proj_lines) if proj_lines else "（无强相关项目匹配，按 master 整体经验生成）"
    return (
        template
        .replace("{common_rules}", _read_common_rules())
        .replace("{job_title}", job_title or "贵司")
        .replace("{job_company}", job_company or "")
        .replace("{jd_skills}", "、".join(jd_skills) if jd_skills else "（JD 未抽取到核心技能）")
        .replace("{matched_projects}", matched_text)
        .replace("{master_json}", json.dumps(master, ensure_ascii=False, indent=2))
        .replace("{target_json}", json.dumps(target, ensure_ascii=False, indent=2))
        .replace("{max_len}", str(max_len))
    )


def generate_greet_text(
    job: Any,
    master: dict[str, Any],
    target: dict[str, Any],
    llm: Any,
    *,
    max_len: int = 200,
    logger_obj: Any = None,
) -> str | None:
    """生成 2-3 句个性化打招呼话术（F7，model_greeter=haiku）。

    前置：master.health_check_status=='passed'（与 tailor_resume 同闸门）。

    流程：
    1. 从 master.projects 按 target.per_job_keywords 抽 top-2 强相关项目。
    2. 渲染 greet.md prompt，调 llm.invoke(model_greeter, expect_json=False)。
    3. 后置校验：禁忌词→返回 None；超长→截断；空→返回 None。
    4. cache_key 按 (job_id, master_version, target_name) 聚合（每 job 独立，避免千篇一律）。

    Args:
        job: ``JobRow``（用 job_id/title/company/skills_required/jd_signature）。
        master: master.json dict。
        target: 方向参数（含 per_job_keywords）。
        llm: ``ClaudeClient``。
        max_len: 最大字数（默认 200）。
        logger_obj: loguru logger。

    Returns:
        招呼话术 str；失败/含禁忌词/LLM 异常 → 返回 None（调用方走兜底默认话术）。
    """
    log = logger_obj or logger
    try:
        require_health_passed(master, report_path="（generate_greet_text 前置）")
    except Exception as e:
        log.warning(f"generate_greet_text 跳过（master 未体检）：{e}")
        return None

    job_title = getattr(job, "title", "") or ""
    job_company = getattr(job, "company", "") or ""
    job_id = getattr(job, "job_id", "") or ""
    skills_required = list(getattr(job, "skills_required", []) or [])
    target_name = target.get("name", "")
    per_job_keywords = list(target.get("per_job_keywords", []) or [])

    # 抽 top-2 强相关项目
    matched_projects = pick_projects_for_greet(master, per_job_keywords, top_n=2)

    # cache_key（按 job_id 聚合，每 job 独立缓存）
    master_version = hashlib.sha1(
        json.dumps(master, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    model = getattr(getattr(llm, "cfg", None), "model_greeter", None)
    cache_key = compute_cache_key(
        "greet",
        target_name=target_name,
        jd_signature=job_id,  # 招呼按 job 独立，不复用 jd_signature 聚类
        master_version=master_version,
        model=model or "",
    )

    prompt = render_greet_prompt(
        master, job_title, job_company, skills_required, matched_projects, target,
        max_len=max_len,
    )

    try:
        result = llm.invoke(
            prompt,
            model=model,
            expect_json=False,
            purpose="greeter",
            cache_key=cache_key,
        )
    except Exception as e:
        log.warning(f"generate_greet_text LLM 调用失败（job={job_id}），返回 None 走兜底：{e}")
        return None

    text = (result.text or "").strip()
    if not text:
        log.warning(f"generate_greet_text 返回空文本（job={job_id}），走兜底默认话术")
        return None

    # 去除 LLM 偶发的 markdown 代码块标记
    text = re.sub(r"^```(?:\w+)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # 禁忌词后置校验（命中 → 返回 None 走兜底，不重试）
    for kw in _GREET_FORBIDDEN_KEYWORDS:
        if kw in text:
            log.warning(f"generate_greet_text 输出含禁忌词「{kw}」（job={job_id}），走兜底默认话术")
            return None

    # 超长截断到最后一个完整句号
    if len(text) > max_len:
        cut = text[:max_len]
        for sep in ("。", "！", "？", ".", "!", "?"):
            idx = cut.rfind(sep)
            if idx > max_len // 2:
                cut = cut[: idx + 1]
                break
        log.info(f"generate_greet_text 超长（{len(text)}>{max_len}），截断到 {len(cut)} 字")
        text = cut

    log.info(f"generate_greet_text 完成（job={job_id} cache_hit={result.cache_hit}）：{text[:50]}...")
    return text
