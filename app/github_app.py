from datetime import datetime, timedelta, timezone

import httpx
from jose import jwt

from app.config import settings
from app.db import supabase

GITHUB_API = "https://api.github.com"


def github_app_enabled() -> bool:
    return bool(settings.github_app_id and settings.github_app_private_key)


def create_github_app_jwt() -> str:
    if not github_app_enabled():
        raise ValueError("GitHub App credentials are not configured")
    now = datetime.now(timezone.utc)
    payload = {
        "iat": int((now - timedelta(seconds=60)).timestamp()),
        "exp": int((now + timedelta(minutes=9)).timestamp()),
        "iss": settings.github_app_id,
    }
    return jwt.encode(payload, settings.github_app_private_key, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    app_jwt = create_github_app_jwt()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    resp.raise_for_status()
    return resp.json()["token"]


async def list_installation_repos(installation_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GITHUB_API}/installation/repositories",
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    resp.raise_for_status()
    return resp.json().get("repositories", [])


async def get_repo_access_token(repo: dict) -> str | None:
    installation_id = repo.get("github_app_installation_id")
    if installation_id:
        return await get_installation_token(int(installation_id))

    user_id = repo.get("user_id")
    if not user_id:
        return None
    result = supabase.rpc(
        "get_decrypted_token",
        {"p_user_id": user_id, "p_key": settings.jwt_secret},
    ).execute()
    return result.data or None
