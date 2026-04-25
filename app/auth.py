from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import settings
from app.db import supabase

security = HTTPBearer()


def create_access_token(user_id: str, github_username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "github_username": github_username,
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
    result = (
        supabase.table("user_profiles")
        .select("id, github_username, email")
        .eq("id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=401, detail="User not found")

    # Decrypt GitHub access token via RPC
    token_result = supabase.rpc(
        "get_decrypted_token", {"p_user_id": user_id, "p_key": settings.jwt_secret}
    ).execute()

    return {
        "id": result.data["id"],
        "github_username": result.data["github_username"],
        "email": result.data.get("email"),
        "github_access_token": token_result.data,
    }
