"""
Golden-benchmark runner.

Replays every case in evals/cases/ through the CURRENT diagnose_failure() pipeline
and scores the output against the known-good outcome. Use it to gate every prompt /
model / schema change: run it before and after, compare the scorecards.

    python -m evals.run_eval                      # full run, current model
    python -m evals.run_eval --limit 5            # quick smoke
    python -m evals.run_eval --concurrency 4      # parallelize model calls
    python -m evals.run_eval --baseline evals/results/2026-06-14T10-00.json   # diff vs prior

By default the harness runs the SINGLE-SHOT path (call_with_tool with current_files
supplied in-prompt) — reproducible and GitHub-free. This isolates model + prompt +
schema quality, which is exactly what you tune. Pass --live (with GH_TOKEN) to also
exercise the agentic investigation loop end-to-end.

agent_calls logging is monkeypatched to a no-op so eval runs never pollute prod tables.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

# ── neutralize prod side effects BEFORE importing the pipeline ────────────────
import app.agent.kimi_client as _kc
_kc._log_agent_call = lambda *a, **k: None          # don't write to agent_calls
_kc.mark_agent_run_outcome = lambda *a, **k: None

from app.agent.diagnosis_agent import diagnose_failure          # noqa: E402
from app.agent.kimi_client import DiagnosisValidationError       # noqa: E402
from evals.score import CaseResult, aggregate, render_scorecard  # noqa: E402

CASES_DIR = Path(__file__).parent / "cases"
RESULTS_DIR = Path(__file__).parent / "results"


def load_cases(limit: int = 0) -> list[dict]:
    files = sorted(CASES_DIR.glob("*.json"))
    cases = [json.loads(f.read_text()) for f in files]
    return cases[:limit] if limit else cases


async def run_case(case: dict, live: bool, gh_token: str | None, model: str = "kimi") -> CaseResult:
    case = {**case, "_eval_model": model}
    exp = case.get("expected", {})
    investigation_context = None
    if live and gh_token:
        investigation_context = {
            "repo_full_name": case["repo_full_name"],
            "access_token": gh_token,
            "default_branch": "main",
        }

    start = time.time()
    try:
        diagnosis = await diagnose_failure(
            logs=case["logs"],
            repo_full_name=case["repo_full_name"],
            commit_message=case.get("commit_message", ""),
            workflow_name=case.get("workflow_name", "CI"),
            iteration=1,
            run_id=None,
            current_files=case.get("current_files") or None,
            model=case.get("_eval_model", "kimi"),
            investigation_context=investigation_context,
        )
    except (DiagnosisValidationError, Exception) as e:  # noqa: BLE001 — eval must never crash
        return CaseResult(
            case_id=case["id"],
            source=case["source"],
            valid_diagnosis=False,
            error=f"{type(e).__name__}: {str(e)[:160]}",
            latency_ms=int((time.time() - start) * 1000),
        ).score()

    return CaseResult(
        case_id=case["id"],
        source=case["source"],
        valid_diagnosis=True,
        latency_ms=int((time.time() - start) * 1000),
        predicted_category=diagnosis.category,
        expected_category=exp.get("category"),
        predicted_fix_type=diagnosis.fix_type,
        expected_fix_type=exp.get("fix_type"),
        produced_files=[fc.path for fc in diagnosis.files_changed],
        expected_files=exp.get("files_changed_paths", []),
    ).score()


async def main_async(args) -> None:
    cases = load_cases(args.limit)
    if not cases:
        print(f"No cases in {CASES_DIR}. Run:  python -m evals.seed_from_db")
        return

    print(f"Running {len(cases)} cases  model={args.model}  concurrency={args.concurrency}  live={args.live}")
    sem = asyncio.Semaphore(args.concurrency)

    async def _guarded(c):
        async with sem:
            r = await run_case(c, args.live, args.gh_token, model=args.model)
            mark = "✓" if r.valid_diagnosis else "✗"
            print(f"  {mark} {r.case_id} ({r.source})  {r.latency_ms}ms"
                  + (f"  cat={r.predicted_category}/{r.expected_category}" if r.valid_diagnosis and r.source == "verified" else ""))
            return r

    results = await asyncio.gather(*[_guarded(c) for c in cases])
    agg = aggregate(list(results))

    label = args.label or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
    print(render_scorecard(agg, label))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{label}.json"
    out.write_text(json.dumps({"label": label, "aggregate": agg,
                               "results": [r.to_dict() for r in results]}, indent=2))
    print(f"\nResults → {out}")

    if args.baseline:
        _print_diff(json.loads(Path(args.baseline).read_text()), agg)


def _print_diff(baseline: dict, current: dict) -> None:
    b = baseline["aggregate"]
    print("\n── DIFF vs baseline " + baseline.get("label", "?") + " ──")
    pairs = [
        ("valid_diagnosis_rate_pct", current["valid_diagnosis_rate_pct"], b["valid_diagnosis_rate_pct"]),
        ("category_acc", current["verified_cohort"]["category_accuracy_pct"], b["verified_cohort"]["category_accuracy_pct"]),
        ("actionability", current["verified_cohort"]["actionability_pct"], b["verified_cohort"]["actionability_pct"]),
        ("file_recall", current["verified_cohort"]["mean_file_recall"], b["verified_cohort"]["mean_file_recall"]),
        ("latency_p90", current["latency_ms"]["p90"], b["latency_ms"]["p90"]),
    ]
    for name, cur, base in pairs:
        if cur is None or base is None:
            continue
        delta = round(cur - base, 2)
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
        print(f"   {name:20} {base} → {cur}  {arrow}{abs(delta)}")


def main() -> None:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--label", type=str, default="")
    ap.add_argument("--baseline", type=str, default="")
    ap.add_argument("--live", action="store_true", help="exercise the agentic loop (needs GH_TOKEN)")
    ap.add_argument("--gh-token", type=str, default=os.environ.get("GH_TOKEN"))
    ap.add_argument("--model", type=str, default="auto", help="model to evaluate: auto | kimi | deepseek-v4-pro | deepseek-v4-flash")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
