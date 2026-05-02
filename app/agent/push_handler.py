import ast
import base64
import hashlib
import logging
from datetime import datetime, timezone

import httpx

from app.agent.diagnosis_agent import diagnose_failure
from app.agent.pr_creator import create_fix_pr
from app.agent.processor import _fetch_commit_diff, _materialize_patch_file_changes
from app.config import settings
from app.db import supabase
from app.github_app import get_repo_access_token

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"


async def handle_push_event(payload: dict):
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
        return

    repo = repo_result.data[0]
    access_token = await get_repo_access_token(repo)
    if not access_token:
        return

    branch = (payload.get("ref") or "").split("/")[-1]
    commit_sha = payload.get("after") or ""
    commit_message = ((payload.get("head_commit") or {}).get("message") or "")[:500]
    changed_files = _collect_changed_python_files(payload)
    if not changed_files:
        return

    current_files = await _fetch_changed_files(
        repo_full_name=repo_full_name,
        access_token=access_token,
        ref=commit_sha,
        paths=changed_files,
    )
    syntax_logs = _collect_syntax_errors(current_files)
    if not syntax_logs:
        return

    ci_run_id = _insert_push_ci_run(repo["id"], branch, commit_sha, commit_message)
    logger.info(f"Push preflight detected syntax issues for run {ci_run_id}")

    commit_diff = await _fetch_commit_diff(commit_sha, repo_full_name, access_token) if commit_sha else ""
    diagnosis = await diagnose_failure(
        logs=syntax_logs,
        repo_full_name=repo_full_name,
        commit_message=commit_message,
        workflow_name="push-preflight",
        iteration=1,
        run_id=ci_run_id,
        commit_sha=commit_sha,
        commit_diff=commit_diff or None,
        current_files=current_files,
        investigation_context={
            "repo_full_name": repo_full_name,
            "access_token": access_token,
            "default_branch": repo.get("default_branch", "main"),
        },
    )
    await _materialize_patch_file_changes(
        diagnosis=diagnosis,
        repo_full_name=repo_full_name,
        access_token=access_token,
        default_branch=repo.get("default_branch", "main"),
    )

    diagnosis_row = _store_push_diagnosis(ci_run_id, diagnosis)
    if diagnosis.fix_type == "manual_required" or not diagnosis.files_changed:
        supabase.table("ci_runs").update({
            "status": "diagnosed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", ci_run_id).execute()
        return

    pr_result = await create_fix_pr(
        repo_full_name=repo_full_name,
        access_token=access_token,
        run_id=ci_run_id,
        diagnosis=diagnosis_row,
    )
    supabase.table("ci_runs").update({
        "status": "fixed",
        "fix_branch_name": pr_result["branch"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()
    supabase.table("diagnoses").update({
        "github_pr_url": pr_result["pr_url"],
        "github_pr_number": pr_result["pr_number"],
    }).eq("id", diagnosis_row["id"]).execute()


def _collect_changed_python_files(payload: dict) -> list[str]:
    paths: list[str] = []
    for commit in payload.get("commits", []):
        for key in ("added", "modified"):
            for path in commit.get(key, []):
                if path.endswith(".py") and path not in paths:
                    paths.append(path)
    return paths[:10]


async def _fetch_changed_files(repo_full_name: str, access_token: str, ref: str, paths: list[str]) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    files: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for path in paths:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}",
                headers=headers,
                params={"ref": ref},
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            if data.get("type") != "file" or not data.get("content"):
                continue
            files[path] = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
    return files


def _collect_syntax_errors(current_files: dict[str, str]) -> str:
    parts: list[str] = []
    for path, content in current_files.items():
        try:
            ast.parse(content, filename=path)
        except SyntaxError as e:
            line = ""
            if e.lineno and 0 < e.lineno <= len(content.splitlines()):
                line = content.splitlines()[e.lineno - 1]
            parts.append(
                f'File "{path}", line {e.lineno}\n'
                f"{line}\n"
                f"SyntaxError: {e.msg}"
            )
    return "\n\n".join(parts)


def _insert_push_ci_run(repo_id: str, branch: str, commit_sha: str, commit_message: str) -> str:
    synthetic_run_id = int(hashlib.sha1(f"{repo_id}:{commit_sha}:push".encode()).hexdigest()[:15], 16)
    insert = supabase.table("ci_runs").insert({
        "repo_id": repo_id,
        "github_run_id": synthetic_run_id,
        "github_workflow_name": "push-preflight",
        "run_name": "push-preflight",
        "branch": branch,
        "commit_sha": commit_sha,
        "commit_message": commit_message,
        "status": "diagnosing",
    }).execute()
    return insert.data[0]["id"]


def _store_push_diagnosis(ci_run_id: str, diagnosis) -> dict:
    row = {
        "run_id": ci_run_id,
        "iteration": 1,
        "problem_summary": diagnosis.problem_summary,
        "root_cause": diagnosis.root_cause,
        "fix_description": diagnosis.fix_description,
        "fix_type": diagnosis.fix_type,
        "confidence": diagnosis.confidence,
        "is_flaky_test": diagnosis.is_flaky_test,
        "category": diagnosis.category,
        "logs_truncated_warning": diagnosis.logs_truncated_warning,
        "speculative": diagnosis.speculative,
        "files_changed": [fc.model_dump() for fc in diagnosis.files_changed],
    }
    result = supabase.table("diagnoses").insert(row).execute()
    return result.data[0] if result.data else row
