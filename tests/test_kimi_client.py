import pytest

from app.agent.kimi_client import call_with_tool

SIMPLE_TOOL = {
    "name": "submit_answer",
    "description": "Submit a single answer",
    "parameters": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    },
}


@pytest.mark.asyncio
async def test_kimi_basic_tool_call():
    result = await call_with_tool(
        system_prompt="You are a helpful assistant.",
        user_prompt="What is 2 + 2? Use the submit_answer tool.",
        tool_schema=SIMPLE_TOOL,
    )
    assert "answer" in result
    assert "4" in result["answer"]
