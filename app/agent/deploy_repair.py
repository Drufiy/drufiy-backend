"""
Deploy-aware repair — diagnoses and fixes Vercel deployment failures on a
CI-verified fix branch. See ROADMAP.md "Deploy-Aware Repair (Vercel)".

Vercel builds usually finish after GitHub Actions does, so a Vercel deployment
is often still BUILDING/QUEUED at the moment CI passes. handle_deploy_check is
called both inline from webhook.py's verification handler (first check) and
from reconciler.py's 60s sweep (retries while deploy_check_pending is true).
"""

import logging
from datetime import datetime, timezone

from app.agent.diagnosis_agent import compute_error_signature, diagnose_failure
from app.agent.kimi_client import DiagnosisValidationError
from app.agent.pr_creator import PRCreationError, push_fix_to_branch
from app.agent.processor import _store_diagnosis
from app.agent.vercel_client import fetch_vercel_build_logs, find_vercel_deployment, is_deployment_pending
from app.db import supabase
from app.token_crypto import get_vercel_token

logger = logging.getLogger(__name__)

MAX_DEPLOY_FIX_ATTEMPTS = 2
DEPLOY_CHECK_MAX_WAIT_MINUTES = 10


async def handle_deploy_check(
    ci_run_id: str,
    repo_full_name: str,
    commit_sha: str,
    access_token: str,
    user_id: str,
    default_branch: str,
    pr_number: int | None,
    fix_branch: str | None,
) -> bool:
    """
    Returns True if auto-merge should be blocked this cycle (deploy still
    pending, a fix attempt is in flight, or manual review is needed).
    Returns False if it's safe to proceed with the normal auto-merge check
    (deploy succeeded, or no Vercel token configured for this repo owner).
    """
    vercel_token = get_vercel_token(user_id)
    if not vercel_token:
        return False

    deployment = await find_vercel_deployment(commit_sha, vercel_token)

    if deployment is None or is_deployment_pending(deployment.get("state")):
        return _mark_pending(ci_run_id, commit_sha)

    state = deployment.get("state")
    if state == "READY":
        _clear_pending(ci_run_id)
        return False
    if state != "ERROR":
        # CANCELED / BLOCKED / DELETED — not a fixable build failure, don't block merge
        _clear_pending(ci_run_id)
        return False

    claimed = (
        supabase.table("ci_runs")
        .update({"status": "deploy_diagnosing", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", ci_run_id)
        .eq("status", "verified")
        .execute()
    )
    if not claimed.data:
        # Another handler (webhook vs. reconciler tick) already claimed this run this cycle.
        return True

    ci_run_row = supabase.table("ci_runs").select("deploy_fix_attempts").eq("id", ci_run_id).single().execute()
    attempts = (ci_run_row.data or {}).get("deploy_fix_attempts", 0)

    if attempts >= MAX_DEPLOY_FIX_ATTEMPTS:
        _exhaust(ci_run_id, "Vercel deployment is still failing after Prash's fix attempts — manual review needed.")
        return True

    logs = await fetch_vercel_build_logs(deployment["uid"], vercel_token)
    if not logs:
        _exhaust(ci_run_id, "Vercel deployment failed but Prash could not retrieve the build log.")
        return True

    error_signature = compute_error_signature(logs)

    try:
        diagnosis = await diagnose_failure(
            logs=logs,
            repo_full_name=repo_full_name,
            commit_message="",
            workflow_name="vercel-deploy",
            iteration=attempts + 1,
            run_id=ci_run_id,
            commit_sha=commit_sha,
            investigation_context={
                "repo_full_name": repo_full_name,
                "access_token": access_token,
                "default_branch": default_branch,
            },
        )
    except DiagnosisValidationError as e:
        logger.error(f"Deploy diagnosis failed for run {ci_run_id}: {e}")
        _exhaust(ci_run_id, f"Vercel deployment failed and diagnosis errored: {str(e)[:200]}")
        return True

    diagnosis_row = _store_diagnosis(
        ci_run_id,
        diagnosis,
        iteration=_next_diagnosis_iteration(ci_run_id),
        error_signature=error_signature,
        failure_source="deploy",
    )

    can_auto_apply = (
        diagnosis.fix_type in ("safe_auto_apply", "review_recommended")
        and diagnosis.confidence >= 0.6
        and diagnosis.files_changed
        and fix_branch
    )
    if not can_auto_apply:
        _exhaust(ci_run_id, f"Vercel deployment failed: {diagnosis.root_cause[:300]}. Needs manual review.")
        return True

    try:
        await push_fix_to_branch(
            repo_full_name=repo_full_name,
            access_token=access_token,
            branch_name=fix_branch,
            diagnosis=diagnosis_row,
            iteration=attempts + 1,
            pr_number=pr_number,
            failure_source="deploy",
        )
    except PRCreationError as e:
        logger.error(f"Deploy fix push failed for run {ci_run_id}: {e}")
        _exhaust(ci_run_id, f"Vercel deployment failed and the fix could not be pushed: {str(e)[:200]}")
        return True

    supabase.table("ci_runs").update({
        "status": "fixed",
        "deploy_fix_attempts": attempts + 1,
        "deploy_check_pending": False,
        "verification_checked_workflows": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()
    logger.info(f"Deploy fix pushed for run {ci_run_id}, attempt {attempts + 1}")
    return True


def _next_diagnosis_iteration(ci_run_id: str) -> int:
    result = (
        supabase.table("diagnoses")
        .select("iteration")
        .eq("run_id", ci_run_id)
        .order("iteration", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return min(int(result.data[0]["iteration"]) + 1, 6)
    return 1


def _mark_pending(ci_run_id: str, commit_sha: str) -> bool:
    row = supabase.table("ci_runs").select("deploy_check_started_at").eq("id", ci_run_id).single().execute()
    started_at = (row.data or {}).get("deploy_check_started_at")
    now_iso = datetime.now(timezone.utc).isoformat()

    if started_at:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - started).total_seconds() > DEPLOY_CHECK_MAX_WAIT_MINUTES * 60:
            _clear_pending(ci_run_id)
            logger.warning(
                f"Deploy check for run {ci_run_id} timed out after {DEPLOY_CHECK_MAX_WAIT_MINUTES}m "
                f"— no longer blocking merge"
            )
            return False
        supabase.table("ci_runs").update({
            "deploy_check_pending": True,
            "deploy_check_commit_sha": commit_sha,
            "updated_at": now_iso,
        }).eq("id", ci_run_id).execute()
        return True

    supabase.table("ci_runs").update({
        "deploy_check_pending": True,
        "deploy_check_started_at": now_iso,
        "deploy_check_commit_sha": commit_sha,
        "updated_at": now_iso,
    }).eq("id", ci_run_id).execute()
    return True


def _clear_pending(ci_run_id: str) -> None:
    supabase.table("ci_runs").update({
        "deploy_check_pending": False,
        "deploy_check_started_at": None,
        "deploy_check_commit_sha": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()


def _exhaust(ci_run_id: str, note: str) -> None:
    supabase.table("ci_runs").update({
        "status": "verified",
        "external_checks_note": note,
        "deploy_check_pending": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()
