import json
import asyncio
import base64
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from app.agent.diagnosis_agent import diagnose_failure
from app.agent.kimi_client import DiagnosisValidationError, call_with_tool, mark_agent_run_outcome
from app.agent.log_fetcher import (
    InsufficientPermissionsError,
    LogFetchError,
    LogsNotAvailableError,
    fetch_workflow_logs,
)
from app.agent.pr_creator import PRCreationError, apply_unified_patch, create_fix_pr
from app.agent.workflow_diff import assess_diff_risk
from app.config import settings
from app.db import supabase
from app.github_app import get_repo_access_token
from app.notifier import notify_diagnosis_failed, notify_exhausted

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"
SANITY_REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Approve or reject a proposed CI fix after reviewing it for correctness and safety.",
    "parameters": {
        "type": "object",
        "required": ["approve", "reason"],
        "properties": {
            "approve": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
}


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
        commit_sha = ci_run.get("commit_sha") or ""
        commit_message = ci_run.get("commit_message") or ""
        workflow_name = ci_run.get("github_workflow_name") or "CI"

        # ── 2. Decrypt GitHub token ──────────────────────────────────────────
        access_token = await get_repo_access_token(repo)
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

        # ── 4. Fetch source files for diagnosis context ──────────────────────
        commit_diff = ""
        if commit_sha:
            commit_diff = await _fetch_commit_diff(
                commit_sha=commit_sha,
                repo_full_name=repo_full_name,
                access_token=access_token,
            )

        current_files = await _fetch_relevant_files(
            logs=logs,
            repo_full_name=repo_full_name,
            access_token=access_token,
            default_branch=repo.get("default_branch", "main"),
            workflow_name=workflow_name,
        )

        # RAG: fetch past verified fixes for this repo as few-shot context
        similar_fixes = _fetch_similar_fixes(repo["id"])

        # ── 5. Diagnose ──────────────────────────────────────────────────────
        try:
            diagnosis = await diagnose_failure(
                model="kimi",
                logs=logs,
                repo_full_name=repo_full_name,
                commit_message=commit_message,
                workflow_name=workflow_name,
                iteration=1,
                run_id=ci_run_id,
                commit_sha=commit_sha,
                commit_diff=commit_diff or None,
                current_files=current_files or None,
                similar_fixes=similar_fixes or None,
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
        except DiagnosisValidationError as e:
            logger.error(f"Diagnosis validation failed for run {ci_run_id}: {e}")
            await _mark_failed(ci_run_id, "diagnosis_failed", str(e)[:300])
            return

        # ── 6. Store diagnosis ───────────────────────────────────────────────
        diagnosis_row = _store_diagnosis(ci_run_id, diagnosis, iteration=1)
        _update_status(ci_run_id, "diagnosed")
        logger.info(f"Diagnosis stored for run {ci_run_id}: fix_type={diagnosis.fix_type} confidence={diagnosis.confidence}")

        # ── 6.5. Flaky tests: rerun once, then auto-skip if still failing ────
        if diagnosis.is_flaky_test:
            logger.info(f"Flaky test detected for run {ci_run_id} — attempting automatic rerun")
            rerun_requested = await _trigger_rerun(github_run_id, repo_full_name, access_token)
            rerun_conclusion = None
            if rerun_requested:
                rerun_conclusion = await _wait_for_run_completion(
                    github_run_id=github_run_id,
                    repo_full_name=repo_full_name,
                    access_token=access_token,
                )
                if rerun_conclusion == "success":
                    _mark_rerun_resolved(ci_run_id, diagnosis_row.get("id"))
                    logger.info(f"Flaky rerun passed for run {ci_run_id}")
                    return

            logger.info(
                f"Flaky rerun did not resolve run {ci_run_id} "
                f"(requested={rerun_requested}, conclusion={rerun_conclusion}) — creating skip PR"
            )
            skipped = await _auto_skip_test(
                ci_run_id=ci_run_id,
                repo_full_name=repo_full_name,
                access_token=access_token,
                default_branch=repo.get("default_branch", "main"),
                diagnosis=diagnosis,
                diagnosis_row=diagnosis_row,
                logs=logs,
            )
            if skipped:
                return

        # ── 7. Auto-apply if safe ────────────────────────────────────────────
        if diagnosis.fix_type == "safe_auto_apply" and not diagnosis.is_flaky_test:
            # Deduplication: skip if there's already an open Drufiy PR for this repo
            if await _has_open_drufiy_pr(repo_full_name, access_token):
                logger.info(f"Skipping PR for run {ci_run_id} — existing open Drufiy PR found for {repo_full_name}")
            else:
                logger.info(f"safe_auto_apply — creating fix PR for run {ci_run_id}")
                await _apply_fix(ci_run_id, repo_full_name, access_token, repo["id"], diagnosis_row, diagnosis)
        else:
            logger.info(f"Fix type '{diagnosis.fix_type}' — waiting for user action on run {ci_run_id}")

    except Exception as e:
        logger.exception(f"process_failure crashed run_id={ci_run_id}: {e}")
        await _mark_failed(ci_run_id, "diagnosis_failed", f"Unexpected error: {str(e)[:200]}")


async def process_iteration_2(ci_run_id: str, new_logs: str, previous_diagnosis: dict):
    """
    Called when CI fails on a Drufiy fix branch.
    Re-diagnoses with context from the previous failed attempt.
    """
    next_iteration = max(int(previous_diagnosis.get("iteration", 1)) + 1, 2)
    logger.info(f"process_iteration_2 start run_id={ci_run_id} next_iteration={next_iteration}")
    try:
        _update_status(ci_run_id, "diagnosing")

        ci_run, repo = _load_run_and_repo(ci_run_id)
        if not ci_run or not repo:
            await _mark_failed(ci_run_id, "diagnosis_failed", "ci_run or repo not found for iteration 2")
            return

        repo_full_name = repo["repo_full_name"]
        commit_sha = ci_run.get("commit_sha") or ""
        commit_message = ci_run.get("commit_message") or ""
        workflow_name = ci_run.get("github_workflow_name") or "CI"

        if not new_logs:
            await _mark_failed(ci_run_id, "exhausted", f"Iteration {next_iteration} had no logs to diagnose")
            return

        access_token_iter2 = await get_repo_access_token(repo)
        commit_diff = ""
        if access_token_iter2 and commit_sha:
            commit_diff = await _fetch_commit_diff(
                commit_sha=commit_sha,
                repo_full_name=repo_full_name,
                access_token=access_token_iter2,
            )
        current_files_iter2 = {}
        if access_token_iter2:
            current_files_iter2 = await _fetch_relevant_files(
                logs=new_logs,
                repo_full_name=repo_full_name,
                access_token=access_token_iter2,
                default_branch=repo.get("default_branch", "main"),
                workflow_name=workflow_name,
            )

        # RAG: inject past verified fixes — especially useful on retry iterations
        similar_fixes_iter2 = _fetch_similar_fixes(repo["id"])

        try:
            diagnosis = await diagnose_failure(
                model="kimi",
                logs=new_logs,
                repo_full_name=repo_full_name,
                commit_message=commit_message,
                workflow_name=workflow_name,
                iteration=next_iteration,
                previous_diagnosis=previous_diagnosis,
                run_id=ci_run_id,
                commit_sha=commit_sha,
                commit_diff=commit_diff or None,
                current_files=current_files_iter2 or None,
                similar_fixes=similar_fixes_iter2 or None,
                investigation_context={
                    "repo_full_name": repo_full_name,
                    "access_token": access_token_iter2,
                    "default_branch": repo.get("default_branch", "main"),
                },
            )
            await _materialize_patch_file_changes(
                diagnosis=diagnosis,
                repo_full_name=repo_full_name,
                access_token=access_token_iter2,
                default_branch=repo.get("default_branch", "main"),
            )
        except DiagnosisValidationError as e:
            logger.error(f"Iteration {next_iteration} diagnosis failed for run {ci_run_id}: {e}")
            await _mark_failed(ci_run_id, "exhausted", str(e)[:300])
            return

        diagnosis_row = _store_diagnosis(ci_run_id, diagnosis, iteration=next_iteration)
        _update_status(ci_run_id, "diagnosed")
        logger.info(f"Iteration {next_iteration} diagnosis stored for run {ci_run_id}: fix_type={diagnosis.fix_type}")

        # Follow-up iterations: only auto-apply if very high confidence.
        if (
            diagnosis.fix_type == "safe_auto_apply"
            and not diagnosis.is_flaky_test
            and diagnosis.confidence >= 0.9
        ):
            access_token = await get_repo_access_token(repo)
            if access_token:
                await _apply_fix(ci_run_id, repo_full_name, access_token, repo["id"], diagnosis_row, diagnosis)

    except Exception as e:
        logger.exception(f"process_iteration_2 crashed run_id={ci_run_id}: {e}")
        await _mark_failed(ci_run_id, "exhausted", f"Unexpected error: {str(e)[:200]}")


# ── Source file fetcher ───────────────────────────────────────────────────────

# File extensions worth fetching as context
_SOURCE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".java", ".rb", ".php", ".cs", ".cpp", ".c", ".h",
    ".json", ".toml", ".yaml", ".yml",
}

# Patterns to extract file paths from CI logs
_FILE_PATH_RE = re.compile(
    r"""
    (?:
        File\s+"?([^"'\s,]+\.[a-zA-Z]+)"?    |   # Python: File "path.py"
        (?:ERROR|error)\s+(?:in\s+)?([^\s:]+\.[a-zA-Z]+)  |   # Generic: error in path.ext
        \s+at\s+[^(]+\(([^)]+\.[a-zA-Z]+):\d+\)  |  # JS stack frame: at fn (file.ts:10)
        ([a-zA-Z0-9_./\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb))\b  # bare path with known extension
    )
    """,
    re.VERBOSE,
)

# Common dependency manifests — always fetch these if they exist
_MANIFEST_FILES = [
    "package.json", "requirements.txt", "pyproject.toml",
    "Cargo.toml", "go.mod", "pom.xml", "Gemfile",
    "tsconfig.json", "jest.config.js", "jest.config.ts",
]
_PYTHON_IMPORT_RE = re.compile(r"^(?:from|import)\s+([\w.]+)", re.MULTILINE)


async def _fetch_relevant_files(
    logs: str,
    repo_full_name: str,
    access_token: str,
    default_branch: str,
    workflow_name: str | None = None,
    max_files: int = 12,
    max_file_bytes: int = 20_000,
) -> dict[str, str]:
    """
    Extract file paths from CI logs, fetch them from GitHub, return {path: content}.
    Also fetches dependency manifests for context. Best-effort — never raises.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Extract candidate paths from logs
    candidates: list[str] = []
    for match in _FILE_PATH_RE.finditer(logs):
        path = next((g for g in match.groups() if g), None)
        if not path:
            continue
        # Normalise: strip leading ./ and leading /
        path = path.lstrip("./")
        # Filter out noise (node_modules, __pycache__, absolute system paths, urls)
        if any(skip in path for skip in ("node_modules", "__pycache__", "/usr/", "/home/runner", "http")):
            continue
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        if ext in _SOURCE_EXTENSIONS and path not in candidates:
            candidates.append(path)

    workflow_files = await _find_workflow_files(
        repo_full_name=repo_full_name,
        access_token=access_token,
        default_branch=default_branch,
        workflow_name=workflow_name,
    )

    # Prepend workflows and manifests so they're always included if slots remain
    priority_paths = workflow_files + _MANIFEST_FILES
    paths_to_fetch = priority_paths + [p for p in candidates if p not in priority_paths]

    result: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            while paths_to_fetch and len(result) < max_files:
                path = paths_to_fetch.pop(0)
                if path in result:
                    continue
                try:
                    resp = await client.get(
                        f"https://api.github.com/repos/{repo_full_name}/contents/{path}",
                        headers=headers,
                        params={"ref": default_branch},
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if data.get("type") != "file" or not data.get("content"):
                        continue
                    content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
                    if len(content) > max_file_bytes:
                        content = content[:max_file_bytes] + f"\n... [truncated at {max_file_bytes} chars] ..."
                    result[path] = content
                    _queue_python_imports(path, content, paths_to_fetch, result, max_files)
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"Failed to fetch source files for {repo_full_name}: {e}")

    if result:
        logger.info(f"Fetched {len(result)} source files for {repo_full_name}: {list(result.keys())}")
    return result


async def _find_workflow_files(
    repo_full_name: str,
    access_token: str,
    default_branch: str,
    workflow_name: str | None,
) -> list[str]:
    """Best-effort lookup for the workflow file(s) most likely tied to this run."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/contents/.github/workflows",
                headers=headers,
                params={"ref": default_branch},
            )
    except Exception as e:
        logger.warning(f"Failed to list workflow files for {repo_full_name}: {e}")
        return _workflow_name_candidates(workflow_name)

    if resp.status_code != 200:
        logger.warning(f"Workflow directory lookup returned {resp.status_code} for {repo_full_name}")
        return _workflow_name_candidates(workflow_name)

    items = resp.json()
    workflow_paths = [
        item["path"] for item in items
        if item.get("type") == "file" and item.get("path", "").endswith((".yml", ".yaml"))
    ]
    if not workflow_paths:
        return _workflow_name_candidates(workflow_name)

    if not workflow_name:
        return workflow_paths[:2]

    workflow_slug = _slugify_workflow_name(workflow_name)
    matched = []
    for path in workflow_paths:
        filename = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if _slugify_workflow_name(filename) == workflow_slug:
            matched.append(path)

    if matched:
        return matched

    guessed = _workflow_name_candidates(workflow_name)
    return guessed + [path for path in workflow_paths if path not in guessed][:1]


def _workflow_name_candidates(workflow_name: str | None) -> list[str]:
    if not workflow_name:
        return []
    slug = _slugify_workflow_name(workflow_name)
    if not slug:
        return []
    return [
        f".github/workflows/{slug}.yml",
        f".github/workflows/{slug}.yaml",
    ]


def _slugify_workflow_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug


def _queue_python_imports(
    fetched_path: str,
    content: str,
    paths_to_fetch: list[str],
    result: dict[str, str],
    max_files: int,
):
    """Follow simple Python import chains for fetched source files."""
    if not fetched_path.endswith(".py") or len(result) >= max_files:
        return

    base_dir = fetched_path.rsplit("/", 1)[0] if "/" in fetched_path else ""
    for match in _PYTHON_IMPORT_RE.finditer(content):
        module = match.group(1)
        module_path = module.replace(".", "/")
        candidates = []
        if module.startswith("."):
            relative = module.lstrip(".").replace(".", "/")
            if relative:
                candidates.append(f"{base_dir}/{relative}.py")
                candidates.append(f"{base_dir}/{relative}/__init__.py")
        else:
            candidates.extend([
                f"{module_path}.py",
                f"{module_path}/__init__.py",
                f"src/{module_path}.py",
                f"src/{module_path}/__init__.py",
            ])

        for candidate in candidates:
            normalized = candidate.lstrip("./")
            if normalized not in result and normalized not in paths_to_fetch:
                paths_to_fetch.append(normalized)


async def _fetch_commit_diff(commit_sha: str, repo_full_name: str, access_token: str) -> str:
    """Fetch the commit diff that likely introduced the failing change."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github.diff",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/commits/{commit_sha}",
                headers=headers,
            )
    except Exception as e:
        logger.warning(f"Failed to fetch commit diff for {repo_full_name}@{commit_sha[:7]}: {e}")
        return ""

    if resp.status_code != 200:
        logger.warning(
            f"Commit diff fetch returned {resp.status_code} for {repo_full_name}@{commit_sha[:7]}"
        )
        return ""

    diff = resp.text[:8000]
    if diff:
        logger.info(f"Fetched commit diff for {repo_full_name}@{commit_sha[:7]} ({len(diff)} chars)")
    return diff


async def _trigger_rerun(github_run_id: int, repo_full_name: str, access_token: str) -> bool:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{GITHUB_API}/repos/{repo_full_name}/actions/runs/{github_run_id}/rerun-failed-jobs",
                headers=headers,
            )
    except Exception as e:
        logger.warning(f"Failed to trigger rerun for {repo_full_name} run {github_run_id}: {e}")
        return False

    if resp.status_code not in (201, 202, 204):
        logger.warning(
            f"Rerun request failed for {repo_full_name} run {github_run_id}: "
            f"{resp.status_code} {resp.text[:200]}"
        )
        return False
    return True


async def _wait_for_run_completion(
    github_run_id: int,
    repo_full_name: str,
    access_token: str,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 10,
) -> str | None:
    """Poll the rerun until it completes so we can auto-skip immediately if needed."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    deadline = time.monotonic() + timeout_seconds

    async with httpx.AsyncClient(timeout=15.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(
                    f"{GITHUB_API}/repos/{repo_full_name}/actions/runs/{github_run_id}",
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"Failed to poll rerun status for {repo_full_name} run {github_run_id}: {e}")
                await _sleep(poll_interval_seconds)
                continue

            if resp.status_code != 200:
                logger.warning(
                    f"Rerun status poll returned {resp.status_code} for "
                    f"{repo_full_name} run {github_run_id}"
                )
                await _sleep(poll_interval_seconds)
                continue

            run = resp.json()
            status = run.get("status")
            conclusion = run.get("conclusion")
            if status == "completed":
                return conclusion

            await _sleep(poll_interval_seconds)

    logger.warning(f"Timed out waiting for rerun completion for {repo_full_name} run {github_run_id}")
    return None


async def _auto_skip_test(
    ci_run_id: str,
    repo_full_name: str,
    access_token: str,
    default_branch: str,
    diagnosis,
    diagnosis_row: dict,
    logs: str,
) -> bool:
    """Create a PR that skips the flaky test after a rerun still fails."""
    target = _infer_test_target(diagnosis.problem_summary, logs)
    test_file = target.get("test_file")
    test_name = target.get("test_name")
    if not test_file:
        logger.warning(f"Could not infer flaky test file for run {ci_run_id}")
        return False

    current_content = await _fetch_repo_file(repo_full_name, access_token, test_file, default_branch)
    if current_content is None:
        logger.warning(f"Could not fetch flaky test file {test_file} for run {ci_run_id}")
        return False

    new_content = _build_skipped_test_content(test_file, current_content, test_name)
    if not new_content or new_content == current_content:
        logger.warning(
            f"Could not build skip patch for run {ci_run_id} "
            f"(file={test_file}, test_name={test_name!r})"
        )
        return False

    skip_diagnosis = {
        "id": diagnosis_row.get("id") or f"skip-test-{ci_run_id}",
        "fix_type": "safe_auto_apply",
        "files_changed": [{
            "path": test_file,
            "new_content": new_content,
            "explanation": (
                f"Automatically skipped flaky test '{test_name or test_file}' after rerun still failed"
            ),
        }],
    }

    _update_status(ci_run_id, "applying")
    try:
        pr_result = await create_fix_pr(
            repo_full_name=repo_full_name,
            access_token=access_token,
            run_id=ci_run_id,
            diagnosis=skip_diagnosis,
        )
    except PRCreationError as e:
        logger.error(f"Auto skip-test PR creation failed for run {ci_run_id}: {e}")
        await _mark_failed(ci_run_id, "diagnosis_failed", f"Auto skip-test PR failed: {str(e)[:200]}")
        return False

    supabase.table("ci_runs").update({
        "status": "fixed",
        "fix_branch_name": pr_result["branch"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()

    supabase.table("diagnoses").update({
        "github_pr_url": pr_result["pr_url"],
        "github_pr_number": pr_result["pr_number"],
        "fix_description": f"{diagnosis.root_cause}\n\nAuto action: reran failed jobs, then opened a skip PR when rerun still failed.",
    }).eq("id", diagnosis_row["id"]).execute()
    logger.info(f"Auto skip-test PR created for run {ci_run_id}: {pr_result['pr_url']}")
    return True


def _infer_test_target(problem_summary: str, logs: str) -> dict[str, str | None]:
    combined = f"{problem_summary}\n{logs}"

    file_match = re.search(r"(tests?/[\w./-]+\.(?:py|ts|tsx|js|jsx))", combined)
    test_file = file_match.group(1) if file_match else None

    pytest_match = re.search(r"::([A-Za-z_][A-Za-z0-9_]*)", combined)
    if pytest_match:
        return {"test_file": test_file, "test_name": pytest_match.group(1)}

    func_match = re.search(r"\b(test_[A-Za-z0-9_]+)\b", combined)
    if func_match:
        return {"test_file": test_file, "test_name": func_match.group(1)}

    jest_match = re.search(r"●[^\n]*›\s*([^\n]+)", combined)
    if jest_match:
        return {"test_file": test_file, "test_name": jest_match.group(1).strip()}

    return {"test_file": test_file, "test_name": None}


async def _fetch_repo_file(
    repo_full_name: str,
    access_token: str,
    path: str,
    ref: str,
) -> str | None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}",
                headers=headers,
                params={"ref": ref},
            )
    except Exception as e:
        logger.warning(f"Failed to fetch repo file {path} from {repo_full_name}: {e}")
        return None

    if resp.status_code != 200:
        return None

    data = resp.json()
    if data.get("type") != "file" or not data.get("content"):
        return None
    return base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")


def _build_skipped_test_content(test_file: str, current_content: str, test_name: str | None) -> str | None:
    is_pytest = test_file.endswith(".py")
    is_jest = test_file.endswith((".ts", ".tsx", ".js", ".jsx"))
    if is_pytest:
        if test_name:
            pattern = re.compile(rf"^([ \t]*)(async def|def)\s+({re.escape(test_name)})\s*\(", re.MULTILINE)
            match = re.search(pattern, current_content)
            if match:
                indent = match.group(1)
                insert_pos = match.start()
                skip_line = f'{indent}@pytest.mark.skip(reason="Skipped by Drufiy - flaky or placeholder test")\n'
                new_content = current_content[:insert_pos] + skip_line + current_content[insert_pos:]
                if "import pytest" not in new_content:
                    new_content = "import pytest\n" + new_content
                return new_content

        # Fallback: skip the whole pytest module when we know the file but not the exact test.
        prefix = 'import pytest\npytestmark = pytest.mark.skip(reason="Skipped by Drufiy - flaky test module")\n'
        if "pytestmark = pytest.mark.skip(" in current_content:
            return None
        if "import pytest" in current_content:
            return f'pytestmark = pytest.mark.skip(reason="Skipped by Drufiy - flaky test module")\n{current_content}'
        return prefix + current_content

    if is_jest:
        if test_name:
            pattern = re.compile(rf"(test|it)\s*\(\s*['\"]({re.escape(test_name)})['\"]")
            if re.search(pattern, current_content):
                return re.sub(pattern, r"\1.skip('\2'", current_content)
        fallback_pattern = re.compile(r"\b(test|it)\s*\(")
        if re.search(fallback_pattern, current_content):
            return re.sub(fallback_pattern, r"\1.skip(", current_content, count=1)

    return None


def _mark_rerun_resolved(ci_run_id: str, diagnosis_id: str | None):
    supabase.table("ci_runs").update({
        "status": "verified",
        "error_message": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()
    mark_agent_run_outcome(ci_run_id, "verified")
    if diagnosis_id:
        supabase.table("diagnoses").update({
            "verification_status": "verified",
            "fix_description": "Automatically resolved by rerunning failed jobs after flaky test diagnosis.",
        }).eq("id", diagnosis_id).execute()


async def _sleep(seconds: int):
    import asyncio
    await asyncio.sleep(seconds)


async def _sanity_check_fix(ci_run_id: str, diagnosis) -> tuple[bool, str]:
    """
    Sanity-check the proposed fix using Kimi.
    If the review call fails for any reason, approve by default (fail-open)
    so we don't block PRs on transient API errors.
    """
    review_prompt = f"""
Review this proposed CI fix before it is auto-applied.

Problem summary:
{diagnosis.problem_summary}

Root cause:
{diagnosis.root_cause}

Fix description:
{diagnosis.fix_description}

Proposed file changes:
{json.dumps([fc.model_dump() for fc in diagnosis.files_changed], indent=2)}

Approve only if the fix correctly addresses the root cause, preserves unrelated code,
and is unlikely to introduce a new breakage.
"""
    try:
        review = await call_with_tool(
            system_prompt="You are a strict CI fix reviewer. Reject any risky or incomplete auto-apply.",
            user_prompt=review_prompt,
            tool_schema=SANITY_REVIEW_TOOL,
            run_id=ci_run_id,
            call_type="sanity_review",
            model="kimi",
        )
    except Exception as e:
        logger.warning(f"Sanity review failed for run {ci_run_id}: {e}")
        return True, f"Sanity review failed open: {e}"

    return bool(review.get("approve")), review.get("reason", "")


async def _materialize_patch_file_changes(diagnosis, repo_full_name: str, access_token: str, default_branch: str):
    for file_change in diagnosis.files_changed:
        if file_change.new_content or not file_change.patch:
            continue
        current_content = await _fetch_repo_file(repo_full_name, access_token, file_change.path, default_branch)
        if current_content is None:
            raise DiagnosisValidationError(f"Could not fetch {file_change.path} to apply patch")
        file_change.new_content = apply_unified_patch(current_content, file_change.patch)


def _fetch_similar_fixes(repo_id: str, limit: int = 3) -> list[dict]:
    """
    Return the most recent verified fixes for this repo as RAG context.
    Best-effort — returns [] on any error.
    """
    try:
        # Step 1: collect all ci_run IDs for this repo
        runs_resp = (
            supabase.table("ci_runs")
            .select("id")
            .eq("connected_repo_id", repo_id)
            .execute()
        )
        run_ids = [r["id"] for r in (runs_resp.data or [])]
        if not run_ids:
            return []

        # Step 2: most recent verified diagnoses that produced a file fix
        diag_resp = (
            supabase.table("diagnoses")
            .select("problem_summary, root_cause, fix_description, category, confidence, files_changed")
            .in_("run_id", run_ids)
            .eq("verification_status", "verified")
            .neq("fix_type", "manual_required")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        fixes = diag_resp.data or []
        logger.info(f"RAG: fetched {len(fixes)} verified fixes for repo_id={repo_id}")
        return fixes
    except Exception as e:
        logger.warning(f"RAG fetch failed for repo_id={repo_id}: {e}")
        return []




# ── Internal helpers ──────────────────────────────────────────────────────────

async def _has_open_drufiy_pr(repo_full_name: str, access_token: str) -> bool:
    """
    Check if there's already an open PR from Drufiy for this repo.
    Prevents duplicate PRs when multiple CI runs fail for the same issue.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo_full_name}/pulls",
                headers=headers,
                params={"state": "open", "per_page": 10, "sort": "created", "direction": "desc"},
            )
            if resp.status_code != 200:
                return False
            for pr in resp.json():
                head_ref = pr.get("head", {}).get("ref", "")
                if head_ref.startswith("drufiy/fix-run-"):
                    logger.info(f"Dedup: found open Drufiy PR #{pr['number']} ({head_ref}) for {repo_full_name}")
                    return True
    except Exception as e:
        logger.warning(f"Dedup check failed for {repo_full_name}: {e}")
    return False


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
        "speculative": diagnosis.speculative,
        "files_changed": [fc.model_dump() for fc in diagnosis.files_changed],
        "required_secrets": diagnosis.required_secrets,
    }
    result = supabase.table("diagnoses").insert(row).execute()
    return result.data[0] if result.data else row


async def _apply_fix(ci_run_id: str, repo_full_name: str, access_token: str, repo_id: str, diagnosis_row: dict, diagnosis):
    """Create the fix PR and update ci_run + diagnoses tables."""
    _update_status(ci_run_id, "applying")

    approved, review_reason = await _sanity_check_fix(ci_run_id, diagnosis)
    if not approved:
        logger.warning(f"Sanity review rejected auto-apply for run {ci_run_id}: {review_reason}")
        supabase.table("ci_runs").update({
            "status": "diagnosed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", ci_run_id).execute()
        supabase.table("diagnoses").update({
            "fix_type": "review_recommended",
            "fix_description": f"{diagnosis.fix_description}\n\nReview: {review_reason}",
        }).eq("id", diagnosis_row["id"]).execute()
        return

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
        # Fetch repo name for Slack alerts before writing status
        repo_name = ""
        try:
            run_row = supabase.table("ci_runs").select("connected_repos(repo_full_name)").eq("id", ci_run_id).single().execute()
            repo_name = ((run_row.data or {}).get("connected_repos") or {}).get("repo_full_name", "")
        except Exception:
            pass

        supabase.table("ci_runs").update({
            "status": status,
            "error_message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", ci_run_id).execute()
        if status in ("verified", "exhausted", "diagnosis_failed"):
            mark_agent_run_outcome(ci_run_id, status)

        # Slack alerts for terminal failure states
        if status == "exhausted":
            await notify_exhausted(ci_run_id, repo_name)
        elif status == "diagnosis_failed":
            await notify_diagnosis_failed(ci_run_id, repo_name, message)
    except Exception as e:
        logger.error(f"Failed to mark run {ci_run_id} as {status}: {e}")
