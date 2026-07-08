import logging

from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)


_warned_missing_key = False


def token_encryption_key() -> str:
    global _warned_missing_key
    if not settings.token_encryption_key and not _warned_missing_key:
        logger.warning("TOKEN_ENCRYPTION_KEY not set — falling back to JWT_SECRET. Set a separate key in production.")
        _warned_missing_key = True
    return settings.token_encryption_key or settings.jwt_secret


def token_decryption_keys() -> list[str]:
    primary = token_encryption_key()
    if primary == settings.jwt_secret:
        return [primary]
    return [primary, settings.jwt_secret]


def store_github_token(user_id: str, access_token: str) -> None:
    supabase.rpc(
        "store_encrypted_token",
        {
            "p_user_id": user_id,
            "p_token": access_token,
            "p_key": token_encryption_key(),
        },
    ).execute()


def get_github_token(user_id: str) -> str | None:
    for key in token_decryption_keys():
        try:
            result = supabase.rpc(
                "get_decrypted_token",
                {"p_user_id": user_id, "p_key": key},
            ).execute()
            if result.data:
                return result.data
        except Exception as e:
            logger.warning("GitHub token decrypt failed for user %s: %s", user_id, e)
    return None
