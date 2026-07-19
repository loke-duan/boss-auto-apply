"""Pydantic 模型：master.json / job 行 / target（设计 §4.2、§4.1、§5.7）。

这些模型是「数据契约」的载体：

- ``Master`` / ``Basics`` / ``SkillModule`` / ``Project`` / ``Experience``：
  履历母库，F4 定制只能从中选材。
- ``JobRow``：jobs 表一行的强类型视图，db.py CRUD 与状态机都用它。
- ``RecommendedTarget``：F1.5 推荐方向（profiler 产物，确认后写 targets.confirmed.yaml）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    # master.json
    "Basics",
    "CandidateProfile",
    "SkillModule",
    "Project",
    "Experience",
    "Constraints",
    "Master",
    # jobs 表
    "JobRow",
    # target
    "RecommendedTarget",
    "HealthStatus",
    # enums / 共享类型
    "SeniorityLevel",
]

HealthStatus = Literal["pending", "passed", "failed"]

# 资历级别枚举（F1 candidate_profile + F3 matcher level_match 共用）
# ADR-0005：通用化画像字段，取代"产品经理/保险"硬编码。
SeniorityLevel = Literal["初级", "中级", "高级", "资深", "专家"]


# ============================================================
# master.json 模型
# ============================================================
class CandidateProfile(BaseModel):
    """候选人画像（ADR-0005，master.basics.candidate_profile）。

    通用化基座：从简历一次推导，下游多处复用（matcher/pipeline/greet）。
    取代旧的"产品经理白名单/黑名单"硬编码——不同候选人（产品经理/Java/销售）画像不同，
    matcher 据此动态判断"是否值得投"。

    字段语义：
    - target_role_keywords：BOSS 搜索用的目标岗位关键词（如 ["产品经理","PM","Product Manager"]）
    - role_category：核心职业类别（如 "产品经理"/"Java 开发"/"销售总监"）
    - seniority_level：资历级别（5 档枚举）
    - work_years_authoritative：可验证工龄（必须 == basics.work_years_total，防 LLM 篡改）
    - core_industries：核心行业经验（如 ["保险","金融科技"]），用于 business_domain_match 判断
    - exclusion_keywords：明确排除的方向词（如 ["助理","实习","销售"]），由 seniority 推导
    """

    model_config = ConfigDict(extra="allow")

    target_role_keywords: list[str] = Field(default_factory=list,
        description="BOSS 搜索用的目标岗位关键词 3-6 个")
    role_category: str = ""
    seniority_level: SeniorityLevel = "中级"
    work_years_authoritative: float = 0.0
    core_industries: list[str] = Field(default_factory=list,
        description="候选人核心行业经验 1-4 个")
    exclusion_keywords: list[str] = Field(default_factory=list,
        description="明确排除的方向词（如 助理/实习/销售）")


class Basics(BaseModel):
    """求职者基本信息。"""

    model_config = ConfigDict(extra="allow")

    name: str
    phone: str = ""
    email: str = ""
    cities: list[str] = Field(default_factory=list)
    headline: str = ""
    work_years_total: float
    work_years_note: str = ""
    degree: str = ""
    major: str = ""
    school: str = ""
    edu_period: str = ""
    # ADR-0005：候选人画像（F1 推导，老 master.json 无此字段时由 F1.55 补全）
    candidate_profile: CandidateProfile | None = None


class SkillModule(BaseModel):
    """技能模块（master.skill_modules.{key}）。"""

    model_config = ConfigDict(extra="allow")

    summary: str = ""
    bullet_points: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class Project(BaseModel):
    """项目经历（master.projects[]）。"""

    model_config = ConfigDict(extra="allow")

    name: str
    period: str = ""
    company: str = ""
    stack: list[str] = Field(default_factory=list)
    role: str = ""
    highlights: list[str] = Field(default_factory=list)
    interview_safe: bool = True
    data_standards_applied: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    """工作经历（master.experiences[]）。"""

    model_config = ConfigDict(extra="allow")

    company: str
    position: str = ""
    period: str = ""
    highlights: list[str] = Field(default_factory=list)
    interview_safe: bool = True
    freelance_strategy: str = ""


class Constraints(BaseModel):
    """硬约束（master.constraints）。

    所有字段默认空/0——候选人专属值应从 config.target_constraints 注入或
    由 LLM 在解析时从简历提取，不在代码中硬编码。
    """

    model_config = ConfigDict(extra="allow")

    primary_direction: str = ""
    salary_band: dict[str, str] = Field(default_factory=dict)
    degree_filter: str = ""
    work_years_authoritative: float = 0.0
    data_standards_locked: list[str] = Field(default_factory=list)
    forbidden_words_extra: list[str] = Field(default_factory=list)


class Master(BaseModel):
    """履历母库 master.json（设计 §4.2）。"""

    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    basics: Basics
    skill_modules: dict[str, SkillModule] = Field(default_factory=dict)
    projects: list[Project] = Field(default_factory=list)
    experiences: list[Experience] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    health_check_status: HealthStatus = "pending"

    @field_validator("health_check_status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in ("pending", "passed", "failed"):
            raise ValueError(f"health_check_status 必须是 pending/passed/failed，得到 {v!r}")
        return v

    # ---- 便捷方法 ----
    def is_health_passed(self) -> bool:
        """体检是否通过（F1.5/F4 闸门）。"""
        return self.health_check_status == "passed"

    def project_names(self) -> set[str]:
        """所有项目名集合（F4 后置校验用）。"""
        return {p.name for p in self.projects}

    def all_data_standards_applied(self) -> set[str]:
        """所有项目已应用的数据表述标准集合（F4 后置校验用）。"""
        out: set[str] = set()
        for p in self.projects:
            out.update(p.data_standards_applied)
        return out


# ============================================================
# jobs 表行模型（db.py 用）
# ============================================================
# 状态枚举（设计 §6.1 + M3 §11.1：新增 text_sent 中间态）
JOB_STATUS = (
    "found",
    "jd_analyzed",
    "resume_tailored",
    "pdf_generated",
    "image_ready",
    "greeted",
    "text_sent",      # M3 新增：话术发送成功（聊天框确认）
    "image_sent",
    "skipped",
    "failed",
)
ERROR_CATEGORIES = ("network", "business", "risk", "llm")


class JobRow(BaseModel):
    """jobs 表一行的强类型视图。

    与 db.py 的 schema 一一对应；``skills_required`` 在 DB 里存 JSON 串，
    这里是 list[str]（db.py 负责序列化/反序列化）。
    """

    model_config = ConfigDict(extra="allow")

    job_id: str
    target_name: str
    city: str
    keyword: str | None = None
    title: str | None = None
    company: str | None = None
    salary: str | None = None
    experience: str | None = None
    degree: str | None = None
    jd_full: str | None = None
    skills_required: list[str] = Field(default_factory=list)
    match_score: float = 0.0
    match_reason: str | None = None
    jd_signature: str | None = None
    # v3（ADR-0005）matcher 通用化字段
    level_match: str | None = None  # overqualified|match|underqualified|unknown
    job_category: str | None = None
    category_passed: bool | None = None
    business_domain_match_score: float | None = None
    status: str = "found"
    tailored_resume_path: str | None = None
    resume_pdf_path: str | None = None
    resume_image_path: str | None = None
    tailored_greet: str | None = None
    greet_sent_at: str | None = None
    text_sent_at: str | None = None      # M3 新增：话术发送时间（text_sent 中间态落库）
    image_sent_at: str | None = None
    error_msg: str | None = None
    error_category: str | None = None
    retry_count: int = 0
    health_checked: int = 0
    created_at: str = ""
    updated_at: str = ""


# ============================================================
# target（F1.5 产物）
# ============================================================
class RecommendedTarget(BaseModel):
    """F1.5 推荐的一条求职方向（设计 §5.7）。"""

    model_config = ConfigDict(extra="allow")

    name: str
    sub_direction: str
    city: str
    keywords: list[str] = Field(default_factory=list)
    per_job_keywords: list[str] = Field(default_factory=list)
    salary: str = ""
    weight: float = 0.0
    reason: str = ""
    interview_safe: bool = True

    def to_search_dict(self) -> dict[str, Any]:
        """转成 searcher 需要的搜索参数 dict。"""
        return {
            "name": self.name,
            "sub_direction": self.sub_direction,
            "city": self.city,
            "keywords": self.keywords,
            "per_job_keywords": self.per_job_keywords,
            "salary": self.salary,
            "weight": self.weight,
        }
