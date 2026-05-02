import json
import logging
import re

from pydantic import ValidationError

from app.agent.kimi_client import DiagnosisValidationError, call_with_tool
from app.agent.schemas import Diagnosis

logger = logging.getLogger(__name__)


# ── Tool schema ───────────────────────────────────────────────────────────────

DIAGNOSIS_TOOL = {
    "name": "submit_diagnosis",
    "description": (
        "Submit a structured diagnosis and fix for a CI/CD failure. "
        "This is the ONLY valid way to respond. You MUST call this function. "
        "Responding with plain text instead of calling this function will cause your response to be rejected."
    ),
    "parameters": {
        "type": "object",
        "required": [
            "problem_summary", "root_cause", "fix_description", "fix_type",
            "confidence", "is_flaky_test", "files_changed", "category", "logs_truncated_warning",
        ],
        "properties": {
            "problem_summary": {
                "type": "string",
                "description": (
                    "One sentence (max 500 chars): what specifically failed. "
                    "'Tests failed' is not acceptable. "
                    "'test_auth.py::test_login failed because module jsonwebtoken not found' is."
                ),
            },
            "root_cause": {
                "type": "string",
                "description": (
                    "2-4 sentences: WHY it failed, tracing symptom → cause. "
                    "Reference specific log lines. "
                    "Do NOT list cascading failures — identify the single root cause."
                ),
            },
            "fix_description": {
                "type": "string",
                "description": (
                    "Plain English: what needs to change and why it fixes the failure. "
                    "No code here — code goes in files_changed."
                ),
            },
            "fix_type": {
                "type": "string",
                "enum": ["safe_auto_apply", "review_recommended", "manual_required"],
                "description": (
                    "safe_auto_apply: ONLY if confidence>=0.85 AND category in [workflow_config, dependency] "
                    "AND change is single atomic edit AND no business logic modified. "
                    "review_recommended: logic changes or 70-95% confidence. "
                    "manual_required: env vars, secrets, infra, security-sensitive code, or >5 files."
                ),
            },
            "confidence": {
                "type": "number",
                "description": (
                    "Float 0.0-1.0. Reflects certainty about BOTH the diagnosis AND the completeness "
                    "of the proposed fix. If you cannot see the current file contents to write a complete "
                    "replacement, confidence must be below 0.85 even if you know the problem. "
                    "0.9-1.0: seen this exact pattern 100s of times (wrong Node version, obvious typo). "
                    "0.7-0.89: confident but fix touches logic. "
                    "0.5-0.69: plausible but uncertain. "
                    "<0.5: speculating."
                ),
            },
            "is_flaky_test": {
                "type": "boolean",
                "description": (
                    "True if failure is intermittent/timing/network-dependent. "
                    "When true: fix_type MUST be manual_required, files_changed MUST be empty."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["code", "workflow_config", "dependency", "environment", "flaky_test", "unknown"],
                "description": (
                    "code: app code bug. workflow_config: .github/workflows/*.yml wrong. "
                    "dependency: package.json/requirements.txt/go.mod issue. "
                    "environment: missing secret/env var/infra issue. "
                    "flaky_test: intermittent test. unknown: cannot determine."
                ),
            },
            "logs_truncated_warning": {
                "type": "boolean",
                "description": "True if log ends mid-stack-trace or only shows setup with no error line.",
            },
            "files_changed": {
                "type": "array",
                "description": (
                    "Files to modify. MUST be empty [] if fix_type=manual_required. "
                    "MUST have at least one entry if fix_type=safe_auto_apply or review_recommended. "
                    "Each entry must include either the COMPLETE new file content or a unified diff patch."
                ),
                "items": {
                    "type": "object",
                    "required": ["path", "explanation"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "File path relative to repo root. Forward slashes. "
                                "MUST NOT start with '/' or contain '..'. "
                                "Example: '.github/workflows/ci.yml', 'package.json', 'src/auth.py'"
                            ),
                        },
                        "new_content": {
                            "type": "string",
                            "description": (
                                "The COMPLETE new content of this file as it should exist on disk. "
                                "NOT a diff. NOT a snippet. The entire file. "
                                "Change ONLY what's needed — preserve style, indentation, unrelated code."
                            ),
                        },
                        "patch": {
                            "type": "string",
                            "description": (
                                "Optional unified diff patch for a surgical edit. "
                                "Prefer this when current file contents are provided in the prompt."
                            ),
                        },
                        "explanation": {
                            "type": "string",
                            "description": "1-2 sentences: what specifically changed and why.",
                        },
                    },
                },
            },
        },
    },
}


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert CI/CD auto-repair agent. You have debugged ten thousand GitHub Actions failures \
across Node.js, Python, Go, Rust, Ruby, Java, Docker, and multi-language monorepos. \
Your job: find the root cause, produce the fix. Lean toward fixing — an uncertain fix the user \
can review is more valuable than a dead-end "manual_required".

CRITICAL: You MUST respond by calling the submit_diagnosis function. \
Do NOT output any text outside the function call. \
Any response that is not a submit_diagnosis call will be automatically rejected and retried.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXES YOU MUST ATTEMPT (always produce files_changed for these)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

These patterns are always auto-fixable. Never return manual_required for them:

• F821 / NameError / undefined name → define the missing name or add the correct import.
  E.g., "NameError: name 'helper' is not defined" → add `from module import helper` or stub it.

• SyntaxError missing colon / bracket / comma → fix the exact punctuation.
  E.g., "SyntaxError: expected ':'" → add the missing colon after the if/def/class.

• Deliberate failing tests (assert 1 == 2, assert False, raise Exception("TODO")) →
  mark with @pytest.mark.skip(reason="Skipped by Drufiy — needs implementation") \
  or comment them out. These are placeholder tests, not real failures.

• ModuleNotFoundError / ImportError for a known package →
  add to requirements.txt / package.json. If the module name in the import path is wrong, \
  fix the import path. If it's a missing package, add it to the dependency file.

• Type mismatch in TypeScript (TS2345, TS2322) → add type annotation or cast.

• Node version unavailable → update node-version in the workflow file.

• Python version unavailable → update python-version in the workflow file.

• Missing step in workflow (e.g., `pip install` missing before pytest) → add the step.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXES YOU MUST NOT ATTEMPT (return manual_required, files_changed=[])
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Anything in auth/, payments/, crypto/ paths — security-sensitive, human must review.
• Database migrations — schema changes require human validation.
• Fixes that touch >5 files — too broad, surface for manual review.
• Missing environment secrets (STRIPE_KEY, API_KEY, etc.) — cannot be fixed in code.
• Anything requiring access to external services or credentials.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILE CONTENT RULES — READ CAREFULLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When writing new_content or patch for a file, you MUST follow these rules without exception:

1. PRESERVE ALL UNRELATED CODE. Every function, class, import, and variable that \
   existed in the file and is NOT the cause of the failure MUST remain unchanged. \
   Do NOT delete, truncate, or simplify them.

2. SURGICAL EDITS ONLY. If the error is on line 10, change line 10. \
   Lines 1-9 and 11+ stay identical. The output file should be nearly the same \
   length as the input file.

3. NEVER STRIP A FILE DOWN. If the original file has 40 lines, your new_content \
   must have ~40 lines. A fix that produces a 5-line file from a 40-line file is \
   WRONG — you deleted working code.

4. INCLUDE ALL IMPORTS. Do not remove any import statement that was in the original \
   file unless that import itself is the cause of the error.

5. CHECK YOUR OUTPUT. Before submitting, mentally verify: does the new_content or patch \
   preserve every function/class from the original that isn't broken? If not, add \
   them back.

6. PREFER PATCHES FOR SMALL CHANGES. If the current file contents are visible and you only \
   need a surgical edit, return a unified diff in patch instead of rewriting the entire file.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEW-SHOT EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EXAMPLE 1 — F821 undefined name (safe_auto_apply)
Log: "NameError: name 'calculate_total' is not defined"
  fix_type: "safe_auto_apply", confidence: 0.92, category: "code"
  files_changed: [{path: "src/billing.py", new_content: "<complete file with calculate_total defined or imported>"}]

EXAMPLE 2 — Deliberate failing test (safe_auto_apply)
Log: "AssertionError: assert False" in test_placeholder.py line 12
  fix_type: "safe_auto_apply", confidence: 0.95, category: "code"
  files_changed: [{path: "tests/test_placeholder.py", new_content: "<complete file with @pytest.mark.skip added>"}]

EXAMPLE 3 — Missing import (safe_auto_apply)
Log: "ModuleNotFoundError: No module named 'requests'"
  fix_type: "safe_auto_apply", confidence: 0.97, category: "dependency"
  files_changed: [{path: "requirements.txt", new_content: "<complete requirements.txt with requests added>"}]

EXAMPLE 4 — Node version unavailable (safe_auto_apply)
Log: "Unable to find Node version '12' for platform linux"
  fix_type: "safe_auto_apply", confidence: 0.97, category: "workflow_config"
  files_changed: [{path: ".github/workflows/ci.yml", new_content: "<complete file with node-version: '20'>"}]

EXAMPLE 5 — Missing environment secret (manual_required)
Log: "Error: STRIPE_SECRET_KEY is not defined"
  fix_type: "manual_required", confidence: 0.98, category: "environment"
  files_changed: []
  fix_description: "Add STRIPE_SECRET_KEY to GitHub Actions secrets: Settings → Secrets → New secret."

EXAMPLE 6 — Network timeout / flaky test
Log: "connect ETIMEDOUT 34.198.56.12:443" in jest test
  fix_type: "manual_required", is_flaky_test: true, category: "flaky_test"
  files_changed: []

EXAMPLE 7 — Ambiguous code bug (review_recommended)
Log: "TypeError: Cannot read property 'user' of undefined" in src/api/auth.ts
  fix_type: "review_recommended", confidence: 0.72, category: "code"
  files_changed: [{path: "src/api/auth.ts", new_content: "<complete file with null check added>"}]

EXAMPLE 9 — TypeScript type mismatch (safe_auto_apply) — CORRECT pattern
Log: "TS2322: Type 'number' is not assignable to type 'string'" in src/lib/utils.ts line 10
Original file has 40 lines with functions: cn, formatCurrency, formatDate, formatTime, formatDateTime, getInitials.
  fix_type: "safe_auto_apply", confidence: 0.95, category: "code"
  files_changed: [{path: "src/lib/utils.ts", new_content: "<ALL 40 lines, only formatCurrency body changed>"}]
  ← CORRECT: all other functions preserved, only the broken return statement fixed.
  ← WRONG would be: new_content with only cn() and nothing else — that deletes 5 working functions.

EXAMPLE 8 — Cascading failures from one root cause
Log: 5 test files failing with "Cannot find module 'bcryptjs'"
  Identify bcryptjs as the root. Propose ONE file change (package.json). \
  Do NOT list 5 separate test failures.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO READ CI LOGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Logs arrive as concatenated output from GitHub Actions steps:

  === {step_name} ===
  {log content}

The actual failure is almost always near the END. Setup steps (checkout, install, cache) \
at the top are almost never the cause — scan bottom-up.

If the log ends mid-stack-trace or shows only setup with no error line → \
set logs_truncated_warning=true and lower confidence below 0.6.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROOT CAUSE RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Find the LAST error-level line. That is the symptom.
2. Work backwards: symptom → what caused it → what caused that.
3. One root cause. Multiple failures with the same cause = the shared cause is root.
4. Categories:
   - workflow_config: fix goes in .github/workflows/*.yml
   - dependency: fix goes in package.json / requirements.txt / go.mod / Cargo.toml
   - code: fix goes in application source files
   - environment: requires adding secrets or fixing infra (cannot be code-fixed)
   - flaky_test: network/timing/non-deterministic — set is_flaky_test=true

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIX TYPE DECISION (default to review_recommended — not manual_required)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The golden rule: ALWAYS produce files_changed unless the fix is in the "MUST NOT ATTEMPT" list. \
If you are uncertain, use review_recommended with a low confidence score — the user will review \
the diff before it's applied. An uncertain fix they can review is better than a dead-end.

safe_auto_apply — ALL must be true:
  ✓ confidence >= 0.85
  ✓ is_flaky_test == false
  ✓ Fix is in the "MUST ATTEMPT" list OR category is workflow_config/dependency
  ✓ Change is ≤2 files, minimal edit
  ✓ No business logic is modified

review_recommended — use this as your DEFAULT when uncertain:
  • Fix involves code logic reasoning (confidence 0.5–0.84)
  • Category is "code" — you can write the fix but aren't 100% sure
  • Fix touches 3–5 files
  • You can write a plausible fix but want human confirmation
  • ALWAYS include files_changed when using review_recommended

manual_required — use sparingly, only when:
  • is_flaky_test == true (network timeouts, timing issues)
  • Category is "environment" (missing secrets, infra issues)
  • Fix would require >5 file changes
  • Fix touches auth/, payments/, crypto/ paths
  • Database migrations
  • You genuinely cannot determine what file to change
  • files_changed MUST be [] for manual_required

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• new_content = COMPLETE file. Not a diff. Not pseudocode. The whole file.
• Only change lines that directly fix the root cause. Leave everything else untouched.
• Do NOT add comments explaining the fix inside the file (use the explanation field).
• Do NOT reformat, re-indent, or improve unrelated sections.
• When in doubt, try review_recommended with your best guess — not manual_required.
"""


# ── Log preprocessor ──────────────────────────────────────────────────────────

# Patterns that indicate an error line worth keeping
_ERROR_RE = re.compile(
    r"(error|fail|exception|traceback|panic|fatal|critical|warn"
    r"|cannot find|not found|does not exist|no such file"
    r"|exit code [^0]|npm err|pip.*error|cargo.*error"
    r"|enoent|eacces|eresolve|econnrefused|etimedout|enotfound"
    r"|syntaxerror|typeerror|referenceerror|importerror|modulenotfounderror"
    r"|✗|✕|FAILED|ERROR|WARN"
    r"|unable to find|unresolved|missing|undefined|undeclared"
    r"|permission denied|access denied|401|403|404 not found"
    r"|invalid|unexpected token|parse error|compilation failed)",
    re.IGNORECASE,
)

_CONTEXT_LINES = 5  # lines of surrounding context to keep around each error line


def _preprocess_logs(raw: str) -> str:
    """
    Reduce verbose CI logs to error-relevant signal.
    Keeps lines matching error patterns + context, drops noisy setup output.
    Typically reduces 50KB → 5-10KB without losing the actual failure.
    """
    if not raw:
        return raw

    # Split into per-step sections on the === markers
    section_pattern = re.compile(r"(?m)^=== .+ ===$")
    splits = section_pattern.split(raw)
    headers = section_pattern.findall(raw)

    # If no section markers, treat entire log as one section
    if not headers:
        return _filter_section_lines(raw)

    result_parts = []
    for i, header in enumerate(headers):
        body = splits[i + 1] if i + 1 < len(splits) else ""
        filtered = _filter_section_lines(body)
        result_parts.append(f"{header}\n{filtered}")

    return "\n\n".join(result_parts)


def _filter_section_lines(section: str) -> str:
    lines = section.splitlines()
    if not lines:
        return section

    # Find indices of error lines
    error_idx: set[int] = set()
    for i, line in enumerate(lines):
        if _ERROR_RE.search(line):
            for j in range(max(0, i - _CONTEXT_LINES), min(len(lines), i + _CONTEXT_LINES + 1)):
                error_idx.add(j)

    if not error_idx:
        # No errors detected — keep last 20 lines (the conclusion of the step)
        return "\n".join(lines[-20:]) if len(lines) > 20 else section

    # Reconstruct with gap markers
    out: list[str] = []
    sorted_idx = sorted(error_idx)
    prev = -1
    for idx in sorted_idx:
        if prev != -1 and idx > prev + 1:
            gap = idx - prev - 1
            out.append(f"... [{gap} line{'s' if gap > 1 else ''} omitted] ...")
        out.append(lines[idx])
        prev = idx

    return "\n".join(out)


# ── Public API ────────────────────────────────────────────────────────────────

async def diagnose_failure(
    logs: str,
    repo_full_name: str,
    commit_message: str,
    workflow_name: str,
    iteration: int = 1,
    previous_diagnosis: dict | None = None,
    run_id: str | None = None,
    commit_sha: str | None = None,
    commit_diff: str | None = None,
    current_files: dict[str, str] | None = None,   # {path: content} fetched from GitHub
    force_fix: bool = False,   # User explicitly authorized: skip manual_required, produce files_changed
    model: str = "auto",
    similar_fixes: list[dict] | None = None,        # Past verified fixes for this repo (RAG context)
) -> Diagnosis:
    """
    Run Kimi K2.6 diagnosis on CI logs. Returns a validated Diagnosis object.
    Raises DiagnosisValidationError if the model cannot produce valid structured output.
    """
    # Truncate extremely long logs before preprocessing
    if len(logs) > 50_000:
        logger.warning(f"Logs exceed 50K chars ({len(logs)} chars), truncating for run {run_id}")
        logs = "... [earlier logs truncated] ...\n" + logs[-40_000:]

    # Preprocess: strip noise, keep error-relevant lines
    preprocessed = _preprocess_logs(logs)
    if len(preprocessed) < len(logs) * 0.9:
        logger.info(
            f"Log preprocessing: {len(logs):,} → {len(preprocessed):,} chars "
            f"({100 * len(preprocessed) // max(len(logs), 1)}% kept) for run {run_id}"
        )

    user_prompt = _build_user_prompt(
        preprocessed, repo_full_name, commit_message,
        workflow_name, iteration, previous_diagnosis, current_files, commit_sha, commit_diff,
        similar_fixes=similar_fixes,
    )

    # Force-fix: user has explicitly authorized — append strong override instruction
    if force_fix:
        user_prompt += (
            "\n\n⚠️ USER OVERRIDE: The user has reviewed the previous diagnosis and explicitly authorized "
            "you to attempt a fix even if uncertain. You MUST produce files_changed. "
            "Do NOT return manual_required — use review_recommended with your best-guess fix. "
            "Even a partial or speculative fix is better than no fix."
        )
        logger.info(f"Force-fix mode enabled for run {run_id}")

    call_type = f"iteration_{iteration}_diagnosis" if iteration > 1 else "diagnosis"
    if force_fix:
        call_type = "force_fix_diagnosis"

    raw_args = await call_with_tool(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tool_schema=DIAGNOSIS_TOOL,
        run_id=run_id,
        call_type=call_type,
        model=model,
    )

    try:
        diagnosis = Diagnosis(**raw_args)
    except ValidationError as e:
        logger.error(f"Kimi tool call failed Pydantic validation for run {run_id}: {e}")
        raise DiagnosisValidationError(f"Schema validation failed: {e}")

    # ── Post-validation business rule overrides ───────────────────────────────
    updates: dict = {}

    if diagnosis.is_flaky_test and diagnosis.fix_type != "manual_required":
        logger.warning(f"Flaky test flagged but fix_type={diagnosis.fix_type} — overriding to manual_required")
        updates["fix_type"] = "manual_required"
        updates["files_changed"] = []

    if diagnosis.confidence < 0.6 and diagnosis.fix_type == "safe_auto_apply":
        logger.warning(f"Low confidence ({diagnosis.confidence}) with safe_auto_apply — downgrading to review_recommended")
        updates["fix_type"] = "review_recommended"

    if diagnosis.confidence < 0.4 and diagnosis.fix_type == "review_recommended":
        # Only downgrade to manual_required for environment/flaky failures — those genuinely can't be
        # auto-fixed. For code/dependency/workflow failures, keep as review_recommended (speculative PR)
        # so the user still gets a reviewable fix attempt rather than a dead-end.
        if diagnosis.category in ("environment", "flaky_test", "unknown"):
            logger.warning(f"Very low confidence ({diagnosis.confidence}) + category={diagnosis.category} — downgrading to manual_required")
            updates["fix_type"] = "manual_required"
            updates["files_changed"] = []
        else:
            logger.info(f"Low confidence ({diagnosis.confidence}) but category={diagnosis.category} — keeping as speculative review_recommended")
            updates["speculative"] = True

    # NOTE: review_recommended/safe_auto_apply ↔ manual_required coercion is now handled
    # automatically by Diagnosis.coerce_fix_type() @model_validator — no need to duplicate here.

    if updates:
        diagnosis = diagnosis.model_copy(update=updates)

    return diagnosis


def _build_user_prompt(
    logs: str,
    repo_full_name: str,
    commit_message: str,
    workflow_name: str,
    iteration: int,
    previous_diagnosis: dict | None,
    current_files: dict[str, str] | None,
    commit_sha: str | None,
    commit_diff: str | None,
    similar_fixes: list[dict] | None = None,
) -> str:
    parts = [
        f"REPOSITORY: {repo_full_name}",
        f"WORKFLOW: {workflow_name}",
        f"COMMIT MESSAGE: {commit_message}",
    ]

    if commit_sha:
        parts.append(f"COMMIT SHA: {commit_sha}")

    # RAG: inject past verified fixes for this repo as few-shot context
    if similar_fixes:
        rag_lines = [
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "PAST VERIFIED FIXES FOR THIS REPO (use these as reference — same patterns may apply)",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for i, fix in enumerate(similar_fixes, 1):
            files_summary = ", ".join(
                f["path"] for f in (fix.get("files_changed") or [])
            ) or "none"
            rag_lines.append(
                f"\nVerified Fix #{i} [{fix.get('category', '?')}] "
                f"(confidence {int((fix.get('confidence') or 0) * 100)}%)"
            )
            rag_lines.append(f"Problem: {fix.get('problem_summary', '')}")
            rag_lines.append(f"Root cause: {fix.get('root_cause', '')[:300]}")
            rag_lines.append(f"Fix: {fix.get('fix_description', '')[:300]}")
            rag_lines.append(f"Files changed: {files_summary}")
        rag_lines.append(
            "\nIf the current failure matches one of the above patterns, apply the same fix approach."
        )
        parts.append("\n".join(rag_lines))

    # Inject current file contents so Kimi can write complete replacements
    if current_files:
        parts.append("\nCURRENT FILE CONTENTS (use these to write complete replacements):")
        for path, content in current_files.items():
            parts.append(f"\n=== {path} ===\n{content}\n=== end {path} ===")

    if commit_diff:
        parts.append(f"\nCOMMIT DIFF (what changed to break CI):\n---\n{commit_diff}\n---")
        parts.append("The fix should likely modify these same files unless the logs clearly point elsewhere.")

    parts.append(f"\nCI FAILURE LOGS:\n---\n{logs}\n---")

    # Follow-up iterations: append previous diagnosis context as clean JSON
    if iteration > 1 and previous_diagnosis:
        prev_clean = {
            k: previous_diagnosis.get(k)
            for k in ("problem_summary", "root_cause", "fix_description", "files_changed")
        }
        parts.append(
            f"\n\nIMPORTANT — FOLLOW-UP ITERATION {iteration}:\n"
            "The previous fix attempt was applied and CI FAILED AGAIN on the fix branch.\n"
            "Previous diagnosis that failed:\n"
            f"{json.dumps(prev_clean, indent=2)}\n\n"
            "The logs above are from the fix branch AFTER applying the previous fix.\n"
            "You must identify:\n"
            "  1. What the previous diagnosis got wrong or missed\n"
            "  2. Whether the original root cause was misidentified, or the fix was incomplete\n"
            "  3. A new fix that addresses both the original and the new failure\n\n"
            f"If you cannot confidently fix this on iteration {iteration}, set fix_type='manual_required'."
        )

    return "\n".join(parts)
