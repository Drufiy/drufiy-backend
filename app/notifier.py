"""
Internal Slack notification helper.
All calls are best-effort — never raises, never blocks the pipeline.
Set SLACK_WEBHOOK_URL env var to enable. Leave unset to disable silently.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_EMOJI = {
    "info": "ℹ️",
    "success": "✅",
    "warning": "⚠️",
    "error": "🔴",
}


async def notify(message: str, level: str = "info") -> None:
    """Fire-and-forget Slack message. Silently skipped if webhook not configured."""
    if not settings.slack_webhook_url:
        return
    emoji = _EMOJI.get(level, "•")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                settings.slack_webhook_url,
                json={"text": f"{emoji} {message}"},
            )
    except Exception as e:
        logger.debug(f"Slack notify failed (non-critical): {e}")


# ── Convenience wrappers ──────────────────────────────────────────────────────

async def notify_new_signup(github_username: str) -> None:
    await notify(f"🎉 New signup: *@{github_username}*", level="success")


async def notify_diagnosis_failed(run_id: str, repo: str, error: str) -> None:
    await notify(
        f"Diagnosis failed for *{repo}* (run `{run_id[:8]}`)\n> {error[:200]}",
        level="error",
    )


async def notify_exhausted(run_id: str, repo: str) -> None:
    await notify(
        f"Run *exhausted* after 4 iterations — needs manual review\n"
        f"Repo: *{repo}* · Run: `{run_id[:8]}`\n"
        f"<{settings.frontend_url}/run/{run_id}|Open in Drufiy>",
        level="error",
    )


async def notify_deepseek_fallback(run_id: str, reason: str) -> None:
    await notify(
        f"DeepSeek fallback triggered for run `{run_id[:8]}`\n> {reason[:200]}",
        level="warning",
    )


async def notify_reconciler_rescued(run_id: str, from_status: str) -> None:
    await notify(
        f"Reconciler rescued stuck run `{run_id[:8]}` (was *{from_status}*)",
        level="warning",
    )


async def notify_verified(run_id: str, repo: str, pr_url: str) -> None:
    await notify(
        f"Fix *verified* ✅ — CI passed on fix branch\n"
        f"Repo: *{repo}* · <{pr_url}|View PR>",
        level="success",
    )
