import logging

from pydantic import ValidationError

from app.agent.kimi_client import DiagnosisValidationError, call_with_tool
from app.agent.schemas import Diagnosis

logger = logging.getLogger(__name__)

DIAGNOSIS_TOOL = {
    "name": "submit_diagnosis",
    "description": "Submit a structured diagnosis and fix for a CI/CD failure. Use ONLY this tool to respond.",
    "parameters": {
        "type": "object",
        "required": [
            "problem_summary", "root_cause", "fix_description", "fix_type",
            "confidence", "is_flaky_test", "files_changed", "category", "logs_truncated_warning",
        ],
        "properties": {
            "problem_summary": {
                "type": "string",
                "description": "One sentence (max 500 chars): what failed. Be specific.",
            },
            "root_cause": {
                "type": "string",
                "description": "2-4 sentences explaining WHY it failed, tracing from the symptom to the cause.",
            },
            "fix_description": {
                "type": "string",
                "description": "Plain-English description of what needs to change and why it resolves the failure.",
            },
            "fix_type": {
                "type": "string",
                "enum": ["safe_auto_apply", "review_recommended", "manual_required"],
                "description": "safe_auto_apply: deterministic, low-risk. review_recommended: logic changes or 70-95% confidence. manual_required: needs env vars, secrets, infra access, or you cannot determine the fix.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the diagnosis as a float 0.0-1.0.",
            },
            "is_flaky_test": {
                "type": "boolean",
                "description": "True if this appears to be a flaky test (intermittent, timing, network-dependent).",
            },
            "category": {
                "type": "string",
                "enum": ["code", "workflow_config", "dependency", "environment", "flaky_test", "unknown"],
            },
            "logs_truncated_warning": {
                "type": "boolean",
                "description": "True if the logs appear truncated before the actual error.",
            },
            "files_changed": {
                "type": "array",
                "description": "Files to modify. Empty array if fix_type='manual_required'.",
                "items": {
                    "type": "object",
                    "required": ["path", "new_content", "explanation"],
                    "properties": {
                        "path": {"type": "string"},
                        "new_content": {"type": "string", "description": "COMPLETE new file content. NOT a diff."},
                        "explanation": {"type": "string"},
                    },
                },
            },
        },
    },
}

SYSTEM_PROMPT = """You are an expert CI/CD diagnostician. You have debugged ten thousand GitHub Actions failures across Node, Python, Go, Rust, Ruby, Java, Docker, and multi-language monorepos. You do one thing: look at failure logs, find the single root cause, and propose a minimal, safe fix.

# How to read CI logs

Logs come to you as concatenated output from one or more failed GitHub Actions jobs. The format is:

  === {filename} ===
  {log contents}
  ...

Logs may be truncated to the last 80,000 characters. The most recent content (the actual failure) is usually near the END. Setup output at the top is almost never the cause.

If the log ends mid-stack-trace or only shows setup steps, set `logs_truncated_warning: true` and lower your confidence.

# How to find the root cause

1. Scan for the LAST error-level line. That is the symptom.
2. Work backwards from the symptom to find what triggered it.
3. Distinguish between: APP CODE bug, WORKFLOW misconfig, DEPENDENCY issue, ENVIRONMENT issue, FLAKY TEST.
4. DO NOT confuse cascading errors with the root cause.

# Flaky test detection

Set is_flaky_test=true when you see: timeout on a network call, non-deterministic values in assertions, race condition language, "connection reset", "EAI_AGAIN". When is_flaky_test=true: fix_type MUST be 'manual_required', files_changed MUST be empty.

# Output a complete new_content, never a diff

Return the ENTIRE new file contents. Change only what's necessary. Do NOT refactor, reformat, or improve unrelated code. Preserve existing style and indentation.

# When to refuse

Set fix_type='manual_required' when: environment issue (secret missing, permissions denied), insufficient context, fix requires >5 files, security-sensitive code, logs_truncated_warning=true AND cannot identify error confidently.

# fix_type decision rules (strict)

safe_auto_apply REQUIRES ALL of: confidence >= 0.85, is_flaky_test == false, category in ['workflow_config', 'dependency'], single atomic change, no business logic modified.

# Output

You MUST respond by calling the submit_diagnosis function. Do not output any text outside the function call.
"""


async def diagnose_failure(
    logs: str,
    repo_full_name: str,
    commit_message: str,
    workflow_name: str,
    iteration: int = 1,
    previous_diagnosis: dict | None = None,
    run_id: str | None = None,
) -> Diagnosis:
    if len(logs) > 100_000:
        logger.warning(f"Logs exceed 100K chars, truncating for run {run_id}")
        logs = "... [earlier logs truncated] ...\n" + logs[-80_000:]

    user_prompt = _build_user_prompt(logs, repo_full_name, commit_message, workflow_name, iteration, previous_diagnosis)
    call_type = "iteration_2_diagnosis" if iteration == 2 else "diagnosis"

    raw_args = await call_with_tool(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tool_schema=DIAGNOSIS_TOOL,
        run_id=run_id,
        call_type=call_type,
        temperature=0.1,
    )

    try:
        diagnosis = Diagnosis(**raw_args)
    except ValidationError as e:
        logger.error(f"Kimi tool call failed Pydantic validation: {e}")
        raise DiagnosisValidationError(f"Schema validation failed: {e}")

    # Business rule overrides
    if diagnosis.is_flaky_test and diagnosis.fix_type != "manual_required":
        logger.warning("Flaky test with non-manual fix_type — overriding to manual_required")
        diagnosis = diagnosis.model_copy(update={"fix_type": "manual_required", "files_changed": []})

    if diagnosis.confidence < 0.6 and diagnosis.fix_type == "safe_auto_apply":
        logger.warning(f"Low confidence ({diagnosis.confidence}) with safe_auto_apply — downgrading")
        diagnosis = diagnosis.model_copy(update={"fix_type": "review_recommended"})

    return diagnosis


def _build_user_prompt(logs, repo_full_name, commit_message, workflow_name, iteration, previous_diagnosis):
    base = f"""REPOSITORY: {repo_full_name}
WORKFLOW: {workflow_name}
COMMIT MESSAGE: {commit_message}

CI LOGS:
---
{logs}
---
"""
    if iteration == 2 and previous_diagnosis:
        return base + f"""

IMPORTANT: This is iteration 2. The previous fix attempt FAILED CI. Here is what was tried:

PREVIOUS DIAGNOSIS:
problem_summary: {previous_diagnosis.get('problem_summary')}
root_cause: {previous_diagnosis.get('root_cause')}
fix_description: {previous_diagnosis.get('fix_description')}
files_changed: {previous_diagnosis.get('files_changed')}

The new logs above are from the fix branch. Identify what the previous diagnosis got wrong and propose a new fix. If you cannot confidently fix this on iteration 2, set fix_type='manual_required'.
"""
    return base
