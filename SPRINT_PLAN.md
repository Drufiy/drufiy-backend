# Drufiy — 4-Day A-Class Prototype Sprint Plan (v2)

**NSRCEL IIM-B Deadline | Autonomous CI/CD Troubleshooter for Solo Devs**

**Model:** Kimi K2.6 (Moonshot AI, OpenAI-compatible API)
**Founders:** Aradhya Mishra + Maneesh Awasthi
**Revision Date:** April 25, 2026

---

## What changed from v1

1. **Model switched to Kimi K2.6** via OpenAI-compatible API at `api.moonshot.ai/v1`. All tool-use prompts rewritten for OpenAI-format `tools` + `tool_choice`. Fallback path added for malformed JSON.
2. **Sprint shape restructured.** Day 1–2: both founders on backend (split). Day 3: both on frontend together. Day 4: integration + demo. Frontend collapses from 4 days to 1.
3. **Demo rewritten to remove the live push.** Pre-staged scenarios only. Removes the single biggest point of live-demo failure.
4. **Verification loop fixed.** Now waits for *all* workflow_run events on a commit SHA to conclude before marking verified. Previous version had a race condition across multi-workflow repos.
5. **Three new systemic improvements added:**
   - **Dry-run mode** (diagnosis without PR — fallback for demo + future product feature)
   - **Known-good workflow file cache** in Supabase (anti-hallucination guardrail)
   - **Full API call logging** to `agent_calls` table (becomes training corpus for prompt iteration)
6. **Email notifications (Resend) cut.** Traded for dry-run + caching + logging.
7. **All Claude Code prompts rewritten with maximum specification** — explicit file paths, function signatures, imports, return types, edge case lists, testing requirements, and rationale for every prompt.
8. **Diagnosis prompt upgraded** to handle log truncation, fix-type disambiguation (code vs workflow vs env), and flaky test detection.

---

## Decision: Product name is **Prash by Drufiy**. Tagline: "Your CI never breaks twice."

**One-liner:** Drufiy watches your GitHub Actions, diagnoses failures with AI, and opens a fix PR — automatically, then verifies the fix works.

---

## What you're building

A web app where:

1. Dev connects GitHub repo to Drufiy via OAuth
2. Drufiy installs a webhook on that repo
3. When a GitHub Actions workflow fails → Drufiy auto-fetches the full logs
4. **Kimi K2.6** analyzes logs → outputs structured diagnosis + file fixes via tool-calling
5. Drufiy stores the diagnosis (dry-run mode: stops here, shows diff in UI)
6. User clicks "Apply Fix" (or auto-apply if `fix_type == 'safe_auto_apply'`) → Drufiy creates a branch, commits the fix, opens a PR
7. Drufiy watches the new CI run on that branch → if **all workflows pass**, marks "Verified ✓" → if any fail, runs second diagnosis iteration with previous attempt as context
8. User gets a real-time dashboard showing all of this happening via Supabase Realtime

**The core VC claim:** Drufiy is not "AI that suggests fixes." It is an autonomous agent that verifies its own fixes and iterates. That verification loop is the moat.

---

## Architecture (final)

```
GitHub Actions workflow fails
           ↓
GitHub sends POST → /webhook/github
           ↓
FastAPI verifies HMAC signature
FastAPI returns 200 within 100ms ←── GitHub times out at 10s
           ↓ (async BackgroundTask)
Download workflow logs (GitHub API → ZIP → extract → concatenate → truncate to 80K chars)
           ↓
Log full input + metadata to agent_calls table ←── training corpus
           ↓
Kimi K2.6 via OpenAI-compat API → tools=[diagnosis_tool], tool_choice={"type":"function","function":{"name":"submit_diagnosis"}}
  → outputs: { problem_summary, root_cause, fix_description, fix_type, confidence, files_changed, is_flaky_test }
           ↓
Validate tool output against Pydantic schema ←── if malformed, retry once at temp=0
If still malformed twice → mark ci_run as 'failed', store error, skip gracefully
           ↓
Store diagnosis in Supabase (diagnoses table)
Log full output to agent_calls table
Update ci_run status → "diagnosed"
           ↓
Supabase Realtime pushes update → Frontend dashboard updates live
           ↓ (user clicks "Apply Fix" OR auto-apply if fix_type == 'safe_auto_apply')
           ↓
For each file in files_changed:
  Fetch cached known-good version from connected_repos.known_good_files
  Diff against Kimi's new_content
  If diff touches >1 region of unrelated code → flag and require manual review
           ↓
Create branch `prash/fix-run-{id}` → PUT each file via GitHub Contents API → Open PR
Update known_good_files cache with new content (optimistic — updated to verified on success)
           ↓
LISTEN for workflow_run events on fix branch
Wait until ALL workflows for the commit SHA have concluded ←── race fix
           ↓
    ────────┴────────
    │              │
  ALL PASS       ANY FAIL
    │              │
    ↓              ↓
Mark           Re-run diagnosis with
"Verified ✓"   previous_diagnosis + new logs
Commit cache   (max 2 iterations)
```

---

## Tech stack (final)

| Layer | Tool | Reason |
|---|---|---|
| Frontend | Vite 8 + React 19 + shadcn/ui + Tailwind v4 | Fast, modern, dark by default |
| Backend | FastAPI (`drufiy-backend` repo) | Existing code, async, fast |
| AI | Kimi K2.6 (`kimi-k2.6`) via OpenAI SDK | 6x cheaper than Opus, 75% cache discount, native tool-calling |
| Model fallback | Sonnet 4.6 (one-line swap) | Safety net if Kimi breaks in rehearsal |
| Auth | GitHub OAuth → JWT (python-jose) | One-click login, token in claim |
| Database | Supabase (existing project) | Already set up |
| Realtime | Supabase Realtime (built-in) | Zero extra infra |
| Frontend Deploy | Vercel | Free, auto-deploys from GitHub push |
| Backend Deploy | GCP Cloud Run | Min instances = 1 during demo |

---

## Team split

**Maneesh** owns: `app/routes/*`, `app/webhook.py`, `app/auth.py`, `main.py`, Supabase schema, GCP Cloud Run deploy, frontend pages: `/`, `/login`, `/auth/callback`, `/repos`, `/runs/:id`, `/history`

**Aradhya** owns: `app/agent/*` (log_fetcher, diagnosis_agent, pr_creator, processor, workflow_diff), prompt engineering, Kimi API integration, frontend pages: `/dashboard`, `/failures`

---

## Fix branch prefix

Branches are named `prash/fix-run-{run_id_prefix}` (NOT `drufiy/fix-run-`).

---

## The 20-minute rule (non-negotiable)

If you're stuck on *anything* for 20 minutes, stop and paste the full error + full relevant file contents into Claude Code. No rabbit holes.

---

## The cut list (drop in this order if behind)

1. Known-good file cache guardrail (keep logging it, skip the diff-check)
2. Iteration 2 loop (show iteration 1 works, mention 2 verbally in pitch)
3. History page filters
4. Dashboard stats endpoint (hardcode numbers in UI for demo)
5. Full polish pass on day 3 evening

**Do not cut:** Dry-run mode, agent_calls logging, the verification loop itself, HMAC signature verification, rate limiting.

---

## Environment Variables

### Backend `.env`

```
SUPABASE_URL=https://nvzntmgpqvubwkynogtd.supabase.co
SUPABASE_SERVICE_KEY=<service role key>
KIMI_API_KEY=<moonshot key>
KIMI_BASE_URL=https://api.moonshot.ai/v1
KIMI_MODEL=kimi-k2.6
ANTHROPIC_API_KEY=<optional, for fallback>
GITHUB_CLIENT_ID=Ov23liWImjFWN5SlW8HB
GITHUB_CLIENT_SECRET=<secret>
GITHUB_WEBHOOK_SECRET=<hex secret>
JWT_SECRET=<base64 secret>
JWT_ALGORITHM=HS256
JWT_EXPIRY_HOURS=168
FRONTEND_URL=https://prashbydrufiy.vercel.app
PUBLIC_BACKEND_URL=https://drufiy-backend-jpv6slkiua-el.a.run.app
ENV=production
```

### Frontend `.env.local`

```
VITE_API_URL=https://drufiy-backend-jpv6slkiua-el.a.run.app
VITE_GITHUB_CLIENT_ID=Ov23liWImjFWN5SlW8HB
VITE_SUPABASE_URL=https://nvzntmgpqvubwkynogtd.supabase.co
VITE_SUPABASE_ANON_KEY=<anon key>
```

---

## Daily Sync Protocol

| Time | Who | Duration | What |
|---|---|---|---|
| 9:00 AM | Both | 10 min | What each ships today, blockers |
| 1:00 PM | Both | 15 min | Mid-day screen share |
| 7:00 PM | Both | 20 min | End-of-day integration test |
| 11:00 PM | Both | 5 min message | "Shipped: X. Branch: Y. Tomorrow: Z." |

---

*Full detailed prompts, SQL schema, demo script, and VC Q&A in the original sprint plan document.*
