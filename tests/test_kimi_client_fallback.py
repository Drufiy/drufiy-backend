import pytest

from app.agent import kimi_client


SIMPLE_TOOL = {
    "name": "submit_answer",
    "description": "Submit a single answer",
    "parameters": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    },
}


def test_timeout_errors_are_recoverable():
    assert kimi_client._is_recoverable_model_error(TimeoutError("request timed out"))
    assert kimi_client._is_recoverable_model_error(Exception("429 rate limit exceeded"))
    assert not kimi_client._is_recoverable_model_error(Exception("400 malformed request"))


@pytest.mark.asyncio
async def test_deepseek_fallback_runs_after_two_invalid_kimi_attempts(monkeypatch):
    calls = {"kimi": 0, "fallback": 0}

    async def fake_kimi(messages, tool_schema):
        calls["kimi"] += 1
        return None, "no tool call", {"latency_ms": 1}

    async def fake_fallback(client, model, messages, tool_schema, label):
        calls["fallback"] += 1
        return {"answer": "4"}, '{"answer":"4"}', {"latency_ms": 2}

    monkeypatch.setattr(kimi_client, "_call_kimi", fake_kimi)
    monkeypatch.setattr(kimi_client, "_call_openai_compatible_fallback", fake_fallback)
    monkeypatch.setattr(kimi_client, "_log_agent_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(kimi_client, "deepseek", object())

    result = await kimi_client.call_with_tool(
        system_prompt="You are a test model.",
        user_prompt="Use the tool.",
        tool_schema=SIMPLE_TOOL,
        model="unit",
    )

    assert result == {"answer": "4"}
    assert calls == {"kimi": 2, "fallback": 1}


class _FakeMessage:
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls


@pytest.mark.asyncio
async def test_blank_model_turn_never_produces_empty_assistant_message(monkeypatch):
    """
    Real crash from fromthepage-prash run a6e162bc: the model returned no tool
    call AND no content on an investigation step. The nudge path appended
    {"role": "assistant", "content": ""} with no tool_calls, which DeepSeek's
    API rejects on the next call: "message at position N with role
    'assistant' must not be empty". Every assistant turn we send back must
    have non-empty content or a tool_calls list.
    """
    sent_message_batches = []

    async def fake_call_with_tools(messages, tools, model="auto", timeout=None):
        # First step: model goes blank (no content, no tool call) — triggers the nudge.
        return _FakeMessage(content=None, tool_calls=None), "raw", {"latency_ms": 1}

    async def fake_call_deepseek(model, messages, tool_schema, timeout=None):
        sent_message_batches.append(messages)
        return {"problem_summary": "ok", "confidence": 0.9}, "{}", {"latency_ms": 1}

    monkeypatch.setattr(kimi_client, "_call_with_tools", fake_call_with_tools)
    monkeypatch.setattr(kimi_client, "_call_deepseek", fake_call_deepseek)
    monkeypatch.setattr(kimi_client, "_log_agent_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(kimi_client, "deepseek", object())

    result = await kimi_client.call_with_investigation(
        system_prompt="You are a test model.",
        user_prompt="Diagnose it.",
        diagnosis_tool_schema={"name": "submit_diagnosis", "description": "x", "parameters": {}},
        investigation_tools=[],
        execute_tool=lambda *a, **k: "",
        max_steps=1,
        model="deepseek-v4-pro",
    )

    assert result == {"problem_summary": "ok", "confidence": 0.9}
    assert sent_message_batches, "final call should have been reached"
    for messages in sent_message_batches:
        for msg in messages:
            if msg["role"] == "assistant":
                assert msg.get("content") or msg.get("tool_calls"), (
                    f"assistant message must not be empty with no tool_calls: {msg}"
                )
