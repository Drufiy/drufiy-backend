"""
Seed the golden benchmark from real production runs.

Pulls every VERIFIED ci_run (the fix was applied AND CI went green on the fix
branch — i.e. ground-truth-correct) plus a sample of diagnosis_failed / exhausted
runs (negative + regression cases), and reconstructs a reproducible eval case from
the exact prompt that was sent to the model (recorded in agent_calls.input_messages).

A "golden case" snapshots the *inputs* (repo, workflow, commit message, CI logs,
current file contents) so the eval can be replayed offline through the current
diagnose_failure() pipeline and scored against the known-good outcome.

Usage:
    python -m evals.seed_from_db            # writes evals/cases/*.json
    python -m evals.seed_from_db --limit 5  # smaller sample while iterating

Requires SUPABASE_URL + SUPABASE_SERVICE_KEY in the environment (.env is loaded).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Range": "0-99999"}

CASES_DIR = Path(__file__).parent / "cases"

# ── prompt parsing ────────────────────────────────────────────────────────────

_FILE_BLOCK_RE = re.compile(r"=== (?P<path>[^\n=]+?) ===\n(?P<body>.*?)\n=== end (?P=path) ===", re.DOTALL)


def _field(prompt: str, label: str) -> str:
    m = re.search(rf"^{re.escape(label)}:\s*(.+)$", prompt, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extract_logs(prompt: str) -> str:
    # Logs sit between "CI FAILURE LOGS:\n---\n" and the matching closing "\n---"
    m = re.search(r"CI FAILURE LOGS:\n---\n(.*?)\n---\n", prompt, re.DOTALL)
    if not m:
        m = re.search(r"CI FAILURE LOGS:\n---\n(.*)", prompt, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_current_files(prompt: str) -> dict[str, str]:
    files: dict[str, str] = {}
    if "CURRENT FILE CONTENTS" not in prompt:
        return files
    region = prompt.split("CURRENT FILE CONTENTS", 1)[1]
    region = region.split("CI FAILURE LOGS:", 1)[0]
    for m in _FILE_BLOCK_RE.finditer(region):
        files[m.group("path").strip()] = m.group("body")
    return files


def _get(table: str, select: str, extra: str = "") -> list[dict]:
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{table}?select={select}{extra}", headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def _build_case(run: dict, diag: dict, agent_call: dict) -> dict | None:
    messages = agent_call.get("input_messages")
    if not isinstance(messages, list):
        return None
    user_prompt = next((m["content"] for m in messages if m.get("role") == "user"), "")
    if not user_prompt:
        return None

    logs = _extract_logs(user_prompt)
    if not logs:
        return None

    expected_files = [f.get("path") for f in (diag.get("files_changed") or []) if f.get("path")]

    return {
        "id": run["id"][:8],
        "source": "verified" if run["status"] == "verified" else run["status"],
        # ── reproducible inputs ────────────────────────────────────────────
        "repo_full_name": _field(user_prompt, "REPOSITORY") or run.get("github_workflow_name", ""),
        "workflow_name": _field(user_prompt, "WORKFLOW") or run.get("github_workflow_name") or "CI",
        "commit_message": _field(user_prompt, "COMMIT MESSAGE") or run.get("commit_message", ""),
        "logs": logs,
        "current_files": _extract_current_files(user_prompt),
        # ── ground truth (only trustworthy for source == "verified") ──────
        "expected": {
            "category": diag.get("category"),
            "fix_type": diag.get("fix_type"),
            "confidence": diag.get("confidence"),
            "files_changed_paths": expected_files,
            "problem_summary": diag.get("problem_summary"),
            "verified": run["status"] == "verified",
        },
        "notes": "Smoke/test traffic — verify before trusting as ground truth"
        if "smoke test" in (run.get("commit_message") or "").lower()
        else "",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap cases per bucket (0 = all)")
    args = ap.parse_args()

    CASES_DIR.mkdir(parents=True, exist_ok=True)

    # Verified runs are ground-truth-correct: the fix was applied and CI went green.
    verified = _get("ci_runs", "id,status,github_workflow_name,commit_message", "&status=eq.verified")
    # A handful of negatives for regression coverage (we expect the model to at least
    # produce a *valid* diagnosis here even if it previously failed).
    negatives = _get(
        "ci_runs",
        "id,status,github_workflow_name,commit_message",
        "&status=in.(exhausted,diagnosis_failed)&order=created_at.desc&limit=15",
    )

    runs = verified + negatives
    written = 0
    skipped = 0
    by_bucket: dict[str, int] = {}

    for run in runs:
        bucket = "verified" if run["status"] == "verified" else "negative"
        if args.limit and by_bucket.get(bucket, 0) >= args.limit:
            continue

        diags = _get(
            "diagnoses",
            "run_id,iteration,problem_summary,category,fix_type,confidence,files_changed,verification_status",
            f"&run_id=eq.{run['id']}&order=iteration.desc&limit=1",
        )
        diag = diags[0] if diags else {}
        acs = _get(
            "agent_calls",
            "call_type,input_messages",
            f"&run_id=eq.{run['id']}&order=created_at.asc&limit=1",
        )
        if not acs:
            skipped += 1
            continue

        case = _build_case(run, diag, acs[0])
        if not case:
            skipped += 1
            continue

        (CASES_DIR / f"{bucket}_{case['id']}.json").write_text(json.dumps(case, indent=2))
        written += 1
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1

    print(f"Wrote {written} cases to {CASES_DIR} (skipped {skipped} with no replayable prompt)")
    print(f"Buckets: {by_bucket}")


if __name__ == "__main__":
    main()
