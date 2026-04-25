import json
import logging
import time

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)

kimi = AsyncOpenAI(
    api_key=settings.kimi_api_key,
    base_url=settings.kimi_base_url,
    timeout=60.0,
    default_headers={
        "HTTP-Referer": "https://drufiy.vercel.app",
        "X-Title": "Drufiy",
    },
)
claude = (
    AsyncAnthropic(api_key=settings.anthropic_api_key)
    if settings.anthropic_api_key
    else None
)


class DiagnosisValidationError(Exception):
    pass


# ── Internal call helpers ────────────────────────────────────────────────────

async def _call_kimi(messages: list, tool_schema: dict, temperature: float):
    start = time.time()
    response = await kimi.chat.completions.create(
        model=settings.kimi_model,
        messages=messages,
        tools=[{"type": "function", "function": tool_schema}],
        tool_choice={"type": "function", "function": {"name": tool_schema["name"]}},
        temperature=temperature,
        max_tokens=8000,
    )
    latency_ms = int((time.time() - start) * 1000)
    usage = {
        "input_tokens": response.usage.prompt_tokens,
        "output_tokens": response.usage.completion_tokens,
        "latency_ms": latency_ms,
    }
    msg = response.choices[0].message
    if not msg.tool_calls:
        return None, msg.content or "", usage
    try:
        args = json.loads(msg.tool_calls[0].function.arguments)
        return args, msg.tool_calls[0].function.arguments, usage
    except json.JSONDecodeError:
        return None, msg.tool_calls[0].function.arguments, usage


async def _call_claude_fallback(messages: list, tool_schema: dict):
    if not claude:
        return None, "", {}
    anthropic_tool = {
        "name": tool_schema["name"],
        "description": tool_schema["description"],
        "input_schema": tool_schema["parameters"],
    }
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_messages = [m for m in messages if m["role"] != "system"]
    start = time.time()
    response = await claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=system,
        messages=user_messages,
        tools=[anthropic_tool],
        tool_choice={"type": "tool", "name": tool_schema["name"]},
    )
    latency_ms = int((time.time() - start) * 1000)
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "latency_ms": latency_ms,
    }
    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_block:
        return None, "", usage
    return tool_block.input, json.dumps(tool_block.input), usage


def _log_agent_call(run_id, call_type, model, messages, raw, parsed, usage, valid):
    try:
        supabase.table("agent_calls").insert(
            {
                "run_id": run_id,
                "call_type": call_type,
                "model": model,
                "input_messages": messages,
                "output_raw": raw,
                "output_parsed": parsed,
                "tool_call_valid": valid,
                "validation_error": None if valid else "Failed to parse tool arguments",
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "latency_ms": usage.get("latency_ms"),
            }
        ).execute()
    except Exception as e:
        logger.error(f"Failed to log agent call: {e}")


# ── Public API ───────────────────────────────────────────────────────────────

async def call_with_tool(
    system_prompt: str,
    user_prompt: str,
    tool_schema: dict,
    run_id: str | None = None,
    call_type: str = "diagnosis",
    temperature: float = 0.2,
) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    for attempt_num, temp in enumerate([temperature, 0.0]):
        args, raw, usage = await _call_kimi(messages, tool_schema, temp)
        _log_agent_call(run_id, call_type, settings.kimi_model, messages, raw, args, usage, valid=(args is not None))
        if args is not None:
            return args
        logger.warning(f"Kimi attempt {attempt_num + 1} returned malformed tool output; retrying at temp=0")

    if claude:
        logger.warning("Kimi failed twice, falling back to Claude Sonnet 4.6")
        args, raw, usage = await _call_claude_fallback(messages, tool_schema)
        _log_agent_call(run_id, call_type, "claude-sonnet-4-6", messages, raw, args, usage, valid=(args is not None))
        if args is not None:
            return args

    raise DiagnosisValidationError("All model attempts returned malformed tool output")
