import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import create_access_token, get_current_user
from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)
router = APIRouter()


class OAuthCallbackRequest(BaseModel):
    code: str


# ── GitHub OAuth callback ────────────────────────────────────────────────────

@router.post("/github/callback")
async def github_callback(body: OAuthCallbackRequest):
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Exchange code for access token
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": body.code,
            },
            headers={"Accept": "application/json"},
        )

    token_data = token_resp.json()
    if "error" in token_data:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "oauth_failed",
                "message": token_data.get("error_description", token_data["error"]),
            },
        )

    access_token = token_data["access_token"]
    gh_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        user_resp = await client.get("https://api.github.com/user", headers=gh_headers)
        user_resp.raise_for_status()
        gh_user = user_resp.json()

        email = gh_user.get("email")
        if not email:
            emails_resp = await client.get("https://api.github.com/user/emails", headers=gh_headers)
            emails_resp.raise_for_status()
            primary = next(
                (e["email"] for e in emails_resp.json() if e.get("primary") and e.get("verified")),
                None,
            )
            email = primary

    github_user_id = gh_user["id"]
    login = gh_user["login"]

    # Upsert user_profiles (id auto-generated on first insert via gen_random_uuid())
    result = supabase.table("user_profiles").upsert(
        {
            "github_user_id": github_user_id,
            "github_username": login,
            "email": email,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="github_user_id",
    ).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to upsert user profile")

    user_row = result.data[0]
    user_id = user_row["id"]

    # Encrypt and store the GitHub access token
    try:
        supabase.rpc(
            "store_encrypted_token",
            {"p_user_id": user_id, "p_token": access_token, "p_key": settings.jwt_secret},
        ).execute()
    except Exception as e:
        logger.warning(f"Failed to store encrypted token for user {user_id}: {e}")

    jwt = create_access_token(user_id=user_id, github_username=login)

    return {
        "token": jwt,
        "user": {"id": user_id, "github_username": login, "email": email},
    }


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "github_username": current_user["github_username"],
        "email": current_user.get("email"),
    }


@router.post("/logout")
async def logout():
    return {"success": True}
