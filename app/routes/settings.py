import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agent.vercel_client import validate_vercel_token
from app.auth import get_current_user
from app.token_crypto import clear_vercel_token, get_vercel_token, store_vercel_token

logger = logging.getLogger(__name__)
router = APIRouter()


class VercelTokenRequest(BaseModel):
    token: str


@router.get("/vercel-token/status")
async def vercel_token_status(current_user: dict = Depends(get_current_user)):
    return {"connected": bool(get_vercel_token(current_user["id"]))}


@router.post("/vercel-token")
async def save_vercel_token(
    body: VercelTokenRequest,
    current_user: dict = Depends(get_current_user),
):
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")

    valid = await validate_vercel_token(token)
    if not valid:
        raise HTTPException(status_code=400, detail="Vercel rejected this token — check it's a valid personal access token")

    store_vercel_token(current_user["id"], token)
    logger.info(f"Vercel token saved for user {current_user['id']}")
    return {"connected": True}


@router.delete("/vercel-token")
async def remove_vercel_token(current_user: dict = Depends(get_current_user)):
    clear_vercel_token(current_user["id"])
    return {"connected": False}
