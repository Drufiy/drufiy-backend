# Prash Eval Harness — Golden Benchmark

A reproducible benchmark built from **real production runs**. Use it to gate every
prompt / model / schema change: run before and after, compare scorecards. No more
shipping prompt edits to prod and hoping.

## Why this exists

Production success rate is ~18% end-to-end, and the single biggest failure bucket is
`diagnosis_failed` (29%) — the model not returning a schema-valid answer *at all*.
Until now there was no way to measure whether a change helped or hurt. This harness
makes the headline number — **valid_diagnosis rate** — measurable offline.

## Layout

```
evals/
  seed_from_db.py   # pull real runs from Supabase → cases/*.json
  cases/            # golden cases (one JSON per run); committed
  run_eval.py       # replay cases through diagnose_failure(), score, write results
  score.py          # scoring rubric + scorecard rendering
  results/          # scorecards per run (gitignored)
```

## A golden case

Each case snapshots the **inputs** (repo, workflow, commit message, CI logs, current
file contents — extracted from the exact prompt recorded in `agent_calls`) and the
**ground-truth outcome** (the verified fix). Cases sourced from `verified` runs are
trustworthy ground truth: the fix was applied and CI actually went green.

## Running

```bash
# 1. (re)seed from the live DB — needs SUPABASE_URL + SUPABASE_SERVICE_KEY
python -m evals.seed_from_db

# 2. run the benchmark against the current pipeline
python -m evals.run_eval

# quick smoke (2 cases)
python -m evals.run_eval --limit 2

# compare against a saved baseline
python -m evals.run_eval --label after-fix \
    --baseline evals/results/before-fix.json

# also exercise the agentic investigation loop (needs GH_TOKEN with repo read)
GH_TOKEN=ghp_xxx python -m evals.run_eval --live
```

By default the runner uses the **single-shot path** (`call_with_tool` with files
supplied in-prompt) — GitHub-free and reproducible — which isolates model + prompt +
schema quality. `--live` adds the real investigation loop for full-pipeline eval.

`agent_calls` logging is monkeypatched off, so eval runs never write to prod tables.

## Metrics (ordered by what the data says matters)

| Metric | Meaning |
|---|---|
| **valid_diagnosis_rate** | Did the model return a schema-valid `Diagnosis`? *(the headline — prod ≈71%)* |
| category_accuracy | Correct failure classification (drives routing) |
| actionability | Produced a fix when one was expected |
| file_recall | Fraction of expected changed-files the model targeted |
| fix_type_accuracy | Exact `fix_type` agreement (soft signal) |
| latency p50/p90/max | Wall-clock per diagnosis |

## Known limitations (read before trusting the score)

1. **Category coverage is biased.** The golden set is seeded only from *verified*
   runs, and the only categories that ever verified are `code` and `dependency`.
   `workflow_config` (0/12 in prod) and `environment` (0/17) have **no** ground-truth
   cases yet. Hand-author cases for those (drop a JSON in `cases/` matching the schema)
   — that is where the product is weakest and where the benchmark is currently blind.
2. **~60-70% of production traffic was internal smoke testing.** Cases carry a `notes`
   field flagging smoke-test commits. Treat per-category counts accordingly.
3. **Single-shot ≠ prod path.** Prod runs the agentic loop. Use `--live` periodically
   to catch loop-specific regressions the default mode won't see.

## Adding a hand-authored case

```json
{
  "id": "wf-missing-checkout",
  "source": "verified",
  "repo_full_name": "you/repo",
  "workflow_name": "CI",
  "commit_message": "ci: add lint job",
  "logs": "=== 0_lint.txt ===\n... error: No such file or directory ...",
  "current_files": { ".github/workflows/ci.yml": "name: CI\non: push\n..." },
  "expected": {
    "category": "workflow_config",
    "fix_type": "safe_auto_apply",
    "files_changed_paths": [".github/workflows/ci.yml"],
    "verified": true
  },
  "notes": "hand-authored — covers the 0% workflow_config gap"
}
```
