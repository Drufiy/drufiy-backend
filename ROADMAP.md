# Prash — Engineering Roadmap

**Updated: 2026-06-21** | Primary model: **DeepSeek V4 Pro** | Fallback: **Kimi K2.6**
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
| A9 | Latency caps (client timeout, wall-clock budget) | **DONE (revised 2026-06-21)** | Replaced flat per-call caps with a **nested budget**: DeepSeek 240s shared diagnosis budget, 285s wall-clock, 8-min reconciler cutoff. See "Diagnosis Reliability Hardening". Old 70s/120s caps were killing legitimate long DeepSeek thinks |
| A10 | Reconciler: sweep `pending` + `diagnosing` + `applying` | **DONE** | `reconciler.py:56-61` — sweeps all stuck states |
| A11 | `diagnosed` black hole → `needs_secret` state | **PARTIAL** | `required_secrets` field exists in schema. UI rendering + auto-add safe env defaults not confirmed |
| A12 | Log preprocessing tail safety net | **DONE** | `diagnosis_agent.py:522` — appends RAW TAIL last 40 lines |
| A13 | Source tagging (smoke_test vs user) | **DONE** | `webhook.py:494` — tags `source` on ci_run insert |
| A14 | Sanity review → real second opinion or delete | **REMOVED** | Deleted entirely — CI on fix branch is the real gate, model pre-check was redundant and added latency |

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
| C4 | Patch format for file changes | **REMOVED** — `patch` field deleted from schema, tool definition, and all call sites. `new_content` is the only path | N/A |
| D1 | Increase iterations 3→4 | **PARTIAL** — raised from 2→3 in self-verification loop. Consider 4 after data shows it helps | P3 |
| D2 | Parallel model calls (speed) | **NOT STARTED** — current: sequential primary→fallback | P3 |
| E1 | Slack/Discord alerts | **NOT STARTED** — no `_notify` function | P2 |
| E4 | Guard against Cloud Run scale-down | **DONE** via reconciler sweeps | Done |
| F | GitHub App full migration | **PARTIAL** — App exists, `get_installation_token` works, but not the primary auth path | P1 |
| G1 | Pre-emptive fix on push webhook | **NOT STARTED** | P3 (future) |
| G2 | PR review agent (cross-model sanity check) | **REMOVED** — A14 deleted; CI on fix branch is the real gate | N/A |
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

### Additional Bug Found & Fixed: Reconciler Overwrites Verified Runs (2026-06-14)

**Symptom:** A verified run reverted to "Failed" on the dashboard after a deploy rollout.

**Root cause:** Deploy killed a diagnosis mid-flight. The reconciler found it stuck in `diagnosing`, blindly reset it to `pending`, and re-ran `process_failure`. The new diagnosis attempt failed ("Investigation loop did not yield a final diagnosis"), overwriting the `verified` status.

**Fix:**
1. Reconciler now checks for existing PRs before re-queuing — if a PR exists, marks the run `verified` instead of resetting to `pending` (`reconciler.py:237`)
2. Startup recovery no longer blindly resets to `pending` — defers to the async reconciler loop which has the PR check (`main.py:23`)

**Status:** Fixed, deployed as `drufiy-backend-00124-np2`.

---

### ~~Session N+2: Outcome Tracking + Data Flywheel Foundation~~ — DONE (2026-06-15)

**Shipped and backfilled.** Prash now tracks real outcomes for every PR it creates.

**What shipped:**
1. **5 outcome columns on `diagnoses`**: `pr_merged_at`, `pr_closed_without_merge`, `human_edited_before_merge`, `reverted_within_7d`, `time_to_merge_ms`
2. **`pull_request` webhook handler** — records merge/close outcomes when PRs are closed
3. **Human edit detection** — checks if non-Drufiy commits were added before merge
4. **`GET /runs/admin/outcomes`** — real success metrics by category
5. **`POST /internal/backfill-outcomes`** — retroactive outcome data for existing PRs
6. **`POST /internal/check-reverts`** — daily revert scanning (cron-ready)
7. **Dashboard stats** now include `merge_stats` (merged, rejected, merge_rate)

**Backfill results (77 PRs):**
- **32% overall merge rate** (25/77 merged)
- **Code fixes: 45% merge rate** (best category)
- **Workflow config: 18%**, **Dependency: 13%**
- **0 human edits before merge** — all merges were clean
- **Avg time to merge: 1.0 min** (mostly auto-merged)

**Requires:** Enable `pull_request` events on GitHub App settings for live tracking.

**Estimated effort:** 1 day

---

### ~~Session N+3: Immediate Bug Fixes + Debt~~ — DONE (2026-06-15)

**All 6 bugs resolved.** Deployed as `drufiy-backend-00130-dvs`.

| Bug | Fix | Status |
|-----|-----|--------|
| A9: Kimi client timeout still 240s | Reduced to 90s in `kimi_client.py:19` | **DONE** |
| A9: No wall-clock cap on `diagnose_failure` | 120s `asyncio.wait_for` on both call sites in `processor.py` | **DONE** |
| A14: Sanity check uses same model | Removed entirely — CI verification is the real gate, saves ~10s per run | **REMOVED** |
| Health-check probe noise (169.254.*) | Downgraded to `logger.debug` in `webhook.py` | **DONE** |
| Re-run dedup swallowing new failures | Confirmed correct: dedup is by `(repo, sha, run_id)` — different workflows get different IDs | **OK** |
| `patch` field still in schema | Removed from `schemas.py`, tool definition, `_materialize_patch_file_changes`, and all fallback paths | **DONE** |

---

### ~~Session N+4: Deeper CI/CD — Release Pipelines + Flaky Tests~~ — DONE (2026-06-15)

**All 4 items shipped.** Deployed as `drufiy-backend-00137-f92`.

| Item | What shipped |
|------|-------------|
| Flaky test tracking | `flaky_tests` table, `flaky_tracker.py` (record/lookup), auto-record on rerun pass/fail, `GET /runs/admin/flaky-tests` endpoint |
| Dependency conflict resolution | Prompt guidance for ERESOLVE/ResolutionImpossible/peer deps, few-shot examples 13-14, error regex patterns for version conflicts |
| Release pipeline awareness | Dockerfile + docker-compose in `_MANIFEST_FILES`, Docker path extraction in `_FILE_PATH_RE`, deploy-failure error patterns, few-shot example 15, prompt section on Docker/deploy fixes |
| Matrix build support | `_extract_matrix_summary` in log_fetcher prepends job list to logs, prompt section on matrix failure reasoning, few-shot example 16 |

---

### Atomic Commit Fix — DONE (2026-06-15)

**Problem:** Prash pushed one commit per file via GitHub Contents API. Intermediate CI runs on partial commits triggered false iteration 2s (e.g. hypnochic-v2 validators.ts commit passed but main.ts wasn't pushed yet → CI failed → iteration 2 fired unnecessarily).

**Fix:** `pr_creator.py` — replaced per-file Contents API calls with Git Tree API in `_commit_files_atomic`. All file changes are now batched into a single atomic commit. Both `create_fix_pr` and `push_fix_to_branch` updated. Deployed as `drufiy-backend-00144-hlw`.

---

### External Checks Detector — DONE (2026-06-15)

**Problem:** After Prash verifies a fix (GitHub Actions passes), users saw ❌ on the PR from Vercel/Netlify/Cloudflare and didn't understand what happened. Dashboard showed Verified but GitHub showed failures — no explanation.

**Fix:** `external_checks.py` — after verification, queries GitHub check-runs + commit-status APIs. Filters out GitHub Actions (Prash's domain), surfaces any other failing checks. Stores human-readable note on `ci_runs.external_checks_note` (e.g. "CI fix verified — 1 external check failing: Vercel. Not related to this fix."). Returned in all run API responses. Deployed as `drufiy-backend-00142-ws5`.

---

### Pre-Existing Failure Detection — DONE (2026-06-15)

**Problem:** When Prash's fix removes the original blocker, a latent unrelated error in the repo surfaces on the fix branch. Prash blamed itself and burned all retries trying to fix something it didn't break.

**Fix:** `preexisting_detector.py` — compares error-implicated files against Prash's changed files. If zero overlap, the failure is pre-existing. Webhook sets status to `blocked_preexisting` instead of retrying. Deployed as `drufiy-backend-00140-4tg`.

---

### Multi-Repo Test Run Results (2026-06-15 → 2026-06-18)

Tested Prash against 5 repos with different failure types. Results:

| Repo | Bug type | Result | Notes |
|------|----------|--------|-------|
| lagom-humanizer | npm peer dep conflict (react ^18 vs ^19) | **Failed** | Diagnosed correctly at 95%. Fix bumped react-dom + workflow but missed `@types/react` → still conflicting. Incomplete dependency fix. |
| trimly | Multi-file TypeScript (2 missing types + arg mismatch across 3 files) | **Verified ✅** | Diagnosed all 3 errors at 95%, single atomic commit, CI passed. |
| hypnochic-v2 | TS type mismatch across matrix build (node 18/20/22) | **Verified ✅** | 1st run failed (DeepSeek API disconnect). 2nd run verified. Atomic commit confirmed working. |
| IRIS-backend | Python state machine bug + shell test | **Verified ✅** (after backend fixes + 1 assisted fix) | See "Diagnosis Reliability Hardening" below. Required fixing 3 backend bugs (verification_workflows column, DeepSeek timeout budget, reconciler race) + 1 manual fix where Prash diagnosed the wrong root cause. PR #3 merged, main green. |
| Appi-Claw | flake8 F821 undefined name | **Verified ✅** | Diagnosed at 97%, single commit, CI passed, auto-merged (PR #6). **Bonus:** also caught a *latent pre-existing* bug — `json` undefined at module scope in `shine.py` — that was not planted. |

**Key findings from testing:**
- Dependency fixes that require bumping multiple interdependent packages (react + react-dom + @types/react + @types/react-dom) are under-specified in the prompt. Prash fixes the direct dependency but misses transitive type package alignment. Needs few-shot example for full peer dep chain fixes.
- **`verification_workflows` column missing from DB** — `webhook.py:269` and `processor.py:347` write `"verification_workflows": []` on ci_run insert/update but the column was never added to the schema. Causes `PGRST204` error mid-iteration. Needs `ALTER TABLE ci_runs ADD COLUMN IF NOT EXISTS verification_workflows JSONB DEFAULT '[]'` + Supabase schema cache reload.
- **Diagnosis timeout recurring at 120s — ROOT CAUSE CORRECTED.** Earlier hypothesis (runner metadata crowding out test output) is wrong: every truncation path in the code keeps the *tail* (`log_fetcher.py:104` → `[-80_000:]`, `diagnosis_agent.py:648` → `[-40_000:]`), so head metadata is dropped, not kept. The real cause is a **log-availability race**: GitHub's `/actions/runs/{id}/logs` returns only the orchestration/queue log immediately on `workflow_run.completed`; the per-step `.txt` files (which contain the pytest failure) lag in archival by a few seconds. Proof: the bf8d8d7 diagnosis text said "only queue/wait events visible, no step output." When DeepSeek gets logs with no error signal, it burns the investigation loop searching for a failure that isn't in the prompt until the 120s cap trips → "no diagnosis available." **Fix:** in `fetch_workflow_logs` / `_parse_zip_logs`, detect when the ZIP has only setup/orchestration entries (no per-step test output) and poll-retry the log fetch with backoff (e.g. 3 attempts, 3-5s apart) before handing to diagnosis. Secondary guard: if preprocessed logs contain no `_ERROR_RE` hits, short-circuit instead of letting the model spin to the wall-clock cap.

---

### Diagnosis Reliability Hardening — Test Campaign Close-out (2026-06-20/21)

Completed the 5-repo test campaign. Final tally: **4/5 verified** (trimly, hypnochic-v2, IRIS-backend, Appi-Claw), **1 failed** (lagom-humanizer — incomplete dependency chain). Of the 4 verified, **3 were fully autonomous** (trimly, hypnochic-v2, Appi-Claw); IRIS required backend fixes + 1 assisted code fix.

**Deployed:** `drufiy-backend-00155-kbc` (commit `a923282`).

#### The DeepSeek diagnosis-timeout saga (root cause + permanent fix)

IRIS-backend failed diagnosis 4 times with "Diagnosis timed out after 120 seconds." The earlier hypotheses (runner metadata, log-availability race) were **defense-in-depth but not the cause**. The real cause:

1. **DeepSeek V4 Pro thinking is unbounded** (no token budget, unlike Kimi's 1500). On IRIS's large prompt it thinks 80–104s. Production data confirms **2 calls exceeded 70s, max 103.6s** — these were being killed mid-think by the old 70s per-call cap, which treated "slow but working" as "broken."
2. **The timeout budget never nested.** Old design: 70s per-call cap, 4 investigation steps, 120s wall-clock. Even 2 steps (70+70) blew the 120s. And the forced-final call re-invoked DeepSeek after a timeout instead of falling to Kimi.

**Fix — a single nested budget (verified to hold):**
- **DeepSeek diagnosis budget: 240s total**, shared across all investigation steps (not per-call). Each call gets `remaining` time. `kimi_client.py: DEEPSEEK_DIAGNOSIS_BUDGET`.
- **Outer wall-clock: 285s** (`processor.py`, both call sites).
- **Reconciler `diagnosing` cutoff raised 5min → 8min** so it only rescues genuinely-dead tasks, never re-queues a live diagnosis (`reconciler.py`).
- **`max_steps` 4 → 2** (DeepSeek rarely needs more).
- **On genuine DeepSeek failure, route the final call to Kimi** (don't re-call DeepSeek).
- Nesting: `~25s fetch + (2s preprocess + 240s DeepSeek + ~43s Kimi reserve) = ~310s << 480s reconciler`. ✅

**Two bugs caught during senior review (before re-testing):**
- *Fictional Kimi reserve:* DeepSeek 260s + Kimi 30s > 280s outer → Kimi was killed mid-call. Re-budgeted so layers actually nest.
- *Reconciler race:* `diagnosing` status is set before the GitHub fetches, so the 5-min clock included fetch time; worst case ~305s > 300s → reconciler re-queued live runs (duplicate parallel `process_failure`). Fixed by the 8-min cutoff.

**Also found (separate fix):** `verification_workflows` column was missing from the `ci_runs` schema → `PGRST204` mid-iteration. Added via migration + schema reload.

**Infra note:** Cloud Run **CPU throttling is ON** (default). FastAPI background tasks get near-zero CPU after the webhook responds, slowing DeepSeek's streaming. The 240s budget absorbs this. Switching to "CPU always allocated" would speed diagnosis but is a billing change — deferred.

#### KEY QA FINDING — Prash diagnosed the WRONG root cause when the real exception was masked

IRIS's `run_shell("echo hello")` test failed. Prash diagnosed it as a subprocess-API problem (`create_subprocess_exec` → `create_subprocess_shell`) at 85% confidence. **It was wrong.** The actual bug: on the success path, `run_shell` called `logger.debug(...)`, but the test stubs `loguru` with an incomplete fake (no `.debug`) that wins in `sys.modules` via test ordering. The `AttributeError` was swallowed by a broad `except Exception` that returned `{"status": "error"}`. The CI log only showed the downstream assertion (`status != "ok"`), never the real exception.

**Why Prash missed it:** the true exception was invisible — masked by `except Exception` + a stubbed logger. Prash pattern-matched the surface symptom ("returns error") to the most common cause (subprocess). Its self-verification loop retried twice but kept re-applying variations of the *same wrong hypothesis*, then gave up ("manual intervention required"). The fix was found only after manually adding a `print(e)` to stderr to surface the swallowed exception.

This is a **class of bug Prash systematically struggles with** and is the most important improvement target — see "Lessons → Improvements" below.

#### Lessons learned → Improvement backlog

| # | Lesson | Proposed improvement | Priority |
|---|--------|---------------------|----------|
| L1 | **Masked exceptions defeat diagnosis.** When code swallows the real error (`except Exception: return {"status":"error"}`) and CI only shows a downstream assertion, Prash diagnoses the surface symptom and gets it wrong. | When the failing assertion is on a returned status/dict (not a raw traceback), Prash's investigation loop should **fetch the failing function's source and reason about its exception paths** — what could throw on the success path. Add a few-shot example of a swallowed-exception bug. Consider a heuristic: "no traceback in logs + assertion on a dict field → investigate the producer function." | **P0** |
| L2 | **Repeated-identical-failure = wrong hypothesis.** Self-verification retried twice with the same root cause and failed identically both times. | On iteration N, if the error signature is **identical** to iteration N-1, force a **strategy change**: escalate investigation depth, fetch more files, or explicitly instruct the model "your previous hypothesis was wrong, consider a different cause." Don't re-apply variations. | **P0** |
| L3 | **Confidence is miscalibrated upward.** 85% on the wrong IRIS fix; 95% on the failed lagom fix. | Recalibrate confidence against the outcome data (merge/revert rates) now that it exists. High confidence on fixes that fail CI should be impossible after calibration. | **P1** |
| L4 | **Timeout budgets must nest as a system.** Three independent timeout layers (per-call, wall-clock, reconciler) silently fought each other. | Document the budget invariant in code (done in `kimi_client.py` comments). Add a startup assertion that `fetch + wall_clock < reconciler_cutoff`. | **P2** |
| L5 | **DeepSeek needs room, not a leash.** The model is better than Kimi but thinks longer; capping it short threw away its advantage and fell back to the weaker/pricier model. | Keep the generous shared budget. If prompts grow, raise the reconciler cutoff rather than shrinking DeepSeek's budget. | Done |
| L6 | **Dependency-chain fixes still incomplete** (lagom-humanizer). | Few-shot example for full peer-dep chains (react → react-dom → @types/react → @types/react-dom). | P2 (carried) |
| L7 | **Prash reads broadly — good.** Appi-Claw: it caught a latent `json`-scope bug nobody planted. | Keep; this is the data-flywheel payoff. Surface "bonus findings" explicitly in the PR body. | Nice-to-have |

#### Measured performance (production `agent_calls`, last 200 calls)

| Metric | DeepSeek V4 Pro | Kimi K2.6 (fallback) |
|--------|-----------------|----------------------|
| Calls sampled | 77 | 89 |
| Valid structured output | **98%** | 100% |
| Latency median | **17.3s** | 10.7s |
| Latency max | **103.6s** | 48.8s |
| Calls > 70s (old cap would kill) | **2** | 0 |
| Cost / call (est.) | ~$0.0095 | ~$0.003 (partial) |

**Repo-level success this campaign:** 4/5 verified (80%); **fully autonomous: 3/5 (60%)** — matches the 60% end-to-end target, but IRIS needed assistance and lagom failed, so there is real headroom (L1–L3).

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
| Multi-CI provider support | Add adapters for CircleCI, GitLab CI, Jenkins, Bitbucket Pipelines. ~1-1.5 days per provider. Core pipeline is CI-agnostic — each adapter needs: webhook route, log fetcher, connect/setup flow. Keep GitHub Actions as default | New |
| Confidence recalibration | Outcome data now exists — recalibrate against actual merge/revert rates | Ready |
| Dependency chain fix completeness | When bumping a dep, also bump all `@types/*` peers and transitive requirements. Add few-shot example: react→react-dom→@types/react→@types/react-dom full chain. Tested gap: lagom-humanizer test. | P2 |
| RAG upgrade — embeddings | Replace keyword RAG with semantic search over past fixes | New |
| Learning flywheel — few-shot | Retrieve similar past failures as few-shot context in prompts | New |
| Multi-provider CI | Support CircleCI, GitLab CI, Jenkins, Bitbucket Pipelines — one adapter per provider (webhook + log fetcher), diagnosis pipeline stays unchanged | New |
| CLI client (`prash`) | Thin client over the existing API — `prash init` (repo auth), `prash diagnose` (local diagnosis before push), `prash why <incident>` (pull from production-awareness layer). Build as thin client only, never a second agent runtime. Requires production-awareness backend (N+5) before `prash why` is useful. Modeled after Claude Code's CLI distribution strategy. | New — after N+5 |

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

**Cloud Run:** `drufiy-backend-00155-kbc` (asia-south1) — CPU throttling ON (default); background tasks throttled post-response
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
        → (sanity check removed — CI on fix branch is the real gate)
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
| Latency p50 (DeepSeek) | **17.3s** (measured 2026-06-21) | <20s ✅ |
| Latency max (DeepSeek) | **103.6s** (measured) | within 240s budget ✅ |
| DeepSeek valid-output rate | **98%** (measured, n=77) | >95% ✅ |
| Cost per call (DeepSeek) | **~$0.0095** (measured) | track |
| workflow_config success rate | Was 0% (A8 fixed) | 50%+ |
| environment success rate | Was 0% | 30%+ (with needs_secret UI) |

---

*Previous documents: MODEL_AUDIT_AND_FIX_PLAN.md (raw audit), IMPROVEMENTS.md (original feature roadmap), SPRINT_PLAN.md (initial sprint)*
*This document supersedes all three as the active roadmap.*
