# Prash — Model Audit & File-by-File Fix Plan
*Generated 2026-06-14. Brutal, data-backed, no bias. All code below is for one engineer (Aradhya) to execute — not split.*

---

## 0. TL;DR

- **End-to-end success rate is ~18%** (19/103 runs verified). The largest single bucket is **`diagnosis_failed` (29%)** — the model failing to return a *schema-valid answer at all*. This is plumbing, not intelligence.
- **You do not have a model-quality problem yet. You have a reliability-and-delivery problem wearing a model-quality costume.** Your *hardest* category (code) is your *best* (46%); your *easiest* (workflow_config, environment) are **0%**.
- Three things are silently broken in production right now: **RAG never runs** (wrong column name), **temperature config kills runs** (3 confirmed), and **the investigation loop burns its steps and gives up** (#1 error string in the DB).
- Fix the plumbing first (Week 1), unblock the 0% categories (Week 2), then tune the model against the **eval harness** now living in `evals/` (Week 3). Never tune the prompt again without running it.

---

## 1. The data (pulled live from Supabase, 103 runs)

### 1.1 Outcomes
| Status | Count | % |
|---|---|---|
| verified (real success) | 19 | 18% |
| **diagnosis_failed** | **30** | **29%** |
| exhausted | 26 | 25% |
| diagnosed (silent stop) | 21 | 20% |
| pending (stuck) | 7 | 7% |

> ⚠️ ~60-70% of this traffic is your own smoke testing (trimly/lagom/Iris). The real-user success rate is **unknown** because nothing tags test vs real traffic — see Fix A13.

### 1.2 Success by category
| Category | Verified/Total | Rate |
|---|---|---|
| code | 16/35 | **46%** |
| dependency | 2/5 | 40% |
| environment | 0/17 | **0%** |
| workflow_config | 0/12 | **0%** |
| NO_DIAGNOSIS | 0/24 | 0% |

### 1.3 Where it actually breaks (error strings, verbatim from DB)
| Failure signature | Where | Root cause |
|---|---|---|
| "Kimi investigation loop did not yield a final diagnosis" (11+) | diagnosis_failed/exhausted | Investigation loop burns steps with no forced submit → Fix A4 |
| "No tool call in response" / "Failed to parse tool arguments" | many `agent_calls` rows | Tool-call reliability → Fix A4/A5 |
| "invalid temperature: only 1 is allowed for this model" (3) | diagnosis_failed | Hardcoded `temperature=0.6` → Fix A3 |
| "violates check constraint" (diagnoses) (3) | exhausted | Off-enum category / out-of-range value → Fix A7 |
| "forbidden — token lacks required scope" / "resource not found" | PR creation | OAuth missing `workflow` scope → Fix A8 (this is *why* workflow_config = 0%) |
| "DeepSeek returned no valid tool call" / "thinking is enabled" 400 | older runs | Broken fallback, since disabled → Fix A6 |
| latency 144s, 379s | `agent_calls` | Unbounded loop + 240s timeouts → Fix A9 |

### 1.4 Latency (from `agent_calls`)
Most calls 5–20s, but outliers at **144,833ms and 379,035ms**. The eval harness confirmed **60s and 116s** for a *single-shot* diagnosis on a real case. The full pipeline (investigation loop + sanity review + iteration) compounds this.

---

## 2. File-by-file fixes

> Ordered P0 → P2. Each block is self-contained. Run `python -m evals.run_eval` before and after each to measure.

---

### 🔴 Fix A1 — RAG is dead (1 word) · `app/agent/processor.py:849`

`_fetch_similar_fixes` queries a column that does not exist (`connected_repo_id`). Confirmed against the live DB: `column ci_runs.connected_repo_id does not exist`. The `except` swallows it → returns `[]` **every run**. Your "learns from every fix" story has never executed once.

```python
# processor.py — _fetch_similar_fixes(), line ~849
# BEFORE
            .eq("connected_repo_id", repo_id)
# AFTER
            .eq("repo_id", repo_id)
```

While you're here, broaden it so it isn't starved by the same-repo + verified-only gate (only 16 verified diagnoses exist total). Match by category across repos:

```python
        diag_resp = (
            supabase.table("diagnoses")
            .select("problem_summary, root_cause, fix_description, category, confidence, files_changed")
            .eq("verification_status", "verified")
            .neq("fix_type", "manual_required")
            .order("created_at", desc=True)
            .limit(50)               # pull a pool…
            .execute()
        )
        # …then prefer same-repo, fall back to same-category cross-repo
        pool = diag_resp.data or []
        same_repo = [d for d in pool if d.get("run_id") in set(run_ids)]
        fixes = (same_repo or pool)[:limit]
```

**Add a regression test** (`tests/test_rag.py`): assert `_fetch_similar_fixes` returns a non-empty list when a verified diagnosis exists for the repo. This bug shipped because nothing tested the happy path.

---

### 🔴 Fix A2 — Config drift boots-breaks the app · `app/config.py`

The local `.env` still carries `NVIDIA_*` / `GEMINI_*` keys, but `config.py` dropped those fields and pydantic-settings **forbids extras** — so `Settings()` raises on startup with this `.env`. The eval harness hit this immediately. It's a latent landmine: any environment still carrying old keys won't boot.

```python
# config.py:53
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")
```

Also fix cost tracking (every `estimated_cost_usd` is currently `None`) by giving Kimi default prices and adding the fallback-model registry (used by Fix A6):

```python
    # Kimi pricing (Moonshot K2.6, USD per 1M tokens) — set so cost tracking works
    kimi_input_price_per_1m_tokens: float | None = 0.60
    kimi_output_price_per_1m_tokens: float | None = 2.50

    # ── Fallback / consensus model (OpenAI-compatible) ──────────────────────
    fallback_enabled: bool = False
    fallback_api_key: str | None = None
    fallback_base_url: str | None = None
    fallback_model: str | None = None
    fallback_input_price_per_1m_tokens: float | None = None
    fallback_output_price_per_1m_tokens: float | None = None
```

Then **delete the dead `NVIDIA_*`/`GEMINI_*` lines from every `.env`** (local + Cloud Run) so secrets don't linger.

---

### 🔴 Fix A3 — Temperature kills runs · `app/agent/kimi_client.py`

`temperature=0.6` is hardcoded in three places with a comment asserting it's "required." For some model variant the API rejects it outright ("only 1 is allowed"). Magic numbers the provider disagrees with. Make it config-driven **and** self-healing: on a temperature 400, retry once at `temperature=1`.

Add a single resilient wrapper and route all `kimi.chat.completions.create` calls through it:

```python
# kimi_client.py — new helper
async def _create_chat(client, **kwargs):
    """Create a completion, transparently recovering from per-model temperature rules."""
    try:
        return await client.chat.completions.create(**kwargs)
    except Exception as e:
        msg = str(e).lower()
        if "temperature" in msg and kwargs.get("temperature") != 1:
            logger.warning("Model rejected temperature=%s — retrying at 1", kwargs.get("temperature"))
            kwargs["temperature"] = 1
            return await client.chat.completions.create(**kwargs)
        raise
```

Replace each `await kimi.chat.completions.create(...)` with `await _create_chat(kimi, ...)` in `_call_kimi_reasoning` (line ~125), `_call_kimi_structured` (line ~177), and `_call_kimi_with_tools` (line ~325). Same for the fallback client.

---

### 🔴 Fix A4 — The investigation loop burns its steps and gives up · `app/agent/kimi_client.py:410` (`call_with_investigation`)

This is the **#1 cause of `diagnosis_failed`/`exhausted`.** Today the loop ([kimi_client.py:425](app/agent/kimi_client.py:425)) calls with all tools and **no `tool_choice`**, so the model can emit prose and call nothing. When that happens, the code logs an error but **does not append anything to `messages`**, so the next iteration re-sends an identical request → identical no-op → all 3 steps wasted → one final forced call that has to one-shot it or the run dies.

Rewrite the loop with three changes: (1) on a no-tool-call step, **nudge** instead of repeating; (2) **break early** to the forced submit once the model has gathered context; (3) make the forced final **retry-safe** (uses Fix A3).

```python
async def call_with_investigation(
    system_prompt, user_prompt, diagnosis_tool_schema, investigation_tools,
    execute_tool, run_id=None, call_type="diagnosis", max_steps=4,
) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    all_tools = investigation_tools + [diagnosis_tool_schema]

    for step in range(max_steps):
        message, raw, usage = await _call_kimi_with_tools(messages, all_tools)

        # No tool call → don't waste the step re-sending; nudge the model forward.
        if not message or not message.tool_calls:
            _log_agent_call(run_id, f"{call_type}_step_{step+1}", settings.kimi_model,
                            messages, raw, None, usage, valid=False,
                            error="no tool call — nudging")
            messages.append({"role": "assistant", "content": (message.content if message else "") or ""})
            messages.append({"role": "user", "content":
                "You did not call a tool. Either call an investigation tool to gather "
                "what you still need, or call submit_diagnosis now with your best fix."})
            continue

        tool_call = message.tool_calls[0]
        tool_name = tool_call.function.name
        try:
            tool_args = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            tool_args = {}

        # Model submitted → done.
        if tool_name == diagnosis_tool_schema["name"]:
            _log_agent_call(run_id, f"{call_type}_step_{step+1}", settings.kimi_model,
                            messages, raw, tool_args, usage, valid=True)
            return tool_args

        # Investigation tool → execute, append result, continue.
        tool_result = await execute_tool(tool_name, tool_args)
        assistant_msg = {
            "role": "assistant", "content": message.content or "",
            "tool_calls": [{"id": tool_call.id, "type": "function",
                            "function": {"name": tool_name, "arguments": tool_call.function.arguments}}],
        }
        if getattr(message, "reasoning_content", None):
            assistant_msg["reasoning_content"] = message.reasoning_content
        messages.append(assistant_msg)
        messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_result})
        _log_agent_call(run_id, f"{call_type}_step_{step+1}", settings.kimi_model,
                        messages, raw, {"tool": tool_name}, usage, valid=True)

    # Budget spent → force a structured submit (thinking off, temp-resilient via Fix A3).
    final_messages = messages + [{"role": "user", "content":
        "Investigation complete. Submit your structured diagnosis now using submit_diagnosis."}]
    args, raw, usage = await _call_kimi_structured(final_messages, diagnosis_tool_schema)
    _log_agent_call(run_id, f"{call_type}_final", settings.kimi_model, final_messages, raw,
                    args, usage, valid=(args is not None),
                    error=None if args else "forced final produced no tool call")
    if args is None:
        raise DiagnosisValidationError("Investigation loop did not yield a final diagnosis.")
    return args
```

Expected impact: this single fix should recover a large chunk of the 29% `diagnosis_failed`. **Measure it** with the harness `--live`.

---

### 🔴 Fix A5 — A blank reasoning pass aborts the whole run · `app/agent/kimi_client.py:236` (`_call_kimi`)

`if not reasoning: return None` makes an *empty thinking response* fatal — even though the structured call alone might succeed. Fall through instead:

```python
    reasoning, reasoning_raw, reasoning_usage = await _call_kimi_reasoning(messages)
    if not reasoning:
        logger.info("Empty reasoning — falling through to direct structured call")
        args, structured_raw, structured_usage = await _call_kimi_structured(messages, tool_schema)
        return args, structured_raw, _merge_usage(reasoning_usage, structured_usage)
```

---

### 🟠 Fix A6 — There is no fallback model; Kimi is a single point of failure · `app/agent/kimi_client.py:519` (`call_with_tool`)

[kimi_client.py:533](app/agent/kimi_client.py:533) hard-disables DeepSeek. When Kimi fails twice, the run dies with zero redundancy. Re-introduce a **generic** fallback driven by the config registry from Fix A2 (provider-agnostic — works for DeepSeek V4 Pro, MiniMax M3, whatever the eval picks):

```python
# module level
fallback_client = (
    AsyncOpenAI(api_key=settings.fallback_api_key, base_url=settings.fallback_base_url, timeout=90.0)
    if settings.fallback_enabled and settings.fallback_api_key else None
)

async def call_with_tool(system_prompt, user_prompt, tool_schema, run_id=None,
                         call_type="diagnosis", temperature=0.6, model="auto") -> dict:
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]

    # Attempts 1-2: Kimi (primary)
    for attempt in (1, 2):
        args, raw, usage = await _call_kimi(messages, tool_schema)
        _log_agent_call(run_id, call_type, settings.kimi_model, messages, raw, args, usage,
                        valid=(args is not None),
                        error=None if args else f"kimi attempt {attempt} no tool call")
        if args is not None:
            return args

    # Attempt 3: fallback model (redundancy)
    if fallback_client:
        logger.warning("Kimi failed twice — trying fallback %s", settings.fallback_model)
        args, raw, usage = await _call_openai_compatible_fallback(
            fallback_client, settings.fallback_model, messages, tool_schema, "fallback")
        _log_agent_call(run_id, call_type, settings.fallback_model or "fallback", messages, raw,
                        args, usage, valid=(args is not None),
                        error=None if args else "fallback no tool call")
        if args is not None:
            return args

    raise DiagnosisValidationError("No model produced a valid tool call (kimi x2 + fallback).")
```

Also update `_estimate_cost_usd` to match `settings.fallback_model` and its prices. **Model choice is decided by the eval harness, not by vibes** — see §3.

---

### 🟠 Fix A7 — Off-enum categories crash inserts · `app/agent/schemas.py`

The DB holds categories (`env_config`, `import_error`, `database_migration`) that aren't in your `Literal`, and "violates check constraint" killed runs. Normalize *before* validation so a near-miss repairs instead of crashing:

```python
# schemas.py — inside Diagnosis
_CATEGORY_ALIASES = {
    "env_config": "environment", "env": "environment", "secrets": "environment",
    "import_error": "dependency", "imports": "dependency", "deps": "dependency",
    "database_migration": "code", "db_migration": "code",
    "ci": "workflow_config", "ci_config": "workflow_config", "workflow": "workflow_config",
    "flaky": "flaky_test", "test": "code",
}

@field_validator("category", mode="before")
@classmethod
def _normalize_category(cls, v):
    if isinstance(v, str):
        v = v.strip().lower()
        return cls._CATEGORY_ALIASES.get(v, v)
    return v
```

If `diagnoses.category` ever gains a DB `CHECK`, keep it in lockstep with this `Literal`. (It currently has none — the crash came from `confidence`/`iteration` checks; verify `iteration` never exceeds 2 in the retry path, [processor.py:206](app/agent/processor.py:206).)

---

### 🟠 Fix A8 — Workflow fixes can't be committed → the 0% workflow_config · `app/agent/pr_creator.py` + frontend authorize URL

Editing `.github/workflows/*.yml` requires the GitHub **`workflow`** OAuth scope. Your PR PUTs are getting 403 "token lacks required scope" — which is almost certainly *why* workflow_config verifies 0/12: the diagnosis is fine, the delivery is rejected.

1. **Frontend** (`Prash-frontend`): the OAuth authorize URL must request `scope=repo workflow`. The backend only *reads* granted scopes ([github_oauth.py:55](app/routes/github_oauth.py)) and already has a `/scopes` check — so the request side is the gap. Verify and fix the `authorize?...&scope=` string.
2. **Backend**: give a *specific, actionable* error when a workflow file is rejected, so the UI can prompt re-auth instead of dumping "forbidden":

```python
# pr_creator.py — _put_file(), replace the final status check
    resp = await client.put(f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}", json=body)
    if resp.status_code == 403 and path.startswith(".github/workflows/"):
        raise PRCreationError(
            "WORKFLOW_SCOPE_REQUIRED: editing workflow files needs the GitHub 'workflow' "
            "scope. Ask the user to reconnect and grant it (or use the GitHub App).")
    if resp.status_code not in (200, 201):
        _raise_github_error(resp, f"Failed to commit {path}")
```
3. **Strategic**: the GitHub App path already exists (`get_installation_token`, [github_app.py:28](app/github_app.py:28)). Finishing the App migration with **Contents: write + Workflows: write** permissions removes this whole class of failure *and* fixes org repos. This is the highest-leverage delivery fix.

---

### 🟠 Fix A9 — Cap latency · `app/agent/diagnosis_agent.py` + `kimi_client.py`

1. Drop the Kimi client timeout from 240s → **90s** ([kimi_client.py:18](app/agent/kimi_client.py:18)). A single 379s call is never worth it.
2. Hard-cap the whole diagnosis wall-clock in `diagnose_failure`:

```python
# diagnosis_agent.py — wrap the model call section
import asyncio
try:
    raw_args = await asyncio.wait_for(_run_diagnosis_call(...), timeout=120)
except asyncio.TimeoutError:
    raise DiagnosisValidationError("Diagnosis exceeded 120s budget")
```
3. Parallelize: `_fetch_relevant_files` + `_fetch_commit_diff` already run before the model; ensure they're `asyncio.gather`-ed, not awaited serially ([processor.py:99-115](app/agent/processor.py:99)).

---

### 🟡 Fix A10 — 7 runs stuck in `pending` forever · `app/agent/reconciler.py`

The reconciler already sweeps `diagnosing`/`applying`/`fixed` (good — the old roadmap is stale here). But **nothing sweeps `pending`**, and `process_failure` is fired via `background_tasks` — if the worker dies before it starts, the run is orphaned. Add:

```python
# reconciler.py — call from reconcile_stuck_verifications()
async def _recover_stuck_pending(cutoff: str) -> int:
    stuck = (supabase.table("ci_runs").select("id")
             .eq("status", "pending").lt("created_at", cutoff).execute()).data or []
    from app.agent.processor import process_failure
    resolved = 0
    for run in stuck:
        try:
            await process_failure(run["id"]); resolved += 1
        except Exception as e:
            logger.warning(f"Reconciler: failed to requeue pending {run['id'][:8]}: {e}")
    return resolved
```
Use a generous cutoff (10 min). Wire it into the sweep list at [reconciler.py:62](app/agent/reconciler.py:62).

---

### 🟡 Fix A11 — `diagnosed` is a silent black hole (20% of runs) · `app/agent/processor.py`

manual_required / sanity-rejected / high-risk-downgraded runs all land at `diagnosed` and **just sit** — no PR, no next action. `environment` (0/17) is entirely this. Two changes:

1. **environment with safe defaults → still ship a PR.** When `category=environment` and the missing vars have safe CI defaults (`CI`, `NODE_ENV`, `PORT`, `RAILS_ENV`), add them to the workflow YAML automatically (the prompt already says to — enforce it in code by checking `required_secrets` against a safe-default map and synthesizing a YAML `env:` patch).
2. **Real secrets → an explicit, surfaced state.** Instead of generic `diagnosed`, set `status="needs_secret"` and ensure `required_secrets` reaches the UI so the user gets a 1-click "Add Secret" form. A named state the dashboard can render beats a dead "diagnosed".

> This is the difference between "Prash did nothing" and "Prash told me exactly which secret to add." For 1 in 6 failures, that's the whole product experience.

---

### 🟡 Fix A12 — Log preprocessing deletes the evidence · `app/agent/diagnosis_agent.py:479` (`_filter_section_lines`)

The regex keep-only filter drops any failure line that doesn't match a keyword (subtle assertion diffs, unusually-phrased errors). Always preserve the **tail of the last section verbatim** as a safety net:

```python
# _preprocess_logs — after building result_parts, before returning:
    # Safety net: always append the raw last 40 lines of the final section,
    # so a non-keyword failure line is never fully discarded.
    last_body = splits[-1] if splits else raw
    tail = "\n".join(last_body.splitlines()[-40:])
    return "\n\n".join(result_parts) + "\n\n=== RAW TAIL (last 40 lines) ===\n" + tail
```

---

### 🟡 Fix A13 — You can't tell real traffic from your own tests · schema + `app/webhook.py`

~60-70% of the 103 runs are your smoke tests, so every metric is polluted. Add a `source` column and tag it:

```sql
ALTER TABLE public.ci_runs ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'user';
```
```python
# webhook.py — when inserting ci_run, detect internal test commits
commit_message = (workflow_run.get("head_commit") or {}).get("message", "")
source = "smoke_test" if "drufiy smoke test" in commit_message.lower() else "user"
# add  "source": source  to the insert payload (webhook.py:462)
```
Then your `/admin/stats` and the eval seeder can filter to `source='user'` for honest numbers.

---

### 🟢 Fix A14 — Sanity review is theater · `app/agent/processor.py:789`

`_sanity_check_fix` uses **Kimi to grade Kimi** and **fails open** (approve on any error). Near-zero independent signal, plus latency and cost. Either (a) route it through the *fallback* model from Fix A6 so it's a genuine second opinion, or (b) delete it and rely on the diff-risk guard + verification loop. Don't keep paying for a rubber stamp.

---

## 3. Model strategy

Your instinct (cheap + fast + accurate + strong on benchmarks) is right, but **don't pick by reading benchmark blogs — pick by running `evals/`.** That's exactly what the harness is for.

**Recommendation:**
- **Keep Kimi K2.6 as primary.** Your own memory flags DeepSeek V4 *Flash* at 55.2% LiveCodeBench vs K2.6's 89.6% with an 11% silent tool-call failure rate. Don't regress the primary to save pennies — the cost problem is latency and retries, not the per-token price.
- **Add exactly one fallback** for redundancy + consensus (Fix A6), chosen empirically. Candidates to bench: **DeepSeek V4 Pro** (not Flash), **MiniMax M3**. Wire each into `fallback_*` config, run `python -m evals.run_eval --label kimi-only` then `--label kimi+minimax --baseline …`, and keep whichever lifts `valid_diagnosis_rate` and `category_acc` without wrecking p90 latency.
- **Use the fallback for consensus on the hard cases only** (category=unknown or confidence<0.5), not every call — that controls cost while raising accuracy where it matters.

The registry in Fix A2 makes swapping the fallback a **config change, not a code change** — so this becomes an experiment you run weekly, not a rewrite.

---

## 4. The eval harness (already built, in `evals/`)

This is your single most important new asset. Without it, every prompt tweak is a guess shipped to prod — which is exactly how you got here.

```
evals/
  seed_from_db.py   # reconstructs golden cases from real runs (already run → 14 cases)
  cases/*.json      # committed golden cases (verified runs = ground truth)
  run_eval.py       # replays cases through diagnose_failure(), scores, diffs vs baseline
  score.py          # rubric: valid_diagnosis%, category_acc, actionability, file_recall, latency
  README.md         # how to run + known limitations
```

Validated working — a live 2-case run scored `valid_diagnosis 100%`, `category 100%`, `file_recall 1.0`, and surfaced the latency problem (60s/116s) and the config-drift bug.

**Workflow going forward:**
```bash
python -m evals.run_eval --label before          # baseline
# … apply a fix …
python -m evals.run_eval --label after --baseline evals/results/before.json
```
The diff prints `valid_diagnosis`, `category_acc`, `actionability`, `file_recall`, `latency_p90` deltas. **Merge nothing that drops valid_diagnosis.**

**Two gaps to close in the harness (do these in Week 1):**
1. **Hand-author cases for the 0% categories.** The seeder only mines *verified* runs, and only `code`/`dependency` ever verified — so `workflow_config` and `environment` have zero coverage. Drop hand-built JSONs (schema in the README) for the exact failures you keep losing. *The benchmark is currently blind where the product is weakest.*
2. **Run `--live` periodically** to exercise the agentic loop (default mode is single-shot and won't catch loop regressions — i.e. Fix A4).

---

## 5. Sequenced rollout

**Week 1 — stop the bleeding (pure bugs, ~1 day each, all measurable on `evals/`)**
A1 RAG column · A2 config drift + cost · A3 temperature · A4 investigation loop · A5 reasoning fall-through · A7 category normalize · A10 pending sweep · A13 source flag · + hand-author 0%-category eval cases.
→ Expected: big cut into the 29% `diagnosis_failed` and the 7 stuck `pending`.

**Week 2 — unblock the 0% categories**
A8 workflow scope / GitHub App (Contents+Workflows write) · A11 kill the `diagnosed` black hole (env defaults + `needs_secret` state).
→ Expected: workflow_config and environment move off 0%.

**Week 3 — now tune the model, with measurement**
A6 fallback registry + consensus on hard cases · A9 latency caps · A14 real second-opinion (or delete) · run the fallback bake-off through `evals/`.
→ Expected: higher accuracy on the genuinely hard cases, controlled cost and latency.

**The order matters.** Reaching for a smarter model in Week 1 would mask the plumbing bugs and cost you weeks. Fix delivery, make it measurable, *then* optimize intelligence.
```
