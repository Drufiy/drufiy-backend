import logging
from datetime import datetime, timezone

from app.db import supabase

logger = logging.getLogger(__name__)


async def process_failure(ci_run_id: str):
    """
    Day 1 stub: transitions ci_run through diagnosing → diagnosed.
    Day 2: replace with real log fetch + Kimi diagnosis + diff check.
    """
    logger.info(f"process_failure start run_id={ci_run_id}")
    try:
        _update_status(ci_run_id, "diagnosing")

        # ── Day 2: uncomment and replace below with real pipeline ──
        # from app.config import settings
        # from app.agent.log_fetcher import fetch_workflow_logs, LogsNotAvailableError, InsufficientPermissionsError, LogFetchError
        # from app.agent.diagnosis_agent import diagnose_failure
        # from app.agent.kimi_client import DiagnosisValidationError
        # from app.agent.workflow_diff import assess_diff_risk
        # ... (see sprint plan Day 2 for full implementation)

        import asyncio
        await asyncio.sleep(0)  # yield control, keep as async

        _update_status(ci_run_id, "diagnosed")
        logger.info(f"process_failure done (stub) run_id={ci_run_id}")

    except Exception as e:
        logger.exception(f"process_failure crashed run_id={ci_run_id}: {e}")
        await _mark_failed(ci_run_id, "diagnosis_failed", f"Unexpected error: {str(e)[:200]}")


async def process_iteration_2(ci_run_id: str, new_logs: str, previous_diagnosis: dict):
    """Day 2 stub."""
    logger.info(f"process_iteration_2 stub run_id={ci_run_id}")


def _update_status(ci_run_id: str, status: str):
    supabase.table("ci_runs").update({
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ci_run_id).execute()


async def _mark_failed(ci_run_id: str, status: str, message: str):
    try:
        supabase.table("ci_runs").update({
            "status": status,
            "error_message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", ci_run_id).execute()
    except Exception as e:
        logger.error(f"Failed to mark run {ci_run_id} as failed: {e}")
