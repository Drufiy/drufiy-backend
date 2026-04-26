import json
import logging
import time

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)


# ── Client setup ─────────────────────────────────────────────────────────────

kimi = AsyncOpenAI(
    api_key=settings.kimi_api_key,
    base_url=settings.kimi_base_url,
    timeout=120.0,                    # Moonshot can be slow on long logs
)

claude = (
    AsyncAnthropic(api_key=settings.anthropic_api_key)
    if settings.anthropic_api_key
    else None
)


class DiagnosisValidationError(Exception):
    pass


# ── Internal helpers ─────────────────────────────────────────────────────────

def _extract_json_from_prose(text: str) -> dict | None:
    """
    Last-resort: if the model returned prose instead of a tool call,
    try to extract a JSON object from the response text.
    Handles cases where the model outputs the JSON inline without wrapping it.
    """
    if not text:
        return None
    # Look for the outermost {...} block
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    candidate = json.loads(text[start:i + 1])
                    # Must have at least the core fields to be a valid diagnosis
                    if "fix_type" in candidate and "problem_summary" in candidate:
                        logger.info("Extracted valid JSON from prose response")
                        return candidate
                except json.JSONDecodeError:
                    pass
    return None


async def _call_kimi(messages: list, tool_schema: dict):
    """
    Returns (parsed_args_or_none, raw_content, usage_info).
    Handles 402 (credit exhausted) gracefully — returns None so Claude fallback fires.
    """
    start = time.time()
    try:
        response = await kimi.chat.completions.create(
            model=settings.kimi_model,
            messages=messages,
            tools=[{"type": "function", "function": tool_schema}],
            tool_choice="required",  # Moonshot requires this for tool calls
            temperature=0.1,
            max_tokens=8000,
        )
    except Exception as e:
        # 402 insufficient credits, 429 rate limit, 503 provider down — all recoverable
        err_str = str(e)
        if any(code in err_str for code in ["402", "429", "503", "insufficient"]):
            logger.warning(f"Kimi API recoverable error ({e.__class__.__name__}): {err_str[:200]} — will try fallback")
            return None, "", {"latency_ms": int((time.time() - start) * 1000)}
        raise

    latency_ms = int((time.time() - start) * 1000)
    usage = {
        "input_tokens": response.usage.prompt_tokens if response.usage else None,
        "output_tokens": response.usage.completion_tokens if response.usage else None,
        "latency_ms": latency_ms,
    }

    msg = response.choices[0].message

    # ── Path 1: proper tool call ──────────────────────────────────────────────
    if msg.tool_calls:
        try:
            args = json.loads(msg.tool_calls[0].function.arguments)
            return args, msg.tool_calls[0].function.arguments, usage
        except json.JSONDecodeError:
            raw = msg.tool_calls[0].function.arguments
            # Try to salvage malformed JSON
            extracted = _extract_json_from_prose(raw)
            if extracted:
                return extracted, raw, usage
            return None, raw, usage

    # ── Path 2: model returned prose — try to extract JSON from it ───────────
    prose = msg.content or ""
    extracted = _extract_json_from_prose(prose)
    if extracted:
        return extracted, prose, usage

    return None, prose, usage


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
    try:
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=system,
            messages=user_messages,
            tools=[anthropic_tool],
            tool_choice={"type": "tool", "name": tool_schema["name"]},
        )
    except Exception as e:
        logger.error(f"Claude fallback also failed: {e}")
        return None, "", {}
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


def _log_agent_call(run_id, call_type, model, messages, raw, parsed, usage, valid, error=None):
    try:
        supabase.table("agent_calls").insert({
            "run_id": run_id,
            "call_type": call_type,
            "model": model,
            "input_messages": messages,
            "output_raw": raw,
            "output_parsed": parsed,
            "tool_call_valid": valid,
            "validation_error": error if not valid else None,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "latency_ms": usage.get("latency_ms"),
        }).execute()
    except Exception as e:
        logger.error(f"Failed to log agent call: {e}")
        # Never propagate — logging must not break the pipeline


# ── Public API ────────────────────────────────────────────────────────────────

async def call_with_tool(
    system_prompt: str,
    user_prompt: str,
    tool_schema: dict,
    run_id: str | None = None,
    call_type: str = "diagnosis",
    temperature: float = 1.0,  # kimi-k2.6 only accepts temperature=1; param kept for API compat
) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Attempt 1
    args, raw, usage = await _call_kimi(messages, tool_schema)
    _log_agent_call(run_id, call_type, settings.kimi_model, messages, raw, args, usage,
                    valid=(args is not None), error="No tool call in response" if args is None else None)
    if args is not None:
        return args
    logger.warning("Kimi attempt 1: no valid tool call — retrying")

    # Attempt 2
    args, raw, usage = await _call_kimi(messages, tool_schema)
    _log_agent_call(run_id, call_type, settings.kimi_model, messages, raw, args, usage,
                    valid=(args is not None), error="No tool call after retry" if args is None else None)
    if args is not None:
        return args
    logger.warning("Kimi attempt 2: still no valid tool call — activating Claude fallback")

    # Attempt 3: Claude Sonnet 4.6 fallback
    if claude:
        args, raw, usage = await _call_claude_fallback(messages, tool_schema)
        _log_agent_call(run_id, call_type, "claude-sonnet-4-6", messages, raw, args, usage,
                        valid=(args is not None), error="Claude fallback also returned no tool call" if args is None else None)
        if args is not None:
            logger.info("Claude fallback succeeded")
            return args

    raise DiagnosisValidationError(
        "All 3 model attempts (Kimi x2 + Claude fallback) returned no valid tool call. "
        "Check agent_calls table for raw responses."
    )
