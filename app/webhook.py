import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import supabase

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

    if ci_run["status"] not in ("fixed", "waiting_verification"):
        logger.info(f"Verification event ignored — ci_run {ci_run_id} status={ci_run['status']}")
        return

    # Append this workflow to the checked list
    checked = ci_run.get("verification_checked_workflows") or []
    checked.append({"workflow_id": workflow_id, "workflow_name": workflow_name, "conclusion": conclusion})

    supabase.table("ci_runs").update({
        "verification_checked_workflows": checked,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()

    # Count total workflows for this commit SHA via GitHub API
    try:
        token_result = supabase.rpc(
            "get_decrypted_token",
            {"p_user_id": repo["user_id"], "p_key": settings.jwt_secret},
        ).execute()
        access_token = token_result.data

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/actions/runs",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"head_sha": commit_sha, "branch": branch},
            )
        if resp.status_code != 200:
            logger.warning(f"Could not fetch workflow runs for SHA {commit_sha}: {resp.status_code}")
            return

        all_runs = resp.json().get("workflow_runs", [])
        total_expected = len(all_runs)
        all_concluded = all(r.get("status") == "completed" for r in all_runs)

        if not all_concluded or len(checked) < total_expected:
            logger.info(f"Verification pending — {len(checked)}/{total_expected} workflows done for run {ci_run_id}")
            return

        if all(w["conclusion"] == "success" for w in checked):
            logger.info(f"All workflows passed — marking run {ci_run_id} verified")
            supabase.table("ci_runs").update({
                "status": "verified",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", ci_run_id).execute()

            # Update latest diagnosis
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
            _update_known_good_files(ci_run, repo["id"])

        else:
            logger.info(f"Some workflows failed — triggering iteration 2 for run {ci_run_id}")
            supabase.table("ci_runs").update({
                "status": "iteration_2",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", ci_run_id).execute()

            prev_diag_result = (
                supabase.table("diagnoses")
                .select("*")
                .eq("run_id", ci_run_id)
                .order("iteration", desc=True)
                .limit(1)
                .execute()
            )
            previous_diagnosis = prev_diag_result.data[0] if prev_diag_result.data else {}

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
                    logger.warning(f"Could not fetch iteration 2 logs: {e}")

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

    # Insert ci_run row
    insert = supabase.table("ci_runs").insert({
        "repo_id": repo["id"],
        "github_run_id": workflow_run["id"],
        "github_workflow_id": workflow_run.get("workflow_id"),
        "github_workflow_name": workflow_run.get("name"),
        "run_name": workflow_run.get("display_title") or workflow_run.get("name"),
        "branch": branch,
        "commit_sha": workflow_run.get("head_sha", ""),
        "commit_message": (workflow_run.get("head_commit") or {}).get("message", ""),
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
