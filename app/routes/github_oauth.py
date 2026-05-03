import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import create_access_token, get_current_user
from app.config import settings
from app.db import supabase
from app.github_app import get_installation_token, github_app_enabled, list_installation_repos
from app.notifier import notify_new_signup

logger = logging.getLogger(__name__)
router = APIRouter()


class OAuthCallbackRequest(BaseModel):
    code: str
    redirect_uri: str | None = None  # frontend passes its own origin so exchange matches


# ── GitHub OAuth callback ────────────────────────────────────────────────────

@router.post("/github/callback")
async def github_callback(body: OAuthCallbackRequest):
    exchange_payload: dict = {
        "client_id": settings.github_client_id,
        "client_secret": settings.github_client_secret,
        "code": body.code,
    }
    # Including redirect_uri in the exchange prevents GitHub edge-case mismatches
    if body.redirect_uri:
        exchange_payload["redirect_uri"] = body.redirect_uri

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Exchange code for access token
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json=exchange_payload,
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
    granted_scopes = token_data.get("scope", "")
    logger.info(f"GitHub OAuth granted scopes: {granted_scopes!r}")
    gh_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        user_resp = await client.get("https://api.github.com/user", headers=gh_headers)
        user_resp.raise_for_status()
        # GitHub echoes the token's scopes in the response header
        response_scopes = user_resp.headers.get("X-OAuth-Scopes", granted_scopes)
        logger.info(f"GitHub token scopes (from response header): {response_scopes!r}")
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
    is_new_user = user_row.get("created_at") == user_row.get("updated_at")

    # Slack alert on first signup
    if is_new_user:
        await notify_new_signup(login)

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


@router.get("/scopes")
async def check_scopes(current_user: dict = Depends(get_current_user)):
    """
    Returns whether the stored GitHub token includes the 'workflow' scope.
    Calls GET /user and reads the X-OAuth-Scopes response header from GitHub.
    """
    user_id = current_user["id"]
    try:
        result = supabase.rpc(
            "get_decrypted_token",
            {"p_user_id": user_id, "p_key": settings.jwt_secret},
        ).execute()
        access_token = result.data
    except Exception as e:
        logger.warning(f"Scope check: failed to decrypt token for user {user_id}: {e}")
        return {"has_workflow_scope": False, "scopes": []}

    if not access_token:
        return {"has_workflow_scope": False, "scopes": []}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        scopes_header = resp.headers.get("X-OAuth-Scopes", "")
        scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]
        has_workflow = "workflow" in scopes
        logger.info(f"Scope check for user {user_id}: {scopes}, has_workflow={has_workflow}")
        return {"has_workflow_scope": has_workflow, "scopes": scopes}
    except Exception as e:
        logger.warning(f"Scope check: GitHub request failed for user {user_id}: {e}")
        return {"has_workflow_scope": False, "scopes": []}


@router.post("/logout")
async def logout():
    return {"success": True}


@router.get("/github-app/install-url")
async def github_app_install_url():
    if not github_app_enabled() or not settings.github_app_slug:
        raise HTTPException(status_code=503, detail="GitHub App is not configured")
    return {
        "install_url": f"https://github.com/apps/{settings.github_app_slug}/installations/new"
    }


class RegisterAppInstallRequest(BaseModel):
    installation_id: int
    setup_action: str | None = None


@router.post("/github-app/register")
async def github_app_register(
    body: RegisterAppInstallRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Called by the frontend after GitHub App installation redirect.
    GitHub redirects to the frontend with ?installation_id=XXX, and the frontend
    calls this authenticated endpoint to store the installation.
    """
    if not github_app_enabled():
        raise HTTPException(status_code=503, detail="GitHub App is not configured")

    try:
        installation_token = await get_installation_token(body.installation_id)
        repos = await list_installation_repos(installation_token)
    except Exception as e:
        logger.warning(f"GitHub App register failed for installation {body.installation_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch installation repositories")

    supabase.table("app_installations").upsert(
        {
            "user_id": current_user["id"],
            "installation_id": body.installation_id,
            "account_login": (repos[0].get("owner") or {}).get("login") if repos else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="installation_id",
    ).execute()

    return {
        "installation_id": body.installation_id,
        "repositories_count": len(repos),
    }


@router.get("/github-app/callback")
async def github_app_callback(
    installation_id: int,
    setup_action: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    if not github_app_enabled():
        raise HTTPException(status_code=503, detail="GitHub App is not configured")

    try:
        installation_token = await get_installation_token(installation_id)
        repos = await list_installation_repos(installation_token)
    except Exception as e:
        logger.warning(f"GitHub App callback failed for installation {installation_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch installation repositories")

    supabase.table("app_installations").upsert(
        {
            "user_id": current_user["id"],
            "installation_id": installation_id,
            "account_login": (repos[0].get("owner") or {}).get("login") if repos else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="installation_id",
    ).execute()

    return {
        "installation_id": installation_id,
        "setup_action": setup_action,
        "repositories": [
            {
                "id": repo["id"],
                "name": repo["name"],
                "full_name": repo["full_name"],
                "default_branch": repo.get("default_branch"),
                "installation_id": installation_id,
            }
            for repo in repos
        ],
    }
