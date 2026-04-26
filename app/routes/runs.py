import asyncio
import base64
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException

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
        proposed = file_change["new_content"]
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


# ── GET /runs/history ─────────────────────────────────────────────────────────

@router.get("/history")
def history(
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
):
    runs = (
        supabase.table("ci_runs")
        .select("*, connected_repos(repo_full_name)")
        .eq("connected_repos.user_id", current_user["id"])
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
        .data
    )
    if not runs:
        return []

    run_ids = [r["id"] for r in runs]
    diags = (
        supabase.table("diagnoses")
        .select("run_id, iteration, problem_summary, fix_type, confidence, category, github_pr_url, github_pr_number, verification_status, created_at")
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
