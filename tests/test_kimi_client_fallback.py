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
