# Prash — Engineering Improvement Roadmap

> Full audit of the current pipeline. Every gap, every fix, prioritized.
> Models in use: **Kimi K2.6** (primary) + **DeepSeek** (fallback/consensus).

---

## OWNERSHIP

| Person | Role | Focus |
|--------|------|-------|
| **Maneesh** 🔧 | Backend Engineer | Model layer, agent intelligence, infrastructure, complex pipeline changes |
| **Aradhya** 🎯 | Product Engineer | Product features, quick wins, observability, integrations, growth |

Items marked 🔧 = Maneesh · Items marked 🎯 = Aradhya · Items marked 🤝 = both

---

## TABLE OF CONTENTS

1. [Quick Win — Org/Collab Repo Bug](#a-quick-win--orgcollab-repo-bug)
2. [Kill `manual_required` — Full Autonomy](#b-kill-manual_required--full-autonomy)
3. [Stop Kimi from Hallucinating](#c-stop-kimi-from-hallucinating)
4. [Pipeline Architecture Gaps](#d-pipeline-architecture-gaps)
5. [Reliability + Observability](#e-reliability--observability)
6. [GitHub App Migration](#f-github-app-migration)
7. [Crazy High-Impact Ideas](#g-crazy-high-impact-ideas)
8. [Priority Build Order](#priority-build-order)

---

## A. QUICK WIN — Org/Collab Repo Bug
> 🎯 **Aradhya** — 1-line fix, ship today

**File:** `app/routes/repos.py` line 39

**Current code:**
```python
params={"affiliation": "owner,collaborator", ...}
```

**Problem:** Misses all organization repos entirely. A user who is a member of `acme-corp` org but doesn't own the repos cannot connect them.

**Fix — 1 line change:**
```python
params={"affiliation": "owner,collaborator,organization_member", ...}
```

**Second problem:** Webhook install at line 122 fails silently for org repos because the user token has `repo` scope but not `admin:repo_hook` on org repos.

**Fix:** When `hook_resp.status_code == 403` for an org repo, return a clear error:
```
"Your GitHub token lacks webhook permissions for org repos.
 Ask your org admin to grant admin:repo_hook, or switch to the Drufiy GitHub App."
```

Long-term fix → see Section F (GitHub App).

---

## B. KILL `manual_required` — FULL AUTONOMY
> 🤝 Split between Maneesh and Aradhya — see per-subsection ownership

Right now the pipeline bails out and says "manual_required" for:
- Missing environment secrets
- Flaky tests
- "Unknown" category failures
- Confidence < 0.4

**This defeats the entire product purpose.** Every `manual_required` is a failure. Here's how to autonomously handle each case:

---

### B1. Environment/Secrets → Auto-detect + 1-click UI
> 🎯 **Aradhya** — schema change + prompt addition + UI wire-up

**Current behaviour:** Kimi detects `STRIPE_KEY is not defined` → returns `manual_required` → user is on their own.

**What should happen:**

1. When `category == "environment"`, Kimi **must extract the exact secret name(s)** from logs into a new field `required_secrets: ["STRIPE_KEY", "DATABASE_URL"]`
2. The API returns this list with the run
3. The UI auto-populates the "Add Secret" form — user just types the value and clicks "Add"
4. For non-sensitive env vars that have common defaults (`CI=true`, `NODE_ENV=test`, `PORT=3000`) — **add them directly to the workflow YAML** as `env:` keys. No user action needed at all.

**Schema change needed:**
```python
# In schemas.py — add to Diagnosis
required_secrets: list[str] = Field(default_factory=list,
    description="Secret names that need to be set for this failure to be fixed")
```

**Prompt change needed:**
```
When category=environment:
  - Extract EVERY secret/env var name from the logs
  - List them in required_secrets
  - For secrets with obvious safe defaults (CI, NODE_ENV, PORT), add them
    to the workflow YAML in files_changed instead of required_secrets
```

---

### B2. Flaky Tests → Auto-retry, then Auto-skip
> 🔧 **Maneesh** — pipeline logic in `processor.py`

**Current behaviour:** `is_flaky_test=true` → `manual_required` → user has to manually skip it.

**What should happen:**

**Step 1 — Auto-retry (free, zero code change):**
- When `is_flaky_test=true` detected, immediately trigger `POST /repos/{repo}/actions/runs/{id}/rerun-failed-jobs`
- This resolves ~40% of flaky test failures by itself

**Step 2 — If retry also fails:**
- Automatically call the existing `skip-test` logic
- Create the skip PR without waiting for user action
- The `skip-test` endpoint code already works — just call it from the pipeline

**Implementation in `processor.py`:**
```python
if diagnosis.is_flaky_test:
    # Step 1: try a re-run first
    rerun_success = await _trigger_rerun(github_run_id, repo_full_name, access_token)
    if not rerun_success:
        # Step 2: auto-skip the test
        await _auto_skip_test(ci_run_id, repo_full_name, access_token, diagnosis)
    return  # Never reaches manual_required
```

---

### B3. "Unknown" Category → Multi-model Consensus
> 🔧 **Maneesh** — parallel model calls, consensus logic

**Current behaviour:** Kimi says `category: unknown` → `manual_required` → done.

**What should happen:**

When Kimi returns `category: unknown` OR `confidence < 0.5`:
1. Fire the **same logs** at **DeepSeek** in parallel
2. If DeepSeek returns a confident, different diagnosis → use it
3. If both agree on the same root cause → confidence goes up, proceed with fix
4. Only if both genuinely disagree → then it is truly unknown

**Implementation:**
```python
# In processor.py
if diagnosis.category == "unknown" or diagnosis.confidence < 0.5:
    deepseek_diagnosis = await diagnose_failure(
        logs=logs, ..., model="deepseek"
    )
    diagnosis = _merge_diagnoses(diagnosis, deepseek_diagnosis)
```

**`_merge_diagnoses` logic:**
- If both agree on root_cause → merge, average confidence, use fix from higher-confidence one
- If DeepSeek is more confident → use DeepSeek's diagnosis
- If both are unknown → truly manual

---

### B4. Low Confidence → Speculative PR
> 🎯 **Aradhya** — threshold tweak in `diagnosis_agent.py`, PR title tagging

**Current behaviour:** `confidence < 0.4` → downgraded to `manual_required` → nothing.

**What should happen:**

Instead of killing the pipeline:
1. Create a `review_recommended` PR tagged `[SPECULATIVE]` in the title
2. Add a PR comment: *"Drufiy is 30% confident this fixes the issue. Please review carefully before merging."*
3. Track whether speculative PRs get merged — that becomes training signal for improving the model
4. **Never return `manual_required` for a code/dependency/workflow failure.** A bad-but-reviewable PR is always better than nothing.

**Threshold change in `diagnosis_agent.py`:**
```python
# Current: confidence < 0.4 → manual_required
# New: never downgrade to manual_required for non-environment/non-flaky failures
if diagnosis.confidence < 0.4 and diagnosis.category not in ("environment", "flaky_test"):
    updates["fix_type"] = "review_recommended"  # speculative, not manual
    # Do NOT set manual_required
```

---

## C. STOP KIMI FROM HALLUCINATING
> 🔧 **Maneesh** owns this entire section — model layer, prompt engineering, client changes

### C1. Re-enable Thinking (Two-Call Pattern)

**File:** `app/agent/kimi_client.py`

**Current problem:**
```python
extra_body={"thinking": {"type": "disabled"}}
```

Chain-of-thought is **disabled** because it conflicts with forced `tool_choice`. This is the #1 cause of hallucinations — Kimi is answering without reasoning.

**Fix — Two-call pattern:**

**Call 1 (thinking ON, free-text):**
```python
response_1 = await client.chat.completions.create(
    model="kimi-k2.6",
    messages=[{
        "role": "user",
        "content": f"Analyze this CI failure. Reason step by step:\n{logs}\n\nFiles:\n{files}\n\nWhat is the root cause? What file needs to change? What exactly on what line?"
    }],
    extra_body={"thinking": {"type": "enabled", "budget_tokens": 2000}}
)
reasoning = response_1.choices[0].message.content
```

**Call 2 (thinking OFF, forced tool_choice):**
```python
response_2 = await client.chat.completions.create(
    model="kimi-k2.6",
    messages=[
        {"role": "user", "content": f"CI failure logs:\n{logs}"},
        {"role": "assistant", "content": f"My analysis: {reasoning}"},
        {"role": "user", "content": "Now submit your structured diagnosis using the tool."}
    ],
    tools=[DIAGNOSIS_TOOL],
    tool_choice={"type": "function", "function": {"name": "submit_diagnosis"}},
    extra_body={"thinking": {"type": "disabled"}}
)
```

Result: Full reasoning quality + structured output. Fewer hallucinations.

---

### C2. Fetch More Context — Files + Commit Diff
> 🔧 **Maneesh**

**File:** `app/agent/processor.py`

**Current limits:**
- `max_files = 6` — too low for monorepos
- Never fetches the CI workflow file that failed
- Never fetches `tsconfig.json`, `jest.config.js`, `pyproject.toml`
- **Never fetches the commit diff** — the most valuable signal of all

**Fixes:**

**Bump max_files to 12:**
```python
max_files: int = 12
```

**Always fetch the failing workflow file:**
```python
# Add to _MANIFEST_FILES
_MANIFEST_FILES = [
    "package.json", "requirements.txt", "pyproject.toml",
    "Cargo.toml", "go.mod", "pom.xml", "Gemfile",
    f".github/workflows/{workflow_name.lower().replace(' ', '-')}.yml",  # ADD THIS
    "tsconfig.json", "jest.config.js", "jest.config.ts",  # ADD THESE
]
```

**Fetch the breaking commit diff (add to `processor.py`):**
```python
async def _fetch_commit_diff(commit_sha: str, repo_full_name: str, access_token: str) -> str:
    """Fetch what changed in the commit that broke CI. Most valuable signal."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.diff"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/commits/{commit_sha}",
            headers=headers,
        )
    if resp.status_code != 200:
        return ""
    # Truncate to 8KB — we need signal, not the full diff
    return resp.text[:8000]
```

Then in `diagnose_failure` user prompt:
```python
if commit_diff:
    parts.append(f"\nCOMMIT DIFF (what changed to break CI):\n---\n{commit_diff}\n---")
    parts.append("The fix should likely modify these same files.")
```

---

### C3. Import-Chain Tracing
> 🔧 **Maneesh**

When `src/auth.py` fails, the real bug might be in `src/utils.py` that `auth.py` imports from. Currently Prash never follows imports.

**Fix — Add to `_fetch_relevant_files`:**
```python
# After fetching a file, parse its imports and queue them too
import_re = re.compile(r"^(?:from|import)\s+([\w.]+)", re.MULTILINE)
for fetched_path, content in list(result.items()):
    if fetched_path.endswith(".py"):
        for match in import_re.finditer(content):
            module = match.group(1).replace(".", "/")
            for ext in [".py", "/index.py"]:
                candidate = f"src/{module}{ext}"
                if candidate not in result and len(result) < max_files:
                    # Queue this import for fetching
                    paths_to_fetch.append(candidate)
```

---

### C4. Switch from Full File to Diff/Patch Format
> 🔧 **Maneesh** — `schemas.py` + `pr_creator.py`

**Current problem:** `new_content` requires the ENTIRE file. For a 500-line file, Kimi outputs 500 lines to change 1 line. This:
- Wastes tokens (expensive + slow)
- Causes Kimi to hallucinate/truncate the rest
- Triggers the "high risk" diff guard on files with lots of changes

**Fix — Patch-based approach:**

Add an alternative `patch` field to `FileChange`:
```python
class FileChange(BaseModel):
    path: str
    new_content: str | None = None   # Full file (current)
    patch: str | None = None          # Unified diff (new)
    explanation: str

    @model_validator(mode="after")
    def require_content_or_patch(self):
        if not self.new_content and not self.patch:
            raise ValueError("Either new_content or patch must be provided")
        return self
```

In `pr_creator.py`, apply patch server-side:
```python
import difflib, patch as patch_lib

if file_change.get("patch"):
    current = base64.b64decode(get_resp.json()["content"]).decode()
    new_content = patch_lib.apply_patch(current, file_change["patch"])
else:
    new_content = file_change["new_content"]
```

Update system prompt to prefer patches for files where full content is known.

---

### C5. Learn from Past Fixes (RAG)
> 🎯 **Aradhya** — Supabase query + prompt injection (no ML needed for v1)

Currently the `diagnoses` table stores every fix ever made but **nothing feeds it back into future diagnoses**.

**Fix — Semantic search over past successful fixes:**

On each new failure:
1. Search `diagnoses` where `verification_status = 'verified'` for similar `problem_summary`
2. Use simple keyword overlap (no embeddings needed for v1): match on error type, file extension, category
3. If a past fix matches → include it in the prompt as "this pattern was fixed before"

```python
async def _find_similar_past_fix(problem_summary: str, category: str) -> dict | None:
    # Search verified diagnoses in same category with matching keywords
    result = supabase.table("diagnoses")\
        .select("problem_summary, fix_description, files_changed")\
        .eq("verification_status", "verified")\
        .eq("category", category)\
        .limit(5)\
        .execute()
    
    if not result.data:
        return None
    
    # Simple keyword match — upgrade to embeddings later
    keywords = set(problem_summary.lower().split())
    for diag in result.data:
        past_keywords = set(diag["problem_summary"].lower().split())
        if len(keywords & past_keywords) >= 3:  # 3+ words in common
            return diag
    return None
```

Then in the prompt:
```python
if past_fix:
    parts.append(f"\nSIMILAR FIX THAT WORKED BEFORE:\n{json.dumps(past_fix, indent=2)}")
    parts.append("Consider this pattern when proposing your fix.")
```

---

## D. PIPELINE ARCHITECTURE GAPS

### D1. Increase Iterations: 2 → 4
> 🔧 **Maneesh** — `reconciler.py`

**File:** `app/agent/reconciler.py` line 170

**Current:** After 2 failed attempts → `exhausted`.

**Fix:** Allow 4 iterations. Each iteration uses higher confidence thresholds:
```python
# In reconciler.py
if max_iteration >= 4:
    mark_exhausted()
elif max_iteration == 3:
    # Iteration 4: both Kimi AND DeepSeek must agree
    run_consensus_diagnosis()
else:
    run_iteration(max_iteration + 1)
```

---

### D2. Parallel Model Calls — Kimi + DeepSeek
> 🔧 **Maneesh** — `processor.py`, `kimi_client.py`

**Current:** Sequential fallback: Kimi → Kimi retry → (Gemini/Nvidia removed). 90-120s timeout each. Worst case: 4+ minutes waiting.

**Fix:** Fire both in parallel, take first valid result:

```python
# In kimi_client.py / processor.py
async def diagnose_with_consensus(logs, ...):
    kimi_task = asyncio.create_task(
        diagnose_failure(logs, ..., model="kimi-k2.6")
    )
    deepseek_task = asyncio.create_task(
        diagnose_failure(logs, ..., model="deepseek-coder")
    )
    
    # Take first valid result
    done, pending = await asyncio.wait(
        [kimi_task, deepseek_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    
    # Cancel the slower one
    for task in pending:
        task.cancel()
    
    primary = done.pop().result()
    
    # If both finished, use agreement to boost confidence
    if not pending:
        secondary = deepseek_task.result()
        if _diagnoses_agree(primary, secondary):
            primary = primary.model_copy(update={"confidence": min(primary.confidence + 0.1, 1.0)})
    
    return primary
```

---

### D3. Agentic Loop — Give Kimi Tools to Investigate
> 🔧 **Maneesh** — biggest engineering task, own it end-to-end

**Current:** Single-shot. Logs go in → one call → done.

**Fix:** Multi-turn investigation loop before final diagnosis:

```python
INVESTIGATION_TOOLS = [
    {
        "name": "fetch_file",
        "description": "Fetch the current content of a file from the repo",
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"}
            }
        }
    },
    {
        "name": "list_directory",
        "description": "List files in a directory",
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string"}
            }
        }
    },
    {
        "name": "search_code",
        "description": "Search for a function/class/variable name in the repo",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"}
            }
        }
    },
    {
        "name": "submit_diagnosis",
        # ... existing tool schema
    }
]

# Run investigation loop (max 3 tool calls before forcing submit_diagnosis)
messages = [{"role": "user", "content": user_prompt}]
for _ in range(3):
    response = await kimi_client.call(messages, tools=INVESTIGATION_TOOLS)
    if response.tool_name == "submit_diagnosis":
        return parse_diagnosis(response)
    
    # Execute the investigation tool
    tool_result = await execute_tool(response.tool_name, response.tool_args, repo, token)
    messages.append({"role": "assistant", "tool_call": response})
    messages.append({"role": "tool", "content": tool_result})

# Force diagnosis after 3 investigation steps
return await force_submit_diagnosis(messages)
```

---

### D4. Reconciler Covers ALL Stuck States
> 🔧 **Maneesh** — `reconciler.py`

**Current:** Reconciler only sweeps `status = 'fixed'` stuck runs.

**Fix:** Sweep all stuck states:
```python
# In reconciler.py — add sweeps for:
stuck_states = {
    "diagnosing": timedelta(minutes=5),   # Kimi call hung
    "applying": timedelta(minutes=3),      # PR creation hung  
    "fixed": timedelta(minutes=3),         # Verification webhook missed
    "pending": timedelta(minutes=10),      # Background task dropped
}
```

For `diagnosing` stuck runs → reset to `pending` and re-queue `process_failure`.
For `applying` stuck runs → check if PR exists on GitHub, update status accordingly.

---

### D5. Remove Gemini and Nvidia — Replace with DeepSeek
> 🤝 **Maneesh** (config + client code) + **Aradhya** (update Cloud Run env vars, remove old API keys)

**File:** `app/config.py`

**Remove:**
```python
# DELETE THESE
nvidia_api_key: str | None = None
nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
nvidia_model: str = "meta/llama-3.3-70b-instruct"

gemini_api_key: str | None = None
gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
gemini_model: str = "gemini-2.0-flash"
```

**Add:**
```python
# DeepSeek (fallback + consensus)
deepseek_api_key: str | None = None
deepseek_base_url: str = "https://api.deepseek.com/v1"
deepseek_model: str = "deepseek-coder"
```

**Model routing strategy:**
- Primary: Kimi K2.6 (best at code, strongest reasoning)
- Fallback/Consensus: DeepSeek Coder (strong at code understanding)
- When to use DeepSeek:
  - Kimi times out or returns validation error
  - `category == "unknown"` → run both for consensus
  - Iteration 2+ → run both for higher accuracy
  - `confidence < 0.5` on Kimi → get a second opinion

**`kimi_client.py` changes:**
```python
async def call_with_tool(system_prompt, user_prompt, tool_schema, run_id, call_type, model="kimi"):
    if model == "deepseek":
        client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url
        )
        model_name = settings.deepseek_model
    else:
        client = AsyncOpenAI(
            api_key=settings.kimi_api_key,
            base_url=settings.kimi_base_url
        )
        model_name = settings.kimi_model
    # ... rest of call logic
```

---

## E. RELIABILITY + OBSERVABILITY

### E1. Alerts When Things Break
> 🎯 **Aradhya** — set up Slack webhook, add `_notify` calls in processor + webhook

**Add a `_notify` function called in key moments:**

```python
async def _notify_slack(message: str, level: str = "info"):
    if not settings.slack_webhook_url:
        return
    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🔴"}.get(level, "•")
    async with httpx.AsyncClient() as client:
        await client.post(settings.slack_webhook_url, json={"text": f"{emoji} {message}"})
```

**Trigger alerts for:**
- 3+ consecutive `diagnosis_failed` in 10 minutes → `"🔴 Kimi failing repeatedly — check API status"`
- DeepSeek fallback triggered → `"⚠️ Kimi fallback to DeepSeek for run {id}"`
- Reconciler resolves stuck run → `"⚠️ Reconciler rescued stuck run {id}"`
- New user signup → `"🎉 New signup: @{github_username}"`
- `exhausted` run → `"🔴 Run {id} exhausted after 4 iterations — needs manual review"`

---

### E2. Fix Success Rate Tracking
> 🎯 **Aradhya** — new `/admin/stats` endpoint in `runs.py`

**Add a stats endpoint / nightly job:**

```python
@router.get("/admin/stats")
async def admin_stats():
    total = supabase.table("ci_runs").select("id", count="exact").execute().count
    verified = supabase.table("ci_runs").select("id", count="exact").eq("status", "verified").execute().count
    exhausted = supabase.table("ci_runs").select("id", count="exact").eq("status", "exhausted").execute().count
    manual = supabase.table("diagnoses").select("id", count="exact").eq("fix_type", "manual_required").execute().count
    
    # Per-category fix rates
    categories = ["code", "workflow_config", "dependency", "environment", "flaky_test"]
    category_stats = {}
    for cat in categories:
        cat_verified = ... # query by category + verified
        cat_total = ...    # query by category
        category_stats[cat] = {"verified": cat_verified, "total": cat_total, "rate": cat_verified/max(cat_total,1)}
    
    return {
        "overall_fix_rate": verified / max(total, 1),
        "total_runs": total,
        "verified": verified,
        "exhausted": exhausted,
        "manual_required_rate": manual / max(total, 1),
        "by_category": category_stats,
    }
```

---

### E3. Store Full Model Call Logs
> 🔧 **Maneesh** — already has `agent_calls` table, needs to be wired up properly in `kimi_client.py`

**Currently:** `agent_calls` table exists but is it being populated? Store:
- Input tokens, output tokens, latency per call
- Which model was used (Kimi vs DeepSeek)
- Whether the resulting diagnosis was verified or exhausted
- Cost per diagnosis (Kimi token pricing)

This lets you answer: "Which model+prompt combo has the best fix rate per dollar?"

---

### E4. Guard Against Cloud Run Scale-Down
> 🔧 **Maneesh** — `reconciler.py` extension

**Current risk:** `background_tasks.add_task(process_failure, ci_run_id)` — if Cloud Run instance dies mid-task (scale-to-zero or deploy), the task is lost. Run stays in `diagnosing` forever.

**Reconciler already covers this for `fixed` state** — extend to `diagnosing` and `applying`:

```python
# In reconciler.py — add to reconcile_stuck_verifications()
stuck_diagnosing = supabase.table("ci_runs")\
    .select("id")\
    .eq("status", "diagnosing")\
    .lt("updated_at", (now - timedelta(minutes=5)).isoformat())\
    .execute()

for run in stuck_diagnosing.data:
    # Reset to pending — will be re-picked-up on next webhook or reconcile tick
    supabase.table("ci_runs").update({"status": "pending"}).eq("id", run["id"]).execute()
    await process_failure(run["id"])
```

---

## F. GITHUB APP MIGRATION
> 🤝 **Both** — Maneesh owns all backend code, Aradhya owns GitHub App registration + config + org outreach strategy

**This is the biggest unlock. Fixes org repos + enables marketplace.**

### Why Switch

| OAuth App (current) | GitHub App (target) |
|---|---|
| Needs user to be repo admin | Works for any org member |
| Manual webhook registration code | Auto-managed by GitHub |
| Token expires silently | Installation tokens auto-refresh |
| Can't install on orgs directly | One-click org install |
| Not in GitHub Marketplace | Marketplace-ready |
| Shared token — security risk | Per-installation tokens |

### Migration Plan

**Step 1 — Create GitHub App (15 min):**
- GitHub → Settings → Developer settings → GitHub Apps → New
- Permissions needed: `Actions: read`, `Contents: write`, `Metadata: read`, `Pull requests: write`, `Webhooks: read`
- Subscribe to: `workflow_run` event
- Set webhook URL to Cloud Run endpoint

**Step 2 — Installation flow (1 day backend work):**
```python
# New endpoint: /auth/github-app/install
# User clicks "Install App" → GitHub redirects to /auth/github-app/callback?installation_id=xxx
@router.get("/auth/github-app/callback")
async def github_app_callback(installation_id: int, ...):
    # Exchange installation_id for installation token
    token = await get_installation_token(installation_id)
    # Fetch all repos accessible via this installation
    repos = await list_installation_repos(token)
    # Store installation_id, user_id, accessible repos
```

**Step 3 — Webhook routing (2 hours):**
Instead of per-repo webhook → GitHub App sends all events to one endpoint, already signed. Simplifies `webhook.py` significantly.

**Step 4 — PR creation (1 hour):**
Replace user OAuth token with installation token when creating PRs. Works for org repos automatically.

---

## G. CRAZY HIGH-IMPACT IDEAS

### G1. Pre-emptive Fix — Fix Before CI Even Fails
> 🔧 **Maneesh** — new `push_handler.py`, static analysis integration

**How:** Subscribe to `push` webhook events (not just `workflow_run`). On every push:
1. Fetch the diff of what changed
2. Run quick static analysis locally (ast.parse for Python, tsc --noEmit for TS)
3. If failure detected → create fix PR BEFORE GitHub Actions even runs

**User experience:** "Prash prevented a CI failure before it happened."

This is the product moat. Nobody else does this.

---

### G2. PR Review Agent — Sanity Check Before Auto-Apply
> 🔧 **Maneesh** — DeepSeek second-opinion call in `processor.py`

Before any `safe_auto_apply` PR is created, run a second model call:

```python
async def _sanity_check_fix(diagnosis, current_files) -> bool:
    """Ask DeepSeek to review Kimi's proposed fix before applying."""
    review_prompt = f"""
    CI failure: {diagnosis.problem_summary}
    
    Proposed fix by another AI:
    {json.dumps([fc.model_dump() for fc in diagnosis.files_changed], indent=2)}
    
    Does this fix correctly address the root cause?
    Does it preserve all unrelated code?
    Will it break anything else?
    
    Reply with: APPROVE or REJECT and one sentence why.
    """
    response = await deepseek_client.call(review_prompt)
    return "APPROVE" in response
```

If DeepSeek rejects → downgrade to `review_recommended`. Catches hallucinated rewrites.

---

### G3. Slack/Discord Bot Integration
> 🎯 **Aradhya** — Slack API setup, new `integrations/slack.py`, interactive buttons

```
🔴 CI failed on `main` — aradhyamishra/trimly

Prash diagnosed: Missing import `jsonwebtoken` in auth.ts
Fix: Added to package.json (confidence: 94%)

[Apply Fix] [View PR] [Dismiss]
```

Reply "merge" to auto-merge. Reply "skip" to dismiss.

**Implementation:** Webhook to Slack on each diagnosis + interactive buttons via Slack API.

---

### G4. Auto-Merge Verified Fixes
> 🎯 **Aradhya** — `webhook.py` (merge call) + Supabase schema (`auto_merge` column)

When `status = verified` (CI passed on fix branch) → auto-merge the PR without user action.

```python
# In webhook.py, after marking verified:
if ci_run["auto_merge_enabled"]:  # per-repo setting
    await _merge_pr(repo_full_name, pr_number, access_token)
```

**Add to `connected_repos` table:** `auto_merge: boolean default false`
**Add to UI:** Toggle "Auto-merge verified fixes" on repo settings page.

---

### G5. Commit Blame — "Who Broke It"
> 🎯 **Aradhya** — GitHub blame API call, add to PR body in `pr_creator.py`

After diagnosing, trace back: which commit introduced the bug?

```python
async def _find_introducing_commit(repo_full_name, file_path, broken_line, access_token):
    # git blame via GitHub API
    blame_resp = await client.get(
        f"https://api.github.com/repos/{repo_full_name}/blame/{file_path}",
        headers={**headers, "Accept": "application/vnd.github.v3.json"}
    )
    # Find which commit last touched the broken line
    # Return: commit SHA, author, timestamp, message
```

Add to PR body:
```
**Regression introduced by:** @developer in abc123 — "feat: add user auth" (2 hours ago)
```

Gold for teams. Turns Prash from a fixer into a full debugging assistant.

---

### G6. Weekly CI Health Report
> 🎯 **Aradhya** — cron job + email/Slack template, uses stats from E2

Every Monday, send each user an email/Slack summary:

```
📊 Your CI Health — Last 7 Days

✅ 12 failures fixed autonomously
⚡ Average fix time: 4.2 minutes  
📈 Fix rate: 87% (↑12% from last week)
🔁 Most common failure: ModuleNotFoundError (4x)
💰 Engineering time saved: ~24 hours

Top fixed repos: trimly (6), iris (4), drufiy-backend (2)
```

---

## PRIORITY BUILD ORDER

### 🔧 Maneesh's List

| # | What | Impact | Effort | File |
|---|------|--------|--------|------|
| M1 | Remove Gemini/Nvidia → add DeepSeek config | Clean up models, unblock M3-M5 | **30 min** | `config.py`, `kimi_client.py` |
| M1.1 | Add DeepSeek V4 Pro API key/config later | Required to activate the fallback in deployed environments | **15 min** | env / Cloud Run |
| M2 | Two-call pattern (thinking ON + tool_choice) | Root fix for hallucinations | **2 hrs** | `kimi_client.py` |
| M3 | Fetch commit diff as diagnosis context | Single biggest accuracy boost | **1 hr** | `processor.py` |
| M4 | Bump max_files → 12 + always fetch workflow file | More context = better diagnosis | **30 min** | `processor.py` |
| M5 | Auto-retry flaky tests before giving up | Kills 30-40% of manual_required | **2 hrs** | `processor.py` |
| M6 | Parallel Kimi + DeepSeek calls | Speed + consensus confidence boost | **3 hrs** | `processor.py` |
| M7 | Multi-model consensus on "unknown" | Turns unknowns into fixable | **2 hrs** | `processor.py` |
| M8 | Extend reconciler to diagnosing/applying | Reliability against Cloud Run scale-down | **1 hr** | `reconciler.py` |
| M9 | Increase iterations: 2 → 4 | More retry attempts before giving up | **1 hr** | `reconciler.py` |
| M10 | Import-chain tracing in file fetcher | Catches errors in imported modules | **2 hrs** | `processor.py` |
| M11 | Store full model call logs (agent_calls) | Know which model performs best | **2 hrs** | `kimi_client.py` |
| M12 | Diff/patch format for file changes | 95% smaller output, fewer hallucinations | **4 hrs** | `schemas.py` + `pr_creator.py` |
| M13 | PR review agent (DeepSeek sanity check) | Catches bad auto-applies before PR | **2 hrs** | `processor.py` |
| M14 | Agentic loop with fetch_file/search tools | Turns Kimi into a real debugger | **1 day** | `kimi_client.py` + `processor.py` |
| M15 | Pre-emptive fix on push webhook | Product moat — nobody else does this | **2 days** | new `push_handler.py` |
| M16 | GitHub App backend (install flow, tokens, routing) | Org access + marketplace | **2 days** | new auth flow |

---

### 🎯 Aradhya's List

| # | What | Impact | Effort | File |
|---|------|--------|--------|------|
| A1 | Add `organization_member` to affiliation | Unblocks org repos — ship today | **1 line** | `repos.py:39` |
| A2 | Speculative PRs for low confidence | Never return manual_required for code failures | **1 hr** | `diagnosis_agent.py` |
| A3 | Extract secret names into `required_secrets` | 1-click secret resolution in UI | **2 hrs** | `schemas.py` + prompt |
| A4 | RAG over past verified fixes | System gets smarter with every fix | **3 hrs** | `diagnosis_agent.py` |
| A5 | Slack alerts for failures/signups/exhausted runs | Know when things break instantly | **1 hr** | `processor.py` + `webhook.py` |
| A6 | Fix success rate tracking — `/admin/stats` | Know what % of fixes actually work | **2 hrs** | `runs.py` |
| A7 | Commit blame — "who broke it" in PR body | Huge value for teams | **2 hrs** | `pr_creator.py` |
| A8 | Auto-merge verified fixes + per-repo toggle | Full autonomy end-to-end | **2 hrs** | `webhook.py` + Supabase schema |
| A9 | Remove Nvidia/Gemini env vars from Cloud Run | Clean up infra, add DeepSeek key | **30 min** | Cloud Run console |
| A10 | GitHub App registration + config | Create the App on GitHub, get credentials | **1 hr** | github.com/settings |
| A11 | Weekly CI health report (email/Slack) | Retention + engagement for users | **1 day** | new cron + template |
| A12 | Slack/Discord bot integration | Team distribution channel | **1 day** | new `integrations/slack.py` |

---

### Score Summary

| Person | Items | Estimated Total Effort |
|--------|-------|----------------------|
| 🔧 Maneesh | 16 items | ~5-6 days of focused work |
| 🎯 Aradhya | 12 items | ~3-4 days of focused work |

---

## CURRENT ARCHITECTURE SUMMARY (as-is)

```
GitHub push → workflow fails
    → GitHub sends workflow_run webhook → /webhook/github
    → webhook.py: verify signature, dedupe, insert ci_run(status=pending)
    → background_tasks: process_failure(ci_run_id)

process_failure:
    1. Fetch ci_run + repo from Supabase
    2. Decrypt GitHub token
    3. fetch_workflow_logs (ZIP → extract → truncate to 80K chars)
    4. _preprocess_logs (50K → 5-10K, keep error lines + context)
    5. _fetch_relevant_files (regex extract paths from logs → fetch up to 6 files from GitHub)
    6. diagnose_failure → Kimi K2.6 (single call, thinking disabled, forced tool_choice)
        → returns Diagnosis(fix_type, confidence, files_changed, category, ...)
    7. Store diagnosis → diagnoses table
    8. If safe_auto_apply:
        → assess_diff_risk (compare vs known_good_files)
        → create_fix_pr (branch + commit files + open PR)
        → ci_run.status = "fixed"
    9. Else: ci_run.status = "diagnosed" (waits for user action)

After PR is created:
    → GitHub runs CI on fix branch
    → workflow_run webhook arrives for fix branch
    → handle_verification_event: collect all workflow conclusions
    → If all pass → status = "verified", update known_good_files
    → If any fail → status = "iteration_2" → process_iteration_2
        → same as process_failure but with previous_diagnosis as context
        → max 2 iterations, then → status = "exhausted"

Reconciler (every 60s):
    → Find ci_runs stuck in "fixed" for >3 min
    → Query GitHub directly for fix branch CI results
    → Resolve to "verified" or trigger iteration_2
```

---

*Last updated: 2026-05-02*
*Pipeline audit by Aradhya · Work divided between Maneesh (🔧) and Aradhya (🎯)*
