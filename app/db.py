from supabase import create_client, Client
from app.config import settings
import logging

logger = logging.getLogger(__name__)

supabase: Client = create_client(settings.supabase_url, settings.supabase_service_key)


async def healthcheck() -> bool:
    try:
        supabase.table("user_profiles").select("id").limit(1).execute()
        return True
    except Exception as e:
        logger.warning(f"Supabase healthcheck failed: {e}")
        return False
