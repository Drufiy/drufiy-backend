import hashlib
import json
import logging
import re
import base64

import httpx

from pydantic import ValidationError

from app.agent.kimi_client import (
    DiagnosisValidationError,
    _args_match_schema,
    _call_kimi_structured,
    call_with_investigation,
    call_with_tool,
)
from app.agent.log_fetcher import _ERROR_RE, _preprocess_logs
from app.agent.repo_memory import RepoMemory
from app.agent.schemas import Diagnosis

logger = logging.getLogger(__name__)

# M3: minimum same-repo/category attempts before historical outcomes are trusted
# enough to cap stated confidence. Below this, the sample is too noisy to act on.
MIN_CALIBRATION_SAMPLES = 4


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
            "required_secrets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "ONLY populate when category='environment'. "
                    "List the EXACT names of every missing secret or env var from the logs "
                    "(e.g. ['STRIPE_SECRET_KEY', 'DATABASE_URL']). "
                    "For common safe defaults (CI=true, NODE_ENV=test, PORT=3000) — "
                    "add them directly to the workflow YAML in files_changed instead. "
                    "Leave empty [] for all other categories."
                ),
            },
            "files_changed": {
                "type": "array",
                "description": (
                    "Files to modify. MUST be empty [] if fix_type=manual_required. "
                    "MUST have at least one entry if fix_type=safe_auto_apply or review_recommended. "
                    "Each entry MUST include 'new_content' with the COMPLETE file. Do NOT use 'patch'."
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
OUTPUT CONCISENESS RULES (strictly enforced)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Be precise and minimal. Do not over-explain. Every token costs money.

• problem_summary: 1 sentence, max 120 chars. Error name + file + line. No filler.
• root_cause: 2-3 sentences max. Symptom → cause → why. No repetition of problem_summary.
• fix_description: 2-3 sentences max. What changes and why it works. No code.
• explanation (per file): 1 sentence. "Added X to Y because Z." Nothing more.
• new_content: Write ONLY what is needed. Do not add comments, blank lines, or \
  reformatting beyond the fix. Keep the file identical except for the changed lines.
• Do NOT repeat the error message verbatim across multiple fields.
• Do NOT write preambles like "Based on the logs..." or "Looking at the error...".

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

• Workflow runs `npm ci` / `npm install` but there is no package.json in the repo →
  The workflow is wrong for the project type. Fix the workflow file. Either:
  (a) Remove the npm steps entirely if the project is a static site (HTML/CSS/JS with no build step), OR
  (b) Add a minimal package.json + lock file if the project legitimately needs npm.
  category: workflow_config, fix_type: safe_auto_apply, files_changed MUST include the workflow file.
  NEVER return manual_required for this — you know exactly what to change.

• Workflow runs `pip install` / `pytest` but there is no requirements.txt / setup.py →
  Same pattern. Fix the workflow to match the actual project structure.
  category: workflow_config, fix_type: safe_auto_apply, files_changed MUST include the workflow file.

• Workflow uses `cache: npm` / `cache: pip` but the lock file (package-lock.json, poetry.lock, etc.)
  doesn't exist → remove the cache option from the workflow step. One-line fix.
  category: workflow_config, fix_type: safe_auto_apply.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL: workflow_config FIXES ALWAYS PRODUCE FILES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If category=workflow_config, you MUST include the fixed workflow file in files_changed.
A workflow_config diagnosis with no files_changed is ALWAYS wrong — the workflow file \
is a text file you can edit directly. Never say "manual action required" for a workflow \
file change you know how to make. Write the corrected workflow YAML and ship it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL: PLATFORM-SPECIFIC DEPENDENCIES (THINK AHEAD)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GitHub Actions CI runs on **Ubuntu Linux** by default. When you fix a CI workflow to \
install dependencies from requirements.txt / package.json / Gemfile, you MUST \
ALSO check the dependency file for platform-specific packages that will fail on Linux.

Known platform-specific Python packages (WILL fail on Ubuntu CI):
  - pyobjc, pyobjc-core, pyobjc-framework-* → macOS only
  - pygetwindow → Windows only
  - pywinauto → Windows only
  - pywin32, win32api, win32com → Windows only
  - AppKit, Foundation → macOS only (pyobjc wrappers)

When you see these, you MUST add PEP 508 environment markers in the SAME PR:
  - pyobjc>=10.0; sys_platform == 'darwin'
  - pygetwindow>=0.0.9; sys_platform == 'win32'
  - pywinauto>=0.6.8; sys_platform == 'win32'

Or use conditional install in the workflow:
  - pip install -r requirements.txt || pip install --ignore-errors -r requirements.txt

THE KEY RULE: When changing `pip install <package>` → `pip install -r requirements.txt`, \
you must ALWAYS read requirements.txt FIRST and include fixes for platform-specific \
packages in the SAME PR. Fix BOTH the workflow AND requirements.txt together. \
A workflow fix that causes a new dependency failure is NOT a fix.

Similarly for Node.js: check package.json for native addons (node-gyp, sharp, canvas) \
that may need system deps on Ubuntu.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ENVIRONMENT FAILURES — REQUIRED_SECRETS EXTRACTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When category=environment (missing secret / env var):
1. Extract EVERY secret name from the logs into required_secrets (e.g. ["STRIPE_KEY", "DATABASE_URL"])
2. For common safe defaults, add them to the workflow YAML instead (no user action needed):
   - NODE_ENV=test, CI=true, PORT=3000, RAILS_ENV=test → add as `env:` in .github/workflows/*.yml
3. For real secrets (API keys, DB passwords) → required_secrets only, files_changed stays []
4. fix_description should tell the user exactly where to add each secret
   (GitHub → Settings → Secrets and variables → Actions → New repository secret)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXES YOU MUST NOT ATTEMPT (return manual_required, files_changed=[])
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Anything in auth/, payments/, crypto/ paths — security-sensitive, human must review.
• Database migrations — schema changes require human validation.
• Fixes that touch >5 files — too broad, surface for manual review.
• Missing environment secrets (STRIPE_KEY, API_KEY, etc.) — cannot be fixed in code.
  → But DO populate required_secrets so the UI can show a 1-click "Add Secret" form.
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

6. ALWAYS USE new_content. Never use the patch field — always provide the complete \
   file content in new_content. Patches are fragile and break on whitespace differences.

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

EXAMPLE 10 — Missing deps + platform-specific packages (THINK AHEAD)
Log: "ModuleNotFoundError: No module named 'httpx'" — CI does `pip install numpy` only
requirements.txt contains: httpx, pyobjc>=10.0, pygetwindow, pywinauto, numpy
CI runs on Ubuntu Linux.
  fix_type: "safe_auto_apply", confidence: 0.95, category: "dependency"
  files_changed: [
    {path: ".github/workflows/ci.yml", new_content: "<workflow with 'pip install -r requirements.txt'>"},
    {path: "requirements.txt", new_content: "<requirements.txt with platform markers added:
      pyobjc>=10.0; sys_platform == 'darwin'
      pygetwindow>=0.0.9; sys_platform == 'win32'
      pywinauto>=0.6.8; sys_platform == 'win32'>"}
  ]
  ← CORRECT: fixes BOTH the install command AND the platform deps in one PR.
  ← WRONG would be: only fixing ci.yml — that causes pyobjc to fail on Ubuntu in the next run.

EXAMPLE 11 — Workflow wrong for project type (MOST IMPORTANT PATTERN)
Log: "npm warn logfile could not be created" + "npm error Could not read package.json"
The repo is a vanilla HTML/CSS/JS static site. No package.json, no package-lock.json.
Workflow runs: setup-node with cache: npm, then npm ci, then npx tsc --noEmit.
  fix_type: "safe_auto_apply", confidence: 0.95, category: "workflow_config"
  files_changed: [
    {path: ".github/workflows/ci.yml", new_content: "name: ci\non: [push, pull_request]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - name: Check HTML\n        run: echo 'Static site — no build step required'"}
  ]
  ← CORRECT: edits the workflow to remove the npm steps entirely. Ships a PR.
  ← WRONG: returning manual_required with no files. You know the fix — write it.
  ← WRONG: saying "add a package.json" when the project doesn't use npm at all.

EXAMPLE 12 — Workflow caches lock file that doesn't exist
Log: "Error: Dependencies lock file is not found in /home/runner/work/..."
Workflow uses setup-node with cache: npm but there is no package-lock.json.
  fix_type: "safe_auto_apply", confidence: 0.97, category: "workflow_config"
  files_changed: [
    {path: ".github/workflows/ci.yml", new_content: "<same workflow but with 'cache: npm' line removed>"}
  ]
  ← One-line fix. Never escalate this to manual_required.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEPENDENCY CONFLICT RESOLUTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When you see dependency version conflicts:
  npm: "ERESOLVE unable to resolve dependency tree", "Could not resolve dependency"
  pip: "ResolutionImpossible", "ERROR: Cannot install X and Y because..."
  yarn: "has unmet peer dependency"

FIX STRATEGY:
1. Read the conflict message carefully — it tells you exactly which packages clash.
2. For npm ERESOLVE: update the conflicting version range in package.json, \
   or add an "overrides" field. Prefer bumping to a compatible version.
3. For pip: adjust version pins in requirements.txt/pyproject.toml to find a compatible set. \
   If pkg A needs X>=2.0 and pkg B needs X<2.0, check if either has a newer release.
4. For peer dependency warnings: add the peer dep explicitly to package.json.
5. ALWAYS provide the complete manifest file in new_content — not just the changed line.
6. These are safe_auto_apply with high confidence when the conflict message is clear.
7. DEPENDENCY CHAIN COMPLETENESS — when a package has known peer or type companions, bump ALL of \
   them together in the SAME package.json edit, matching major versions:
     react ↔ react-dom ↔ @types/react ↔ @types/react-dom
     vue ↔ @vue/compiler-sfc ↔ @vue/runtime-core
     @angular/core ↔ @angular/common ↔ @angular/compiler
   Bumping react to ^18 while leaving @types/react on ^17 is an INCOMPLETE fix — it will pass a \
   naive check but break the type build or runtime. A deterministic guardrail checks major-version \
   alignment across these pairs and downgrades to review_recommended if you miss one, so get it \
   right the first time: read every companion package's current version in the provided file \
   content before writing new_content, and bump every one that's out of sync with the package \
   you're actually fixing — not just the one named in the error message.

EXAMPLE 13b — Peer dependency bump with type packages (safe_auto_apply) — CORRECT pattern
Log: "npm ERR! peer react@'^18.0.0' from react-dom@18.2.0"
package.json (excerpt) BEFORE: react ^17.0.2, react-dom ^18.2.0, @types/react ^17.0.39, @types/react-dom ^17.0.11
  fix_type: "safe_auto_apply", confidence: 0.93, category: "dependency"
  files_changed: [{path: "package.json", new_content: "<complete package.json with react ^18.2.0, react-dom ^18.2.0, @types/react ^18.2.0, @types/react-dom ^18.2.0 — ALL FOUR bumped together>"}]
  ← Bumping only "react" here and leaving @types/react on ^17 would be WRONG — the type packages
    would then disagree with the runtime packages about the React major version.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROOT CAUSE VS SUPPRESSION — when a linter, type checker, or static analyzer fails
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When PHPStan, ESLint, mypy, tsc, or a similar analysis tool fails, there are always
two ways to make the check pass: fix the code the tool is correctly flagging, or
loosen the tool so it stops flagging it. These are NOT equivalent fixes.

FIX STRATEGY — in this order:
1. Read what the tool is ACTUALLY complaining about (the specific type error, the
   specific rule violation) and fix that in the source: add the missing type
   annotation, add the null check, cast the value, add the missing return path.
   This is the real fix — prefer it whenever the change is small and scoped.
2. Only fall back to loosening the tool itself (lowering a PHPStan `level`,
   dropping a tsconfig `strict` flag, disabling an ESLint rule, adding
   `# type: ignore` / `@ts-ignore` / `// eslint-disable`) when the real fix would
   require touching code far outside the scope of this failure, or you genuinely
   cannot determine the correct fix from the available context.
3. If you DO fall back to loosening the check, you MUST say so explicitly and
   honestly in fix_description — state plainly that this suppresses the check
   rather than resolving what it caught, e.g. start the description with
   "Note: this relaxes the analyzer rather than fixing the underlying issue —".
   Do not describe a suppression as if it were a resolution. A confident-sounding
   description that hides what the fix actually does is worse than an honest
   low-confidence one.

EXAMPLE 18 — PHPStan level 9 type error (real fix, PREFERRED)
Log: "PHPStan level 9: Cannot cast mixed to string in functions.php:42"
  fix_type: "safe_auto_apply", confidence: 0.90, category: "code"
  files_changed: [{path: "functions.php", new_content: "<complete file with the mixed value \
explicitly cast/validated at line 42 before use, e.g. (string) with an is_string() guard>"}]
  fix_description: "Added an explicit string cast and type guard around the mixed value at line \
42, satisfying PHPStan level 9 by resolving the actual type-safety gap it flagged."

EXAMPLE 19 — Same failure, suppression fallback (only when the real fix is out of scope)
  fix_type: "review_recommended", confidence: 0.75, category: "workflow_config"
  files_changed: [{path: ".github/workflows/ci.yml", new_content: "<workflow with PHPStan level \
lowered from 9 to 5>"}]
  fix_description: "Note: this relaxes the analyzer rather than fixing the underlying issue — \
lowered PHPStan from level 9 to 5 because the flagged type-safety gaps span many legacy files \
beyond the scope of this CI failure. The 2 underlying type errors in functions.php are still \
unresolved; only the check that was catching them was loosened."
  ← Honest about what it actually did. WRONG would be describing this as "fixed the type errors."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEPLOY / DOCKER FAILURES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When the failure is in a Docker build or deploy step (not test/lint):
  "COPY failed", "RUN pip install ... error", "docker build ... failed"
  "Error: Process completed with exit code 1" in a deploy step

FIX STRATEGY:
1. Read the Dockerfile or docker-compose.yml from the context files.
2. Common fixes: wrong COPY path, missing dependency in RUN install, wrong base image tag.
3. For deploy config failures: check the workflow YAML's deploy step for bad env references.
4. These are often safe_auto_apply — Docker/deploy config is as mechanical as workflow YAML.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MATRIX BUILD FAILURES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When CI uses a strategy matrix (multiple OS/version combos):
  The logs may contain headers like "=== Test (node 18) ===" or "=== build (ubuntu, 3.11) ===".
  The metadata section below may include a "matrix_failures" field listing which combos failed.

FIX STRATEGY:
1. Identify WHICH matrix entry failed — don't assume all of them did.
2. If only one combo failed: the fix is likely version-specific (deprecated API, platform difference).
3. If all combos failed: the fix is likely a code/dependency issue unrelated to the matrix.
4. Fix the code to work across the matrix, OR update the matrix config if the version is unsupported.

EXAMPLE 13 — npm ERESOLVE dependency conflict (safe_auto_apply)
Log: "npm ERR! ERESOLVE unable to resolve dependency tree"
     "npm ERR! peer react@'^17.0.0' from react-dom@17.0.2"
     "npm ERR! Could not resolve dependency: react@18.2.0"
  fix_type: "safe_auto_apply", confidence: 0.90, category: "dependency"
  files_changed: [{path: "package.json", new_content: "<complete package.json with react-dom bumped to ^18.2.0>"}]

EXAMPLE 14 — pip version conflict (safe_auto_apply)
Log: "ERROR: Cannot install django==4.2 and djangorestframework==3.12 because..."
     "djangorestframework 3.12 requires django<4.0"
  fix_type: "safe_auto_apply", confidence: 0.92, category: "dependency"
  files_changed: [{path: "requirements.txt", new_content: "<complete file with djangorestframework>=3.14>"}]

EXAMPLE 15 — Docker COPY failure (safe_auto_apply)
Log: "COPY failed: file not found in build context: ./dist/app.js"
  fix_type: "safe_auto_apply", confidence: 0.90, category: "workflow_config"
  files_changed: [{path: "Dockerfile", new_content: "<complete Dockerfile with corrected COPY path or added build step>"}]

EXAMPLE 16 — Matrix: one version fails (safe_auto_apply)
Log: Node 20 passes, Node 22 fails with "ERR_IMPORT_ASSERTION_TYPE_MISSING"
  fix_type: "safe_auto_apply", confidence: 0.88, category: "code"
  files_changed: [{path: "src/loader.ts", new_content: "<complete file using import attributes syntax>"}]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MASKED / SWALLOWED EXCEPTIONS (READ CAREFULLY — a common wrong-diagnosis trap)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Sometimes the log shows only an AssertionError comparing a returned status/dict/result field \
(e.g. `assert result["status"] == "ok"`, `assert response.status == 200`) with NO underlying \
traceback (no KeyError, TypeError, AttributeError, etc. visible anywhere in the log). This is a \
strong signal that the code under test has a broad `except Exception:` (or equivalent) that \
CAUGHT the real error and returned a generic error status instead of letting it propagate. The \
log only shows the downstream assertion — the actual cause never surfaces in CI output.

WHEN YOU SEE THIS PATTERN:
1. Do NOT pattern-match the surface symptom to the most common cause for that kind of call \
   (e.g. "returns error" → "must be a network/subprocess issue"). That is frequently WRONG — \
   it's guessing based on what the function usually fails at, not what actually happened here.
2. Use fetch_file to read the source of the function that PRODUCES the asserted result.
3. Look for a broad except block and reason about what could throw on the success path INSIDE \
   that try block — a stubbed/mocked dependency missing a method, a bad attribute access on an \
   object that isn't fully initialized in this environment, a closed resource, etc.
4. If you can't be certain after investigating, lower confidence and use review_recommended \
   rather than guessing at high confidence — a wrong high-confidence fix is worse than an \
   honest uncertain one.

EXAMPLE 17 — Masked exception (review_recommended, NOT the surface symptom)
Log: "AssertionError: assert 'error' == 'ok'" in test_run_shell.py, calling run_shell("echo hello")
No traceback, no KeyError/AttributeError visible anywhere in the log.
  WRONG: fix_type: "safe_auto_apply", confidence: 0.85, category: "code",
    fix_description: "Switch create_subprocess_exec to create_subprocess_shell"
    ← This is guessing based on the function name, not the actual failure.
  CORRECT: fetch_file("src/shell.py") reveals `except Exception as e: return {"status": "error"}` \
    wrapping a `logger.debug(...)` call, and the test stubs a fake logger missing `.debug`.
    fix_type: "review_recommended", confidence: 0.65, category: "code",
    root_cause: "run_shell's success path calls logger.debug(), but the test's stub logger has no \
    .debug method, raising AttributeError that is caught by the broad except and returned as \
    status=error. The subprocess call itself works fine."
    files_changed: [{path: "src/shell.py", new_content: "<file with logger.debug removed or guarded>"}]

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
# _preprocess_logs lives in log_fetcher.py now (M3, ROADMAP.md "P1 BUG:
# Failure-blind log truncation") so it runs BEFORE the hard character-count
# truncation on the ZIP-fetch path, instead of after — error lines get a
# chance to survive truncation regardless of where in a huge job's log they
# sit. Still used here (imported above) for non-ZIP-sourced logs, e.g.
# push_handler.py's syntax-check flow.

# ── L1: masked exception detection ──────────────────────────────────────────
# A failure that's just an assertion on a returned status/dict field, with no
# revealing traceback anywhere in the log, means the real exception is likely
# being swallowed by a broad except block. See ROADMAP.md lesson L1.

_MASKED_EXCEPTION_ASSERT_RE = re.compile(
    r"assert(?:ionerror)?\b.*(\[['\"]?\w+['\"]?\]|\.status\b|\.get\(|status\s*(==|!=)|['\"]error['\"]|['\"]ok['\"])",
    re.IGNORECASE,
)
_REVEALING_EXCEPTION_RE = re.compile(
    r"\b(KeyError|AttributeError|TypeError|ValueError|ConnectionError|NullPointerException|"
    r"NoneType|panic:|RuntimeError|OSError|IOError)\b"
)


def _detect_masked_exception_risk(logs: str) -> bool:
    if not logs:
        return False
    has_assert_on_result = bool(_MASKED_EXCEPTION_ASSERT_RE.search(logs))
    has_revealing_traceback = bool(_REVEALING_EXCEPTION_RE.search(logs))
    return has_assert_on_result and not has_revealing_traceback


# ── L2: repeated-hypothesis detection ───────────────────────────────────────
# Fingerprint a failure so iteration N can be compared against iteration N-1.
# Normalizes out volatile details (timestamps, line numbers, addresses, paths)
# so the same underlying failure hashes identically even if surrounding log
# noise differs between runs. See ROADMAP.md lesson L2.

_SIGNATURE_LINE_RE = re.compile(r"(assertionerror|error:|exception|failed|panic:|traceback)", re.IGNORECASE)
_SIGNATURE_SCRUB_RE = re.compile(
    r"(0x[0-9a-fA-F]+|:\d+:\d+|:\d+\b|\bline\s+\d+\b|\b\d{10,}\b|\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"
    r"|/[\w./-]+\.py\b|/[\w./-]+\.ts\b|/[\w./-]+\.js\b)",
    re.IGNORECASE,
)


def compute_error_signature(logs: str) -> str:
    preprocessed = _preprocess_logs(logs or "")
    signature_lines = [line.strip() for line in preprocessed.splitlines() if _SIGNATURE_LINE_RE.search(line)]
    if not signature_lines:
        signature_lines = preprocessed.splitlines()[-10:]
    normalized = "\n".join(_SIGNATURE_SCRUB_RE.sub("", line) for line in signature_lines)
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


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
    repeated_failure: bool = False,   # iteration N failed with the identical error signature as N-1
    model: str = "auto",
    similar_fixes: list[dict] | None = None,        # Legacy: past verified fixes for this repo
    repo_memory: RepoMemory | None = None,          # Structured repo-specific memory context
    investigation_context: dict | None = None,
) -> Diagnosis:
    """
    Run CI log diagnosis via the configured primary model (DeepSeek V4 Pro or Kimi K2.6).
    Returns a validated Diagnosis object.
    Raises DiagnosisValidationError if the model cannot produce valid structured output.
    """
    # M3: the blind "keep the last 40K chars" cut that used to live here is
    # gone — it ran BEFORE preprocessing and was the primary cause of the
    # log-truncation bug (see ROADMAP.md "P1 BUG: Failure-blind log
    # truncation"). _preprocess_logs() is bounded on its own: it only keeps
    # matched error lines + context (or the last 20 lines if none match), so
    # it doesn't need a separate size cap in front of it.
    preprocessed = _preprocess_logs(logs)
    if len(preprocessed) < len(logs) * 0.9:
        logger.info(
            f"Log preprocessing: {len(logs):,} → {len(preprocessed):,} chars "
            f"({100 * len(preprocessed) // max(len(logs), 1)}% kept) for run {run_id}"
        )

    if not _ERROR_RE.search(preprocessed):
        logger.warning(f"Preprocessed logs contain no error signal for run {run_id} — likely incomplete logs")
        raise DiagnosisValidationError(
            "CI logs contain no error output (likely fetched before step logs were archived). "
            "The run will be retried by the reconciler."
        )

    user_prompt = _build_user_prompt(
        preprocessed, repo_full_name, commit_message,
        workflow_name, iteration, previous_diagnosis, current_files, commit_sha, commit_diff,
        similar_fixes=similar_fixes,
        repo_memory=repo_memory,
    )

    # L1: masked exception risk — assertion on a result/status field, no revealing traceback
    if _detect_masked_exception_risk(preprocessed):
        logger.info(f"Masked-exception risk detected for run {run_id} — injecting investigation directive")
        user_prompt += (
            "\n\n⚠️ MASKED EXCEPTION RISK: This failure is an assertion on a returned status/result "
            "value, not a raw traceback. This strongly suggests the real exception is being caught by a "
            "broad except block and converted into an error status/dict, so the true cause never appears "
            "in the logs. Before finalizing your diagnosis, use fetch_file to read the source of the "
            "function that PRODUCES the asserted result and reason about what could throw on its success "
            "path (a stubbed/mocked dependency missing a method, a bad attribute access, a closed "
            "connection, etc.) that gets swallowed. Do not assume the surface symptom is the root cause."
        )

    # L2: repeated hypothesis — iteration N failed with the identical error signature as N-1
    if repeated_failure:
        user_prompt += (
            "\n\n⚠️ REPEATED FAILURE — SAME ERROR AS PREVIOUS ITERATION: The previous fix was applied "
            "and pushed, but CI failed again with the IDENTICAL error signature. This means your "
            "previous root-cause hypothesis was WRONG — the fix did not address the actual problem. "
            "Do NOT propose a variation of the same fix. You must:\n"
            "  1. Explicitly reconsider what could cause this EXACT failure that your previous diagnosis missed.\n"
            "  2. Use fetch_file / search_code to investigate a DIFFERENT part of the codebase than last "
            "time — the function that actually produces the failing output, not just the file you already changed.\n"
            "  3. Consider whether the real error is being swallowed (see the masked-exception guidance above) "
            "or whether a completely different component is responsible.\n"
            "Repeating the same hypothesis will exhaust the remaining retry budget with no progress."
        )
        logger.info(f"Repeated-failure strategy-change directive injected for run {run_id}")

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
    if repeated_failure:
        call_type = f"iteration_{iteration}_repeated_failure_diagnosis"
    if force_fix:
        call_type = "force_fix_diagnosis"

    if investigation_context:
        raw_args = await call_with_investigation(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            diagnosis_tool_schema=DIAGNOSIS_TOOL,
            investigation_tools=INVESTIGATION_TOOLS,
            execute_tool=lambda name, args: _execute_investigation_tool(name, args, investigation_context),
            run_id=run_id,
            call_type=call_type,
            model=model,
        )
    else:
        raw_args = await call_with_tool(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_schema=DIAGNOSIS_TOOL,
            run_id=run_id,
            call_type=call_type,
            model=model,
        )

    # Filter out files with empty/missing content before validation.
    # The model sometimes returns new_content="" for files it couldn't generate —
    # drop those rather than letting one bad file nuke the entire diagnosis.
    if "files_changed" in raw_args and raw_args["files_changed"]:
        valid_files = []
        for fc in raw_args["files_changed"]:
            content = fc.get("new_content") or ""
            if content.strip():
                valid_files.append(fc)
            else:
                logger.warning(f"Dropping file {fc.get('path', '?')} — empty new_content")
        raw_args["files_changed"] = valid_files

    try:
        diagnosis = Diagnosis(**raw_args)
    except ValidationError as e:
        logger.error(f"Kimi tool call failed Pydantic validation for run {run_id}: {e}")
        raise DiagnosisValidationError(f"Schema validation failed: {e}")

    # M3: cap stated confidence against this repo/category's historical track record
    # before the static gates below act on it, so a poor track record can push a
    # diagnosis through the same downgrade path as genuinely low model confidence.
    diagnosis = _recalibrate_confidence(diagnosis, repo_memory, run_id=run_id)

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

    diagnosis = _apply_deterministic_guardrails(diagnosis, preprocessed)
    diagnosis = _check_dependency_chain_completeness(diagnosis)
    diagnosis = _flag_strictness_suppression(diagnosis)
    diagnosis = await _consult_second_opinion(diagnosis, SYSTEM_PROMPT, user_prompt, run_id)

    # M9 (B4): a manual_required code diagnosis is a dead end for the user —
    # no PR, nothing to review, just a status. environment/flaky_test/unknown
    # genuinely can't be auto-attempted (missing secrets, a human judgment
    # call, or not enough signal), but "code" failures almost always have
    # *some* plausible guess available. Retry once with the existing
    # force_fix override (already used by the manual "force fix" button) to
    # get a best-guess attempt instead, marked speculative so the PR is
    # clearly labeled as a starting point to review, not a confident fix.
    # Bounded to exactly one retry via the `not force_fix` guard.
    if diagnosis.fix_type == "manual_required" and diagnosis.category == "code" and not force_fix:
        logger.info(f"manual_required code diagnosis for run {run_id} — retrying once with force_fix")
        retried = await diagnose_failure(
            logs=logs,
            repo_full_name=repo_full_name,
            commit_message=commit_message,
            workflow_name=workflow_name,
            iteration=iteration,
            previous_diagnosis=previous_diagnosis,
            run_id=run_id,
            commit_sha=commit_sha,
            commit_diff=commit_diff,
            current_files=current_files,
            force_fix=True,
            repeated_failure=repeated_failure,
            model=model,
            similar_fixes=similar_fixes,
            repo_memory=repo_memory,
            investigation_context=investigation_context,
        )
        if retried.fix_type != "manual_required":
            return retried.model_copy(update={"speculative": True})
        # Even forced, the model still couldn't produce anything — genuinely
        # nothing to guess at. Keep the original diagnosis, not the retry.
        return diagnosis

    return diagnosis


_BARE_MODULE_RE = re.compile(
    r"(?:Cannot find module|MODULE_NOT_FOUND.*module|ModuleNotFoundError:\s+No module named)\s+['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]{3,})\b\s+(?:is\s+)?(?:not defined|not set|missing|required)",
    # NO re.IGNORECASE — must be SCREAMING_SNAKE_CASE to qualify as a secret name.
    # "all", "npm", "node" etc. must not match.
)
_DOCKER_COPY_SOURCE_RE = re.compile(r"\bCOPY\s+(?:--\S+\s+)*(?P<path>\.?/?[\w./-]+)", re.IGNORECASE)
_DOCKER_STAT_PATH_RE = re.compile(r"\bstat\s+(?P<path>\.?/?[\w./-]+):", re.IGNORECASE)


def _apply_deterministic_guardrails(
    diagnosis: Diagnosis,
    logs: str,
) -> Diagnosis:
    updates: dict = {}

    missing_modules = [m for m in _extract_missing_modules(logs) if _is_bare_package_name(m)]
    if missing_modules and diagnosis.files_changed:
        if not _changes_dependency_or_workflow(diagnosis):
            updates["fix_type"] = "review_recommended"
            updates["speculative"] = True
            updates["fix_description"] = (
                f"{diagnosis.fix_description}\n\n"
                "Guardrail: the logs show a missing package/module "
                f"({', '.join(sorted(set(missing_modules)))}) rather than a missing source file. "
                "Source-file rewrites are held for review unless the fix updates a manifest or CI workflow."
            )

    secrets = _extract_required_secrets(logs)
    if secrets:
        merged = sorted(set([*diagnosis.required_secrets, *secrets]))
        updates["required_secrets"] = merged
        # Only override category/files when there's no better diagnosis.
        # Never let secret extraction nuke a workflow_config/code/dependency fix.
        if not diagnosis.files_changed and diagnosis.category not in ("workflow_config", "code", "dependency"):
            updates["category"] = "environment"
            updates["fix_type"] = "manual_required"

    # If the model diagnosed workflow_config but produced no files, the prompt didn't
    # drive it hard enough. Keep the category and degrade to review_recommended so the
    # dashboard at least shows what needs to be changed, rather than manual_required.
    if (diagnosis.category == "workflow_config"
            and not diagnosis.files_changed
            and diagnosis.fix_type == "manual_required"
            and "fix_type" not in updates):
        updates["fix_type"] = "review_recommended"
        updates["speculative"] = True

    missing_copy_path = _extract_missing_docker_copy_path(logs)
    if missing_copy_path and diagnosis.fix_type == "safe_auto_apply":
        allowed_paths = {missing_copy_path, missing_copy_path.lstrip("./")}
        touches_dockerfile = any(fc.path.lower().endswith("dockerfile") or fc.path == "Dockerfile" for fc in diagnosis.files_changed)
        creates_exact_path = any(fc.path in allowed_paths for fc in diagnosis.files_changed)
        if not touches_dockerfile and not creates_exact_path:
            updates["fix_type"] = "review_recommended"
            updates["speculative"] = True
            updates["fix_description"] = (
                f"{updates.get('fix_description', diagnosis.fix_description)}\n\n"
                f"Guardrail: Docker reported missing build-context path `{missing_copy_path}`. "
                "The proposed file changes do not touch that exact path or the Dockerfile, so this needs review."
            )

    if not updates:
        return diagnosis
    return diagnosis.model_copy(update=updates)


def _recalibrate_confidence(
    diagnosis: Diagnosis,
    repo_memory: RepoMemory | None,
    run_id: str | None = None,
) -> Diagnosis:
    """
    M3: cap stated confidence against this repo's actual track record for the
    diagnosis category. A category that verifies 20% of the time doesn't get to
    claim 90%+ confidence just because the model feels sure this time.

    Reverted fixes count against the rate even though they were "verified" at
    merge time — a fix that got reverted within 7 days was not actually a
    success, and category_outcomes.verified_rate alone doesn't reflect that.
    """
    if not repo_memory:
        return diagnosis

    stats = repo_memory.category_outcomes.get(diagnosis.category)
    if not stats:
        return diagnosis

    attempts = stats.get("attempts", 0)
    if attempts < MIN_CALIBRATION_SAMPLES:
        return diagnosis

    effective_verified = max(0, stats.get("verified", 0) - stats.get("reverted", 0))
    effective_rate = effective_verified / attempts
    calibrated_ceiling = round(min(1.0, effective_rate + 0.2), 2)

    if diagnosis.confidence <= calibrated_ceiling:
        return diagnosis

    logger.info(
        f"Confidence recalibration: run={run_id} category={diagnosis.category} "
        f"model_confidence={diagnosis.confidence} effective_verified_rate={round(effective_rate, 2)} "
        f"(verified={stats.get('verified', 0)} reverted={stats.get('reverted', 0)} attempts={attempts}) "
        f"-> capped at {calibrated_ceiling}"
    )
    return diagnosis.model_copy(update={"confidence": calibrated_ceiling})


# M4: known peer/type package pairs whose major versions must stay in lockstep.
# A JS/TS build breaks just as hard from a partial bump (react ^18, @types/react ^17
# left behind) as from no bump at all — this is the exact gap that caused the
# lagom-humanizer dependency incident. See ROADMAP.md "Dependency chain completeness".
_PEER_VERSION_PAIRS = [
    ("react", "react-dom"),
    ("react", "@types/react"),
    ("react-dom", "@types/react-dom"),
    ("vue", "@vue/compiler-sfc"),
    ("vue", "@vue/runtime-core"),
    ("@angular/core", "@angular/common"),
    ("@angular/core", "@angular/compiler"),
]


def _extract_major_version(spec: str) -> int | None:
    """Best-effort leading major version from a semver range like '^18.2.0' or '~5.0.1'."""
    if not isinstance(spec, str):
        return None
    spec = spec.strip()
    if not spec or spec in ("*", "latest", "next") or spec.startswith(("workspace:", "file:", "git", "link:")):
        return None
    match = re.search(r"(\d+)", spec)
    return int(match.group(1)) if match else None


def _check_dependency_chain_completeness(diagnosis: Diagnosis) -> Diagnosis:
    """
    M4: deterministic check for peer/type package major-version alignment in a
    rewritten package.json. Prompt instructions alone weren't reliable enough —
    this catches an incomplete bump instead of letting it ship as safe_auto_apply.
    """
    package_json = next(
        (fc for fc in diagnosis.files_changed if fc.path.endswith("package.json") and fc.new_content),
        None,
    )
    if not package_json:
        return diagnosis

    try:
        manifest = json.loads(package_json.new_content)
    except (json.JSONDecodeError, TypeError):
        return diagnosis

    if not isinstance(manifest, dict):
        return diagnosis

    versions: dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        section_data = manifest.get(section)
        if isinstance(section_data, dict):
            versions.update(section_data)

    mismatches = []
    for pkg_a, pkg_b in _PEER_VERSION_PAIRS:
        if pkg_a not in versions or pkg_b not in versions:
            continue
        major_a = _extract_major_version(versions[pkg_a])
        major_b = _extract_major_version(versions[pkg_b])
        if major_a is not None and major_b is not None and major_a != major_b:
            mismatches.append(f"{pkg_a}@{versions[pkg_a]} vs {pkg_b}@{versions[pkg_b]}")

    if not mismatches:
        return diagnosis

    logger.warning(f"Dependency chain incomplete — peer major-version mismatch: {mismatches}")
    return diagnosis.model_copy(update={
        "fix_type": "review_recommended",
        "speculative": True,
        "fix_description": (
            f"{diagnosis.fix_description}\n\n"
            "Guardrail: package.json bumps one package in a peer/type group without matching "
            f"the others — major-version mismatch: {'; '.join(mismatches)}. Held for review "
            "instead of auto-applied; a partial peer bump breaks the build the same way a "
            "missing bump does."
        ),
    })


# M6: language patterns indicating the fix loosens an analyzer/linter/test gate
# rather than resolving what it caught — "lowered the level", "disabled the
# rule", "relaxed strictness". Deliberately matches the MODEL'S OWN description
# of its fix, not file diffs (which would need the original file content this
# guardrail doesn't have access to) — the model already states in plain
# language what it did, so read that instead of re-deriving it from a diff.
#
# Flexible gap between the action verb and target noun (not a rigid adjacent
# phrase) — verified against the real diagnosis this milestone is fixing:
# "Lower PHPStan analysis level from 9 to 5..." has a tool name (PHPStan)
# sitting between "Lower" and "level" that a strict "lowered the level"
# phrase would miss entirely.
_SUPPRESSION_LANGUAGE_RE = re.compile(
    r"\b(lower(?:ed|ing)?|disable[ds]?|disabling|relax(?:ed|ing)?|loosen(?:ed|ing)?"
    r"|downgrad(?:ed?|ing)|suppress(?:ed|ing)?|turn(?:ed)?\s+off|skip(?:ped|ping)?)\b"
    r".{0,40}?\b(level|strictness|severity|check|rule|lint|test|analyzer|analysis)\b",
    re.IGNORECASE | re.DOTALL,
)

_HONEST_DISCLOSURE_RE = re.compile(r"^\s*note:.{0,80}(relax|suppress|loosen|lower)", re.IGNORECASE)


def _flag_strictness_suppression(diagnosis: Diagnosis) -> Diagnosis:
    """
    M6: prompt instructions ask the model to disclose when a fix loosens an
    analyzer/linter/test gate instead of fixing what it caught (see "ROOT
    CAUSE VS SUPPRESSION" in the system prompt) — this is the deterministic
    backstop for when it doesn't. Found live: a PHPStan level 9->5 diagnosis
    described itself as if it were a resolution, not a suppression.
    """
    text = f"{diagnosis.fix_description} {diagnosis.root_cause}"
    if not _SUPPRESSION_LANGUAGE_RE.search(text):
        return diagnosis
    if _HONEST_DISCLOSURE_RE.search(diagnosis.fix_description):
        return diagnosis  # model already disclosed it honestly, per the prompt

    logger.info(f"Diagnosis loosens a check without an honest disclosure — prepending one: {text[:200]}")
    return diagnosis.model_copy(update={
        "fix_description": (
            "Note: this appears to relax a linter/analyzer/test check rather than fixing the "
            f"underlying issue it caught — verify that's acceptable before merging.\n\n{diagnosis.fix_description}"
        ),
    })


# M8: only consult a second model when the primary diagnosis is already
# uncertain — this is a narrow, rare trigger (not a blanket "race everything"
# like the D2 idea that got explicitly skipped for cost reasons), so the
# added cost is proportional to how often it actually helps. Real 30-day
# production data: 28/30 diagnoses landed at confidence >=0.92; both
# low-confidence cases were caused by missing log signal (now fixed by
# M1-M5), not model disagreement — but genuine disagreement is still a real
# failure mode worth checking for going forward.
_LOW_CONFIDENCE_THRESHOLD = 0.5


async def _consult_second_opinion(
    diagnosis: Diagnosis,
    system_prompt: str,
    user_prompt: str,
    run_id: str | None,
) -> Diagnosis:
    """
    Fire one independent Kimi call (forced tool_choice, single-shot — not the
    full investigation loop) for low-confidence/unknown diagnoses, and record
    whether it agrees. Deliberately does NOT use agreement to raise confidence
    or change fix_type — compounding two uncertain guesses into apparent
    certainty would be worse than the problem this solves. It surfaces
    agreement/disagreement as a signal for the human reviewer, who already
    sees this diagnosis either way since low-confidence routes to
    review_recommended/manual_required regardless.
    """
    if diagnosis.category != "unknown" and diagnosis.confidence >= _LOW_CONFIDENCE_THRESHOLD:
        return diagnosis

    try:
        second_args, _, _ = await _call_kimi_structured(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            DIAGNOSIS_TOOL,
        )
    except Exception as e:
        logger.warning(f"Second-opinion Kimi call failed for run {run_id}: {e}")
        return diagnosis

    if not second_args or not _args_match_schema(second_args, DIAGNOSIS_TOOL):
        logger.info(f"Second-opinion Kimi call for run {run_id} didn't return a valid diagnosis — skipping")
        return diagnosis

    second_fix_type = second_args.get("fix_type")
    second_category = second_args.get("category")
    agrees = second_fix_type == diagnosis.fix_type and second_category == diagnosis.category

    logger.info(
        f"Second opinion for run {run_id}: agrees={agrees} "
        f"(primary={diagnosis.fix_type}/{diagnosis.category}, kimi={second_fix_type}/{second_category})"
    )
    note = (
        f"\n\nCross-model check: Kimi's independent second opinion "
        f"{'agrees with this diagnosis' if agrees else f'DISAGREES — Kimi suggested fix_type={second_fix_type}, category={second_category}'}."
    )
    return diagnosis.model_copy(update={"fix_description": diagnosis.fix_description + note})


def _extract_missing_modules(logs: str) -> list[str]:
    return [m.group(1).strip() for m in _BARE_MODULE_RE.finditer(logs or "")]


def _is_bare_package_name(module_name: str) -> bool:
    return not (
        module_name.startswith(".")
        or module_name.startswith("/")
        or module_name.startswith("@/")
        or "/" in module_name and module_name.startswith(("src/", "app/", "lib/", "tests/"))
    )


def _changes_dependency_or_workflow(diagnosis: Diagnosis) -> bool:
    manifest_names = {
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "requirements.txt", "pyproject.toml", "poetry.lock", "Pipfile",
        "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
    }
    for file_change in diagnosis.files_changed:
        path = file_change.path
        if path in manifest_names or path.startswith(".github/workflows/"):
            return True
    return False


def _extract_required_secrets(logs: str) -> list[str]:
    # Known safe CI env vars that are never real secrets
    _SAFE = {"CI", "NODE_ENV", "PORT", "RAILS_ENV", "HOME", "PATH", "LANG", "TZ",
             "NPM", "NODE", "YARN", "PNPM", "ALL", "ERROR", "WARN", "INFO", "DEBUG"}
    return [
        match.group(1)
        for match in _SECRET_RE.finditer(logs or "")
        if match.group(1) not in _SAFE
        and "_" in match.group(1)  # real secrets almost always have underscores
    ]


def _extract_missing_docker_copy_path(logs: str) -> str | None:
    recent_copy_path: str | None = None
    for line in (logs or "").splitlines():
        lower = line.lower()
        copy_match = _DOCKER_COPY_SOURCE_RE.search(line)
        if copy_match:
            recent_copy_path = copy_match.group("path").strip()

        if not any(marker in lower for marker in ("not found", "no such file", "failed", "does not exist")):
            continue

        for pattern in (_DOCKER_COPY_SOURCE_RE, _DOCKER_STAT_PATH_RE):
            match = pattern.search(line)
            if match:
                return match.group("path").strip()
        if recent_copy_path:
            return recent_copy_path
    return None


INVESTIGATION_TOOLS = [
    {
        "name": "fetch_file",
        "description": "Fetch the current content of a file from the repo.",
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
    },
    {
        "name": "list_directory",
        "description": "List files in a directory.",
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
    },
    {
        "name": "search_code",
        "description": "Search for a function, class, or symbol in the repository.",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "string"}},
        },
    },
]


def _validate_investigation_path(path: str) -> str | None:
    if ".." in path or path.startswith("/"):
        return "Path must be relative and cannot contain '..'"
    return None


async def _execute_investigation_tool(tool_name: str, tool_args: dict, context: dict) -> str:
    if tool_name == "fetch_file":
        path = tool_args.get("path", "")
        if err := _validate_investigation_path(path):
            return json.dumps({"error": err, "path": path})
        return await _investigation_fetch_file(context, path)
    if tool_name == "list_directory":
        path = tool_args.get("path", "")
        if err := _validate_investigation_path(path):
            return json.dumps({"error": err, "path": path})
        return await _investigation_list_directory(context, path)
    if tool_name == "search_code":
        return await _investigation_search_code(context, tool_args.get("query", ""))
    return json.dumps({"error": f"Unknown investigation tool: {tool_name}"})


async def _investigation_fetch_file(context: dict, path: str) -> str:
    if not path:
        return json.dumps({"error": "path is required"})
    headers = _gh_headers(context["access_token"])
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{context['repo_full_name']}/contents/{path}",
            headers=headers,
            params={"ref": context["default_branch"]},
        )
    if resp.status_code != 200:
        return json.dumps({"error": f"fetch_file failed with {resp.status_code}", "path": path})
    data = resp.json()
    content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
    return json.dumps({"path": path, "content": content[:20000]})


async def _investigation_list_directory(context: dict, path: str) -> str:
    headers = _gh_headers(context["access_token"])
    target = path.strip("/") if path else ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{context['repo_full_name']}/contents/{target}",
            headers=headers,
            params={"ref": context["default_branch"]},
        )
    if resp.status_code != 200:
        return json.dumps({"error": f"list_directory failed with {resp.status_code}", "path": target})
    entries = [
        {"path": item.get("path"), "type": item.get("type")}
        for item in (resp.json() if isinstance(resp.json(), list) else [])
    ]
    return json.dumps({"path": target or ".", "entries": entries[:200]})


async def _investigation_search_code(context: dict, query: str) -> str:
    if not query:
        return json.dumps({"error": "query is required"})
    headers = _gh_headers(context["access_token"])
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://api.github.com/search/code",
            headers=headers,
            params={"q": f"{query} repo:{context['repo_full_name']}", "per_page": 10},
        )
    if resp.status_code != 200:
        return json.dumps({"error": f"search_code failed with {resp.status_code}", "query": query})
    items = [
        {"path": item.get("path"), "name": item.get("name")}
        for item in resp.json().get("items", [])
    ]
    return json.dumps({"query": query, "matches": items})


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


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
    repo_memory: RepoMemory | None = None,
) -> str:
    parts = [
        f"REPOSITORY: {repo_full_name}",
        f"WORKFLOW: {workflow_name}",
        f"COMMIT MESSAGE: {commit_message}",
    ]

    if commit_sha:
        parts.append(f"COMMIT SHA: {commit_sha}")

    if repo_memory:
        context = repo_memory.as_prompt_context()
        if context:
            parts.append(context)

    # RAG: inject past verified fixes for this repo as few-shot context
    if similar_fixes and not repo_memory:
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
            "DO NOT give up — you MUST produce a files_changed fix attempt. "
            "Your fix will be pushed to the same branch for CI verification."
        )

    return "\n".join(parts)
