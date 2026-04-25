import logging
from datetime import datetime, timezone

from app.agent.diagnosis_agent import diagnose_failure
from app.agent.kimi_client import DiagnosisValidationError
from app.agent.log_fetcher import (
    InsufficientPermissionsError,
    LogFetchError,
    LogsNotAvailableError,
    fetch_workflow_logs,
)
from app.agent.pr_creator import PRCreationError, create_fix_pr
from app.agent.workflow_diff import assess_diff_risk
from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)


# ── Public entry points ───────────────────────────────────────────────────────

async def process_failure(ci_run_id: str):
    """
    Full pipeline for a new CI failure:
      1. Fetch ci_run + repo from Supabase
      2. Decrypt user's GitHub token
      3. Download workflow logs (ZIP → extract → truncate)
      4. Kimi K2.6 diagnosis → Pydantic-validated Diagnosis
      5. Store diagnosis in diagnoses table
      6. If safe_auto_apply → diff-risk check → create fix PR
      7. Update ci_run status throughout
    """
    logger.info(f"process_failure start run_id={ci_run_id}")
    try:
        _update_status(ci_run_id, "diagnosing")

        # ── 1. Load ci_run + repo ────────────────────────────────────────────
        ci_run, repo = _load_run_and_repo(ci_run_id)
        if not ci_run or not repo:
            await _mark_failed(ci_run_id, "diagnosis_failed", "ci_run or connected_repo not found in DB")
            return

        repo_full_name = repo["repo_full_name"]
        github_run_id = ci_run["github_run_id"]
        commit_message = ci_run.get("commit_message") or ""
        workflow_name = ci_run.get("github_workflow_name") or "CI"

        # ── 2. Decrypt GitHub token ──────────────────────────────────────────
        access_token = _get_access_token(repo["user_id"])
        if not access_token:
            await _mark_failed(ci_run_id, "diagnosis_failed", "Could not retrieve GitHub access token — user may need to re-authenticate")
            return

        # ── 3. Fetch logs ────────────────────────────────────────────────────
        try:
            logs = await fetch_workflow_logs(
                github_run_id=github_run_id,
                repo_full_name=repo_full_name,
                access_token=access_token,
            )
        except LogsNotAvailableError as e:
            logger.warning(f"Logs not available for run {ci_run_id}: {e}")
            await _mark_failed(ci_run_id, "skipped", f"Logs not available: {e}")
            return
        except InsufficientPermissionsError as e:
            logger.warning(f"Insufficient permissions for run {ci_run_id}: {e}")
            await _mark_failed(ci_run_id, "skipped", f"Insufficient permissions: {e}")
            return
        except LogFetchError as e:
            logger.error(f"Log fetch error for run {ci_run_id}: {e}")
            await _mark_failed(ci_run_id, "diagnosis_failed", f"Log fetch failed: {e}")
            return

        # ── 4. Diagnose ──────────────────────────────────────────────────────
        try:
            diagnosis = await diagnose_failure(
                logs=logs,
                repo_full_name=repo_full_name,
                commit_message=commit_message,
                workflow_name=workflow_name,
                iteration=1,
                run_id=ci_run_id,
            )
        except DiagnosisValidationError as e:
            logger.error(f"Diagnosis validation failed for run {ci_run_id}: {e}")
            await _mark_failed(ci_run_id, "diagnosis_failed", str(e)[:300])
            return

        # ── 5. Store diagnosis ───────────────────────────────────────────────
        diagnosis_row = _store_diagnosis(ci_run_id, diagnosis, iteration=1)
        _update_status(ci_run_id, "diagnosed")
        logger.info(f"Diagnosis stored for run {ci_run_id}: fix_type={diagnosis.fix_type} confidence={diagnosis.confidence}")

        # ── 6. Auto-apply if safe ────────────────────────────────────────────
        if diagnosis.fix_type == "safe_auto_apply" and not diagnosis.is_flaky_test:
            logger.info(f"safe_auto_apply — creating fix PR for run {ci_run_id}")
            await _apply_fix(ci_run_id, repo_full_name, access_token, repo["id"], diagnosis_row, diagnosis)
        else:
            logger.info(f"Fix type '{diagnosis.fix_type}' — waiting for user action on run {ci_run_id}")

    except Exception as e:
        logger.exception(f"process_failure crashed run_id={ci_run_id}: {e}")
        await _mark_failed(ci_run_id, "diagnosis_failed", f"Unexpected error: {str(e)[:200]}")


async def process_iteration_2(ci_run_id: str, new_logs: str, previous_diagnosis: dict):
    """
    Called by webhook.py when CI fails on the fix branch.
    Re-diagnoses with context from the previous failed attempt.
    """
    logger.info(f"process_iteration_2 start run_id={ci_run_id}")
    try:
        _update_status(ci_run_id, "diagnosing")

        ci_run, repo = _load_run_and_repo(ci_run_id)
        if not ci_run or not repo:
            await _mark_failed(ci_run_id, "diagnosis_failed", "ci_run or repo not found for iteration 2")
            return

        repo_full_name = repo["repo_full_name"]
        commit_message = ci_run.get("commit_message") or ""
        workflow_name = ci_run.get("github_workflow_name") or "CI"

        if not new_logs:
            await _mark_failed(ci_run_id, "exhausted", "Iteration 2 had no logs to diagnose")
            return

        try:
            diagnosis = await diagnose_failure(
                logs=new_logs,
                repo_full_name=repo_full_name,
                commit_message=commit_message,
                workflow_name=workflow_name,
                iteration=2,
                previous_diagnosis=previous_diagnosis,
                run_id=ci_run_id,
            )
        except DiagnosisValidationError as e:
            logger.error(f"Iteration 2 diagnosis failed for run {ci_run_id}: {e}")
            await _mark_failed(ci_run_id, "exhausted", str(e)[:300])
            return

        diagnosis_row = _store_diagnosis(ci_run_id, diagnosis, iteration=2)
        _update_status(ci_run_id, "diagnosed")
        logger.info(f"Iteration 2 diagnosis stored for run {ci_run_id}: fix_type={diagnosis.fix_type}")

        # Iteration 2: only auto-apply if very high confidence — be conservative
        if (
            diagnosis.fix_type == "safe_auto_apply"
            and not diagnosis.is_flaky_test
            and diagnosis.confidence >= 0.9
        ):
            access_token = _get_access_token(repo["user_id"])
            if access_token:
                await _apply_fix(ci_run_id, repo_full_name, access_token, repo["id"], diagnosis_row, diagnosis)

    except Exception as e:
        logger.exception(f"process_iteration_2 crashed run_id={ci_run_id}: {e}")
        await _mark_failed(ci_run_id, "exhausted", f"Unexpected error: {str(e)[:200]}")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_run_and_repo(ci_run_id: str):
    try:
        result = (
            supabase.table("ci_runs")
            .select("*, connected_repos(*)")
            .eq("id", ci_run_id)
            .single()
            .execute()
        )
        if not result.data:
            return None, None
        ci_run = result.data
        repo = ci_run.pop("connected_repos", None)
        return ci_run, repo
    except Exception as e:
        logger.error(f"Failed to load ci_run {ci_run_id}: {e}")
        return None, None


def _get_access_token(user_id: str) -> str | None:
    try:
        result = supabase.rpc(
            "get_decrypted_token",
            {"p_user_id": user_id, "p_key": settings.jwt_secret},
        ).execute()
        return result.data or None
    except Exception as e:
        logger.error(f"Failed to decrypt token for user {user_id}: {e}")
        return None


def _store_diagnosis(ci_run_id: str, diagnosis, iteration: int) -> dict:
    row = {
        "run_id": ci_run_id,
        "iteration": iteration,
        "problem_summary": diagnosis.problem_summary,
        "root_cause": diagnosis.root_cause,
        "fix_description": diagnosis.fix_description,
        "fix_type": diagnosis.fix_type,
        "confidence": diagnosis.confidence,
        "is_flaky_test": diagnosis.is_flaky_test,
        "category": diagnosis.category,
        "logs_truncated_warning": diagnosis.logs_truncated_warning,
        "files_changed": [fc.model_dump() for fc in diagnosis.files_changed],
    }
    result = supabase.table("diagnoses").insert(row).execute()
    return result.data[0] if result.data else row


async def _apply_fix(ci_run_id: str, repo_full_name: str, access_token: str, repo_id: str, diagnosis_row: dict, diagnosis):
    """Create the fix PR and update ci_run + diagnoses tables."""
    _update_status(ci_run_id, "applying")

    # Diff-risk check before applying (guardrail against hallucinated rewrites)
    for file_change in diagnosis.files_changed:
        risk = await assess_diff_risk(
            repo_id=repo_id,
            file_path=file_change.path,
            proposed_content=file_change.new_content,
        )
        if risk.risk_level == "high":
            logger.warning(
                f"High-risk diff for {file_change.path} in run {ci_run_id} "
                f"({risk.risk_reason}) — downgrading to review_recommended"
            )
            supabase.table("ci_runs").update({
                "status": "diagnosed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", ci_run_id).execute()
            supabase.table("diagnoses").update({"fix_type": "review_recommended"}).eq("id", diagnosis_row["id"]).execute()
            return

    try:
        pr_result = await create_fix_pr(
            repo_full_name=repo_full_name,
            access_token=access_token,
            run_id=ci_run_id,
            diagnosis=diagnosis_row,
        )
    except PRCreationError as e:
        logger.error(f"PR creation failed for run {ci_run_id}: {e}")
        await _mark_failed(ci_run_id, "diagnosis_failed", f"PR creation failed: {str(e)[:200]}")
        return

    supabase.table("ci_runs").update({
        "status": "fixed",
        "fix_branch_name": pr_result["branch"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()

    supabase.table("diagnoses").update({
        "github_pr_url": pr_result["pr_url"],
        "github_pr_number": pr_result["pr_number"],
    }).eq("id", diagnosis_row["id"]).execute()

    logger.info(f"Fix PR created for run {ci_run_id}: {pr_result['pr_url']}")


def _update_status(ci_run_id: str, status: str):
    supabase.table("ci_runs").update({
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()


async def _mark_failed(ci_run_id: str, status: str, message: str):
    try:
        supabase.table("ci_runs").update({
            "status": status,
            "error_message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", ci_run_id).execute()
    except Exception as e:
        logger.error(f"Failed to mark run {ci_run_id} as {status}: {e}")
