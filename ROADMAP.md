# Prash — Engineering Roadmap

**Updated: 2026-06-14** | Primary model: **DeepSeek V4 Pro** | Fallback: **Kimi K2.6**
**Founders:** Aradhya Mishra + Maneesh Awasthi

---

## VISION

Prash becomes the **AI DevOps layer** — not a band-aid for CI, but the system that owns your build pipeline, release pipeline, container orchestration, and production awareness. Current coding tools (Claude Code, Codex) don't know what's happening in production. Prash sits at the CI/CD + production boundary — that's the moat.

**The trust ladder (how we get there):**
1. ~~Suggest a fix~~ *(done)*
2. ~~Open a PR, human merges~~ *(done — where we are today)*
3. **Auto-verify and auto-merge when CI passes** *(partially built — auto-merge exists, verify loop incomplete)*
4. Fix proactively before the human even looks *(future)*
5. Touch production with guardrails *(future — read-only first)*

**Sequencing rule:** Go deep on CI repair until it's boringly perfect. That earns the right to climb. Resist breadth until the core is flawless.

---

## STATUS TRACKER — AUDIT FIXES (A-series)

*From the original MODEL_AUDIT_AND_FIX_PLAN.md. Crossed-off items are verified in code.*

| ID | Fix | Status | Notes |
|----|-----|--------|-------|
| A1 | RAG column fix (`connected_repo_id` → `repo_id`) | **DONE** | `processor.py:912` — queries `repo_id` correctly now |
| A2 | Config drift (`extra="ignore"`) + cost tracking | **DONE** | `config.py:72` — `extra="ignore"`, Kimi + DeepSeek prices set |
| A3 | Temperature retry wrapper (`_create_chat`) | **DONE** | `kimi_client.py` — all calls go through `_create_chat` |
| A4 | Investigation loop rewrite (nudge, don't waste steps) | **DONE** | `call_with_investigation` rewritten, works with any model |
| A5 | Blank reasoning fall-through | **DONE** | Two-call pattern replaced entirely with native single-call for DeepSeek |
| A6 | Fallback model (cross-provider) | **DONE** | DeepSeek primary, Kimi fallback. `call_with_tool` falls through on failure |
| A7 | Category normalization / aliases | **DONE** | `schemas.py:4` — `_CATEGORY_ALIASES` + `_normalize_category` validator |
| A8 | Workflow scope error + GitHub App permissions | **PARTIAL** | `pr_creator.py:148` — returns `WORKFLOW_SCOPE_REQUIRED` error. Frontend OAuth scope + full App migration still needed |
| A9 | Latency caps (client timeout, wall-clock budget) | **PARTIAL** | DeepSeek client timeout=90s, Kimi still 240s. No `asyncio.wait_for` wall-clock cap on `diagnose_failure` |
| A10 | Reconciler: sweep `pending` + `diagnosing` + `applying` | **DONE** | `reconciler.py:56-61` — sweeps all stuck states |
| A11 | `diagnosed` black hole → `needs_secret` state | **PARTIAL** | `required_secrets` field exists in schema. UI rendering + auto-add safe env defaults not confirmed |
| A12 | Log preprocessing tail safety net | **DONE** | `diagnosis_agent.py:522` — appends RAW TAIL last 40 lines |
| A13 | Source tagging (smoke_test vs user) | **DONE** | `webhook.py:494` — tags `source` on ci_run insert |
| A14 | Sanity review → real second opinion or delete | **PENDING** | `processor.py:840` — still uses same-model sanity check. Should route through Kimi (different model) now that primary is DeepSeek, or delete |

---

## STATUS TRACKER — IMPROVEMENTS (M/A-series)

*From IMPROVEMENTS.md. Only tracking items not covered by the A-series above.*

### Done

| ID | Item | Status |
|----|------|--------|
| A (org repos) | `organization_member` affiliation | **DONE** — `repos.py:74` |
| B2 (flaky tests) | Auto-retry + auto-skip | **DONE** — `processor.py:175, 659` |
| C1 (thinking) | Two-call → native single-call (DeepSeek) | **DONE** — replaced with `tool_choice="auto"` + thinking ON |
| C2 (commit diff) | Fetch breaking commit diff | **DONE** — `processor.py:100-130`, `_fetch_commit_diff` |
| C2 (max files) | Bump max_files to 12 | **DONE** — `processor.py:380` |
| C3 (imports) | Python import-chain tracing | **DONE** — `processor.py:441`, `_queue_python_imports` |
| C5 (RAG) | Past verified fixes as context | **DONE** — `_fetch_similar_fixes` works |
| D3 (agentic loop) | Multi-turn investigation tools | **DONE** — `call_with_investigation` with fetch_file, list_dir, search |
| D4 (reconciler) | Sweep all stuck states | **DONE** — pending, diagnosing, applying, fixed |
| D5 (models) | Remove Gemini/Nvidia → DeepSeek | **DONE** — clean config, DeepSeek V4 Pro primary |
| E2 (stats) | `/admin/stats` endpoint | **DONE** — `runs.py:158` |
| E3 (logging) | agent_calls with cost + outcome | **DONE** — columns added, logging fixed |
| G4 (auto-merge) | Auto-merge verified fixes | **DONE** — `webhook.py:221-224`, per-repo toggle |
| G5 (blame) | Commit blame in PR body | **DONE** — `pr_creator.py:44-95`, `_fetch_blame` |
| G6 (weekly report) | Weekly CI health report | **DONE** — `internal.py:204`, cron endpoint |

### Pending

| ID | Item | Status | Priority |
|----|------|--------|----------|
| B1 | Environment secrets → auto-detect + 1-click UI | **PARTIAL** — schema has `required_secrets`, UI wire-up unknown | P1 |
| B3 | Multi-model consensus on `unknown` category | **NOT STARTED** — DeepSeek is now primary, Kimi is fallback; consensus logic not wired | P2 |
| B4 | Speculative PRs for low confidence | **NOT STARTED** — still downgrades to `manual_required` | P2 |
| C4 | Patch format for file changes | **REVERSED** — prompt now requires `new_content`, `patch` deprecated. Correct decision: patches are fragile | N/A |
| D1 | Increase iterations 3→4 | **PARTIAL** — raised from 2→3 in self-verification loop. Consider 4 after data shows it helps | P3 |
| D2 | Parallel model calls (speed) | **NOT STARTED** — current: sequential primary→fallback | P3 |
| E1 | Slack/Discord alerts | **NOT STARTED** — no `_notify` function | P2 |
| E4 | Guard against Cloud Run scale-down | **DONE** via reconciler sweeps | Done |
| F | GitHub App full migration | **PARTIAL** — App exists, `get_installation_token` works, but not the primary auth path | P1 |
| G1 | Pre-emptive fix on push webhook | **NOT STARTED** | P3 (future) |
| G2 | PR review agent (cross-model sanity check) | **SAME AS A14** — needs fix | P2 |
| G3 | Slack/Discord bot integration | **NOT STARTED** | P3 |

---

## TODAY'S SESSION CHANGES (2026-06-14)

### What shipped

1. **Switched primary model** from Kimi K2.6 → DeepSeek V4 Pro
   - 7x faster, 4.6x cheaper ($0.435/$0.87 vs $2/$2 per 1M tokens)
   - 73% fix_type accuracy in evals (vs Kimi's 87%) — monitoring
2. **Native single-call thinking** for DeepSeek V4 — `thinking ON + tool_choice="auto"` in one pass
   - Replaced two-call pattern that was sabotaging accuracy (reasoning severed from decision)
3. **Generalized all model routing** — `model="auto"` → configured primary, Kimi as cross-provider fallback
4. **Fixed agent_calls logging** — `estimated_cost_usd` and `diagnosis_outcome` columns added to DB
5. **Fixed patch application failures** — prompt requires `new_content` (full file), `patch` deprecated
6. **Live tested 2 cases** — workflow_config fix (merged), TypeScript code fix (merged)
7. **Deployed** to Cloud Run revision `drufiy-backend-00111-fdt`

### Key discovery

DeepSeek V4 has **thinking ON by default**. Forced `tool_choice` (type: function) is **incompatible** with thinking mode — returns 400. `tool_choice="auto"` works with thinking ON. This means: single-call with `tool_choice="auto"` + thinking ON is strictly better than the old two-call pattern.

---

## NEXT SESSIONS — PRIORITIZED BUILD LIST

### ~~Session N+1: Self-Verification Loop~~ — DONE (2026-06-14)

**Shipped and live-tested.** Prash now retries on the same branch when CI fails on a fix:
- `push_fix_to_branch()` in `pr_creator.py` — commits to existing branch, posts retry comment on PR
- `process_iteration_2` pushes to existing branch (not new branch), lowered confidence threshold to 0.6
- Max iterations raised from 2 → 3
- Retry prompt forces model to always attempt a fix (no bail-out to `manual_required`)
- Race condition fixes in webhook status transitions

**Live test result:** Two-layer TypeScript bug (missing `applyTax` + missing `TaxConfig`). Prash fixed utils.ts (iteration 1, CI failed), then auto-retried and created config.ts (iteration 2, CI passed). PR #5 auto-merged with 2 commits. Deployed as `drufiy-backend-00114-pg6`.

### Critical Bug Found & Fixed: Duplicate PR Explosion (2026-06-14)

**Symptom:** 8 PRs created on hypnochic-v2 during self-verification loop test.

**Root cause — triple race condition:**
1. **Duplicate webhook events:** GitHub fires TWO `workflow_run` events per push on a fix branch (`push` + `pull_request`). Both complete simultaneously, both call `handle_verification_event`, both independently trigger `process_iteration_2` — creating duplicate branches/PRs.
2. **Widened status gate:** During development, the allowed status set was expanded to include `diagnosing`, `diagnosed`, `iteration_*` — letting stale events slip through during in-progress iterations.
3. **Non-atomic iteration claim:** The read-then-write pattern in `handle_verification_event` had no locking — two concurrent handlers could both read `status="fixed"`, both compute `next_iteration=2`, and both fire `process_iteration_2`.

**Fix (two layers):**
1. **Tight status gate** — only `"fixed"` and `"applying"` statuses pass the guard (`webhook.py:128`)
2. **Atomic iteration claim** — `.eq("status", "fixed")` WHERE clause on the update ensures only ONE concurrent handler transitions to `iteration_N`. If `claim.data` is empty, another handler already won → bail early (`webhook.py:255-262`)

**Status:** Fixed and verified. Deployed as `drufiy-backend-00120-7dj`.

**Verification test (3-layer bug):** Missing `types.ts` + missing `validators.ts` + `calculateDiscount` signature mismatch.
Prash fixed it in 3 iterations on a single PR (#10): types.ts (iter 1, CI fail) → validators.ts (iter 2, CI fail) → main.ts (iter 3, CI pass → auto-merged). Zero duplicate PRs.

---

### Session N+2: Outcome Tracking + Data Flywheel Foundation

**Why:** Every fix generates a signal — CI passed? PR merged? Human edited it first? Reverted within 7 days? This is the foundation for "self-learning" — without it, that claim is aspirational.

**What to build:**
1. Extend `agent_calls` or add `fix_outcomes` table:
   - `ci_passed_on_fix_branch: bool`
   - `pr_merged: bool`
   - `human_edited_before_merge: bool`
   - `reverted_within_7d: bool`
   - `time_to_merge_ms: int`
2. Webhook handlers to capture merge/revert events
3. Dashboard widget showing real success metrics
4. Recalibrate confidence against actual merge rates (not model vibes)

**Estimated effort:** 1 day

---

### Session N+3: Immediate Bug Fixes + Debt

**Bugs to fix:**

| Bug | File | Severity |
|-----|------|----------|
| A9: Kimi client timeout still 240s (should be 90s) | `kimi_client.py:19` | Medium |
| A9: No wall-clock cap on `diagnose_failure` | `diagnosis_agent.py` | Medium — add `asyncio.wait_for(..., timeout=120)` |
| A14: Sanity check uses same model (now DeepSeek checks DeepSeek) | `processor.py:840` | Medium — route through Kimi for genuine second opinion, or delete |
| Invalid webhook signature from 169.254.169.126 in logs | `webhook.py` | Low — confirm it's a health-check probe, not a dropped webhook |
| Re-run dedup: GitHub re-runs reuse same `run_id` → deduped | `webhook.py` | Low — intended behavior, but confirm new failures on same SHA aren't swallowed |
| `patch` field still in schema (deprecated) | `schemas.py` | Low — consider removing entirely so model can't regress |

**Estimated effort:** 0.5 day

---

### Session N+4: Deeper CI/CD — Release Pipelines + Flaky Tests

**Why (user's answer):** Next expansion is deeper into CI/CD before touching production.

**What to build:**
1. **Flaky test detection + tracking** — track tests that fail-then-pass across runs, build a flaky test database per repo
2. **Dependency hell resolution** — when `npm install` / `pip install` fails due to version conflicts, parse the conflict and propose a resolution
3. **Release pipeline awareness** — understand deploy workflows (not just build/test), handle deploy-specific failures (image build, push, rollout)
4. **Matrix build support** — handle multi-OS/multi-version CI matrices, identify which specific matrix entry failed

**Estimated effort:** 2-3 days

---

### Session N+5: Production Awareness (Read-Only First)

**Why:** "Current coding tools don't know what's happening in production" — this is the moat. But touching production is high-blast-radius. Start read-only.

**What to build (thin first slice):**
1. **Crash loop detection** — connect to container logs (Cloud Run, k8s), detect crash loops, correlate with recent deploys
2. **"Your deploy is crashlooping, here's why"** — diagnosis of runtime errors using production logs + the deploying commit diff
3. **Read-only dashboard** — show deploy health, error rates, recent incidents
4. **No remediation yet** — just awareness + diagnosis. Remediation comes after trust is earned.

**Integration options:**
- Cloud Run logs (GCP Logging API)
- Kubernetes events (k8s API)
- Sentry/Datadog webhook integration
- Generic log ingestion endpoint

**Estimated effort:** 3-5 days

---

### Backlog (do later, not forgotten)

| Item | Description | From |
|------|-------------|------|
| B3 — Multi-model consensus | Fire both DeepSeek + Kimi on `unknown` or low-confidence, take agreement | IMPROVEMENTS.md |
| B4 — Speculative PRs | Never return `manual_required` for code failures — create `[SPECULATIVE]` PR instead | IMPROVEMENTS.md |
| D1 — Increase iterations 3→4 | Currently at 3 (raised from 2), consider 4 | IMPROVEMENTS.md |
| D2 — Parallel model calls | Fire both models simultaneously, take first valid | IMPROVEMENTS.md |
| E1 — Slack/Discord alerts | `_notify` on consecutive failures, fallback triggers, signups | IMPROVEMENTS.md |
| F — GitHub App full migration | Replace OAuth as primary auth, enable marketplace | IMPROVEMENTS.md |
| G1 — Pre-emptive fix on push | Detect failures before CI runs via static analysis | IMPROVEMENTS.md |
| G3 — Slack/Discord bot | Interactive fix buttons in Slack/Discord | IMPROVEMENTS.md |
| Confidence recalibration | Once outcome data exists, recalibrate against actual merge/revert rates | New |
| RAG upgrade — embeddings | Replace keyword RAG with semantic search over past fixes | New |
| Learning flywheel — few-shot | Retrieve similar past failures as few-shot context in prompts | New |

---

## PRODUCTION CONFIG (current)

```
PRIMARY_MODEL=deepseek
DEEPSEEK_API_KEY=sk-581303...
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_INPUT_PRICE_PER_1M_TOKENS=0.435
DEEPSEEK_OUTPUT_PRICE_PER_1M_TOKENS=0.87

KIMI_API_KEY=<set>
KIMI_BASE_URL=https://api.moonshot.ai/v1
KIMI_MODEL=kimi-k2.6
```

**Cloud Run:** `drufiy-backend-00120-7dj` (asia-south1)
**Frontend:** `prashbydrufiy.vercel.app`

---

## CURRENT ARCHITECTURE (as-is, post 2026-06-14)

```
GitHub push → workflow fails
    → GitHub sends workflow_run webhook → /webhook/github
    → webhook.py: verify HMAC, dedupe by (repo+sha+run_id), tag source, insert ci_run(status=pending)
    → background_tasks: process_failure(ci_run_id)

process_failure:
    1. Fetch ci_run + repo from Supabase
    2. Decrypt GitHub token (or get installation token via GitHub App)
    3. fetch_workflow_logs (ZIP → extract → concatenate → truncate to 80K chars)
    4. _preprocess_logs (keep error lines + context + RAW TAIL safety net)
    5. _fetch_relevant_files (regex paths from logs → up to 12 files, including workflow YAML, tsconfig, imports)
    6. _fetch_commit_diff (what changed in the breaking commit)
    7. _fetch_similar_fixes (RAG: past verified fixes as context)
    8. diagnose_failure → DeepSeek V4 Pro (primary, single-call native thinking + tool_choice=auto)
        → Kimi K2.6 fallback if DeepSeek fails
        → Optional: investigation loop (fetch_file, list_dir, search_code tools)
        → Returns: Diagnosis(fix_type, confidence, files_changed, category, required_secrets, ...)
    9. Category normalization via _CATEGORY_ALIASES
    10. Store diagnosis in Supabase (diagnoses table)
    11. If safe_auto_apply:
        → _sanity_check_fix (cross-model review — needs fix, see A14)
        → assess_diff_risk
        → create_fix_pr (branch + commit files via new_content + open PR with blame)
        → ci_run.status = "fixed"
    12. If environment + has required_secrets:
        → ci_run.status = "needs_secret" (or "diagnosed" — see A11)
    13. If flaky_test:
        → auto-retry failed jobs → if still fails, auto-skip test
    14. Else: ci_run.status = "diagnosed"

After PR is created:
    → GitHub runs CI on fix branch
    → workflow_run webhook → handle_verification_event
    → If all pass → status = "verified"
        → If auto_merge enabled → merge PR automatically
    → If any fail → atomic claim (WHERE status="fixed") → status = "iteration_N"
        → process_iteration_2 → push retry to same branch
        → max 3 iterations → "exhausted"
    ⚠️ GAP: no active watching of fix branch CI — relies on webhook delivery

Reconciler (every 60s):
    → Sweeps: pending (>10min), diagnosing (>5min), applying (>3min), fixed (>3min)
    → Requeues or resolves stuck runs

agent_calls logging:
    → Every model call logged with: model, latency, tokens, estimated_cost_usd, diagnosis_outcome
    → Training corpus for future model/prompt tuning
```

---

## KEY NUMBERS TO TRACK

| Metric | Baseline (2026-06-14) | Target |
|--------|----------------------|--------|
| End-to-end success rate | ~18% (103 runs, old model) | 60%+ |
| DeepSeek first-try fix_type accuracy | 73% (eval) | 85%+ (with retry loop) |
| Kimi first-try fix_type accuracy | 87% (eval) | — (fallback) |
| DeepSeek fallback rate | Unknown (just deployed) | <10% |
| Latency p50 | ~15s | <20s |
| Latency p90 | ~60s | <45s |
| Cost per fix | Unknown | Track via agent_calls |
| workflow_config success rate | Was 0% (A8 fixed) | 50%+ |
| environment success rate | Was 0% | 30%+ (with needs_secret UI) |

---

*Previous documents: MODEL_AUDIT_AND_FIX_PLAN.md (raw audit), IMPROVEMENTS.md (original feature roadmap), SPRINT_PLAN.md (initial sprint)*
*This document supersedes all three as the active roadmap.*
