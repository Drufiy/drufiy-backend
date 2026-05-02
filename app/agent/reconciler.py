"""
Verification reconciler — sweeps ci_runs stuck in processing states and resolves them.

Why this exists:
  GitHub sends workflow_run webhook events when fix-branch CI completes.
  If the server is mid-deploy when that event arrives, the old handler processes
  it, the branch-name query returns 0 results, and the run is never marked verified.
  No new webhook will come, so it stays spinning forever.

  This reconciler runs every 60 seconds and heals runs stuck in:
  - diagnosing: background task likely died mid-run
  - applying: PR creation likely completed partially or the worker died
  - fixed: verification webhook likely got missed
"""

import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)

# Simple in-process lock — prevents concurrent reconcile runs overlapping.
_reconciling = False


async def _get_decrypted_token(user_id: str) -> str | None:
    try:
        result = supabase.rpc(
            "get_decrypted_token",
            {"p_user_id": user_id, "p_key": settings.jwt_secret},
        ).execute()
        return result.data or None
    except Exception as e:
        logger.warning(f"Reconciler: failed to decrypt token for user {user_id}: {e}")
        return None


async def reconcile_stuck_verifications() -> int:
    """
    Sweep ci_runs stuck in diagnosing/applying/fixed and recover them.
    Returns the number of runs resolved.
    """
    global _reconciling
    if _reconciling:
        logger.debug("Reconciler already running — skipping this tick")
        return 0
    _reconciling = True

    resolved = 0
    try:
        now = datetime.now(timezone.utc)
        diagnosing_cutoff = (now - timedelta(minutes=5)).isoformat()
        applying_cutoff = (now - timedelta(minutes=3)).isoformat()
        fixed_cutoff = (now - timedelta(minutes=3)).isoformat()

        resolved += await _recover_stuck_diagnosing(diagnosing_cutoff)
        resolved += await _recover_stuck_applying(applying_cutoff)
        resolved += await _recover_stuck_fixed(fixed_cutoff)

    finally:
        _reconciling = False

    if resolved:
        logger.info(f"Reconciler: resolved {resolved} stuck run(s)")
    return resolved


async def _reconcile_one(ci_run: dict) -> int:
    """Process a single stuck run. Returns 1 if resolved, 0 if still waiting."""
    ci_run_id = ci_run["id"]
    repo = ci_run.get("connected_repos") or {}
    fix_branch = ci_run.get("fix_branch_name", "")
    repo_full_name = repo.get("repo_full_name", "")

    if not fix_branch or not repo_full_name:
        logger.warning(f"Reconciler: run {ci_run_id[:8]} missing fix_branch or repo — skipping")
        return 0

    access_token = await _get_decrypted_token(repo.get("user_id", ""))
    if not access_token:
        logger.warning(f"Reconciler: no token for run {ci_run_id[:8]} — skipping")
        return 0

    # Query GitHub for all recent runs, filter client-side by fix branch
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/actions/runs",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 50},
            )
        if resp.status_code != 200:
            logger.warning(f"Reconciler: GitHub API {resp.status_code} for {repo_full_name}")
            return 0
    except Exception as e:
        logger.warning(f"Reconciler: GitHub request failed for {ci_run_id[:8]}: {e}")
        return 0

    all_runs = [
        r for r in resp.json().get("workflow_runs", [])
        if r.get("head_branch") == fix_branch
    ]
    completed = [r for r in all_runs if r.get("status") == "completed"]

    if not completed:
        # Fix branch CI hasn't run yet (e.g. PR was just created) — leave it
        logger.debug(f"Reconciler: run {ci_run_id[:8]} fix branch has no completed CI yet — waiting")
        return 0

    # Check if all completed runs passed
    failed_runs = [r for r in completed if r.get("conclusion") != "success"]
    now = datetime.now(timezone.utc).isoformat()

    if not failed_runs:
        # All CI green → verified
        logger.info(f"Reconciler: run {ci_run_id[:8]} all CI passed → verified")
        supabase.table("ci_runs").update({
            "status": "verified",
            "updated_at": now,
        }).eq("id", ci_run_id).execute()

        # Update diagnosis verification_status
        diag = (
            supabase.table("diagnoses")
            .select("id")
            .eq("run_id", ci_run_id)
            .order("iteration", desc=True)
            .limit(1)
            .execute()
        )
        if diag.data:
            supabase.table("diagnoses").update({"verification_status": "verified"}).eq("id", diag.data[0]["id"]).execute()

        # Update known_good_files
        _update_known_good_files(ci_run, repo.get("id", ""))
        return 1

    else:
        # Some CI failed — check iteration depth
        diag_result = (
            supabase.table("diagnoses")
            .select("*")
            .eq("run_id", ci_run_id)
            .order("iteration", desc=True)
            .limit(1)
            .execute()
        )
        previous_diagnosis = diag_result.data[0] if diag_result.data else {}
        max_iteration = previous_diagnosis.get("iteration", 1)

        if max_iteration >= 4:
            logger.info(f"Reconciler: run {ci_run_id[:8]} fix branch failed on iter {max_iteration} → exhausted")
            supabase.table("ci_runs").update({
                "status": "exhausted",
                "error_message": "Fix branch CI failed after 4 iterations — manual intervention required",
                "updated_at": now,
            }).eq("id", ci_run_id).execute()
            return 1

        next_iteration = max_iteration + 1
        logger.info(f"Reconciler: run {ci_run_id[:8]} fix branch CI failed → triggering iteration {next_iteration}")
        supabase.table("ci_runs").update({
            "status": f"iteration_{next_iteration}",
            "updated_at": now,
        }).eq("id", ci_run_id).execute()

        failed_run = failed_runs[0]
        new_logs = ""
        try:
            from app.agent.log_fetcher import fetch_workflow_logs
            new_logs = await fetch_workflow_logs(
                github_run_id=failed_run["id"],
                repo_full_name=repo_full_name,
                access_token=access_token,
            )
        except Exception as e:
            logger.warning(f"Reconciler: could not fetch logs for iter{next_iteration} on {ci_run_id[:8]}: {e}")

        from app.agent.processor import process_iteration_2
        await process_iteration_2(ci_run_id, new_logs, previous_diagnosis)
        return 1


async def _recover_stuck_diagnosing(cutoff: str) -> int:
    stuck_result = (
        supabase.table("ci_runs")
        .select("id")
        .eq("status", "diagnosing")
        .lt("updated_at", cutoff)
        .execute()
    )
    stuck_runs = stuck_result.data or []
    if not stuck_runs:
        return 0

    logger.info(f"Reconciler: found {len(stuck_runs)} run(s) stuck in 'diagnosing'")
    resolved = 0
    from app.agent.processor import process_failure

    for run in stuck_runs:
        ci_run_id = run["id"]
        try:
            supabase.table("ci_runs").update({
                "status": "pending",
                "error_message": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", ci_run_id).execute()
            await process_failure(ci_run_id)
            resolved += 1
        except Exception as e:
            logger.warning(f"Reconciler: failed to requeue diagnosing run {ci_run_id[:8]}: {e}")
    return resolved


async def _recover_stuck_applying(cutoff: str) -> int:
    stuck_result = (
        supabase.table("ci_runs")
        .select("*, connected_repos(*)")
        .eq("status", "applying")
        .lt("updated_at", cutoff)
        .execute()
    )
    stuck_runs = stuck_result.data or []
    if not stuck_runs:
        return 0

    logger.info(f"Reconciler: found {len(stuck_runs)} run(s) stuck in 'applying'")
    resolved = 0
    for ci_run in stuck_runs:
        try:
            resolved += await _recover_one_applying(ci_run)
        except Exception as e:
            logger.warning(f"Reconciler: error recovering applying run {ci_run['id'][:8]}: {e}")
    return resolved


async def _recover_stuck_fixed(cutoff: str) -> int:
    stuck_result = (
        supabase.table("ci_runs")
        .select("*, connected_repos(*)")
        .eq("status", "fixed")
        .lt("updated_at", cutoff)
        .not_.is_("fix_branch_name", "null")
        .execute()
    )
    stuck_runs = stuck_result.data or []
    if not stuck_runs:
        return 0

    logger.info(f"Reconciler: found {len(stuck_runs)} run(s) stuck in 'fixed'")
    resolved = 0
    for ci_run in stuck_runs:
        try:
            resolved += await _reconcile_one(ci_run)
        except Exception as e:
            logger.warning(f"Reconciler: error on fixed run {ci_run['id'][:8]}: {e}")
    return resolved


async def _recover_one_applying(ci_run: dict) -> int:
    ci_run_id = ci_run["id"]
    repo = ci_run.get("connected_repos") or {}
    repo_full_name = repo.get("repo_full_name", "")
    user_id = repo.get("user_id", "")
    if not repo_full_name or not user_id:
        return 0

    access_token = await _get_decrypted_token(user_id)
    if not access_token:
        return 0

    pr = await _find_existing_fix_pr(ci_run_id, repo_full_name, access_token)
    now = datetime.now(timezone.utc).isoformat()
    if not pr:
        logger.warning(f"Reconciler: applying run {ci_run_id[:8]} has no PR on GitHub")
        supabase.table("ci_runs").update({
            "status": "diagnosis_failed",
            "error_message": "Applying stalled and no PR was found on GitHub",
            "updated_at": now,
        }).eq("id", ci_run_id).execute()
        return 1

    supabase.table("ci_runs").update({
        "status": "fixed",
        "fix_branch_name": pr["head"]["ref"],
        "updated_at": now,
    }).eq("id", ci_run_id).execute()

    latest_diag = (
        supabase.table("diagnoses")
        .select("id")
        .eq("run_id", ci_run_id)
        .order("iteration", desc=True)
        .limit(1)
        .execute()
    )
    if latest_diag.data:
        supabase.table("diagnoses").update({
            "github_pr_url": pr["html_url"],
            "github_pr_number": pr["number"],
        }).eq("id", latest_diag.data[0]["id"]).execute()

    ci_run["fix_branch_name"] = pr["head"]["ref"]
    return await _reconcile_one(ci_run)


async def _find_existing_fix_pr(ci_run_id: str, repo_full_name: str, access_token: str) -> dict | None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    branch_prefix = f"drufiy/fix-run-{ci_run_id[:8]}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/pulls",
                headers=headers,
                params={"state": "open", "per_page": 50},
            )
    except Exception as e:
        logger.warning(f"Reconciler: failed to list PRs for {repo_full_name}: {e}")
        return None

    if resp.status_code != 200:
        logger.warning(f"Reconciler: pull request lookup returned {resp.status_code} for {repo_full_name}")
        return None

    for pr in resp.json():
        head_ref = ((pr.get("head") or {}).get("ref")) or ""
        if head_ref.startswith(branch_prefix):
            return pr
    return None


def _update_known_good_files(ci_run: dict, repo_id: str):
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
        logger.warning(f"Reconciler: failed to update known_good_files: {e}")
