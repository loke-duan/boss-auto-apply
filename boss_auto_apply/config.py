"""配置加载 + JSON Schema + Pydantic 双校验（设计 §5.2）。

数据流：``YAML str → dict → jsonschema.validate → AppConfig(**dict)``。

- JSON Schema 负责「结构层」硬约束（ADR-0001 require_health_check 必须 true 等）。
- Pydantic 负责「类型层」强类型化 + 跨字段交叉校验（@model_validator）。

任何一步失败抛 ``ConfigValidationError(errors=[...])``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ConfigValidationError

__all__ = [
    "PipelineCfg",
    "LlmCfg",
    "SearchCfg",
    "SenderCfg",
    "AppConfig",
    "load_config",
    "load_schema",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_SCHEMA_PATH",
]

# 默认路径（相对项目根；main.py 会 chdir 到项目根）
DEFAULT_CONFIG_PATH = "config/config.yaml"
DEFAULT_SCHEMA_PATH = "config/config.schema.json"


# ============================================================
# Pydantic 子模型
# ============================================================
class PipelineCfg(BaseModel):
    """流程开关（pipeline）。"""

    model_config = ConfigDict(extra="allow")  # 兼容老 config 含 bypass_title_filter 字段

    mode: Literal["dry-run", "auto", "confirm", "manual"] = "dry-run"
    dry_run: bool = True
    require_health_check: bool = True
    require_target_confirmation: bool = True
    require_resume_review: bool = True
    dedupe_by_company: bool = True
    skip_greeted: bool = True
    exclude_companies: list[str] = Field(default_factory=list)
    require_jd_match: bool = True
    # v3（ADR-0005）：删除 bypass_title_filter。
    # 旧版 pipeline 层用产品经理白名单过滤 title，无法适配其他候选人。
    # 新版改为 matcher LLM 基于 candidate_profile 判断 category_passed + level_match。
    # 老 config.yaml 仍可能含 bypass_title_filter: true/false，extra="allow" 兼容忽略。


class LlmCfg(BaseModel):
    """LLM 配置（llm）。

    改造1：从 ZCode CLI 换成 Claude CLI。backend/claude_bin/login_hint 是 claude 专用；
    model_* 字段值是 claude CLI 的 ``--model`` alias（sonnet/opus/haiku）。
    """

    backend: Literal["claude"] = "claude"
    claude_bin: str = "claude"
    login_hint: str = "claude auth login"
    model_tailor: str = "sonnet"
    model_profiler: str = "sonnet"
    model_hrbp: str = "sonnet"
    model_matcher: str = "haiku"
    model_greeter: str = "haiku"
    temperature: float = 0.3
    timeout_sec: int = 180
    max_retries: int = 3
    backoff_base_sec: float = 2.0
    tailor_strategy: Literal["cluster", "per_job"] = "cluster"
    cache: bool = True
    polish_level: str = "moderate"


class SearchCfg(BaseModel):
    """搜索配置（search）。"""

    provider: Literal["mock", "boss-cli", "drissionpage"] = "mock"
    fixtures_dir: str = "tests/fixtures"
    per_target_limit: int = 20


class SenderCfg(BaseModel):
    """M3 真发送配置（sender，设计附录 A）。

    所有字段有默认值，M1+M2 dry-run 不需要配置 sender。
    """

    primary: Literal["drissionpage", "boss-cli"] = "drissionpage"
    driver: Literal["drissionpage", "nodriver"] = "drissionpage"
    user_data_dir: str = "~/.boss-auto-apply/chrome-profile"
    stealth: bool = True
    headless: bool = False
    check_chrome_running: bool = True
    auto_quit_idle_sec: int = 600
    send_image_resume: bool = True
    wait_relation_sec: int = 8
    chat_text_max_len: int = 200         # [假设] 聊天框字数限制
    image_max_kb: int = 1024
    image_width_px: int = 1080
    browser_path: str | None = None
    load_mode: Literal["eager", "normal", "none"] = "eager"


class AppConfig(BaseModel):
    """应用配置（config.yaml 全量）。"""

    pipeline: PipelineCfg
    cities: list[str]
    target_constraints: dict[str, Any]
    llm: LlmCfg
    limits: dict[str, Any] = Field(default_factory=dict)
    pdf: dict[str, Any] = Field(default_factory=dict)
    paths: dict[str, Any]
    search: SearchCfg = Field(default_factory=SearchCfg)
    sender: SenderCfg = Field(default_factory=SenderCfg)
    circuit_breaker: dict[str, Any] = Field(default_factory=dict)
    targets: list[Any] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_check(self) -> "AppConfig":
        """跨字段交叉校验（Schema 表达不了的语义）。"""
        # ADR-0001：require_health_check 必须 True
        if not self.pipeline.require_health_check:
            raise ValueError("ADR-0001：pipeline.require_health_check 必须为 true（体检是闸门）")
        # 非 dry-run → search.provider 必须 boss-cli 或 drissionpage（M3+ 默认 drissionpage）
        if not self.pipeline.dry_run and self.search.provider not in ("boss-cli", "drissionpage"):
            raise ValueError(
                "pipeline.dry_run=false 时 search.provider 必须 boss-cli/drissionpage "
                f"（当前 {self.search.provider!r}）"
            )
        # cities 必须都在 target_constraints.salary 有薪资带
        salary = self.target_constraints.get("salary", {}) or {}
        missing = [c for c in self.cities if c not in salary]
        if missing:
            raise ValueError(
                f"cities 中的城市在 target_constraints.salary 缺薪资带：{missing}"
            )
        return self


# ============================================================
# 加载函数
# ============================================================
def load_schema(schema_path: str = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    """读 JSON Schema 文件为 dict。

    Args:
        schema_path: schema 文件路径。

    Returns:
        schema dict。

    Raises:
        ConfigValidationError: schema 文件不存在或 JSON 损坏。
    """
    p = Path(schema_path)
    if not p.exists():
        raise ConfigValidationError(f"config schema 文件不存在：{schema_path}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigValidationError(f"config schema JSON 损坏：{schema_path}", [{"error": str(e)}]) from e


def load_config(
    path: str = DEFAULT_CONFIG_PATH,
    schema_path: str = DEFAULT_SCHEMA_PATH,
    *,
    skip_schema: bool = False,
) -> AppConfig:
    """读 YAML → jsonschema 校验 → Pydantic 强类型化。

    Args:
        path: config.yaml 路径。
        schema_path: config.schema.json 路径。
        skip_schema: True 则跳过 JSON Schema（测试用，生产不建议）。

    Returns:
        强类型 ``AppConfig``。

    Raises:
        ConfigValidationError: 文件不存在 / YAML 损坏 / Schema 校验失败 / Pydantic 校验失败。
    """
    # [假设] config 文件路径相对 cwd；main.py 会 chdir 到项目根。
    p = Path(path)
    if not p.exists():
        # FileNotFoundError 友好包装为 ConfigValidationError（设计 §5.2 测试用例 6）
        raise ConfigValidationError(f"config 文件不存在：{path}（请 cp config/config.example.yaml config/config.yaml）")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigValidationError(f"config YAML 损坏：{path}", [{"error": str(e)}]) from e

    if not isinstance(raw, dict):
        raise ConfigValidationError(f"config 顶层必须是 dict，得到 {type(raw).__name__}")

    # 1) JSON Schema 校验
    if not skip_schema:
        schema = load_schema(schema_path)
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
        if errors:
            detail = [
                {
                    "path": "/".join(str(x) for x in e.absolute_path),
                    "message": e.message,
                }
                for e in errors
            ]
            # 可读汇总
            summary = "; ".join(f"{'/'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message}" for e in errors)
            logger.error(f"config schema 校验失败：{summary}")
            raise ConfigValidationError(f"config 未通过 JSON Schema 校验：{summary}", detail)

    # 2) Pydantic 强类型化（含交叉校验）
    try:
        return AppConfig(**raw)
    except ValueError as e:
        raise ConfigValidationError(f"config 未通过 Pydantic 校验：{e}", [{"error": str(e)}]) from e


def ensure_data_dirs(cfg: AppConfig) -> None:
    """确保 ``paths`` 里所有目录存在（db/log/resumes 等）。

    文件路径（master_json/db）的父目录也会创建。
    """
    paths = cfg.paths
    for key in ("resumes_out", "logs"):
        d = paths.get(key)
        if d:
            os.makedirs(d, exist_ok=True)
    # db / master_json 的父目录
    for key in ("db", "master_json", "master_health", "targets_confirmed"):
        f = paths.get(key)
        if f:
            os.makedirs(os.path.dirname(f) or ".", exist_ok=True)
