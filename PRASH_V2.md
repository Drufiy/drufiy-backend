# Prash v2 — The AI DevOps Agent

**Status:** Approved direction, in planning. Single source of truth for the pivot from "CI/CD fixer" to "AI DevOps agent."
**Owners:** Aradhya Mishra (founder), Aryan (CTO)
**Team on this build:** Aradhya + Aryan, 2 Claude Code accounts each (4 total)
**Note:** Maneesh is on a break. His earlier Docker/Kubernetes research is referenced below as prior groundwork — nothing in this doc assumes he's actively building this sprint.
**Decided:** 2026-08-03

---

## 1. Why we're pivoting (context for anyone reading this cold)

Prash v1 is a hosted service: it watches GitHub Actions, diagnoses why a CI run failed, and opens a pull request with a fix. It works, and it's live at prash.drufiy.com.

Over the past week we ran it against 13 real CI failures across 9 real open-source repos — not synthetic tests, actual broken builds on other people's projects. The result reframed the product:

**Only ~5 of 13 failures were fixable by editing a file.** The other 8 needed something Prash structurally couldn't do: add a secret, fix a permission, know a value a security scanner deliberately redacted, decide whether a stale submodule URL should be updated or the repo made public. Prash can only write diffs. It has no way to *act*.

This isn't a bug. It's the ceiling of the product as designed. No better prompt or model closes it, because the problem isn't diagnosis quality — it's that ~60% of real-world CI failures require doing something outside a code diff.

**The deeper finding:** an AI agent creates value when the *machine has more context than the human it's helping*. CI repair is structurally the worst possible place to test that, because the person who broke the build 30 seconds ago already knows more than any model can reconstruct. The places where the asymmetry flips — production incidents at 3am, Kubernetes failures with no obvious cause, a system nobody has fully in their head — are where an agent is actually worth something.

**The decision:** stop trying to perfect CI repair in isolation. Build Prash into a general-purpose AI DevOps agent — something that watches infrastructure the way Claude Code watches a codebase, and can *act* on what it finds, not just describe it.

---

## 2. What Prash v2 actually is, in one paragraph

Prash becomes a local agent, in the shape of Claude Code, that a developer or ops engineer installs and points at their own infrastructure. It runs quietly in the background watching what it's been given access to — CI, cloud logs, Kubernetes, deployments. The moment something looks wrong, it pings the user. The user opens the Prash interface, and Prash walks them through what it found, fixes it directly if the action is safe, or asks permission first if it isn't. All credentials stay on the user's own machine, in a file they control — Drufiy's servers never hold them.

---

## 3. The workflow — concretely, step by step

**Install & connect (once).**
The user installs Prash locally (CLI, npm/pip package, or eventually a desktop app). They point it at a local `.env`-style file holding their own credentials — GCP key, Kubernetes context, Vercel token, whatever they want it watching. Nothing is uploaded to Drufiy at this step. This is exactly the model Claude Code itself uses for API keys.

**Background watching.**
Once configured, a lightweight Prash process runs continuously — on the user's machine, or on a server they control — polling the systems it has access to: CI status, pod health, deploy state, error rates. This is the "always on" half of the product that a request/response CLI alone can't provide.

**Something breaks.**
Prash notices — a pod is crash-looping, a deploy failed, a CI run broke. It pings the user (desktop notification, Slack, whatever channel is configured). It does **not** silently act on anything above the "safe" tier without this step.

**The user opens the Prash interface.**
This is the working surface — modeled on Claude Code's own interaction pattern, not a static dashboard. Prash presents:
- what it found and why it thinks that's the cause
- what it wants to do about it
- for safe actions: it just does them, and shows what it did
- for anything riskier: it asks first, in plain language, before touching anything

**Verification.**
After any action, Prash checks whether it actually worked — re-checks the pod, re-runs the check, confirms the metric recovered — and reports back honestly if it didn't, rather than claiming success it can't verify.

**v1 interface scope:** a rich CLI/terminal interface (matching Claude Code's actual current form factor) ships this sprint. A full desktop app is a later phase, not part of the two-week build — don't scope it in now.

---

## 4. Architecture — the part that determines whether anyone trusts this

### The decision: credentials never leave the user's machine

We explicitly rejected Prash's servers holding user cloud/Kubernetes/deploy credentials directly. Reasoning:

- If Drufiy's servers held live AWS keys, kubectl access, and registry credentials for every customer, a breach of *our* infrastructure becomes a breach of *every customer's production environment simultaneously*. That's a fundamentally different risk class than the GitHub App token Prash v1 already holds (scoped to opening PRs, revocable by GitHub, bounded blast radius).
- Real engineering orgs run long security reviews before granting that kind of access to any third party. For a small team, that access model is close to unsellable to anyone with an actual security function, and a genuine liability if we're ever compromised.
- The alternative costs us nothing to build — it's an absence of infrastructure, not a feature. "Your keys never touch our servers" is a stronger trust claim than any encryption story, and Claude Code's own adoption is the existence proof that developers accept this model without friction.

**So:** credentials live in a local file, under the user's control, always. Drufiy's servers never receive them, never store them, never see them in transit for the purpose of *acting* on infrastructure.

### What's hosted vs. what's local

| | Runs where | Holds credentials? |
|---|---|---|
| The watcher (polls CI, cloud, k8s, deploys) | User's machine or their own server | No — reads local `.env` only |
| The action engine (diagnose, fix, restart, roll back) | User's machine or their own server | No — uses local credentials directly |
| The interface (what the user opens when pinged) | Local (CLI/terminal this sprint) | No |
| Notifications, cross-project history, team visibility | Can be hosted (Drufiy-run) | No — status and audit-log data only, never secrets |
| Prash v1's existing GitHub webhook service | Stays exactly as-is, untouched | GitHub App token only (existing, scoped, already how it works today) |

The existing hosted service (prash.drufiy.com) is **not being rebuilt or retired.** It keeps watching GitHub the way it does today. Prash v2 is a new, separate local agent built alongside it — not a replacement in this sprint.

### What carries over from v1 (verified by inspection, not assumed)

We checked how tightly each part of the existing codebase depends on the hosted database/web stack:

| Module | Depends on hosted stack? | Verdict |
|---|---|---|
| `diagnosis_agent.py` — prompts, guardrails, confidence logic, honest-refusal behavior (~1,200 lines) | **Zero** | Ports to the local agent unchanged |
| `log_fetcher.py` — log parsing, error filtering, per-job budgeting | **Zero** | Ports unchanged |
| `schemas.py`, `repo_memory.py` | Zero | Port unchanged |
| `kimi_client.py` (model calling) | 3 minor spots (optional call-logging) | Ports after a trivial fix |
| `processor.py` (orchestration) | Heavy (22 couplings) | Does not port — the local agent gets its own orchestrator |
| `webhook.py`, `reconciler.py`, hosted routes | Heavy | Stay with the v1 hosted service, untouched |

**The valuable IP — the actual diagnosis intelligence — has zero ties to the web stack and moves across as-is.** We are not starting over. We're keeping the brain and giving it a new body.

Also directly reusable: `vercel_client.py` as the template for every new connector (authenticate → locate resource → fetch logs → poll state), `deploy_repair.py` as existing proof the loop already works on non-CI failures, and the `evals/` harness for measuring whether changes actually help.

---

## 5. What Prash is allowed to do — the inclusion rule

"Eventually it does everything" is how scope dies. The rule for whether an action gets built:

> An action ships when it is **(1) needed often**, **(2) verifiable** — we can check afterward whether it worked — and **(3) reversible or low-risk**. Anything that fails (3) always requires explicit approval, regardless of permission mode, including any "bypass" automation mode.

### v1 capability set

**Read / investigate (no permission needed):**
GitHub Actions logs, Vercel build logs, Cloud Run logs, Kubernetes pod status/logs/events.

**Act — safe tier (can run without asking, in permissive modes):**
Re-run a failed job. Restart a crash-looping pod or service. Open a fix PR. Ask the user for a missing secret.

**Act — approval tier (always asks, even in bypass mode):**
Roll back a deployment. Scale resources up/down. Apply a config change.

**Never, in v1:**
Database migrations. Anything that destroys data. Anything touching production without an explicit, per-action grant.

### Permission modes (mirrors Claude Code's own model)

`read-only` → `ask every time` (default) → `auto-safe` (safe tier proceeds automatically, approval tier still prompts) → `environment-scoped` (auto on staging, always prompts on production) → `bypass` (for CI/automation use; still refuses the "never" list unconditionally).

---

## 6. The two-week build plan

Four tracks, run in parallel by the two Claude Code accounts each Aradhya and Aryan are running. **Days 1-2 are shared, not parallel** — the whole team defines the action interface together before splitting up, or the four accounts will build four incompatible versions of it.

### Days 1-2 — shared foundation
Write down, as an actual file in the repo, what an "action" is: what it does, its risk tier (safe / approval / never), whether it's reversible, how to dry-run it, how to verify it worked afterward. Everything in Track C builds against this contract. Nothing else starts until this exists.

### Days 3-8 — four tracks in parallel

**Track A — CLI spine & permission engine.**
The `prash` entry point. Loads the local credentials file. Implements the permission modes. Implements the append-only audit log of every action taken.
*Done when:* the existing "open a PR" action runs through this new system end-to-end, including a permission prompt, with nothing else changed in behavior.

**Track B — Read connectors.**
Kubernetes (pod status, logs, events), Cloud Run logs, AWS. Built on the existing `vercel_client.py` pattern: authenticate → locate resource → fetch logs → poll state.
*Done when:* `prash investigate` pulls real diagnostic data from a live cluster and a live Cloud Run service.

**Track C — Write actions.** All three ship this sprint, but in forced dependency order:
1. **Request a missing secret, then complete the job.** No dependencies — starts as soon as Track A's action interface exists (~day 3). Closes the single most common dead end seen in testing (`needs_secret`). Fastest, clearest proof Prash now *finishes* work instead of describing it.
2. **Restart a stuck pod/service.** Blocked on Track B — needs a live connector to act through. Realistically starts ~day 8.
3. **Roll back a bad release.** Blocked on release-tracking existing — Prash needs to know what "last known good" means before "undo" means anything.

Each ships with a dry-run mode and a verification step.

**Track D — Free the brain, fix the standing bug.**
Extract `diagnosis_agent` + `log_fetcher` + `schemas` into a package callable without Supabase. Make `kimi_client`'s call-logging optional. Then fix the long-standing multi-failure bug: when a system has N independent problems, Prash currently either picks one or gives up entirely. It should attempt each independently and report "fixed 3 of 4" as a real partial success, not a total failure.
*Done when:* the AgentCore case from 2026-08-03 (4 independent CI failures) produces 3 real fixes instead of one `manual_required`.

### Days 9-12
Track C picks up restart (now that B has landed) and rollback (once release-tracking exists). Track A wires the audit log into the interface. Track D finishes multi-failure handling.

### Days 13-14
Get this in front of at least one real outside user on their own infrastructure — not a fork, not our own test repos. **As of 2026-08-03, this has never happened even once** for Prash v1; everything we know is from testing on copies of other people's repos, which fail for reasons a real user's own repo often won't hit. This is the first real signal we'll have had.

---

## 7. Explicitly out of scope for this sprint

- The desktop app (CLI/terminal only, this round)
- Docker-layer actions beyond what's needed to support the Kubernetes connector
- AWS write actions (read/investigate only this sprint — AWS write actions are a later phase)
- Database/migration actions of any kind
- Rebuilding or touching the v1 hosted service

---

## 8. Open questions — not yet decided

- Exact shape of the local "interface" beyond terminal output — how much richer than plain CLI text does it need to be in v1?
- Where does the watcher process live for a user without their own always-on server — does it need a lightweight hosted option, or is "runs on your laptop" acceptable for v1?
- What's the actual notification channel — OS-level, Slack, email, all three?
- Pricing/packaging implications of a local-agent model vs. the hosted v1 — not addressed in this document, needs its own pass.

---

## 9. Decision log

**2026-08-03** — Pivot approved: from CI/CD-only hosted service to general-purpose local AI DevOps agent, based on the 13-repo finding that ~60% of real failures require action, not diffs.

**2026-08-03** — Rejected: Drufiy servers holding user cloud/k8s/deploy credentials directly. Reason: unacceptable liability concentration and a materially harder enterprise sell, for a UX gain achievable another way.

**2026-08-03** — Adopted: local-first execution with local-only credentials, hosted layer limited to notifications/history/coordination (no secrets). Confirmed by Aradhya explicitly: "we should not store the users' creds and expect that we own everything, just like other AI coding IDEs."

**2026-08-03** — Confirmed: v1 hosted service (prash.drufiy.com) stays live and unmodified throughout this sprint. Not rebuilt, not retired, no new feature work on it during this fortnight.

**2026-08-03** — Team: Aradhya + Aryan (CTO) building, 2 Claude Code accounts each. Maneesh on a break; his Docker/Kubernetes research is credited groundwork for Track B, not assumed active-build involvement.
