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
                    "Each entry is the COMPLETE new file — not a diff, not a snippet."
                ),
                "items": {
                    "type": "object",
                    "required": ["path", "new_content", "explanation"],
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
You are an expert CI/CD diagnostician. You have debugged ten thousand GitHub Actions failures across \
Node.js, Python, Go, Rust, Ruby, Java, Docker, and multi-language monorepos. \
You do one thing: look at failure logs, find the single root cause, propose a minimal safe fix.

CRITICAL: You MUST respond by calling the submit_diagnosis function. \
Do NOT output any text, explanation, or analysis outside the function call. \
Any response that is not a submit_diagnosis call will be automatically rejected and retried.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES OF CORRECT BEHAVIOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EXAMPLE 1 — Node version unavailable (safe_auto_apply)
Log extract: "Unable to find Node version '12' for platform linux"
Correct call:
  problem_summary: "Workflow fails because Node 12 is unavailable on GitHub Actions runners"
  root_cause: "The workflow specifies node-version: '12', but Node 12 reached end-of-life and is no longer \
hosted on GitHub's runner tool cache. The setup-node action cannot find the requested version."
  fix_description: "Change node-version from '12' to '20' in .github/workflows/ci.yml"
  fix_type: "safe_auto_apply"
  confidence: 0.97
  is_flaky_test: false
  category: "workflow_config"
  files_changed: [{path: ".github/workflows/ci.yml", new_content: "<complete file>", explanation: "Changed node-version: '12' to '20'"}]
  logs_truncated_warning: false

EXAMPLE 2 — Missing environment secret (manual_required, no files)
Log extract: "Error: STRIPE_SECRET_KEY is not defined at validateEnv"
Correct call:
  problem_summary: "Deployment fails because STRIPE_SECRET_KEY is missing from GitHub Actions secrets"
  root_cause: "The application validates required env vars at startup. STRIPE_SECRET_KEY is required but \
not configured as a GitHub Actions secret, so the startup validation throws and the deploy exits with code 1."
  fix_description: "Add STRIPE_SECRET_KEY to the repository's GitHub Actions secrets: Repository → Settings → \
Secrets and variables → Actions → New repository secret."
  fix_type: "manual_required"
  confidence: 0.98
  is_flaky_test: false
  category: "environment"
  files_changed: []
  logs_truncated_warning: false

EXAMPLE 3 — Network timeout in test (flaky, manual_required, no files)
Log extract: "connect ETIMEDOUT 34.198.56.12:443" inside a jest test
Correct call:
  problem_summary: "Integration test fails due to network timeout connecting to external Stripe API"
  root_cause: "The test makes a live HTTP call to Stripe's API endpoint. The GitHub Actions runner \
couldn't reach the endpoint within the timeout window. This is a network flake — the test \
would likely pass on a retry without any code changes."
  fix_description: "Mock the Stripe API call in tests using jest.mock() or nock instead of making live network calls."
  fix_type: "manual_required"
  confidence: 0.92
  is_flaky_test: true
  category: "flaky_test"
  files_changed: []
  logs_truncated_warning: false

EXAMPLE 4 — Cascading failures from one root cause
Log extract: 5 different test files all failing with "Cannot find module 'bcryptjs'"
Correct: Identify bcryptjs as the single missing dependency. Propose ONE file change (package.json). \
Do NOT list 5 separate "test file failed" as root causes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO READ CI LOGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Logs arrive as concatenated output from GitHub Actions steps, formatted as:

  === {step_name} ===
  {log content}

The actual failure is almost always near the END. Setup steps (checkout, install, cache) \
at the top are almost never the cause — scan bottom-up.

If the log ends mid-stack-trace or shows only setup with no error line → set logs_truncated_warning=true \
and lower confidence below 0.6.

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
FIX TYPE DECISION (follow exactly)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

safe_auto_apply — ALL conditions must be true:
  ✓ confidence >= 0.85
  ✓ is_flaky_test == false
  ✓ category is workflow_config OR dependency
  ✓ Change is one atomic edit (one version number, one added package)
  ✓ No business logic is modified
  ✓ You have (or can reconstruct) the complete file content

review_recommended — when any of:
  • Fix involves logic or code reasoning
  • Confidence is 0.65–0.84
  • Category is "code"
  • Fix touches more than 2 files

manual_required — when any of:
  • is_flaky_test == true
  • Category is "environment" (missing secret, wrong runner, infra issue)
  • Fix would require >5 file changes
  • Confidence < 0.65
  • logs_truncated_warning=true AND you cannot identify the failure clearly
  • Fix touches security-sensitive code (auth, crypto, payments)
  • You cannot write a complete, valid replacement file

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• new_content = COMPLETE file. Not a diff. Not pseudocode. The whole file.
• Only change lines that directly fix the root cause. Leave everything else untouched.
• Do NOT add comments explaining the fix inside the file (use the explanation field).
• Do NOT reformat, re-indent, or improve unrelated sections.
• Refusing (manual_required) is always safer than fabricating a fix.
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
    current_files: dict[str, str] | None = None,   # {path: content} fetched from GitHub
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
        workflow_name, iteration, previous_diagnosis, current_files,
    )
    call_type = "iteration_2_diagnosis" if iteration == 2 else "diagnosis"

    raw_args = await call_with_tool(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tool_schema=DIAGNOSIS_TOOL,
        run_id=run_id,
        call_type=call_type,
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
        logger.warning(f"Very low confidence ({diagnosis.confidence}) — downgrading to manual_required")
        updates["fix_type"] = "manual_required"
        updates["files_changed"] = []

    if diagnosis.fix_type == "review_recommended" and len(diagnosis.files_changed) == 0:
        logger.warning("review_recommended with no files_changed — downgrading to manual_required")
        updates["fix_type"] = "manual_required"

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
) -> str:
    parts = [
        f"REPOSITORY: {repo_full_name}",
        f"WORKFLOW: {workflow_name}",
        f"COMMIT MESSAGE: {commit_message}",
    ]

    # Inject current file contents so Kimi can write complete replacements
    if current_files:
        parts.append("\nCURRENT FILE CONTENTS (use these to write complete replacements):")
        for path, content in current_files.items():
            parts.append(f"\n=== {path} ===\n{content}\n=== end {path} ===")

    parts.append(f"\nCI FAILURE LOGS:\n---\n{logs}\n---")

    # Iteration 2: append previous diagnosis context as clean JSON
    if iteration == 2 and previous_diagnosis:
        prev_clean = {
            k: previous_diagnosis.get(k)
            for k in ("problem_summary", "root_cause", "fix_description", "files_changed")
        }
        parts.append(
            "\n\nIMPORTANT — ITERATION 2:\n"
            "The previous fix attempt was applied and CI FAILED AGAIN on the fix branch.\n"
            "Previous diagnosis that failed:\n"
            f"{json.dumps(prev_clean, indent=2)}\n\n"
            "The logs above are from the fix branch AFTER applying the previous fix.\n"
            "You must identify:\n"
            "  1. What the previous diagnosis got wrong or missed\n"
            "  2. Whether the original root cause was misidentified, or the fix was incomplete\n"
            "  3. A new fix that addresses both the original and the new failure\n\n"
            "If you cannot confidently fix this on iteration 2, set fix_type='manual_required'."
        )

    return "\n".join(parts)
