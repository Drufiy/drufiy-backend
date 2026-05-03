import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.agent.kimi_client import mark_agent_run_outcome
from app.config import settings
from app.db import supabase
from app.github_app import get_repo_access_token
from app.notifier import notify_verified

logger = logging.getLogger(__name__)
router = APIRouter()

FIX_BRANCH_PREFIX = "drufiy/fix-run-"


# ── HMAC verification ────────────────────────────────────────────────────────

def verify_signature(body: bytes, header_signature: str | None) -> bool:
    if not header_signature:
        return False
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_signature)


# ── Rate limiting ─────────────────────────────────────────────────────────────

def _check_rate_limit(repo_id: str) -> bool:
    """Returns True if the webhook is allowed (under the limit). Best-effort — never raises."""
    try:
        result = supabase.rpc(
            "check_and_increment_webhook_rate_limit",
            {"p_repo_id": repo_id, "p_max": 10, "p_window_seconds": 3600},
        ).execute()
        data = result.data or {}
        return data.get("allowed", True)
    except Exception as e:
        logger.warning(f"Rate limit RPC failed, allowing through: {e}")
        return True


# ── Verification event handler (runs as background task) ─────────────────────

async def handle_verification_event(payload: dict):
    workflow_run = payload["workflow_run"]
    repo_full_name = payload["repository"]["full_name"]
    commit_sha = workflow_run["head_sha"]
    branch = workflow_run["head_branch"] or ""
    conclusion = workflow_run.get("conclusion")
    workflow_id = workflow_run.get("workflow_id")
    workflow_name = workflow_run.get("name", "")

    # Parse run_id prefix from branch name: drufiy/fix-run-{prefix} or drufiy/fix-run-{prefix}-{ts}
    prefix_part = branch.replace(FIX_BRANCH_PREFIX, "")
    run_id_prefix = prefix_part[:8]

    ci_run_result = (
        supabase.table("ci_runs")
        .select("*, connected_repos(*)")
        .ilike("id", f"{run_id_prefix}%")
        .limit(1)
        .execute()
    )
    if not ci_run_result.data:
        logger.warning(f"Verification event for unknown run prefix {run_id_prefix}")
        return

    ci_run = ci_run_result.data[0]
    ci_run_id = ci_run["id"]
    repo = ci_run["connected_repos"]

    # Also allow "applying" — race condition where fix branch CI completes
    # before the backend finishes updating status from applying → fixed
    if ci_run["status"] not in ("fixed", "waiting_verification", "applying"):
        logger.info(f"Verification event ignored — ci_run {ci_run_id} status={ci_run['status']}")
        return

    # Only process events from the LATEST fix branch for this ci_run.
    # When multiple iterations create new branches, we must ignore events
    # from older branches to prevent stale failures from triggering new iterations.
    stored_branch = ci_run.get("fix_branch_name") or ""
    if stored_branch and branch != stored_branch:
        logger.info(
            f"Verification event ignored — branch {branch} does not match "
            f"latest fix branch {stored_branch} for run {ci_run_id}"
        )
        return

    # Atomically append this workflow — avoids lost-update race conditions when
    # multiple workflow_run events arrive concurrently for the same ci_run.
    append_result = supabase.rpc(
        "append_verification_workflow",
        {
            "p_run_id": ci_run_id,
            "p_entry": {
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "conclusion": conclusion,
                "branch": branch,
            },
        },
    ).execute()
    checked = append_result.data or []

    # Count total workflows on the fix branch via GitHub API
    # NOTE: query by branch only — the fix branch has a different commit SHA
    # than the original failing commit, so filtering by head_sha returns 0 results.
    try:
        access_token = await get_repo_access_token(repo)
        if not access_token:
            logger.warning(f"No access token available for verification run {ci_run_id}")
            return

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/actions/runs",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                # NOTE: GitHub's `branch` filter silently returns 0 for pull_request-triggered
                # runs. Fetch broadly and filter client-side by head_branch instead.
                params={"per_page": 50},
            )
        if resp.status_code != 200:
            logger.warning(f"Could not fetch workflow runs for branch {branch}: {resp.status_code}")
            return

        all_runs_raw = resp.json().get("workflow_runs", [])
        # Filter client-side by head_branch — works for both push and pull_request events.
        # Also exclude CI runs on the original bad commit SHA: when Prash creates a fix branch
        # via the API, GitHub fires a push event on the base (still-broken) commit before the
        # fix is committed. That run will fail and falsely trigger iteration 2.
        original_sha = ci_run.get("commit_sha", "")
        all_runs = [
            r for r in all_runs_raw
            if r.get("head_branch") == branch
            and r.get("head_sha") != original_sha  # skip base-commit CI (expected to fail)
        ]
        # Only count completed runs — ignore in_progress/queued
        completed_runs = [r for r in all_runs if r.get("status") == "completed"]
        total_expected = len(completed_runs)

        if total_expected == 0:
            logger.info(f"Verification pending — no completed runs on {branch} yet")
            return

        if len(checked) < total_expected:
            logger.info(f"Verification pending — {len(checked)}/{total_expected} workflows collected for run {ci_run_id}")
            return

        if all(w["conclusion"] == "success" for w in checked):
            logger.info(f"All workflows passed — marking run {ci_run_id} verified")
            supabase.table("ci_runs").update({
                "status": "verified",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", ci_run_id).execute()
            mark_agent_run_outcome(ci_run_id, "verified")

            # Update latest diagnosis
            diag = (
                supabase.table("diagnoses")
                .select("id, github_pr_number")
                .eq("run_id", ci_run_id)
                .order("iteration", desc=True)
                .limit(1)
                .execute()
            )
            if diag.data:
                supabase.table("diagnoses").update({"verification_status": "verified"}).eq("id", diag.data[0]["id"]).execute()

            # Update known_good_files
            _update_known_good_files(ci_run, repo["id"])

            # Slack: notify team that a fix was verified
            if diag.data:
                pr_url = diag.data[0].get("github_pr_url") or ""
                await notify_verified(ci_run_id, repo_full_name, pr_url)

            # Auto-merge: when the repo owner has enabled it and CI is green, merge the PR
            if repo.get("auto_merge") and diag.data:
                pr_number = diag.data[0].get("github_pr_number")
                if pr_number:
                    await _auto_merge_pr(
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        access_token=access_token,
                        ci_run_id=ci_run_id,
                    )

        else:
            prev_diag_result = (
                supabase.table("diagnoses")
                .select("*")
                .eq("run_id", ci_run_id)
                .order("iteration", desc=True)
                .limit(1)
                .execute()
            )
            previous_diagnosis = prev_diag_result.data[0] if prev_diag_result.data else {}
            max_iteration = previous_diagnosis.get("iteration", 1)
            if max_iteration >= 4:
                logger.info(f"Some workflows failed and run {ci_run_id} is already at iteration {max_iteration} → exhausted")
                supabase.table("ci_runs").update({
                    "status": "exhausted",
                    "error_message": "Fix branch CI failed after 4 iterations — manual intervention required",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", ci_run_id).execute()
                mark_agent_run_outcome(ci_run_id, "exhausted")
                return

            next_iteration = max_iteration + 1
            logger.info(f"Some workflows failed — triggering iteration {next_iteration} for run {ci_run_id}")
            supabase.table("ci_runs").update({
                "status": f"iteration_{next_iteration}",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", ci_run_id).execute()

            # Fetch logs from the failed run on the fix branch
            failed_run = next((r for r in all_runs if r.get("conclusion") != "success"), None)
            new_logs = ""
            if failed_run:
                try:
                    from app.agent.log_fetcher import fetch_workflow_logs
                    new_logs = await fetch_workflow_logs(
                        github_run_id=failed_run["id"],
                        repo_full_name=repo_full_name,
                        access_token=access_token,
                    )
                except Exception as e:
                    logger.warning(f"Could not fetch iteration {next_iteration} logs: {e}")

            from app.agent.processor import process_iteration_2
            await process_iteration_2(ci_run_id, new_logs, previous_diagnosis)

    except Exception as e:
        logger.exception(f"handle_verification_event error for run {ci_run_id}: {e}")


def _update_known_good_files(ci_run: dict, repo_id: str):
    """Optimistically update known_good_files with fixed file contents."""
    try:
        diag = (
            supabase.table("diagnoses")
            .select("files_changed")
            .eq("run_id", ci_run["id"])
            .order("iteration", desc=True)
            .limit(1)
            .execute()
        )
        if not diag.data or not diag.data[0].get("files_changed"):
            return
        for fc in diag.data[0]["files_changed"]:
            supabase.table("known_good_files").upsert(
                {
                    "repo_id": repo_id,
                    "file_path": fc["path"],
                    "content": fc["new_content"],
                    "commit_sha": ci_run.get("commit_sha", ""),
                },
                on_conflict="repo_id,file_path",
            ).execute()
    except Exception as e:
        logger.warning(f"Failed to update known_good_files: {e}")


async def _auto_merge_pr(
    repo_full_name: str,
    pr_number: int,
    access_token: str,
    ci_run_id: str,
) -> None:
    """
    Merge the Drufiy fix PR via GitHub API (squash merge).
    Called only when repo.auto_merge=True and all CI checks pass.
    Best-effort — never raises; logs warnings on failure.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.put(
                f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/merge",
                headers=headers,
                json={
                    "merge_method": "squash",
                    "commit_title": f"fix: Drufiy auto-fix (PR #{pr_number})",
                    "commit_message": (
                        "Automatically merged by Drufiy after all CI checks passed.\n\n"
                        f"CI run: {ci_run_id}"
                    ),
                },
            )

        if resp.status_code == 200:
            logger.info(f"Auto-merged PR #{pr_number} for ci_run {ci_run_id}")
            supabase.table("ci_runs").update({
                "status": "merged",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", ci_run_id).execute()
        elif resp.status_code == 405:
            logger.warning(
                f"Auto-merge failed for PR #{pr_number} — PR not mergeable "
                f"(branch protection or conflicts): {resp.text[:200]}"
            )
        elif resp.status_code == 409:
            logger.warning(
                f"Auto-merge skipped for PR #{pr_number} — already merged or SHA conflict"
            )
        else:
            logger.warning(
                f"Auto-merge returned {resp.status_code} for PR #{pr_number}: {resp.text[:200]}"
            )
    except Exception as e:
        logger.warning(f"Auto-merge exception for PR #{pr_number} ci_run {ci_run_id}: {e}")


# ── Main webhook endpoint ─────────────────────────────────────────────────────

@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    body = await request.body()

    if not verify_signature(body, x_hub_signature_256):
        logger.warning(f"Invalid webhook signature from {request.client.host if request.client else 'unknown'}")
        return JSONResponse(status_code=401, content={"error": "invalid_signature"})

    if not x_github_event:
        return JSONResponse(status_code=400, content={"error": "missing_event_header"})

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})

    if x_github_event != "workflow_run":
        if x_github_event == "push":
            repo_full_name = payload["repository"]["full_name"]
            repo_result = (
                supabase.table("connected_repos")
                .select("*")
                .eq("repo_full_name", repo_full_name)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            if not repo_result.data:
                return {"status": "repo_not_connected"}
            repo = repo_result.data[0]
            if not _check_rate_limit(repo["id"]):
                logger.warning(f"Rate limit hit for push event repo {repo_full_name}")
                return {"status": "rate_limited"}
            from app.agent.push_handler import handle_push_event
            background_tasks.add_task(handle_push_event, payload)
            return {"status": "push_preflight_queued"}
        return {"status": "ignored", "event": x_github_event}

    action = payload.get("action")
    workflow_run = payload.get("workflow_run", {})
    conclusion = workflow_run.get("conclusion")
    branch = workflow_run.get("head_branch") or ""

    # Verification events: any completed event on our fix branches
    if action == "completed" and branch.startswith(FIX_BRANCH_PREFIX):
        background_tasks.add_task(handle_verification_event, payload)
        return {"status": "verification_queued"}

    # New failure: completed + failure on a normal branch
    if action != "completed" or conclusion != "failure":
        return {"status": "ignored"}

    repo_full_name = payload["repository"]["full_name"]

    # Look up connected repo
    repo_result = (
        supabase.table("connected_repos")
        .select("*")
        .eq("repo_full_name", repo_full_name)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not repo_result.data:
        return {"status": "repo_not_connected"}

    repo = repo_result.data[0]

    if not _check_rate_limit(repo["id"]):
        logger.warning(f"Rate limit hit for repo {repo_full_name}")
        return {"status": "rate_limited"}

    commit_sha = workflow_run.get("head_sha", "")
    commit_message = (workflow_run.get("head_commit") or {}).get("message", "")
    github_run_id = workflow_run["id"]

    # ── M-4: Dedupe by (repo_id, commit_sha, github_run_id) ──────────────────
    # Prevents duplicate ci_runs from webhook redeliveries or simultaneous events.
    existing = (
        supabase.table("ci_runs")
        .select("id, status")
        .eq("repo_id", repo["id"])
        .eq("commit_sha", commit_sha)
        .eq("github_run_id", github_run_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        logger.info(
            f"Dedupe: ci_run already exists for repo={repo_full_name} "
            f"sha={commit_sha[:8]} run_id={github_run_id} → {existing.data[0]['id']}"
        )
        return {"status": "duplicate_ignored", "ci_run_id": existing.data[0]["id"]}

    # ── M-4: Skip merge commits from Drufiy PRs ───────────────────────────────
    # When a Drufiy fix PR is merged, GitHub fires a new workflow_run on the merge
    # commit. Without this guard, Prash would try to "fix" its own fix. Detect by:
    # 1. Commit message starts with "Merge" and contains "drufiy/fix-run-"
    # 2. OR: the commit_sha matches the head of a recently-verified/fixed run
    if FIX_BRANCH_PREFIX in commit_message and commit_message.strip().startswith("Merge"):
        logger.info(
            f"Skipping merge commit of Drufiy PR — repo={repo_full_name} "
            f"sha={commit_sha[:8]} msg={commit_message[:80]!r}"
        )
        return {"status": "merge_of_drufiy_pr_ignored"}

    # Fallback: check if this commit_sha is the head of a recently-fixed run
    # (catches edge cases where the merge commit message format differs)
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    recent_fixes = (
        supabase.table("ci_runs")
        .select("commit_sha")
        .eq("repo_id", repo["id"])
        .in_("status", ["verified", "fixed"])
        .gte("created_at", recent_cutoff)
        .execute()
    )
    fixed_shas = {r["commit_sha"] for r in (recent_fixes.data or [])}
    if commit_sha in fixed_shas:
        logger.info(
            f"Skipping commit that is a recent Drufiy fix — repo={repo_full_name} sha={commit_sha[:8]}"
        )
        return {"status": "recent_fix_sha_ignored"}

    # Insert ci_run row (commit_sha, commit_message, github_run_id already extracted above)
    insert = supabase.table("ci_runs").insert({
        "repo_id": repo["id"],
        "github_run_id": github_run_id,
        "github_workflow_id": workflow_run.get("workflow_id"),
        "github_workflow_name": workflow_run.get("name"),
        "run_name": workflow_run.get("display_title") or workflow_run.get("name"),
        "branch": branch,
        "commit_sha": commit_sha,
        "commit_message": commit_message,
        "status": "pending",
        "logs_url": workflow_run.get("logs_url"),
    }).execute()

    if not insert.data:
        logger.error(f"Failed to insert ci_run for run {workflow_run['id']}")
        return JSONResponse(status_code=500, content={"error": "db_error"})

    ci_run_id = insert.data[0]["id"]

    from app.agent.processor import process_failure
    background_tasks.add_task(process_failure, ci_run_id)

    logger.info(f"Queued process_failure for ci_run {ci_run_id} (repo={repo_full_name})")
    return {"status": "queued", "ci_run_id": ci_run_id}
