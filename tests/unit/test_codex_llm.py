"""Codex CLI LLM 后端测试（全部 mock subprocess）。"""

from __future__ import annotations

import json
import subprocess

import pytest

from boss_auto_apply.config import LlmCfg
from boss_auto_apply.errors import ClaudeLoginRequiredError
from boss_auto_apply.llm import CodexClient, create_llm_client


def _cfg(**overrides) -> LlmCfg:
    return LlmCfg(backend="codex", codex_bin="/bin/echo", timeout_sec=10,
                  max_retries=0, **overrides)


def _jsonl(message: str = '{"ok":true}') -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "test"}),
        json.dumps({
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": message},
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }),
    ])


def test_factory_selects_backend():
    assert isinstance(create_llm_client(_cfg()), CodexClient)
    assert not isinstance(create_llm_client(LlmCfg()), CodexClient)


def test_codex_invoke_uses_read_only_jsonl_and_stdin():
    seen = {}

    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(cmd, 0, "Logged in", "")
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, _jsonl(), "")

    result = CodexClient(_cfg(codex_model="gpt-test"), run_fn=fake_run).invoke(
        "return json", model="sonnet", expect_json=True,
    )

    assert result.raw_json == {"ok": True}
    assert result.tokens_in == 12
    assert result.tokens_out == 4
    assert result.model_used == "gpt-test"
    assert seen["cmd"][1:7] == [
        "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
    ]
    assert ["--model", "gpt-test"] == seen["cmd"][7:9]
    assert seen["cmd"][-1] == "-"
    assert "return json" in seen["kwargs"]["input"]


def test_codex_default_model_does_not_forward_claude_alias():
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, _jsonl("ok"), "")

    client = CodexClient(_cfg(), run_fn=fake_run)
    client._login_ok = True
    result = client.invoke("hello", model="haiku")
    assert "--model" not in seen["cmd"]
    assert result.model_used == "codex-default"


def test_codex_login_failure_has_login_hint():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "not logged in")

    with pytest.raises(ClaudeLoginRequiredError, match="codex login"):
        CodexClient(_cfg(), run_fn=fake_run).check_login()
