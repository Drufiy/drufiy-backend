import asyncio
import base64
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.agent.pr_creator import apply_unified_patch
from app.auth import get_current_user
from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)
router = APIRouter()

GITHUB_API = "https://api.github.com"


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_run_with_ownership(run_id: str, user_id: str):
    run = (
        supabase.table("ci_runs")
        .select("*, connected_repos(*)")
        .eq("id", run_id)
        .single()
        .execute()
    )
    if not run.data:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.data["connected_repos"]["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this run")
    return run.data


def _get_latest_diagnosis(run_id: str) -> Optional[dict]:
    result = (
        supabase.table("diagnoses")
        .select("*")
        .eq("run_id", run_id)
        .order("iteration", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ── GET /runs/history ─────────────────────────────────────────────────────────

@router.get("/history")
def history(
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
):
    repos = (
        supabase.table("connected_repos")
        .select("id")
        .eq("user_id", current_user["id"])
        .execute()
        .data
    )
    if not repos:
        return []

    repo_ids = [r["id"] for r in repos]
    runs = (
        supabase.table("ci_runs")
        .select("id, repo_id, branch, commit_sha, commit_message, status, fix_branch_name, created_at, updated_at")
        .in_("repo_id", repo_ids)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
        .data
    )

    run_ids = [r["id"] for r in runs]
    if not run_ids:
        return []

    diags = (
        supabase.table("diagnoses")
        .select("run_id, iteration, problem_summary, fix_type, confidence, category, github_pr_url, github_pr_number, verification_status, required_secrets, speculative, created_at")
        .in_("run_id", run_ids)
        .order("iteration", desc=True)
        .execute()
        .data
    )

    diag_map = {}
    for d in diags:
        if d["run_id"] not in diag_map:
            diag_map[d["run_id"]] = d

    return [{**r, "diagnosis": diag_map.get(r["id"])} for r in runs]


# ── GET /runs/dashboard/stats ─────────────────────────────────────────────────

@router.get("/dashboard/stats")
async def dashboard_stats(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]

    async def _repos_count():
        r = supabase.table("connected_repos").select("id", count="exact").eq("user_id", user_id).eq("is_active", True).execute()
        return r.count or 0

    async def _diagnosed_count():
        repos = supabase.table("connected_repos").select("id").eq("user_id", user_id).execute().data
        if not repos:
            return 0
        ids = [r["id"] for r in repos]
        r = supabase.table("ci_runs").select("id", count="exact").in_("repo_id", ids).not_.is_("status", "null").execute()
        return r.count or 0

    async def _prs_count():
        repos = supabase.table("connected_repos").select("id").eq("user_id", user_id).execute().data
        if not repos:
            return 0
        ids = [r["id"] for r in repos]
        runs = supabase.table("ci_runs").select("id").in_("repo_id", ids).not_.is_("fix_branch_name", "null").execute().data
        if not runs:
            return 0
        run_ids = [r["id"] for r in runs]
        r = supabase.table("diagnoses").select("id", count="exact").in_("run_id", run_ids).not_.is_("github_pr_number", "null").execute()
        return r.count or 0

    async def _verified_count():
        repos = supabase.table("connected_repos").select("id").eq("user_id", user_id).execute().data
        if not repos:
            return 0
        ids = [r["id"] for r in repos]
        r = supabase.table("ci_runs").select("id", count="exact").in_("repo_id", ids).eq("status", "verified").execute()
        return r.count or 0

    repos, diagnosed, prs, verified = await asyncio.gather(
        _repos_count(), _diagnosed_count(), _prs_count(), _verified_count()
    )
    return {
        "repos_connected": repos,
        "failures_diagnosed": diagnosed,
        "prs_created": prs,
        "verified_fixes": verified,
    }


# ── GET /runs/admin/stats ─────────────────────────────────────────────────────

@router.get("/admin/stats")
async def admin_stats(current_user: dict = Depends(get_current_user)):
    """
    Global fix success rate breakdown across all runs for this user.
    Per-category fix rates, manual_required rate, average time to fix.
    """
    user_id = current_user["id"]

    # Get all connected repo IDs for this user
    repos = supabase.table("connected_repos").select("id").eq("user_id", user_id).execute().data
    if not repos:
        return {"message": "No repos connected", "overall_fix_rate": 0}

    repo_ids = [r["id"] for r in repos]

    # Fetch all ci_runs for this user
    all_runs = (
        supabase.table("ci_runs")
        .select("id, status, created_at, updated_at")
        .in_("repo_id", repo_ids)
        .execute()
        .data or []
    )

    total = len(all_runs)
    if total == 0:
        return {"message": "No runs yet", "overall_fix_rate": 0}

    status_counts: dict[str, int] = {}
    for run in all_runs:
        s = run["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    verified = status_counts.get("verified", 0)
    exhausted = status_counts.get("exhausted", 0)
    fixed = status_counts.get("fixed", 0)  # PR created but not yet verified

    # Avg time from created_at → updated_at for verified runs (minutes)
    verified_runs = [r for r in all_runs if r["status"] == "verified"]
    avg_fix_minutes = None
    if verified_runs:
        from datetime import datetime, timezone
        deltas = []
        for r in verified_runs:
            try:
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                updated = datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
                deltas.append((updated - created).total_seconds() / 60)
            except Exception:
                pass
        avg_fix_minutes = round(sum(deltas) / len(deltas), 1) if deltas else None

    # Per-category stats from diagnoses table
    run_ids = [r["id"] for r in all_runs]
    all_diags = (
        supabase.table("diagnoses")
        .select("run_id, category, fix_type, verification_status, speculative")
        .in_("run_id", run_ids)
        .execute()
        .data or []
    )

    # Map run_id → latest diagnosis (highest iteration already filtered via order not available here,
    # so we dedupe by keeping last seen — diagnoses are ordered by created_at ascending)
    diag_map: dict[str, dict] = {}
    for d in all_diags:
        diag_map[d["run_id"]] = d  # last one wins (latest iteration)

    categories = ["code", "workflow_config", "dependency", "environment", "flaky_test", "unknown"]
    category_stats: dict[str, dict] = {}
    manual_required_count = 0
    speculative_count = 0

    for cat in categories:
        cat_diags = [d for d in diag_map.values() if d.get("category") == cat]
        cat_verified = sum(1 for d in cat_diags if d.get("verification_status") == "verified")
        cat_total = len(cat_diags)
        category_stats[cat] = {
            "total": cat_total,
            "verified": cat_verified,
            "fix_rate": round(cat_verified / max(cat_total, 1), 2),
        }

    for d in diag_map.values():
        if d.get("fix_type") == "manual_required":
            manual_required_count += 1
        if d.get("speculative"):
            speculative_count += 1

    return {
        "total_runs": total,
        "verified": verified,
        "fixed_pending_verification": fixed,
        "exhausted": exhausted,
        "overall_fix_rate": round(verified / max(total, 1), 2),
        "manual_required_rate": round(manual_required_count / max(total, 1), 2),
        "speculative_prs": speculative_count,
        "avg_fix_minutes": avg_fix_minutes,
        "by_status": status_counts,
        "by_category": category_stats,
    }


# ── GET /runs/{run_id} ────────────────────────────────────────────────────────

@router.get("/{run_id}")
def get_run(run_id: str, current_user: dict = Depends(get_current_user)):
    ci_run = _get_run_with_ownership(run_id, current_user["id"])
    diagnosis = _get_latest_diagnosis(run_id)

    repo = ci_run.pop("connected_repos", {})
    return {
        **ci_run,
        "repo_full_name": repo.get("repo_full_name"),
        "diagnosis": diagnosis,
    }


# ── GET /runs/{run_id}/logs ─────────────────────────────────────────────────

@router.get("/{run_id}/logs")
async def get_run_logs(run_id: str, current_user: dict = Depends(get_current_user)):
    """
    Fetch CI logs for a run via backend proxy.
    Uses stored logs_url and backend GitHub auth.
    """
    ci_run = _get_run_with_ownership(run_id, current_user["id"])

    logs_url = ci_run.get("logs_url")
    if not logs_url:
        raise HTTPException(status_code=404, detail={"error": "logs_not_available", "message": "No logs URL stored for this run"})

    token = current_user.get("github_access_token")
    if not token:
        raise HTTPException(status_code=401, detail={"error": "no_token", "message": "GitHub access token not available"})

    headers = _gh_headers(token)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # GitHub logs_url redirects to a zip download
        resp = await client.get(logs_url, headers=headers, follow_redirects=True)

        if resp.status_code == 410:
            raise HTTPException(status_code=410, detail={"error": "logs_expired", "message": "CI logs have expired (GitHub retains logs for ~90 days)"})
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail={"error": "logs_not_found", "message": "Logs not found on GitHub"})
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail={"error": "github_error", "message": f"Failed to fetch logs: {resp.status_code}"})

        content_type = resp.headers.get("content-type", "")

        # GitHub returns logs as a zip file
        if "zip" in content_type or resp.content[:4] == b"PK\x03\x04":
            import zipfile
            import io

            try:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    # Extract and concatenate all .txt files (job logs)
                    log_parts = []
                    for name in sorted(zf.namelist()):
                        if name.endswith(".txt"):
                            try:
                                content = zf.read(name).decode("utf-8", errors="replace")
                                log_parts.append(f"=== {name} ===\n{content}")
                            except Exception:
                                pass
                    logs_text = "\n\n".join(log_parts) if log_parts else "(No log files found in archive)"
            except zipfile.BadZipFile:
                logs_text = resp.content.decode("utf-8", errors="replace")
        else:
            # Plain text response
            logs_text = resp.content.decode("utf-8", errors="replace")

    # Strip ANSI escape codes for clean display
    import re
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    clean_logs = ansi_escape.sub("", logs_text)

    return {"logs": clean_logs}


# ── POST /runs/{run_id}/apply-fix ────────────────────────────────────────────

@router.post("/{run_id}/apply-fix")
async def apply_fix(run_id: str, current_user: dict = Depends(get_current_user)):
    ci_run = _get_run_with_ownership(run_id, current_user["id"])
    diagnosis = _get_latest_diagnosis(run_id)

    if not diagnosis:
        raise HTTPException(status_code=404, detail={"error": "no_diagnosis"})
    if diagnosis["fix_type"] == "manual_required":
        raise HTTPException(
            status_code=400,
            detail={"error": "manual_required", "message": "This failure requires manual intervention. See fix_description."},
        )
    if diagnosis.get("is_flaky_test"):
        raise HTTPException(
            status_code=400,
            detail={"error": "flaky_test", "message": "Flaky tests should not be auto-fixed."},
        )
    if ci_run["status"] in ("fixed", "waiting_verification", "verified"):
        raise HTTPException(status_code=400, detail={"error": "already_applied"})

    token = current_user["github_access_token"]
    repo = ci_run["connected_repos"] if "connected_repos" in ci_run else (
        supabase.table("connected_repos").select("*").eq("id", ci_run["repo_id"]).single().execute().data
    )
    repo_full_name = repo["repo_full_name"]

    supabase.table("ci_runs").update({"status": "applying"}).eq("id", run_id).execute()

    try:
        from app.agent.pr_creator import create_fix_pr
        pr_result = await create_fix_pr(
            repo_full_name=repo_full_name,
            access_token=token,
            run_id=run_id,
            diagnosis=diagnosis,
        )
    except Exception as e:
        supabase.table("ci_runs").update({
            "status": "diagnosis_failed",
            "error_message": str(e)[:500],
        }).eq("id", run_id).execute()
        raise HTTPException(status_code=500, detail=str(e))

    supabase.table("ci_runs").update({
        "status": "fixed",
        "fix_branch_name": pr_result["branch"],
    }).eq("id", run_id).execute()

    supabase.table("diagnoses").update({
        "github_pr_url": pr_result["pr_url"],
        "github_pr_number": pr_result["pr_number"],
    }).eq("id", diagnosis["id"]).execute()

    return pr_result


# ── POST /runs/{run_id}/dry-run ───────────────────────────────────────────────

@router.post("/{run_id}/dry-run")
async def dry_run(run_id: str, current_user: dict = Depends(get_current_user)):
    ci_run = _get_run_with_ownership(run_id, current_user["id"])
    diagnosis = _get_latest_diagnosis(run_id)

    if not diagnosis:
        raise HTTPException(status_code=404, detail={"error": "no_diagnosis"})
    if diagnosis["fix_type"] == "manual_required":
        raise HTTPException(
            status_code=400,
            detail={"error": "manual_required", "message": "This failure requires manual intervention."},
        )

    token = current_user["github_access_token"]
    repo = (
        supabase.table("connected_repos")
        .select("*")
        .eq("id", ci_run["repo_id"])
        .single()
        .execute()
        .data
    )
    repo_full_name = repo["repo_full_name"]
    default_branch = repo["default_branch"]

    from app.agent.workflow_diff import assess_diff_risk

    diff_preview = []
    for file_change in (diagnosis.get("files_changed") or []):
        path = file_change["path"]
        explanation = file_change.get("explanation", "")

        # Fetch current file content from GitHub
        current_content = ""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}",
                    headers=_gh_headers(token),
                    params={"ref": default_branch},
                )
            if resp.status_code == 200:
                current_content = base64.b64decode(resp.json()["content"]).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Could not fetch current content for {path}: {e}")

        proposed = file_change.get("new_content")
        if not proposed and file_change.get("patch") and current_content:
            try:
                proposed = apply_unified_patch(current_content, file_change["patch"])
            except Exception as e:
                logger.warning(f"Could not apply dry-run patch for {path}: {e}")
                proposed = ""

        risk = await assess_diff_risk(
            repo_id=repo["id"],
            file_path=path,
            proposed_content=proposed,
        )

        diff_preview.append({
            "file_path": path,
            "current_content": current_content,
            "proposed_content": proposed,
            "explanation": explanation,
            "risk_assessment": {
                "risk_level": risk.risk_level,
                "risk_reason": risk.risk_reason,
                "changed_regions": risk.changed_regions,
                "lines_added": risk.lines_added,
                "lines_removed": risk.lines_removed,
                "has_known_good": risk.has_known_good,
            },
        })

    all_low = all(f["risk_assessment"]["risk_level"] == "low" for f in diff_preview)
    overall = "safe_to_apply" if (all_low and diagnosis["fix_type"] == "safe_auto_apply") else "review_before_applying"

    return {"run_id": run_id, "diff_preview": diff_preview, "overall_recommendation": overall}


# ── POST /runs/{run_id}/force-fix ─────────────────────────────────────────────

async def _run_force_fix(run_id: str, access_token: str):
    """Background task: re-diagnose with force_fix=True, then open a PR."""
    from app.agent.diagnosis_agent import diagnose_failure, DiagnosisValidationError
    from app.agent.log_fetcher import fetch_workflow_logs
    from app.agent.processor import _materialize_patch_file_changes
    from app.agent.pr_creator import create_fix_pr

    now = datetime.now(timezone.utc).isoformat()
    try:
        ci_run = supabase.table("ci_runs").select("*, connected_repos(*)").eq("id", run_id).single().execute().data
        if not ci_run:
            logger.error(f"force-fix: run {run_id} not found")
            return

        repo = ci_run["connected_repos"]
        repo_full_name = repo["repo_full_name"]

        # Re-fetch logs
        logs = ""
        try:
            logs = await fetch_workflow_logs(
                github_run_id=ci_run["github_run_id"],
                repo_full_name=repo_full_name,
                access_token=access_token,
            )
        except Exception as e:
            logger.warning(f"force-fix: could not fetch logs for {run_id}: {e}")

        supabase.table("ci_runs").update({"status": "diagnosing", "updated_at": now}).eq("id", run_id).execute()

        # Re-run diagnosis with force_fix=True
        diagnosis = await diagnose_failure(
            logs=logs,
            repo_full_name=repo_full_name,
            commit_message=ci_run.get("commit_message", ""),
            workflow_name=ci_run.get("github_workflow_name", "CI"),
            run_id=run_id,
            force_fix=True,
        )
        await _materialize_patch_file_changes(
            diagnosis=diagnosis,
            repo_full_name=repo_full_name,
            access_token=access_token,
            default_branch=repo.get("default_branch", "main"),
        )

        # Store the new diagnosis
        iteration = (supabase.table("diagnoses").select("iteration").eq("run_id", run_id)
                     .order("iteration", desc=True).limit(1).execute().data or [{}])[0].get("iteration", 0) + 1
        supabase.table("diagnoses").insert({
            "run_id": run_id,
            "iteration": iteration,
            "problem_summary": diagnosis.problem_summary,
            "root_cause": diagnosis.root_cause,
            "fix_description": diagnosis.fix_description,
            "fix_type": diagnosis.fix_type,
            "confidence": diagnosis.confidence,
            "is_flaky_test": diagnosis.is_flaky_test,
            "files_changed": [fc.model_dump() for fc in diagnosis.files_changed],
            "category": diagnosis.category,
            "logs_truncated_warning": diagnosis.logs_truncated_warning,
        }).execute()

        if diagnosis.fix_type == "manual_required" or not diagnosis.files_changed:
            supabase.table("ci_runs").update({
                "status": "diagnosed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", run_id).execute()
            logger.info(f"force-fix: {run_id} still manual_required after forced re-diagnosis")
            return

        # Apply the fix
        supabase.table("ci_runs").update({"status": "applying", "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", run_id).execute()
        pr_result = await create_fix_pr(
            repo_full_name=repo_full_name,
            access_token=access_token,
            run_id=run_id,
            diagnosis=diagnosis.model_dump(),
        )
        supabase.table("ci_runs").update({
            "status": "fixed",
            "fix_branch_name": pr_result["branch"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", run_id).execute()
        logger.info(f"force-fix: PR created for {run_id} → {pr_result.get('pr_url')}")

    except DiagnosisValidationError as e:
        logger.error(f"force-fix: diagnosis validation failed for {run_id}: {e}")
        supabase.table("ci_runs").update({"status": "diagnosis_failed", "error_message": str(e)[:500],
                                          "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", run_id).execute()
    except Exception as e:
        logger.exception(f"force-fix: unexpected error for {run_id}: {e}")
        supabase.table("ci_runs").update({"status": "diagnosis_failed", "error_message": str(e)[:500],
                                          "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", run_id).execute()


@router.post("/{run_id}/force-fix")
async def force_fix(
    run_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """
    Bypass manual_required — re-diagnose with an explicit forcing prompt
    that instructs the model to produce files_changed even if uncertain.
    """
    ci_run = _get_run_with_ownership(run_id, current_user["id"])
    if ci_run["status"] not in ("diagnosed", "diagnosis_failed", "exhausted"):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_state", "message": f"Run is {ci_run['status']}, not eligible for force-fix"},
        )
    access_token = current_user["github_access_token"]
    if not access_token:
        raise HTTPException(status_code=401, detail={"error": "no_token"})

    background_tasks.add_task(_run_force_fix, run_id, access_token)
    return {"status": "force_fix_queued", "run_id": run_id}


# ── POST /runs/{run_id}/add-secret ────────────────────────────────────────────

class AddSecretRequest(BaseModel):
    name: str
    value: str


@router.post("/{run_id}/add-secret")
async def add_secret(
    run_id: str,
    body: AddSecretRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Add a GitHub Actions secret to the repo, then re-trigger the failed workflow.
    Uses GitHub's public-key sealed-box encryption (libsodium via PyNaCl).
    """
    ci_run = _get_run_with_ownership(run_id, current_user["id"])
    repo = ci_run.get("connected_repos") or (
        supabase.table("connected_repos").select("*").eq("id", ci_run["repo_id"]).single().execute().data
    )
    repo_full_name = repo["repo_full_name"]
    access_token = current_user["github_access_token"]
    if not access_token:
        raise HTTPException(status_code=401, detail={"error": "no_token"})

    headers = _gh_headers(access_token)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Fetch repo public key for secret encryption
        pk_resp = await client.get(
            f"{GITHUB_API}/repos/{repo_full_name}/actions/secrets/public-key",
            headers=headers,
        )
        if pk_resp.status_code != 200:
            raise HTTPException(status_code=502, detail={"error": "github_pubkey_failed", "detail": pk_resp.text})

        pk_data = pk_resp.json()
        key_id = pk_data["key_id"]
        pub_key_b64 = pk_data["key"]

        # 2. Encrypt the secret value using libsodium sealed box
        try:
            from nacl.public import PublicKey, SealedBox
            from nacl.encoding import Base64Encoder
            pub_key_bytes = base64.b64decode(pub_key_b64)
            sealed_box = SealedBox(PublicKey(pub_key_bytes))
            encrypted = sealed_box.encrypt(body.value.encode("utf-8"))
            encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "encryption_failed", "detail": str(e)})

        # 3. PUT the secret
        secret_resp = await client.put(
            f"{GITHUB_API}/repos/{repo_full_name}/actions/secrets/{body.name}",
            headers=headers,
            json={"encrypted_value": encrypted_b64, "key_id": key_id},
        )
        if secret_resp.status_code not in (201, 204):
            raise HTTPException(status_code=502, detail={"error": "github_secret_failed", "detail": secret_resp.text})

        # 4. Re-run the failed workflow jobs
        rerun_resp = await client.post(
            f"{GITHUB_API}/repos/{repo_full_name}/actions/runs/{ci_run['github_run_id']}/rerun-failed-jobs",
            headers=headers,
        )
        rerun_ok = rerun_resp.status_code in (201, 204)
        if not rerun_ok:
            logger.warning(f"add-secret: rerun failed for run {run_id}: {rerun_resp.status_code} {rerun_resp.text[:200]}")

    # Reset ci_run status to pending so it can be re-processed
    supabase.table("ci_runs").update({
        "status": "pending",
        "error_message": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", run_id).execute()

    return {
        "status": "secret_added",
        "secret_name": body.name,
        "workflow_rerun": rerun_ok,
    }


# ── POST /runs/{run_id}/skip-test ─────────────────────────────────────────────

class SkipTestRequest(BaseModel):
    test_name: str
    test_file: Optional[str] = None  # if known, speeds up lookup


@router.post("/{run_id}/skip-test")
async def skip_test(
    run_id: str,
    body: SkipTestRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Open a fix PR that marks the specified test as skipped.
    Works for pytest (adds @pytest.mark.skip) and jest (adds test.skip / xtest).
    """
    ci_run = _get_run_with_ownership(run_id, current_user["id"])
    diagnosis = _get_latest_diagnosis(run_id)
    repo = ci_run.get("connected_repos") or (
        supabase.table("connected_repos").select("*").eq("id", ci_run["repo_id"]).single().execute().data
    )
    repo_full_name = repo["repo_full_name"]
    default_branch = repo.get("default_branch", "main")
    access_token = current_user["github_access_token"]
    if not access_token:
        raise HTTPException(status_code=401, detail={"error": "no_token"})

    headers = _gh_headers(access_token)
    test_name = body.test_name
    test_file = body.test_file

    # If test_file not provided, try to infer from diagnosis problem_summary
    if not test_file and diagnosis:
        summary = diagnosis.get("problem_summary", "")
        # Look for patterns like "test_foo.py::test_bar" or "tests/test_foo.py"
        import re
        match = re.search(r"(tests?/[\w/]+\.py)", summary)
        if match:
            test_file = match.group(1)

    if not test_file:
        raise HTTPException(
            status_code=400,
            detail={"error": "test_file_required", "message": "Could not infer test file from diagnosis. Please provide test_file."},
        )

    # Fetch the test file from GitHub
    async with httpx.AsyncClient(timeout=15.0) as client:
        file_resp = await client.get(
            f"{GITHUB_API}/repos/{repo_full_name}/contents/{test_file}",
            headers=headers,
            params={"ref": default_branch},
        )
        if file_resp.status_code != 200:
            raise HTTPException(
                status_code=404,
                detail={"error": "test_file_not_found", "message": f"{test_file} not found in {default_branch}"},
            )

    file_data = file_resp.json()
    current_content = base64.b64decode(file_data["content"]).decode("utf-8", errors="replace")

    # Detect test framework and add skip decorator
    is_pytest = test_file.endswith(".py")
    is_jest = test_file.endswith((".ts", ".tsx", ".js", ".jsx"))

    new_content = current_content
    if is_pytest:
        # Add @pytest.mark.skip before the failing test function
        import re
        # Try to find "def test_name(" pattern and insert skip decorator
        pattern = re.compile(rf"^([ \t]*)(async def|def)\s+({re.escape(test_name)})\s*\(", re.MULTILINE)
        match = re.search(pattern, current_content)
        if match:
            indent = match.group(1)
            insert_pos = match.start()
            skip_line = f'{indent}@pytest.mark.skip(reason="Skipped by Drufiy — flaky or placeholder test")\n'
            new_content = current_content[:insert_pos] + skip_line + current_content[insert_pos:]
            # Ensure pytest is imported
            if "import pytest" not in new_content:
                new_content = "import pytest\n" + new_content
        else:
            raise HTTPException(
                status_code=400,
                detail={"error": "test_not_found", "message": f"Could not find 'def {test_name}(' in {test_file}"},
            )
    elif is_jest:
        # Replace "test(" or "it(" with "test.skip(" or "it.skip("
        import re
        pattern = re.compile(rf"(test|it)\s*\(\s*['\"]({re.escape(test_name)})['\"]")
        if re.search(pattern, current_content):
            new_content = re.sub(pattern, r"\1.skip('\2'", current_content)
        else:
            raise HTTPException(
                status_code=400,
                detail={"error": "test_not_found", "message": f"Could not find test '{test_name}' in {test_file}"},
            )
    else:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsupported_framework", "message": "Only pytest (.py) and jest (.ts/.js) are supported"},
        )

    if new_content == current_content:
        raise HTTPException(
            status_code=400,
            detail={"error": "no_change", "message": "Test file was not modified — test name may not match exactly"},
        )

    # Create a fix PR with the modified test file
    from app.agent.diagnosis_agent import Diagnosis
    from app.agent.schemas import FileChange
    from app.agent.pr_creator import create_fix_pr

    skip_diagnosis = {
        "id": f"skip-test-{run_id}",
        "fix_type": "safe_auto_apply",
        "files_changed": [{
            "path": test_file,
            "new_content": new_content,
            "explanation": f"Skipped test '{test_name}' — marked as flaky/placeholder by user via Drufiy",
        }],
    }

    try:
        pr_result = await create_fix_pr(
            repo_full_name=repo_full_name,
            access_token=access_token,
            run_id=run_id,
            diagnosis=skip_diagnosis,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "pr_failed", "detail": str(e)})

    supabase.table("ci_runs").update({
        "status": "fixed",
        "fix_branch_name": pr_result["branch"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", run_id).execute()

    return {**pr_result, "skipped_test": test_name, "test_file": test_file}


# ── POST /runs/{run_id}/rediagnose ────────────────────────────────────────────

@router.post("/{run_id}/rediagnose")
async def rediagnose(
    run_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """
    Clear the current diagnosis and re-queue the run for a fresh diagnosis.
    The processor will use a different model attempt (rotates through the chain).
    """
    ci_run = _get_run_with_ownership(run_id, current_user["id"])
    if ci_run["status"] in ("diagnosing", "applying"):
        raise HTTPException(
            status_code=400,
            detail={"error": "already_in_progress", "message": "Run is currently being processed"},
        )

    # Reset to pending — the background processor will pick it up fresh
    supabase.table("ci_runs").update({
        "status": "pending",
        "error_message": None,
        "fix_branch_name": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", run_id).execute()

    logger.info(f"rediagnose: run {run_id} reset to pending by user {current_user['id']}")

    from app.agent.processor import process_failure
    background_tasks.add_task(process_failure, run_id)

    return {"status": "rediagnose_queued", "run_id": run_id}
