"""ClaudeClient — 所有 LLM 调用的唯一出口（设计 §5.5 核心，改造1：zcode→claude）。

职责：subprocess 调 ``claude -p ...``，处理登录/超时/重试/JSON 解析/模型分级/缓存/附件。

关键设计（实测驱动）：
- 命令模板：``claude -p <prompt> --output-format json --model <alias>
  --permission-mode bypassPermissions --no-session-persistence [--add-dir <dir>]``
- **模型分级原生支持**：claude CLI 有 ``--model`` flag（alias: sonnet/opus/haiku），
  比旧 zcode（无 --model，靠三级降级）强得多——故原设计 §5.5.1 的三级降级整段删除。
- 登录探测：``claude auth status`` → JSON，``loggedIn != true`` → 抛 ``ClaudeLoginRequiredError``。
- JSON 解析 defensive：``--output-format json`` 输出信封 ``{"type":"result","result":"...",...}``，
  先取 ``result`` 文本；若调用方要求 JSON，再从 result 文本里 ``extract_json``。
- 附件传递：claude 无 ``--attach``；JD/master 内容**直接拼进 prompt**（方式 A，
  claude 上下文 200K tokens，可控且省 token）。
- 重试/退避：按 §5.5.5 表（network/llm 重试，business/risk 不重试）。
- cache：cache_get/cache_put 走 db.py（注入 conn；为 None 则不缓存）。
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from .config import LlmCfg
from .errors import (
    ClaudeInvocationError,
    ClaudeLoginRequiredError,
    LlmJsonParseError,
    LlmRateLimitError,
    LlmTimeoutError,
)

__all__ = [
    "LlmResult",
    "ClaudeClient",
    "CodexClient",
    "create_llm_client",
    "compute_cache_key",
    "extract_json",
    "RATE_LIMIT_MARKERS",
]

# rate limit / overloaded 关键词（claude 输出含这些串判定为限流）
RATE_LIMIT_MARKERS = ("rate limit", "429", "quota", "too many requests", "rate_limit",
                      "overloaded", "529", "capacity")


# ============================================================
# LlmResult
# ============================================================
@dataclass
class LlmResult:
    """LLM 调用结果。"""

    text: str                      # LLM 文本输出（claude json 信封的 result 字段）
    raw_json: dict | None = None   # 若要求 JSON 输出且解析成功
    tokens_in: int | None = None
    tokens_out: int | None = None
    elapsed_sec: float = 0.0
    cache_hit: bool = False
    model_used: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ============================================================
# cache key
# ============================================================
def compute_cache_key(
    purpose: str,
    *,
    target_name: str = "",
    jd_signature: str = "",
    jd_text: str = "",
    master_version: str = "",
    model: str = "",
) -> str:
    """计算 llm_cache 的 cache_key（设计 §7.3）。

    ``sha1(purpose | target_name | jd_signature_or_hash(jd_text) | master_version | model)[:16]``。

    Args:
        purpose: tailor/matcher/profiler/hrbp/greeter。
        target_name: 方向名。
        jd_signature: JD 聚类签名（优先）；为空则用 jd_text 的 hash。
        jd_text: JD 原文（jd_signature 为空时用）。
        master_version: master.json 改动后失效（hash 前 8 位）。
        model: 换模型视为新缓存。

    Returns:
        16 字符 cache_key。
    """
    import hashlib
    sig = jd_signature or (hashlib.sha1(jd_text.encode("utf-8")).hexdigest()[:12] if jd_text else "")
    raw = "|".join([purpose, target_name, sig, master_version, model])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ============================================================
# JSON 提取（defensive，§5.5.4）
# ============================================================
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(stdout: str) -> dict:
    """从可能混有日志的 stdout 里取最后一个合法 JSON 对象。

    策略：
    1. 优先 ```json ... ``` 围栏，取最后一个围栏内容。
    2. 否则从末尾向前找 `{`，逐个尝试 `json.loads`。
    3. 全失败 → 抛 `LlmJsonParseError`。

    Args:
        stdout: subprocess 原始 stdout（claude json 信封的 result 文本，或混杂物）。

    Returns:
        解析出的 dict。

    Raises:
        LlmJsonParseError: 无法解析。
    """
    # 1) 围栏优先（取最后一个）
    fences = _FENCE_RE.findall(stdout)
    for fenced in reversed(fences):
        cand = fenced.strip()
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    # 2) 末尾倒序找 '{'
    last_brace = stdout.rfind("}")
    if last_brace != -1:
        search_from = 0
        while True:
            open_idx = stdout.find("{", search_from)
            if open_idx == -1 or open_idx > last_brace:
                break
            cand = stdout[open_idx : last_brace + 1]
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            search_from = open_idx + 1

    raise LlmJsonParseError(raw=stdout, expected_schema="JSON object")


# ============================================================
# ClaudeClient
# ============================================================
class ClaudeClient:
    """Claude CLI 的 subprocess 封装（设计 §5.5，改造1：zcode→claude）。

    所有 LLM 调用走这里；core/ 各模块通过本类拿结果，绝不自己 subprocess。

    可注入 ``run_fn``（默认 ``subprocess.run``）便于测试 mock。
    可注入 ``cache_conn``（默认 None）控制是否走 DB 缓存。

    公开方法签名与旧 ``ZcodeAgentClient`` 完全一致（invoke/check_login/tailor/
    profile/hrbp_check/extract_jd_skills/greet），调用方零改动。
    """

    def __init__(
        self,
        cfg: LlmCfg,
        logger_obj: Any = None,
        *,
        run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
        cache_conn: Any = None,
        master_version: str = "",
        check_login_on_init: bool = False,
    ) -> None:
        """
        Args:
            cfg: LLM 配置。
            logger_obj: loguru logger（可注入；默认用模块 logger）。
            run_fn: subprocess.run 替身（测试 mock 用）。
            cache_conn: sqlite3 连接（None 则不缓存）。
            master_version: master.json 版本 hash（缓存失效用）。
            check_login_on_init: True 则构造时立即探测登录。
        """
        self.cfg = cfg
        self.log = logger_obj or logger
        self._run_fn = run_fn or subprocess.run
        self._cache_conn = cache_conn
        self._master_version = master_version
        self._login_ok = False
        if check_login_on_init:
            self.check_login()

    # ------------------------------------------------------------
    # subprocess 调用底座
    # ------------------------------------------------------------
    def _run(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        """调 ``claude <args>``（或 ``claude auth status``）。

        Args:
            args: claude 的 flag 参数（不含 claude 路径）。
            timeout: 超时秒。

        Returns:
            ``CompletedProcess``。

        Raises:
            ClaudeInvocationError: claude 命令缺失。
            LlmTimeoutError: 超时。
        """
        claude_bin = self.cfg.claude_bin
        path = shutil.which(claude_bin) or claude_bin
        # 测试注入 run_fn 时由替身处理命令，不依赖本机安装 CLI。
        if self._run_fn is subprocess.run and not os.path.exists(path):
            raise ClaudeInvocationError(
                f"claude 命令不在 PATH（{claude_bin}）。请装 Claude CLI 并跑 `claude auth login`。"
            )

        cmd = [path] + args
        try:
            self.log.debug(f"claude 调用：{' '.join(cmd[:4])}... (timeout={timeout}s)")
            proc = self._run_fn(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return proc
        except subprocess.TimeoutExpired as e:
            raise LlmTimeoutError(f"claude 调用超时（{timeout}s）") from e

    # ------------------------------------------------------------
    # 登录探测（§5.5.3）
    # ------------------------------------------------------------
    def check_login(self) -> None:
        """探测登录态：跑 ``claude auth status``，解析 JSON，``loggedIn != true`` → 抛错。

        结果缓存到实例（一次进程内只探一次）。
        """
        if self._login_ok:
            return
        try:
            proc = self._run(["auth", "status"], timeout=15)
        except LlmTimeoutError:
            # 探测超时不算未登录（可能是网络慢），保守认为已登录但记 warning
            self.log.warning("check_login 探测超时，假设已登录（后续调用会重试）")
            self._login_ok = True
            return
        except ClaudeInvocationError:
            raise

        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {}
        if data.get("loggedIn") is not True:
            raise ClaudeLoginRequiredError(self.cfg.login_hint)
        self._login_ok = True
        self.log.debug("check_login 通过（claude auth status loggedIn=true）")

    # ------------------------------------------------------------
    # invoke（主入口）
    # ------------------------------------------------------------
    def invoke(
        self,
        prompt: str,
        *,
        model: str | None = None,
        attachments: list[str] | None = None,
        expect_json: bool = False,
        json_schema: str | None = None,
        purpose: str = "generic",
        cache_key: str | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> LlmResult:
        """完整 LLM 调用流程（§5.5.2）。

        流程：
        1. check_login（首次）。
        2. cache_key 命中 → 直接返回（cache_hit=True）。
        3. 拼 prompt（+ 附件内容内联 + schema 约束）。模型走 ``--model`` flag 原生切换。
        4. subprocess 调 claude -p（带超时 + 重试退避）。
        5. 解析输出：``--output-format json`` 取信封 result 文本；expect_json 再 extract_json。
        6. expect_json 且解析失败 → ``LlmJsonParseError``（重试 1 次）。
        7. 成功 → cache_put + 返回 ``LlmResult``。

        Args:
            prompt: prompt 文本。
            model: claude 模型 alias（sonnet/opus/haiku）；None 用默认。
            attachments: 附件路径列表（内容会被内联拼进 prompt）。
            expect_json: True 则要求 JSON 输出并解析。
            json_schema: 期望 JSON schema 描述（注入 prompt + 错误信息）。
            purpose: llm_cache 用途（tailor/matcher/...）。
            cache_key: 命中则 0 调用；为 None 不查缓存。
            cwd: 工作目录（--add-dir 用，None 则用 os.getcwd）。
            timeout_sec: 单次超时；None 用 cfg.timeout_sec。

        Returns:
            ``LlmResult``。

        Raises:
            ClaudeLoginRequiredError: 未登录。
            LlmTimeoutError: 重试耗尽仍超时。
            LlmRateLimitError: rate limit（重试耗尽）。
            LlmJsonParseError: JSON 解析失败（重试 1 次后仍失败）。
            ClaudeInvocationError: claude 命令缺失。
            FileNotFoundError: 附件不存在。
        """
        start = time.monotonic()

        # 1) 登录探测
        self.check_login()

        # 2) 缓存命中
        if cache_key and self._cache_conn is not None and self.cfg.cache:
            from . import db as db_mod  # 延迟 import 避免循环
            hit = db_mod.cache_get(self._cache_conn, cache_key)
            if hit is not None:
                db_mod.cache_hit_incr(self._cache_conn, cache_key)
                self.log.info(f"LLM cache 命中 purpose={purpose} key={cache_key}")
                return LlmResult(
                    text=hit["response"],
                    raw_json=hit.get("response_obj"),
                    cache_hit=True,
                    model_used=hit.get("model"),
                    metadata={"cache_key": cache_key},
                )

        # 3) 拼最终 prompt（附件内容内联 + JSON 约束；模型走 --model flag 不进 prompt）
        final_prompt = self._build_prompt(
            prompt, attachments=attachments, expect_json=expect_json, json_schema=json_schema,
        )

        # 3b) 附件存在性检查
        if attachments:
            for f in attachments:
                if not os.path.exists(f):
                    raise FileNotFoundError(f"附件不存在：{f}")

        # 4) 调用 + 重试
        timeout = timeout_sec or self.cfg.timeout_sec
        result = self._invoke_with_retry(
            final_prompt, model=model, cwd=cwd,
            timeout=timeout, expect_json=expect_json, json_schema=json_schema,
        )

        result.elapsed_sec = time.monotonic() - start
        result.model_used = result.model_used or model or "default"

        # 7) 写缓存
        if cache_key and self._cache_conn is not None and self.cfg.cache and result.raw_json is not None:
            from . import db as db_mod
            db_mod.cache_put(
                self._cache_conn, cache_key, purpose,
                json.dumps(result.raw_json, ensure_ascii=False),
                model=result.model_used,
            )
            self.log.debug(f"LLM cache 写入 purpose={purpose} key={cache_key}")

        return result

    # ------------------------------------------------------------
    # prompt 构建（附件内容内联 + JSON 约束）
    # ------------------------------------------------------------
    def _build_prompt(
        self,
        prompt: str,
        *,
        attachments: list[str] | None,
        expect_json: bool,
        json_schema: str | None,
    ) -> str:
        """拼最终 prompt：附件内容内联（方式 A）+ JSON 约束。

        claude 无 --attach，故把附件文件内容直接拼进 prompt（上下文 200K tokens 足够）。
        模型切换走 --model flag，不进 prompt（删除旧的 [MODEL: x] 元指令）。
        """
        import re as _re
        # 防御性清洗：subprocess 拒绝含 null byte 的参数（ValueError: embedded null byte）。
        # parser 出口已清洗，这里对 prompt + 附件内容兜底（去掉 C0 控制字符 + U+FFFD）。
        def _sanitize(s: str) -> str:
            return _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\uFFFD]", "", s)
        parts: list[str] = [_sanitize(prompt)]
        if attachments:
            for f in attachments:
                try:
                    content = Path_safe_read(f)
                    parts.append(_sanitize(f"\n\n--- 附件：{os.path.basename(f)} ---\n{content}"))
                except OSError as e:
                    self.log.warning(f"附件读取失败（{f}）：{e}，跳过内联")
        if expect_json:
            parts.append(
                "\n\n【输出要求】只输出一个合法 JSON 对象，不要任何 markdown 围栏、"
                "解释或额外文字。"
            )
            if json_schema:
                parts.append(f"\n期望 JSON 结构：\n{json_schema}")
        return "\n".join(parts)

    # ------------------------------------------------------------
    # 重试/退避
    # ------------------------------------------------------------
    def _invoke_with_retry(
        self,
        prompt: str,
        *,
        model: str | None,
        cwd: str | None,
        timeout: int,
        expect_json: bool,
        json_schema: str | None,
    ) -> LlmResult:
        """带重试退避的单次调用循环（§5.5.5）。

        - 超时 / rate limit → 重试（指数退避）。
        - JSON 解析失败 → 重试 1 次（prompt 加「只输出 JSON」）。
        - 登录 / claude 缺失 / business 拒绝 → 不重试，直接抛。
        """
        max_retries = self.cfg.max_retries
        last_exc: Exception | None = None
        json_retry_done = False

        for attempt in range(max_retries + 1):
            try:
                text, usage, model_used = self._call_once(
                    prompt, model=model, cwd=cwd, timeout=timeout,
                )
                low = text.lower()

                # 登录失效串 → 不重试（check_login 已挡，这里防御性）
                if "not logged in" in low or "please log in" in low:
                    raise ClaudeLoginRequiredError(self.cfg.login_hint)
                # rate limit / overloaded → 重试（更激进退避）
                if any(m in low for m in RATE_LIMIT_MARKERS):
                    raise LlmRateLimitError(f"LLM rate limit：{text[:200]}")

                # 解析输出
                raw_json: dict | None = None
                if expect_json:
                    try:
                        raw_json = extract_json(text)
                    except LlmJsonParseError as e:
                        if not json_retry_done and attempt < max_retries:
                            json_retry_done = True
                            self.log.warning(f"JSON 解析失败，重试 1 次（attempt={attempt}）")
                            last_exc = e
                            time.sleep(1.0)
                            continue
                        raise

                return LlmResult(
                    text=text,
                    raw_json=raw_json,
                    tokens_in=usage.get("in"),
                    tokens_out=usage.get("out"),
                    model_used=model_used,
                )

            except (LlmTimeoutError, LlmRateLimitError) as e:
                last_exc = e
                if attempt < max_retries:
                    base = self.cfg.backoff_base_sec
                    if isinstance(e, LlmRateLimitError):
                        sleep = base * (4 ** attempt)
                    else:
                        sleep = base * (2 ** attempt)
                    sleep += random.uniform(0, 0.5 * base)  # jitter
                    self.log.warning(f"LLM 重试 attempt={attempt} {type(e).__name__} sleep={sleep:.1f}s")
                    time.sleep(sleep)
                    continue
                raise
            except ClaudeLoginRequiredError:
                raise
            except ClaudeInvocationError:
                raise
            except LlmJsonParseError:
                raise

        if last_exc:
            raise last_exc
        raise LlmTimeoutError("LLM 调用重试耗尽")

    def _call_once(
        self,
        prompt: str,
        *,
        model: str | None,
        cwd: str | None,
        timeout: int,
    ) -> tuple[str, dict[str, int], str | None]:
        """组装 flag 并调一次 subprocess；解析 claude json 信封。

        Returns:
            (result 文本, {"in": tokens_in, "out": tokens_out}, model_used)
        """
        args: list[str] = [
            "-p", prompt,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
        ]
        if model:
            args += ["--model", model]
        work_dir = cwd or os.getcwd()
        if os.path.isdir(work_dir):
            args += ["--add-dir", work_dir]

        proc = self._run(args, timeout=timeout)
        out = (proc.stdout or "").strip()
        if not out:
            # 空输出：可能是 claude 崩了，给可读错误
            raise LlmJsonParseError(
                raw=f"stdout empty (stderr={proc.stderr[:300]})",
                expected_schema="claude json envelope",
            )
        # 解析 claude json 信封 {"type":"result","result":"...",...,"usage":{...}}
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            # 信封本身不是 JSON：把 stdout 当纯文本（某些错误态会这样）
            return out, {}, None

        if envelope.get("is_error"):
            # claude 把 LLM 报错也包进 json 信封，is_error=true
            err_text = envelope.get("result") or out
            raise LlmRateLimitError(f"claude 返回 is_error：{err_text[:200]}")

        text = envelope.get("result") or ""
        usage_raw = envelope.get("usage") or {}
        usage = {
            "in": usage_raw.get("input_tokens"),
            "out": usage_raw.get("output_tokens"),
        }
        # model_used：优先 modelUsage 的 key，否则用 model 字段
        model_used = None
        model_usage = envelope.get("modelUsage") or {}
        if isinstance(model_usage, dict) and model_usage:
            model_used = next(iter(model_usage.keys()))
        return text, usage, model_used

    # ------------------------------------------------------------
    # 便捷方法（模型分级路由，§5.5.2）
    # ------------------------------------------------------------
    def tailor(self, jd: str, master: dict, target: dict, **kw: Any) -> dict:
        """F4 定制（model_tailor）。返回 LLM JSON dict。"""
        prompt = f"任务：为以下 JD 定制简历。\n\nJD：\n{jd}\n\nmaster：见附件。target：{json.dumps(target, ensure_ascii=False)}"
        res = self.invoke(
            prompt, model=self.cfg.model_tailor, expect_json=True,
            purpose="tailor", attachments=kw.get("attachments"), **_filter_kw(kw),
        )
        return res.raw_json or {}

    def profile(self, master: dict, constraints: dict, **kw: Any) -> dict:
        """F1.5 画像（model_profiler）。"""
        prompt = f"任务：基于 master 推荐求职方向。\n\nconstraints：{json.dumps(constraints, ensure_ascii=False)}"
        res = self.invoke(
            prompt, model=self.cfg.model_profiler, expect_json=True,
            purpose="profiler", attachments=kw.get("attachments"), **_filter_kw(kw),
        )
        return res.raw_json or {}

    def hrbp_check(self, master: dict, **kw: Any) -> dict:
        """F1.6 体检（model_hrbp）。"""
        prompt = "任务：HRBP 简历体检。master 见附件。"
        res = self.invoke(
            prompt, model=self.cfg.model_hrbp, expect_json=True,
            purpose="hrbp", attachments=kw.get("attachments"), **_filter_kw(kw),
        )
        return res.raw_json or {}

    def extract_jd_skills(self, jd: str, **kw: Any) -> dict:
        """F3 JD 抽取（model_matcher，便宜）。"""
        prompt = f"任务：抽取 JD 硬技能。\n\nJD：\n{jd}"
        res = self.invoke(
            prompt, model=self.cfg.model_matcher, expect_json=True,
            purpose="matcher", **_filter_kw(kw),
        )
        return res.raw_json or {}

    def greet(self, jd: str, master: dict, target: dict, **kw: Any) -> str:
        """F7 打招呼话术（model_greeter，便宜）。返回文本。"""
        prompt = f"任务：生成打招呼话术（≤30字）。\n\nJD：\n{jd}\n\ntarget：{json.dumps(target, ensure_ascii=False)}"
        res = self.invoke(
            prompt, model=self.cfg.model_greeter, expect_json=False,
            purpose="greeter", **_filter_kw(kw),
        )
        return res.text.strip()


class CodexClient(ClaudeClient):
    """使用 ``codex exec`` 的非交互 LLM 后端。

    复用 ClaudeClient 的 prompt、缓存、JSON 提取和重试逻辑，仅替换
    CLI 可用性/登录探测与单次调用协议。Codex 以 read-only sandbox 运行，
    prompt 通过 stdin 传入，不授予项目写权限。
    """

    def _codex_path(self) -> str:
        codex_bin = self.cfg.codex_bin
        path = shutil.which(codex_bin) or codex_bin
        if self._run_fn is subprocess.run and not os.path.exists(path):
            raise ClaudeInvocationError(
                f"codex 命令不在 PATH（{codex_bin}）。请安装 Codex CLI 并跑 `codex login`。"
            )
        return path

    def check_login(self) -> None:
        """用 ``codex login status`` 探测 Codex CLI 登录态。"""
        if self._login_ok:
            return
        path = self._codex_path()
        try:
            proc = self._run_fn(
                [path, "login", "status"], capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired as e:
            raise LlmTimeoutError("codex 登录探测超时（15s）") from e
        if proc.returncode != 0:
            raise ClaudeLoginRequiredError("codex login")
        self._login_ok = True
        self.log.debug("check_login 通过（codex login status）")

    def _call_once(
        self,
        prompt: str,
        *,
        model: str | None,
        cwd: str | None,
        timeout: int,
    ) -> tuple[str, dict[str, int], str | None]:
        """执行 ``codex exec --json`` 并从 JSONL 事件取最后一条 agent_message。"""
        path = self._codex_path()
        effective_model = self.cfg.codex_model
        cmd = [
            path, "exec", "--json", "--sandbox", "read-only",
            "--skip-git-repo-check", "--ephemeral",
        ]
        if effective_model:
            cmd += ["--model", effective_model]
        cmd += ["-"]
        try:
            proc = self._run_fn(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd or os.getcwd(),
            )
        except subprocess.TimeoutExpired as e:
            raise LlmTimeoutError(f"codex 调用超时（{timeout}s）") from e

        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            detail = (proc.stderr or out or f"exit={proc.returncode}")[:300]
            if any(marker in detail.lower() for marker in RATE_LIMIT_MARKERS):
                raise LlmRateLimitError(f"Codex CLI 限流：{detail}")
            raise ClaudeInvocationError(f"Codex CLI 调用失败：{detail}")

        messages: list[str] = []
        usage: dict[str, int] = {}
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    messages.append(text)
            if event.get("type") == "turn.completed":
                raw_usage = event.get("usage") or {}
                usage = {
                    "in": raw_usage.get("input_tokens"),
                    "out": raw_usage.get("output_tokens"),
                }
        if not messages:
            raise LlmJsonParseError(raw=out, expected_schema="Codex JSONL agent_message")
        return messages[-1], usage, effective_model or "codex-default"


def create_llm_client(cfg: LlmCfg, **kwargs: Any) -> ClaudeClient:
    """根据 ``llm.backend`` 构建 CLI 后端，业务层保持原有调用接口。"""
    if cfg.backend == "codex":
        return CodexClient(cfg, **kwargs)
    return ClaudeClient(cfg, **kwargs)


# ============================================================
# 辅助
# ============================================================
def Path_safe_read(f: str) -> str:
    """安全读附件文件内容（隔离 Path import 逻辑）。"""
    with open(f, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _filter_kw(kw: dict[str, Any]) -> dict[str, Any]:
    """从 kwargs 取 invoke 认的参数。"""
    out: dict[str, Any] = {}
    for k in ("cache_key", "cwd", "timeout_sec", "json_schema"):
        if k in kw:
            out[k] = kw[k]
    return out
