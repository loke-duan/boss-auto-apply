"""test_llm.py — ClaudeClient 测试（设计 §5.5.6，改造1：zcode→claude，10 case，全 mock subprocess）。

claude CLI 调用模板：
    claude -p <prompt> --output-format json --model <alias>
           --permission-mode bypassPermissions --no-session-persistence [--add-dir <dir>]

输出信封：``{"type":"result","result":"...","is_error":false,"usage":{...},"modelUsage":{...}}``。
"""

from __future__ import annotations

import json
import subprocess

import pytest

from boss_auto_apply.config import LlmCfg
from boss_auto_apply.errors import (
    ClaudeInvocationError,
    ClaudeLoginRequiredError,
    LlmJsonParseError,
    LlmRateLimitError,
    LlmTimeoutError,
)
from boss_auto_apply.llm import ClaudeClient, compute_cache_key, extract_json


def _cfg() -> LlmCfg:
    return LlmCfg(
        claude_bin="claude",
        timeout_sec=10, max_retries=2, backoff_base_sec=0.01,  # 测试用极小退避
    )


def _make_client(run_fn, **kw) -> ClaudeClient:
    """构造一个注入了 run_fn 的 client。"""
    return ClaudeClient(_cfg(), run_fn=run_fn, **kw)


def _claude_envelope(result: str = "ok", *, is_error: bool = False,
                     input_tokens: int = 10, output_tokens: int = 5,
                     model: str = "claude-sonnet-4-6") -> str:
    """构造 claude --output-format json 的标准信封 stdout。"""
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error,
        "result": result,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "modelUsage": {model: {"inputTokens": input_tokens, "outputTokens": output_tokens}},
    })


# ============================================================
# 登录探测
# ============================================================
def test_check_login_not_logged_in():
    """用例1：check_login 未登录 → 抛 ClaudeLoginRequiredError 且 message 含 login 命令。"""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout='{"loggedIn": false, "authMethod": "", "apiProvider": ""}', stderr="",
        )
    client = _make_client(fake_run)
    with pytest.raises(ClaudeLoginRequiredError) as ei:
        client.check_login()
    assert "login" in str(ei.value).lower()


def test_check_login_cached():
    """用例2：check_login 已登录 → 不重复探测（缓存）。"""
    call_count = {"n": 0}
    def fake_run(cmd, **kw):
        call_count["n"] += 1
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout='{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}',
            stderr="",
        )
    client = _make_client(fake_run)
    client.check_login()
    client.check_login()  # 第二次应走缓存
    assert call_count["n"] == 1


# ============================================================
# 缓存
# ============================================================
def test_invoke_cache_hit_no_subprocess(tmp_conn):
    """用例3：invoke 命中 cache_key → 0 次 subprocess 调用。"""
    call_count = {"n": 0}
    def fake_run(cmd, **kw):
        call_count["n"] += 1
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_claude_envelope('{"a":1}'), stderr="",
        )
    client = _make_client(fake_run, cache_conn=tmp_conn)
    client._login_ok = True  # 跳过 check_login（auth status 调用），只测缓存命中路径
    # 先 put 一个缓存
    import json as _json
    from boss_auto_apply import db as db_mod
    db_mod.cache_put(tmp_conn, "ck1", "tailor", _json.dumps({"cached": True}))
    res = client.invoke("prompt", expect_json=True, purpose="tailor", cache_key="ck1")
    assert res.cache_hit is True
    assert res.raw_json == {"cached": True}
    assert call_count["n"] == 0  # 没调 subprocess


# ============================================================
# 超时重试
# ============================================================
def test_invoke_timeout_retries_then_raises():
    """用例4：超时 → 重试 max_retries 次后抛 LlmTimeoutError。"""
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
    client = _make_client(fake_run)
    with pytest.raises(LlmTimeoutError):
        client.invoke("prompt", expect_json=False, purpose="generic")


# ============================================================
# JSON 解析
# ============================================================
def test_extract_json_from_mixed_stdout():
    """用例5：混杂日志的 stdout → extract_json 正确取末段 JSON。"""
    stdout = "INFO starting...\nDEBUG something\n{\"a\": 1, \"b\": 2}\ntrailing"
    obj = extract_json(stdout)
    assert obj == {"a": 1, "b": 2}


def test_extract_json_fenced():
    """围栏 ```json 取。"""
    stdout = 'log line\n```json\n{"x": "y"}\n```\nmore'
    obj = extract_json(stdout)
    assert obj == {"x": "y"}


def test_extract_json_failure():
    """无 JSON → 抛 LlmJsonParseError。"""
    with pytest.raises(LlmJsonParseError):
        extract_json("纯文本无 JSON")


def test_invoke_text_mode():
    """用例6：纯文本输出（expect_json=False）→ raw_json=None，text 正确。"""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_claude_envelope("纯文本回复"), stderr="",
        )
    client = _make_client(fake_run)
    client._login_ok = True  # 跳过 check_login
    res = client.invoke("prompt", expect_json=False, purpose="generic")
    assert res.raw_json is None
    assert "纯文本回复" in res.text


# ============================================================
# 附件（内容内联到 prompt）
# ============================================================
def test_invoke_attachment_not_found(tmp_path):
    """用例7：附件路径不存在 → FileNotFoundError。"""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_claude_envelope('{"a":1}'), stderr="",
        )
    client = _make_client(fake_run)
    client._login_ok = True  # 跳过 check_login
    with pytest.raises(FileNotFoundError):
        client.invoke("prompt", expect_json=True, purpose="generic",
                      attachments=[str(tmp_path / "nope.json")])


# ============================================================
# rate limit
# ============================================================
def test_invoke_rate_limit_raises():
    """用例8：rate limit 输出（is_error=true 或 result 含 rate limit）→ 抛 LlmRateLimitError。"""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout=_claude_envelope("Error: rate limit exceeded (429)"), stderr="",
        )
    client = _make_client(fake_run)
    client._login_ok = True  # 跳过 check_login
    with pytest.raises(LlmRateLimitError):
        client.invoke("prompt", expect_json=False, purpose="generic")


# ============================================================
# 模型切换（--model flag）
# ============================================================
def test_model_flag_passed_to_claude():
    """用例9：模型切换 —— claude CLI 原生支持 --model，prompt 不再注入 [MODEL:] 元指令。

    验证 subprocess cmd 含 ``--model sonnet``，且 prompt 不含 [MODEL:]。
    invoke 首次触发 check_login（auth status 调用，无 --model），随后才是 invoke 调用。
    """
    seen = {"invoke_cmd": []}
    def fake_run(cmd, **kw):
        # check_login 的 cmd 是 [claude, auth, status]（无 -p）；invoke 的 cmd 含 -p
        if "-p" in cmd:
            seen["invoke_cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_claude_envelope('{"a":1}'), stderr="",
        )
    client = _make_client(fake_run)
    client._login_ok = True  # 跳过 check_login，避免 auth status 调用干扰
    res = client.invoke("test", model="sonnet", expect_json=True, purpose="generic")
    assert res.raw_json == {"a": 1}
    # cmd 应含 --model sonnet
    assert "--model" in seen["invoke_cmd"]
    assert "sonnet" in seen["invoke_cmd"]
    # prompt（-p 后面那个参数）不应含 [MODEL:] 元指令
    p_idx = seen["invoke_cmd"].index("-p")
    prompt_arg = seen["invoke_cmd"][p_idx + 1]
    assert "[MODEL:" not in prompt_arg


# ============================================================
# 成功后写缓存
# ============================================================
def test_invoke_writes_cache(tmp_conn):
    """用例10：成功调用后 cache_put 写入 DB，下次 cache_get 命中。"""
    call_count = {"n": 0}
    def fake_run(cmd, **kw):
        call_count["n"] += 1
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_claude_envelope('{"r": 42}'), stderr="",
        )
    client = _make_client(fake_run, cache_conn=tmp_conn)
    client._login_ok = True  # 跳过 check_login
    client.invoke("prompt", expect_json=True, purpose="tailor", cache_key="ck_new")
    # 第二次命中缓存
    call_count["n"] = 0
    res = client.invoke("prompt", expect_json=True, purpose="tailor", cache_key="ck_new")
    assert res.cache_hit is True
    assert call_count["n"] == 0


def test_compute_cache_key_stable():
    """cache_key 相同输入 → 相同输出；不同 model → 不同。"""
    k1 = compute_cache_key("tailor", target_name="T", jd_signature="sig", master_version="v1", model="m1")
    k2 = compute_cache_key("tailor", target_name="T", jd_signature="sig", master_version="v1", model="m1")
    k3 = compute_cache_key("tailor", target_name="T", jd_signature="sig", master_version="v1", model="m2")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 16
