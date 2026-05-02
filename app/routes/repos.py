import base64
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

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


# ── List user's GitHub repos (not yet connected) ────────────────────────────

@router.get("/github-list")
async def list_github_repos(current_user: dict = Depends(get_current_user)):
    token = current_user["github_access_token"]
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not available — please re-authenticate")

    repos = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for page in range(1, 4):
            resp = await client.get(
                f"{GITHUB_API}/user/repos",
                headers=_gh_headers(token),
                params={"per_page": 100, "sort": "updated", "affiliation": "owner,collaborator,organization_member", "page": page},
            )
            if resp.status_code == 401:
                raise HTTPException(status_code=401, detail="GitHub token expired — please re-authenticate")
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            repos.extend(batch)

    filtered = [
        {
            "github_repo_id": r["id"],
            "name": r["name"],
            "full_name": r["full_name"],
            "default_branch": r["default_branch"],
            "private": r["private"],
            "updated_at": r["updated_at"],
        }
        for r in repos
        if not r.get("archived") and not (r.get("fork") and r["owner"]["login"] != current_user["github_username"])
    ]
    return filtered


# ── List connected repos ─────────────────────────────────────────────────────

@router.get("/")
def list_repos(current_user: dict = Depends(get_current_user)):
    result = (
        supabase.table("connected_repos")
        .select("*")
        .eq("user_id", current_user["id"])
        .eq("is_active", True)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


# ── Connect a repo (install webhook) ────────────────────────────────────────

class ConnectRepoRequest(BaseModel):
    repo_full_name: str
    github_repo_id: int
    repo_name: str
    default_branch: str


@router.post("/connect")
async def connect_repo(body: ConnectRepoRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    token = current_user["github_access_token"]
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not available — please re-authenticate")

    # Idempotency: return existing active row if already connected
    existing = (
        supabase.table("connected_repos")
        .select("*")
        .eq("github_repo_id", body.github_repo_id)
        .eq("user_id", user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]

    webhook_url = f"{settings.public_backend_url}/webhook/github"
    headers = _gh_headers(token)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Fetch actual default branch from GitHub
        repo_info_resp = await client.get(f"{GITHUB_API}/repos/{body.repo_full_name}", headers=headers)
        if repo_info_resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Repo not found or no access")
        repo_info_resp.raise_for_status()
        actual_default_branch = repo_info_resp.json().get("default_branch", body.default_branch)

        # Register webhook
        hook_resp = await client.post(
            f"{GITHUB_API}/repos/{body.repo_full_name}/hooks",
            headers=headers,
            json={
                "name": "web",
                "active": True,
                "events": ["workflow_run", "push"],
                "config": {
                    "url": webhook_url,
                    "content_type": "json",
                    "secret": settings.github_webhook_secret,
                    "insecure_ssl": "0",
                },
            },
        )

        if hook_resp.status_code == 403:
            # Check if this is an org repo — gives a clearer error message
            is_org_repo = "/" in body.repo_full_name and body.repo_full_name.split("/")[0] != current_user["github_username"]
            if is_org_repo:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Cannot install webhook on org repo '{body.repo_full_name}'. "
                        "Your token lacks admin:repo_hook permission for this org. "
                        "Ask your org admin to grant access, or use the Drufiy GitHub App (coming soon)."
                    ),
                )
            raise HTTPException(
                status_code=403,
                detail="insufficient_scope — please re-authenticate to grant webhook permissions",
            )
        if hook_resp.status_code == 422:
            # Webhook already exists — fetch existing webhook ID
            hooks_resp = await client.get(
                f"{GITHUB_API}/repos/{body.repo_full_name}/hooks", headers=headers
            )
            hooks_resp.raise_for_status()
            existing_hook = next(
                (h for h in hooks_resp.json() if h["config"].get("url") == webhook_url), None
            )
            if not existing_hook:
                raise HTTPException(status_code=422, detail="Webhook conflict but could not find existing hook")
            webhook_id = existing_hook["id"]
        else:
            hook_resp.raise_for_status()
            webhook_id = hook_resp.json()["id"]

    # Insert connected_repos row
    insert_result = (
        supabase.table("connected_repos")
        .insert(
            {
                "user_id": user_id,
                "github_repo_id": body.github_repo_id,
                "repo_name": body.repo_name,
                "repo_full_name": body.repo_full_name,
                "default_branch": actual_default_branch,
                "webhook_id": webhook_id,
                "is_active": True,
            }
        )
        .execute()
    )
    if not insert_result.data:
        raise HTTPException(status_code=500, detail="Failed to store connected repo")

    repo_row = insert_result.data[0]

    # Warm up known_good_files cache (best-effort)
    try:
        await _seed_known_good_files(
            repo_id=repo_row["id"],
            repo_full_name=body.repo_full_name,
            default_branch=actual_default_branch,
            token=token,
        )
    except Exception as e:
        logger.warning(f"Failed to seed known_good_files for {body.repo_full_name}: {e}")

    # Auto-install minimal CI workflow if none exists (best-effort)
    try:
        await _auto_install_workflow(
            repo_full_name=body.repo_full_name,
            default_branch=actual_default_branch,
            token=token,
        )
    except Exception as e:
        logger.warning(f"Failed to auto-install workflow for {body.repo_full_name}: {e}")

    return repo_row


async def _seed_known_good_files(repo_id: str, repo_full_name: str, default_branch: str, token: str):
    headers = _gh_headers(token)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo_full_name}/contents/.github/workflows",
            headers=headers,
            params={"ref": default_branch},
        )
        if resp.status_code != 200:
            return

        for item in resp.json():
            if not item["name"].endswith((".yml", ".yaml")):
                continue
            file_resp = await client.get(item["url"], headers=headers)
            if file_resp.status_code != 200:
                continue
            file_data = file_resp.json()
            content = base64.b64decode(file_data["content"]).decode("utf-8", errors="replace")
            supabase.table("known_good_files").upsert(
                {
                    "repo_id": repo_id,
                    "file_path": f".github/workflows/{item['name']}",
                    "content": content,
                    "commit_sha": file_data.get("sha", ""),
                },
                on_conflict="repo_id,file_path",
            ).execute()


_WORKFLOW_TEMPLATES: dict[str, str] = {
    "python": """\
name: drufiy-ci
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.x"
      - run: pip install flake8
      - run: python -m py_compile $(git ls-files '*.py') || true
      - run: flake8 --select=E9,F821,F823 --show-source .
""",
    "typescript": """\
name: drufiy-ci
on: [push, pull_request]
jobs:
  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "lts/*"
          cache: npm
      - run: npm ci
      - run: npx tsc --noEmit --skipLibCheck
""",
    "javascript": """\
name: drufiy-ci
on: [push, pull_request]
jobs:
  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "lts/*"
          cache: npm
      - run: npm ci
      - run: npx tsc --noEmit --skipLibCheck
""",
    "_default": """\
name: drufiy-ci
on: [push, pull_request]
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - run: echo "drufiy-ci ok"
""",
}


async def _auto_install_workflow(repo_full_name: str, default_branch: str, token: str) -> None:
    headers = _gh_headers(token)
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Check whether any workflows already exist
        check_resp = await client.get(
            f"{GITHUB_API}/repos/{repo_full_name}/contents/.github/workflows",
            headers=headers,
            params={"ref": default_branch},
        )
        if check_resp.status_code != 404:
            # Workflows directory exists (or unexpected error) — skip silently
            return

        # Detect primary language
        repo_resp = await client.get(f"{GITHUB_API}/repos/{repo_full_name}", headers=headers)
        repo_resp.raise_for_status()
        language = (repo_resp.json().get("language") or "").lower()

        template = _WORKFLOW_TEMPLATES.get(language, _WORKFLOW_TEMPLATES["_default"])
        encoded = base64.b64encode(template.encode()).decode()

        put_resp = await client.put(
            f"{GITHUB_API}/repos/{repo_full_name}/contents/.github/workflows/drufiy-ci.yml",
            headers=headers,
            json={
                "message": "ci: add minimal drufiy-ci workflow",
                "content": encoded,
                "branch": default_branch,
            },
        )
        if put_resp.status_code in (200, 201):
            logger.info(f"Auto-installed drufiy-ci.yml for {repo_full_name} (language={language or 'unknown'})")
        else:
            logger.warning(
                f"Could not create drufiy-ci.yml for {repo_full_name}: "
                f"HTTP {put_resp.status_code} — {put_resp.text[:200]}"
            )


# ── Disconnect a repo ────────────────────────────────────────────────────────

@router.delete("/{repo_id}")
async def disconnect_repo(repo_id: str, current_user: dict = Depends(get_current_user)):
    result = (
        supabase.table("connected_repos")
        .select("*")
        .eq("id", repo_id)
        .eq("user_id", current_user["id"])
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Repo not found")

    repo = result.data
    token = current_user["github_access_token"]

    if token:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{GITHUB_API}/repos/{repo['repo_full_name']}/hooks/{repo['webhook_id']}",
                    headers=_gh_headers(token),
                )
        except Exception as e:
            logger.warning(f"Failed to delete webhook for {repo['repo_full_name']}: {e}")

    supabase.table("connected_repos").update({"is_active": False}).eq("id", repo_id).execute()
    return {"success": True}


# ── List CI runs for a repo ──────────────────────────────────────────────────

@router.get("/{repo_id}/runs")
def list_repo_runs(
    repo_id: str,
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    # Verify ownership
    repo = (
        supabase.table("connected_repos")
        .select("id")
        .eq("id", repo_id)
        .eq("user_id", current_user["id"])
        .single()
        .execute()
    )
    if not repo.data:
        raise HTTPException(status_code=404, detail="Repo not found")

    query = (
        supabase.table("ci_runs")
        .select("id,github_run_id,run_name,branch,commit_sha,commit_message,status,fix_branch_name,error_message,created_at,updated_at")
        .eq("repo_id", repo_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if status:
        query = query.eq("status", status)

    return query.execute().data
