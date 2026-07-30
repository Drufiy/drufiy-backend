"""
Regression test for a live production bug (2026-07-30): DeepSeek returned a
successful tool call whose arguments were shaped like a *different* tool
(fetch_file's {path, line, limit}) under the one function offered
(submit_diagnosis). json.loads() succeeded, so the bad dict got trusted and
crashed Diagnosis(**raw_args) with 6 missing-field errors, 8+ minutes into a
run, after the DeepSeek budget was already spent.

_args_match_schema() is the guard that catches this before it's trusted.
No API calls — the model call itself is faked, same pattern as
test_diagnosis_guardrails.py.
"""
import json

import pytest

from app.agent.kimi_client import _args_match_schema, _call_deepseek

DIAGNOSIS_TOOL = {
    "name": "submit_diagnosis",
    "parameters": {
        "type": "object",
        "required": ["problem_summary", "root_cause", "fix_type", "confidence"],
        "properties": {},
    },
}


def test_matching_args_pass():
    args = {"problem_summary": "x", "root_cause": "y", "fix_type": "safe_auto_apply", "confidence": 0.9}
    assert _args_match_schema(args, DIAGNOSIS_TOOL) is True


def test_wrong_shaped_args_fail():
    # The exact shape that broke production: a fetch_file call under submit_diagnosis's name.
    args = {"path": "functions.php", "line": 240, "limit": 20}
    assert _args_match_schema(args, DIAGNOSIS_TOOL) is False


def test_partial_args_fail():
    args = {"problem_summary": "x", "root_cause": "y"}  # missing fix_type, confidence
    assert _args_match_schema(args, DIAGNOSIS_TOOL) is False


def test_non_dict_fails():
    assert _args_match_schema(["not", "a", "dict"], DIAGNOSIS_TOOL) is False
    assert _args_match_schema(None, DIAGNOSIS_TOOL) is False


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, name, arguments):
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.content = ""
        self.reasoning_content = ""


class _FakeUsage:
    prompt_tokens = 100
    completion_tokens = 50


class _FakeResponse:
    def __init__(self, tool_calls):
        self.choices = [type("Choice", (), {"message": _FakeMessage(tool_calls)})()]
        self.usage = _FakeUsage()


@pytest.mark.asyncio
async def test_call_deepseek_rejects_wrong_shaped_tool_call(monkeypatch):
    # Reproduces the exact production failure: a "successful" tool call under the
    # right function name, but arguments shaped for a different tool entirely.
    wrong_shaped = json.dumps({"path": "functions.php", "line": 240, "limit": 20})

    async def fake_create_chat(client, **kwargs):
        return _FakeResponse([_FakeToolCall("submit_diagnosis", wrong_shaped)])

    monkeypatch.setattr("app.agent.kimi_client._create_chat", fake_create_chat)

    args, raw, usage = await _call_deepseek("deepseek-v4-pro", [{"role": "user", "content": "x"}], DIAGNOSIS_TOOL)

    assert args is None
    assert raw == wrong_shaped


@pytest.mark.asyncio
async def test_call_deepseek_accepts_correctly_shaped_tool_call(monkeypatch):
    correct = json.dumps({
        "problem_summary": "Missing dependency", "root_cause": "package not installed",
        "fix_type": "safe_auto_apply", "confidence": 0.9,
    })

    async def fake_create_chat(client, **kwargs):
        return _FakeResponse([_FakeToolCall("submit_diagnosis", correct)])

    monkeypatch.setattr("app.agent.kimi_client._create_chat", fake_create_chat)

    args, raw, usage = await _call_deepseek("deepseek-v4-pro", [{"role": "user", "content": "x"}], DIAGNOSIS_TOOL)

    assert args is not None
    assert args["fix_type"] == "safe_auto_apply"
