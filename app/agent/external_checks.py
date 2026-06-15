import logging

import httpx

logger = logging.getLogger(__name__)

_GITHUB_ACTIONS_SLUGS = {"github-actions"}


async def detect_external_check_failures(
    repo_full_name: str,
    commit_sha: str,
    access_token: str,
) -> str | None:
    """
    After Prash verifies a fix (GitHub Actions CI passes), check for failing
    external checks (Vercel, Netlify, Cloudflare Pages, etc.) on the same commit.
    Returns a human-readable note if external failures exist, else None.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    external_failures: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            check_resp, status_resp = await _fetch_both(
                client, repo_full_name, commit_sha, headers
            )

        if check_resp:
            for cr in check_resp.get("check_runs", []):
                app = cr.get("app") or {}
                if app.get("slug", "") in _GITHUB_ACTIONS_SLUGS:
                    continue
                conclusion = cr.get("conclusion")
                if conclusion in ("failure", "timed_out", "action_required", "cancelled"):
                    app_name = app.get("name") or app.get("slug") or "Unknown"
                    external_failures.append(app_name)

        if status_resp:
            for s in status_resp.get("statuses", []):
                if s.get("state") in ("failure", "error"):
                    ctx = s.get("context", "")
                    if "github-actions" in ctx.lower():
                        continue
                    provider = ctx.split("/")[0] if "/" in ctx else ctx
                    external_failures.append(provider)

    except Exception as e:
        logger.warning(f"External check detection failed for {repo_full_name}@{commit_sha[:8]}: {e}")
        return None

    if not external_failures:
        return None

    unique = sorted(set(external_failures))
    names = ", ".join(unique)
    count = len(unique)
    return (
        f"CI fix verified — {count} external "
        f"{'check' if count == 1 else 'checks'} failing: {names}. "
        f"Not related to this fix."
    )


async def _fetch_both(
    client: httpx.AsyncClient,
    repo_full_name: str,
    sha: str,
    headers: dict,
) -> tuple[dict | None, dict | None]:
    check_runs = None
    statuses = None
    try:
        r1 = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/commits/{sha}/check-runs",
            headers=headers,
        )
        if r1.status_code == 200:
            check_runs = r1.json()
    except Exception:
        pass
    try:
        r2 = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/commits/{sha}/status",
            headers=headers,
        )
        if r2.status_code == 200:
            statuses = r2.json()
    except Exception:
        pass
    return check_runs, statuses
