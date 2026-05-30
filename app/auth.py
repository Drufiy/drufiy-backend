import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from jose import jwt, JWTError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings
from app.db import supabase
from app.token_crypto import get_github_token

logger = logging.getLogger(__name__)
security = HTTPBearer()
_revocation_table_available: bool | None = None


def create_access_token(user_id: str, github_username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "github_username": github_username,
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expiry_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    jti = payload.get("jti")
    if jti and _is_token_revoked(jti):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    result = (
        supabase.table("user_profiles")
        .select("id, github_username, email")
        .eq("id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=401, detail="User not found")

    return {
        "id": result.data["id"],
        "github_username": result.data["github_username"],
        "email": result.data.get("email"),
        "github_access_token": get_github_token(user_id),
    }


def revoke_access_token(token: str, user_id: str | None = None) -> bool:
    global _revocation_table_available
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
    except JWTError:
        return False

    jti = payload.get("jti")
    if not jti:
        return False

    expires_at = _timestamp_to_iso(payload.get("exp"))
    try:
        supabase.table("jwt_revocations").upsert(
            {
                "jti": jti,
                "user_id": user_id or payload.get("sub"),
                "expires_at": expires_at,
            },
            on_conflict="jti",
        ).execute()
        _revocation_table_available = True
        return True
    except Exception as e:
        _revocation_table_available = False
        logger.warning(f"JWT revocation table unavailable; logout cannot revoke token jti={jti[:8]}: {e}")
        return False


def _is_token_revoked(jti: str) -> bool:
    global _revocation_table_available
    if _revocation_table_available is False:
        return False
    try:
        result = (
            supabase.table("jwt_revocations")
            .select("jti")
            .eq("jti", jti)
            .limit(1)
            .execute()
        )
        _revocation_table_available = True
        return bool(result.data)
    except Exception as e:
        _revocation_table_available = False
        logger.warning(f"JWT revocation check skipped because table is unavailable: {e}")
        return False


def _timestamp_to_iso(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return (datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiry_hours)).isoformat()
