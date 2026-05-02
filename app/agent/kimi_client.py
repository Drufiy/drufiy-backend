import json
import logging
import time

from openai import AsyncOpenAI

from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)


# ── Client setup ─────────────────────────────────────────────────────────────

kimi = AsyncOpenAI(
    api_key=settings.kimi_api_key,
    base_url=settings.kimi_base_url,
    timeout=120.0,
)

deepseek = (
    AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=90.0,
    )
    if settings.deepseek_api_key
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


def _usage_from_response(response, latency_ms: int) -> dict:
    return {
        "input_tokens": response.usage.prompt_tokens if response.usage else None,
        "output_tokens": response.usage.completion_tokens if response.usage else None,
        "latency_ms": latency_ms,
    }


def _merge_usage(*usages: dict) -> dict:
    input_tokens = 0
    output_tokens = 0
    total_latency = 0
    saw_input = False
    saw_output = False

    for usage in usages:
        if not usage:
            continue
        if usage.get("input_tokens") is not None:
            input_tokens += usage["input_tokens"]
            saw_input = True
        if usage.get("output_tokens") is not None:
            output_tokens += usage["output_tokens"]
            saw_output = True
        total_latency += usage.get("latency_ms") or 0

    return {
        "input_tokens": input_tokens if saw_input else None,
        "output_tokens": output_tokens if saw_output else None,
        "latency_ms": total_latency,
    }


async def _call_kimi_reasoning(messages: list):
    """
    First Kimi call: allow thinking and free-form analysis before forcing a tool call.
    Returns (reasoning_text_or_none, raw_content, usage_info).
    """
    start = time.time()
    try:
        response = await kimi.chat.completions.create(
            model=settings.kimi_model,
            messages=messages,
            max_tokens=4000,
            temperature=0.2,
            extra_body={"thinking": {"type": "enabled", "budget_tokens": 2000}},
        )
    except Exception as e:
        err_str = str(e)
        if any(code in err_str for code in ["402", "429", "503", "insufficient"]):
            logger.warning(
                f"Kimi reasoning call recoverable error ({e.__class__.__name__}): "
                f"{err_str[:200]} — will try fallback"
            )
            return None, "", {"latency_ms": int((time.time() - start) * 1000)}
        raise

    latency_ms = int((time.time() - start) * 1000)
    msg = response.choices[0].message
    reasoning = msg.content or ""
    return reasoning, reasoning, _usage_from_response(response, latency_ms)


async def _call_kimi_structured(messages: list, tool_schema: dict):
    """
    Returns (parsed_args_or_none, raw_content, usage_info).
    Handles 402 (credit exhausted) gracefully — returns None so DeepSeek fallback fires.

    Note: kimi-k2.6 with thinking disabled requires exactly temperature=0.6.
    Extended thinking is disabled because it is incompatible with forced tool_choice.
    """
    start = time.time()
    try:
        response = await kimi.chat.completions.create(
            model=settings.kimi_model,
            messages=messages,
            tools=[{"type": "function", "function": tool_schema}],
            tool_choice={"type": "function", "function": {"name": tool_schema["name"]}},
            temperature=0.6,   # required when thinking is disabled on kimi-k2.6
            max_tokens=8000,
            extra_body={"thinking": {"type": "disabled"}},  # disable CoT thinking — incompatible with forced tool_choice
        )
    except Exception as e:
        # 402 insufficient credits, 429 rate limit, 503 provider down — all recoverable
        err_str = str(e)
        if any(code in err_str for code in ["402", "429", "503", "insufficient"]):
            logger.warning(f"Kimi API recoverable error ({e.__class__.__name__}): {err_str[:200]} — will try fallback")
            return None, "", {"latency_ms": int((time.time() - start) * 1000)}
        raise

    latency_ms = int((time.time() - start) * 1000)
    usage = _usage_from_response(response, latency_ms)

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


async def _call_kimi(messages: list, tool_schema: dict):
    """
    Two-step Kimi flow:
    1. Run a reasoning pass with thinking enabled.
    2. Feed that analysis back into a forced tool call with thinking disabled.
    """
    reasoning, reasoning_raw, reasoning_usage = await _call_kimi_reasoning(messages)
    if not reasoning:
        return None, reasoning_raw, reasoning_usage

    structured_messages = [
        *messages,
        {"role": "assistant", "content": f"My analysis:\n{reasoning}"},
        {
            "role": "user",
            "content": "Now submit your structured diagnosis using the tool. Follow the system instructions exactly.",
        },
    ]
    args, structured_raw, structured_usage = await _call_kimi_structured(structured_messages, tool_schema)
    combined_raw = json.dumps(
        {
            "reasoning": reasoning_raw,
            "structured_response": structured_raw,
        }
    )
    return args, combined_raw, _merge_usage(reasoning_usage, structured_usage)


async def _call_openai_compatible_fallback(
    client: AsyncOpenAI,
    model: str,
    messages: list,
    tool_schema: dict,
    label: str,
) -> tuple:
    """
    Generic fallback for any OpenAI-compatible endpoint.
    Returns (parsed_args_or_none, raw_content, usage_info).
    """
    if not client:
        return None, "", {}
    start = time.time()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=[{"type": "function", "function": tool_schema}],
            tool_choice={"type": "function", "function": {"name": tool_schema["name"]}},
            max_tokens=8000,
            temperature=0.2,
        )
    except Exception as e:
        logger.error(f"{label} fallback failed: {e}")
        return None, str(e)[:200], {"latency_ms": int((time.time() - start) * 1000)}

    latency_ms = int((time.time() - start) * 1000)
    usage = {
        "input_tokens": response.usage.prompt_tokens if response.usage else None,
        "output_tokens": response.usage.completion_tokens if response.usage else None,
        "latency_ms": latency_ms,
    }

    msg = response.choices[0].message

    if msg.tool_calls:
        try:
            args = json.loads(msg.tool_calls[0].function.arguments)
            return args, msg.tool_calls[0].function.arguments, usage
        except json.JSONDecodeError:
            raw = msg.tool_calls[0].function.arguments
            extracted = _extract_json_from_prose(raw)
            if extracted:
                return extracted, raw, usage
            return None, raw, usage

    prose = msg.content or ""
    extracted = _extract_json_from_prose(prose)
    if extracted:
        return extracted, prose, usage

    return None, prose, usage


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
    temperature: float = 0.6,   # 0.6 required when thinking disabled on kimi-k2.6
) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Attempt 1: Kimi K2.6
    args, raw, usage = await _call_kimi(messages, tool_schema)
    _log_agent_call(run_id, call_type, settings.kimi_model, messages, raw, args, usage,
                    valid=(args is not None), error="No tool call in response" if args is None else None)
    if args is not None:
        return args
    logger.warning("Kimi attempt 1: no valid tool call — retrying")

    # Attempt 2: Kimi retry
    args, raw, usage = await _call_kimi(messages, tool_schema)
    _log_agent_call(run_id, call_type, settings.kimi_model, messages, raw, args, usage,
                    valid=(args is not None), error="No tool call after retry" if args is None else None)
    if args is not None:
        return args
    logger.warning("Kimi attempt 2: still no valid tool call — trying DeepSeek fallback")

    # Attempt 3: DeepSeek fallback
    if deepseek:
        args, raw, usage = await _call_openai_compatible_fallback(
            deepseek, settings.deepseek_model, messages, tool_schema, "DeepSeek"
        )
        _log_agent_call(run_id, call_type, settings.deepseek_model, messages, raw, args, usage,
                        valid=(args is not None), error="DeepSeek returned no tool call" if args is None else None)
        if args is not None:
            logger.info("DeepSeek fallback succeeded")
            return args

    raise DiagnosisValidationError(
        "All 3 model attempts (Kimi x2 + DeepSeek) returned no valid tool call. "
        "Check agent_calls table for raw responses."
    )
