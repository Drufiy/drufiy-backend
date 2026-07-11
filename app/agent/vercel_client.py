import logging

import httpx

logger = logging.getLogger(__name__)

_VERCEL_API = "https://api.vercel.com"
MAX_LOG_CHARS = 80_000

# Deployment states that mean "still in progress, check back later"
_PENDING_STATES = {"BUILDING", "INITIALIZING", "QUEUED"}


async def find_vercel_deployment(commit_sha: str, vercel_token: str) -> dict | None:
    """
    Look up the Vercel deployment for a given commit SHA.
    Returns {"uid": str, "state": str, "url": str | None} or None if no
    deployment exists yet for this commit.
    """
    headers = {"Authorization": f"Bearer {vercel_token}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{_VERCEL_API}/v7/deployments",
                params={"sha": commit_sha, "limit": 5},
                headers=headers,
            )
        if resp.status_code != 200:
            logger.warning(f"Vercel deployments lookup failed for sha={commit_sha[:8]}: {resp.status_code}")
            return None
        deployments = resp.json().get("deployments", [])
        if not deployments:
            return None
        d = deployments[0]
        return {
            "uid": d.get("uid"),
            "state": d.get("state") or d.get("readyState"),
            "url": d.get("url"),
        }
    except Exception as e:
        logger.warning(f"Vercel deployment lookup failed for sha={commit_sha[:8]}: {e}")
        return None


def is_deployment_pending(state: str | None) -> bool:
    return state in _PENDING_STATES


async def fetch_vercel_build_logs(deployment_id: str, vercel_token: str) -> str:
    """
    Fetch the build log text for a Vercel deployment, tail-truncated to
    MAX_LOG_CHARS (matches the convention in log_fetcher.py).
    """
    headers = {"Authorization": f"Bearer {vercel_token}"}
    lines: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{_VERCEL_API}/v3/deployments/{deployment_id}/events",
                params={"builds": 1, "limit": -1},
                headers=headers,
            )
        if resp.status_code != 200:
            logger.warning(f"Vercel events fetch failed for deployment={deployment_id}: {resp.status_code}")
            return ""
        events = resp.json() or []
        for event in events:
            text = event.get("text") or (event.get("payload") or {}).get("text")
            if text:
                lines.append(text)
    except Exception as e:
        logger.warning(f"Vercel build log fetch failed for deployment={deployment_id}: {e}")
        return ""

    concatenated = "\n".join(lines)
    if len(concatenated) > MAX_LOG_CHARS:
        concatenated = "... [earlier logs truncated] ...\n" + concatenated[-MAX_LOG_CHARS:]
    return concatenated


async def validate_vercel_token(vercel_token: str) -> bool:
    """Sanity-check a token before storing it — used by the Settings save endpoint."""
    headers = {"Authorization": f"Bearer {vercel_token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_VERCEL_API}/v2/user", headers=headers)
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Vercel token validation request failed: {e}")
        return False
