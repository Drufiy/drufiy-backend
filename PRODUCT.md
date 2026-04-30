# Prash by Drufiy — Complete Product Document

**Tagline:** Your CI never breaks twice.
**Status:** Early access — onboarding design partners.
**Founders:** Aradhya Mishra + Maneesh Awasthi

---

## 1. What Is Prash?

Prash is an autonomous CI/CD repair agent for GitHub Actions. When your workflow fails, Prash does not send you a notification and leave you to figure it out. It reads the logs, understands the failure, opens a pull request with a fix, watches the new CI run, and marks the fix "Verified ✓" when all workflows pass.

No human in the loop for routine CI failures. That is the entire product.

### What makes it different

Most developer tools give you AI-generated suggestions. You still have to read them, evaluate them, copy-paste them, push them, and wait to see if they work.

Prash closes the loop. It applies the fix, runs CI again, checks the result, and tells you whether it worked. If the first fix fails, it re-diagnoses with the context from the first attempt (iteration 2). You only get involved when Prash genuinely cannot solve it autonomously.

---

## 2. The Problem

GitHub Actions is the default CI layer for most teams on GitHub. It is also the layer that fails most unpredictably — dependency pins change, workflow YAML syntax drifts, environment variables go missing, Node versions diverge between local and CI.

For a solo dev or small team:
- A broken CI blocks the whole deploy pipeline.
- Debugging CI logs is tedious context-switching — you leave your actual work to become a CI detective.
- The same categories of failure repeat: wrong Node version, missing env var, outdated action tag, flaky test, bad dependency pin.
- There are no good tools for *automated recovery* — only tools that show you the error in a prettier way.

**The gap:** Every existing CI tool (GitHub Actions UI, Datadog, Sentry) surfaces the failure. None of them fix it.

---

## 3. The Product — User Journey

### Step 1: Connect
User logs in with GitHub OAuth. Prash requests `repo` + `admin:repo_hook` scopes — just enough to read logs and install a webhook. User selects which repos to monitor. Prash installs a `workflow_run` webhook on each repo. On connection, Prash snapshots the current workflow files as a "known-good" baseline in its database.

### Step 2: Failure happens
Developer pushes a commit. GitHub Actions workflow fails. GitHub sends a `workflow_run` webhook to Prash within seconds.

### Step 3: Prash diagnoses
Prash downloads the full workflow logs (ZIP → extract → concatenate → truncate to 80K chars to fit context). It feeds the logs, repo name, branch, commit message, and workflow name to **Kimi K2.6** — a frontier reasoning model with native tool-calling — using a structured diagnosis tool. The model outputs:

- `problem_summary` — one-line description of what failed
- `root_cause` — detailed explanation of why
- `fix_description` — what needs to change
- `fix_type` — one of: `safe_auto_apply`, `review_recommended`, `manual_required`
- `confidence` — 0.0 to 1.0
- `files_changed` — list of files with their complete new content
- `category` — `code`, `workflow_config`, `dependency`, `environment`, `flaky_test`, `unknown`
- `is_flaky_test` — boolean flag for flaky tests (Prash will not auto-fix these)
- `logs_truncated_warning` — flagged if logs were cut

### Step 4: Fix PR (if applicable)
If `fix_type == "safe_auto_apply"` and it is not a flaky test, Prash:
1. Runs a diff-risk check against the known-good baseline to catch hallucinated rewrites
2. Creates a branch `prash/fix-run-{id}`
3. Commits every changed file via GitHub Contents API
4. Opens a pull request with a description explaining what failed and what was changed

If `fix_type == "review_recommended"`, Prash shows the proposed diff in the dashboard. The user clicks "Apply Fix" when ready.

If `fix_type == "manual_required"`, Prash explains the problem but does not attempt to write code.

### Step 5: Verification
Prash watches for `workflow_run` events on the fix branch. It waits until **all** workflows for that commit SHA have completed (race-condition-safe: uses atomic append in Supabase). If every workflow passes → status becomes **"Verified ✓"**. If any fail → Prash runs **Iteration 2**: re-diagnoses with the new logs plus the previous failed diagnosis as context, then attempts a second fix PR.

### Step 6: Dashboard
The user's dashboard shows every CI run in real time via Supabase Realtime (WebSocket). Status badges pulse while active. New rows flash green on INSERT. Updated rows flash violet on UPDATE. The full timeline — pending → diagnosing → diagnosed → applying → fixed → waiting_verification → verified — is visible without refresh.

---

## 4. Architecture

### 4.1 Infrastructure

| Component | Technology | Notes |
|---|---|---|
| Frontend | Vite 8 + React 19 + TypeScript | Deployed on Vercel |
| Backend | FastAPI (Python) | Deployed on GCP Cloud Run (asia-south1) |
| Database | Supabase (PostgreSQL) | Hosted, managed |
| Realtime | Supabase Realtime | WebSocket, anon read policies on ci_runs + diagnoses |
| AI model | Kimi K2.6 (Moonshot AI) | OpenAI-compatible API at api.moonshot.ai/v1 |
| Model fallback | Claude Sonnet 4.6 (Anthropic) | Activates if Kimi fails twice |
| Auth | GitHub OAuth → JWT | python-jose, 7-day expiry |
| GitHub API | REST v3 + webhooks | Logs, file writes, PR creation |

### 4.2 Backend modules

```
app/
├── main.py              — FastAPI app, CORS, middleware, route registration, startup tasks
├── config.py            — Pydantic settings (reads from .env / Cloud Run env vars)
├── auth.py              — JWT creation + get_current_user dependency
├── db.py                — Supabase client singleton + healthcheck
├── webhook.py           — GitHub webhook receiver (HMAC verify, rate limit, route to handlers)
├── routes/
│   ├── github_oauth.py  — /auth/github/callback, /auth/me, /auth/logout
│   ├── repos.py         — /repos/ CRUD, webhook install, known_good_files seeding
│   └── runs.py          — /runs/history, /runs/dashboard/stats, /runs/{id}, apply-fix, dry-run
└── agent/
    ├── schemas.py        — Pydantic models: Diagnosis, FileChange (with validators)
    ├── kimi_client.py    — Kimi K2.6 calls, Claude fallback, agent_calls logging
    ├── diagnosis_agent.py — Builds system/user prompts, calls kimi_client, validates output
    ├── log_fetcher.py    — GitHub ZIP log download, extract, concatenate, truncate
    ├── processor.py      — Orchestrates full pipeline: process_failure + process_iteration_2
    ├── pr_creator.py     — Creates branch + commits files + opens PR via GitHub API
    └── workflow_diff.py  — Diff-risk assessment against known_good_files baseline
```

### 4.3 Database schema (key tables)

**`user_profiles`**
- `id` (uuid, PK)
- `github_user_id` (int, unique)
- `github_username` (text)
- `email` (text)
- `updated_at`

**`connected_repos`**
- `id` (uuid, PK)
- `user_id` → user_profiles
- `github_repo_id` (int)
- `repo_full_name` (text, e.g. "org/repo")
- `default_branch`
- `webhook_id` (GitHub webhook ID for cleanup on disconnect)
- `is_active` (bool)

**`known_good_files`**
- `repo_id` → connected_repos
- `file_path` (unique per repo)
- `content` (latest verified workflow file content)
- `commit_sha`
- Used as anti-hallucination baseline: proposed content is diffed against this before applying

**`ci_runs`**
- `id` (uuid, PK)
- `repo_id` → connected_repos
- `github_run_id` (GitHub run ID)
- `github_workflow_name`
- `branch`, `commit_sha`, `commit_message`
- `status` (enum — see below)
- `fix_branch_name` (e.g. `prash/fix-run-abc12345`)
- `error_message`
- `logs_url`
- `verification_workflows` (JSONB array — atomic append for race-safe verification)

**`ci_runs` status lifecycle:**
```
pending → diagnosing → diagnosed → applying → fixed → waiting_verification → verified
                                                                           ↘ iteration_2 → diagnosed → ...
                     ↘ diagnosis_failed
                     ↘ exhausted (all attempts failed)
                     ↘ skipped (logs unavailable / insufficient permissions)
```

**`diagnoses`**
- `run_id` → ci_runs
- `iteration` (1 or 2)
- `problem_summary`, `root_cause`, `fix_description`
- `fix_type` (safe_auto_apply / review_recommended / manual_required)
- `confidence` (float 0–1)
- `is_flaky_test` (bool)
- `category` (code / workflow_config / dependency / environment / flaky_test / unknown)
- `files_changed` (JSONB — array of {path, new_content, explanation})
- `github_pr_url`, `github_pr_number`
- `verification_status`
- `logs_truncated_warning`

**`agent_calls`**
- Full input/output logging for every model call
- `run_id`, `call_type`, `model`, `input_messages`, `output_raw`, `output_parsed`
- `tool_call_valid`, `validation_error`
- `input_tokens`, `output_tokens`, `latency_ms`
- Becomes the training corpus for prompt iteration and fine-tuning

### 4.4 The AI pipeline in detail

```
GitHub ZIP logs (up to ~50MB)
        ↓
log_fetcher.py: unzip → extract all .txt files → concatenate → truncate to 80K chars
        ↓
diagnosis_agent.py: build system prompt + user prompt
        ↓
kimi_client.py: call Kimi K2.6 with tool_schema (submit_diagnosis)
  tool_choice="required", temperature=1 (kimi-k2.6 only accepts these)
        ↓ (if no valid tool call)
Retry once → Claude Sonnet 4.6 fallback
        ↓
Pydantic validation (schemas.py)
  - FileChange: path cannot be absolute or contain "..", content < 200KB, not empty
  - Diagnosis: files_changed required for auto-apply, forbidden for manual_required
        ↓
processor.py: store diagnosis, decide whether to auto-apply
        ↓ (if safe_auto_apply)
workflow_diff.py: diff-risk check against known_good_files
  - "low" risk → proceed
  - "high" risk → downgrade to review_recommended, do not apply
        ↓
pr_creator.py: create branch → PUT each file → open PR
        ↓
webhook.py: listen for workflow_run on fix branch
  - atomic append_verification_workflow RPC (race-safe)
  - fetch total workflow count for commit SHA via GitHub API
  - wait until ALL workflows concluded
        ↓
ALL PASS → "Verified ✓", update known_good_files
ANY FAIL → "Iteration 2": re-diagnose with new logs + previous_diagnosis as context
```

### 4.5 Security

- **Webhook HMAC:** Every GitHub webhook is verified with `sha256=` HMAC using `GITHUB_WEBHOOK_SECRET`. Invalid signatures are rejected with 401 before any processing.
- **Rate limiting:** Supabase RPC `check_and_increment_webhook_rate_limit` — max 10 webhooks per repo per hour.
- **Token encryption:** GitHub access tokens are encrypted at rest using `store_encrypted_token` Supabase RPC. Retrieved via `get_decrypted_token`. Key is `JWT_SECRET`.
- **JWT auth:** All API routes (except `/health`, `/webhook/github`) require a valid JWT in the `Authorization: Bearer` header.
- **GitHub scopes:** Only `repo` + `admin:repo_hook` — minimum to read logs and install webhooks.
- **Diff guardrail:** Known-good baseline prevents Prash from blindly applying hallucinated file rewrites.

### 4.6 Startup behavior

On Cloud Run startup, the backend:
1. **Recovers stuck runs** — any `ci_run` stuck in `diagnosing` or `applying` for > 5 minutes is reset to `pending` (handles server restart mid-pipeline).
2. **Pre-warms Kimi** — sends a trivial 1-token completion to establish the HTTP connection pool and warm provider-side caching. First real diagnosis is faster.

---

## 5. Frontend

Single-page app (SPA) built with Vite 8 + React 19 + TypeScript + Tailwind v4 + shadcn/ui.

### Routes

| Path | Page | Purpose |
|---|---|---|
| `/` | Landing | Marketing page, hero, how it works, CTA |
| `/how-it-works` | HowItWorks | Detailed feature explanation + FAQ |
| `/login` | Login | GitHub OAuth initiation |
| `/auth/callback` | AuthCallback | Exchange GitHub code for JWT, redirect to dashboard |
| `/dashboard` | Dashboard | Stat cards + recent activity, Supabase Realtime |
| `/repos` | Repos | Connect/disconnect GitHub repos |
| `/runs/:id` | RunDetail | Full diagnosis, diff viewer, Apply Fix / Dry Run buttons |
| `/failures` | Failures | Live feed of all CI runs with filter (All/Active/Verified/Failed) |
| `/history` | History | Paginated run history |

### Realtime

Both Dashboard and Failures pages subscribe to Supabase Realtime channels on `ci_runs`. INSERT events add new rows with a green flash animation (1.5s). UPDATE events flash existing rows violet (0.8s). Live indicator with pulsing green dot in the UI. Channel error triggers a toast notification.

### Auth flow

1. User clicks "Continue with GitHub"
2. Frontend redirects to `https://github.com/login/oauth/authorize?client_id=...&redirect_uri=https://prashbydrufiy.vercel.app/auth/callback&scope=...`
3. GitHub redirects back to `/auth/callback?code=...`
4. Frontend POSTs `{code}` to `POST /auth/github/callback` on the backend
5. Backend exchanges code for GitHub access token, upserts `user_profiles`, stores encrypted token, returns JWT
6. Frontend stores JWT in localStorage, redirects to `/dashboard`

---

## 6. Deployment

### Backend (GCP Cloud Run)

- **Region:** asia-south1
- **URL:** `https://drufiy-backend-jpv6slkiua-el.a.run.app`
- **Build:** Docker (source deploy via `gcloud run deploy --source=.`)
- **Min instances:** 1 (never goes cold during demo)
- **Key env vars on Cloud Run:**
  - `FRONTEND_URL=https://prashbydrufiy.vercel.app` (CORS allowlist)
  - `KIMI_BASE_URL=https://api.moonshot.ai/v1`
  - `KIMI_MODEL=kimi-k2.6`
  - `ENV=production`

### Frontend (Vercel)

- **URL:** `https://prashbydrufiy.vercel.app`
- **Project:** `drufiy-web` (renamed) under `maneeshawasthi` account
- **SPA routing:** `vercel.json` with universal rewrite to `index.html` (required for React Router)
- **Key env vars (baked at build time):**
  - `VITE_API_URL` — backend URL
  - `VITE_GITHUB_CLIENT_ID` — OAuth app client ID
  - `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY`

### GitHub OAuth App

- **Client ID:** `Ov23liWImjFWN5SlW8HB`
- **Callback URL:** `https://prashbydrufiy.vercel.app/auth/callback`

---

## 7. Vision and Roadmap

### The core thesis

Every CI failure costs developer time. The average debugging session is 20–45 minutes. Most failures fall into a small taxonomy: wrong Node/Python version, outdated action tag, missing env var, dependency version conflict, flaky test, broken YAML syntax. These are solvable by a model that has seen millions of CI logs.

The key insight that makes Prash non-trivially replicable: **the verification loop**. It is not enough to suggest a fix. The fix must be applied, CI must be re-run, the result must be checked, and only then can you call it "fixed." Closing that loop autonomously — and being honest when you cannot — is the moat.

### Expansion path

**Phase 1 (now):** GitHub Actions, single-repo, workflow YAML + simple code fixes.

**Phase 2:** Multi-file code fixes (dependencies, build config, test scaffolding). Learn from the `agent_calls` corpus — fine-tune on real diagnosis→verification outcomes.

**Phase 3:** GitLab CI, CircleCI, Bitbucket Pipelines. Same architecture, different webhook formats and log download APIs.

**Phase 4:** Proactive prevention. After enough verified fixes, Prash builds a fingerprint of what each repo's CI environment needs and can warn before a push would break CI.

**Phase 5:** Team intelligence. Patterns across an org's repos — if 12 repos have the same broken action version, fix them all in one pass.

### Business model (planned)

| Tier | Price | Limits |
|---|---|---|
| Free | $0 | 3 repos, 50 runs/month |
| Pro | $29/mo per team | Unlimited repos, 500 runs/month |
| Enterprise | Custom | On-prem option, SLA, audit logs |

The `agent_calls` table already captures every model call with token counts and latency, making cost-per-run attribution trivial.

---

## 8. Team

**Maneesh Awasthi** — Backend routes, auth, webhook handling, Supabase schema, GCP Cloud Run deployment, frontend pages: Landing, Login, OAuth callback, Repos, Run detail, History.

**Aradhya Mishra** — AI agent pipeline, Kimi K2.6 integration, prompt engineering, log fetching, PR creation, workflow diff-risk, frontend pages: Dashboard, Failures.

---

## 9. Key Design Decisions

**Why Kimi K2.6?** 6x cheaper than Claude Opus per token, 75% cache discount on Moonshot, native tool-calling, frontier reasoning quality on structured log analysis. Claude Sonnet 4.6 is the fallback if Kimi fails or rate-limits.

**Why tool-calling (not JSON mode)?** Tool-calling with `tool_choice="required"` forces the model to produce structured output in a specific schema. JSON mode produces text that looks like JSON but has no schema enforcement. Pydantic validation on top of tool-calling gives two layers of correctness guarantees.

**Why Supabase Realtime (not polling)?** Zero extra infrastructure. The dashboard would feel dead with polling intervals. Realtime WebSockets give instant status updates — users see "Diagnosing..." → "Fix PR opened" → "Verified ✓" happen live.

**Why the verification-before-cache pattern?** `known_good_files` is updated only after a fix branch is verified (all CI passes). Optimistic updates would risk caching a file that broke CI. This conservative approach means the baseline is always a proven-good state.

**Why atomic append for verification?** GitHub sends one `workflow_run` event per workflow. A repo with 3 workflows sends 3 events, potentially concurrently. Non-atomic updates (read-modify-write) would cause lost updates. The `append_verification_workflow` Supabase RPC uses a single SQL statement to atomically append to the JSONB array, making the count always correct regardless of concurrency.

**Why FastAPI BackgroundTasks (not a queue)?** For the prototype: zero extra infrastructure, Cloud Run handles concurrency natively, and the 10-webhook-per-hour rate limit bounds the load. Production would move to Cloud Tasks or Pub/Sub for durability across restarts.

---

## 10. Known Limitations (as of early access)

- Maximum 2 diagnosis iterations per failure. If iteration 2 also fails, the run is marked `exhausted`.
- Logs are truncated at 80K characters. For very large log outputs, `logs_truncated_warning` is set and the model diagnoses on partial context.
- Flaky tests are detected and flagged but not auto-fixed (by design — retrying flaky tests is the correct fix and that is a workflow-level decision).
- GitHub Actions only. Other CI providers not supported yet.
- `manual_required` failures (infrastructure misconfiguration, secrets management issues) are explained but not touched.
- GitHub App permissions require `admin:repo_hook` — the user must be an admin of the repo to connect it.
