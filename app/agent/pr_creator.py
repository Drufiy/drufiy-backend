import base64
import logging
import re
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"


class PRCreationError(Exception):
    pass


class AuthError(PRCreationError):
    pass


async def create_fix_pr(
    repo_full_name: str,
    access_token: str,
    run_id: str,
    diagnosis: dict,
) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        repo_info = await _get(client, f"{GITHUB_API}/repos/{repo_full_name}")
        default_branch = repo_info["default_branch"]

        ref_info = await _get(client, f"{GITHUB_API}/repos/{repo_full_name}/git/refs/heads/{default_branch}")
        base_sha = ref_info["object"]["sha"]

        # Fetch commit blame — who broke it?
        blame_info = await _fetch_blame(client, repo_full_name, base_sha)

        branch_name = await _create_branch(client, repo_full_name, run_id, base_sha)

        for file_change in (diagnosis.get("files_changed") or []):
            await _put_file(client, repo_full_name, branch_name, file_change)

        pr = await _post(
            client,
            f"{GITHUB_API}/repos/{repo_full_name}/pulls",
            {
                "title": _pr_title(diagnosis),
                "body": _pr_body(diagnosis, branch_name, blame_info),
                "head": branch_name,
                "base": default_branch,
                "maintainer_can_modify": True,
            },
        )

    return {
        "pr_url": pr["html_url"],
        "pr_number": pr["number"],
        "branch": branch_name,
    }


async def _fetch_blame(client, repo_full_name: str, commit_sha: str) -> dict | None:
    """
    Fetch the commit that is at HEAD of the default branch (the one that broke CI).
    Returns author info for the PR body. Best-effort — never raises.
    """
    try:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo_full_name}/commits/{commit_sha}",
            headers={**client.headers, "Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        commit = data.get("commit", {})
        author = commit.get("author", {})
        gh_author = data.get("author") or {}
        return {
            "sha": commit_sha[:7],
            "message": (commit.get("message") or "").split("\n")[0][:80],
            "author_name": author.get("name") or gh_author.get("login") or "unknown",
            "author_login": gh_author.get("login"),
            "date": (author.get("date") or "")[:10],
        }
    except Exception as e:
        logger.warning(f"Could not fetch blame info for {repo_full_name}@{commit_sha[:7]}: {e}")
        return None


async def _create_branch(client, repo_full_name, run_id, base_sha) -> str:
    prefix = run_id[:8]
    for attempt in range(2):
        branch = f"drufiy/fix-run-{prefix}" if attempt == 0 else f"drufiy/fix-run-{prefix}-{int(datetime.now().timestamp())}"
        resp = await client.post(
            f"{GITHUB_API}/repos/{repo_full_name}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        if resp.status_code == 201:
            return branch
        if resp.status_code == 422 and "already exists" in resp.text.lower():
            continue
        _raise_github_error(resp, f"Failed to create branch {branch}")
    raise PRCreationError("Could not create fix branch after 2 attempts")


async def _put_file(client, repo_full_name, branch, file_change):
    path = file_change["path"]

    existing_sha = None
    current_content = ""
    get_resp = await client.get(
        f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}",
        params={"ref": branch},
    )
    if get_resp.status_code == 200:
        existing_sha = get_resp.json().get("sha")
        current_content = base64.b64decode(get_resp.json()["content"]).decode("utf-8", errors="replace")
    elif get_resp.status_code != 404:
        _raise_github_error(get_resp, f"Failed to check existing file {path}")

    new_content = file_change.get("new_content")
    if not new_content and file_change.get("patch"):
        new_content = apply_unified_patch(current_content, file_change["patch"])
    if new_content is None:
        raise PRCreationError(f"Failed to materialize content for {path}")

    body = {
        "message": f"fix: {path} — drufiy auto-fix",
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        body["sha"] = existing_sha

    resp = await client.put(f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}", json=body)
    if resp.status_code not in (200, 201):
        _raise_github_error(resp, f"Failed to commit {path}")


async def _get(client, url):
    resp = await client.get(url)
    if resp.status_code != 200:
        _raise_github_error(resp, f"GET {url}")
    return resp.json()


async def _post(client, url, body):
    resp = await client.post(url, json=body)
    if resp.status_code not in (200, 201):
        _raise_github_error(resp, f"POST {url}")
    return resp.json()


def _raise_github_error(resp, context):
    if resp.status_code == 401:
        raise AuthError(f"{context}: GitHub token invalid or expired")
    if resp.status_code == 403:
        body = resp.text
        if "rate limit" in body.lower():
            raise PRCreationError(f"{context}: GitHub rate limit exceeded. Retry in ~60 minutes.")
        raise PRCreationError(f"{context}: forbidden — token lacks required scope")
    if resp.status_code == 404:
        raise PRCreationError(f"{context}: resource not found")
    raise PRCreationError(f"{context}: {resp.status_code} {resp.text[:300]}")


def apply_unified_patch(current_content: str, patch_text: str) -> str:
    original_lines = current_content.splitlines(keepends=True)
    patch_lines = patch_text.splitlines(keepends=True)
    output: list[str] = []
    original_index = 0
    i = 0

    while i < len(patch_lines):
        line = patch_lines[i]
        if not line.startswith("@@"):
            i += 1
            continue

        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if not match:
            raise PRCreationError("Invalid unified diff hunk header")

        old_start = int(match.group(1)) - 1
        output.extend(original_lines[original_index:old_start])
        original_index = old_start
        i += 1

        while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
            hunk_line = patch_lines[i]
            if hunk_line.startswith(" "):
                output.append(original_lines[original_index])
                original_index += 1
            elif hunk_line.startswith("-"):
                original_index += 1
            elif hunk_line.startswith("+"):
                output.append(hunk_line[1:])
            elif hunk_line.startswith("\\"):
                pass
            else:
                raise PRCreationError("Invalid unified diff content")
            i += 1

    output.extend(original_lines[original_index:])
    return "".join(output)


def _pr_title(diagnosis):
    speculative_tag = "[SPECULATIVE] " if diagnosis.get("speculative") else ""
    return f"{speculative_tag}fix: {diagnosis['problem_summary'][:60]} [Drufiy]"


def _pr_body(diagnosis, branch_name, blame_info: dict | None = None):
    files_section = "\n".join(
        f"- `{f['path']}` — {f['explanation']}" for f in (diagnosis.get("files_changed") or [])
    )
    confidence_pct = int((diagnosis.get("confidence") or 0) * 100)

    speculative_warning = ""
    if diagnosis.get("speculative"):
        speculative_warning = (
            f"> ⚠️ **Speculative fix** — Drufiy is only {confidence_pct}% confident this resolves the issue. "
            "Please review the diff carefully before merging.\n\n"
        )

    blame_section = ""
    if blame_info:
        author = blame_info["author_name"]
        if blame_info.get("author_login"):
            author = f"@{blame_info['author_login']}"
        blame_section = (
            f"\n**Introduced by:** {author} in "
            f"`{blame_info['sha']}` — \"{blame_info['message']}\" ({blame_info['date']})\n"
        )

    return f"""## 🤖 Drufiy Auto-Fix

{speculative_warning}**Problem**
{diagnosis['problem_summary']}

**Root Cause**
{diagnosis['root_cause']}

**Fix Applied**
{diagnosis['fix_description']}

**Files Changed**
{files_section}
{blame_section}
**Confidence:** {confidence_pct}% · **Category:** {diagnosis.get('category', 'unknown')} · **Fix Type:** {diagnosis['fix_type']}

---
*This PR was created automatically by [Drufiy]({settings.frontend_url}). Drufiy will verify that CI passes on this branch before marking the fix complete.*
*Branch: `{branch_name}`*
"""
