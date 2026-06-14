from contextlib import asynccontextmanager
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.logging_config import configure_logging
from app.config import settings
from app import db
from app.db import supabase

# Must be first — replaces basicConfig with JSON formatter for Cloud Run
configure_logging()
logger = logging.getLogger(__name__)


# ── Fix 2: recover ci_runs stuck in transient states after a server restart ───

def _recover_stuck_runs():
    """
    Log any runs stuck in transient states at startup.
    Actual recovery is handled by the async reconciler loop which checks
    for existing PRs before re-queuing (avoids overwriting verified runs).
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        stuck = (
            supabase.table("ci_runs")
            .select("id, status")
            .in_("status", ["diagnosing", "applying"])
            .lt("updated_at", cutoff)
            .execute()
        )
        if stuck.data:
            logger.info(f"Found {len(stuck.data)} stuck run(s) at startup — reconciler will handle them")
        else:
            logger.info("No stuck runs found — clean startup")
    except Exception as e:
        logger.warning(f"Stuck-run recovery check failed (non-fatal): {e}")


# ── Fix 3: pre-warm Kimi so the first real diagnosis isn't slow ───────────────

async def _prewarm_models():
    """Pre-warm HTTP connection pools for primary + fallback models."""
    # DeepSeek (primary)
    try:
        from app.agent.kimi_client import deepseek
        if deepseek:
            await deepseek.chat.completions.create(
                model=settings.deepseek_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            logger.info("DeepSeek pre-warm complete ✓")
    except Exception as e:
        logger.warning(f"DeepSeek pre-warm failed (non-fatal): {e}")

    # Kimi (fallback)
    try:
        from app.agent.kimi_client import kimi
        await kimi.chat.completions.create(
            model=settings.kimi_model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        logger.info("Kimi pre-warm complete ✓")
    except Exception as e:
        logger.warning(f"Kimi pre-warm failed (non-fatal): {e}")


async def _reconciler_loop():
    """
    Background loop: sweep ci_runs stuck in 'fixed' every 60s.
    Resolves spinners that got stuck because webhook events arrived during deploys.
    """
    from app.agent.reconciler import reconcile_stuck_verifications
    while True:
        try:
            await reconcile_stuck_verifications()
        except Exception as e:
            logger.warning(f"Reconciler loop error (non-fatal): {e}")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Drufiy backend starting")

    # Sync recovery of runs stuck mid-pipeline after restart
    _recover_stuck_runs()

    # Pre-warm primary (DeepSeek) + fallback (Kimi) connection pools
    asyncio.create_task(_prewarm_models())

    # Verification reconciler — auto-resolves spinners every 60s
    asyncio.create_task(_reconciler_loop())

    yield
    logger.info("Drufiy backend shutting down")


app = FastAPI(title="Drufiy Backend", version="0.1.0", lifespan=lifespan)

_cors_origins = list({
    settings.frontend_url,                # from FRONTEND_URL env var (Cloud Run)
    "http://localhost:3000",              # local dev
    "https://prash.drufiy.com",           # Prash app
    "https://drufiy.com",                 # marketing site
})
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Secret"],
)


# ── Request logging middleware ────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Skip /health — too noisy (Cloud Run pings it every 10s)
    if request.url.path == "/health":
        return await call_next(request)
    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception on {request.method} {request.url.path}")
    message = str(exc) if settings.env == "development" else "An error occurred"
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": message},
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "drufiy-backend", "version": "0.1.0", "env": settings.env}


# Cache Kimi check result for 60s — don't hammer the API on every health probe
_kimi_health_cache: dict = {"ok": None, "checked_at": 0.0}

@app.get("/health/deep")
async def health_deep():
    supabase_ok = await db.healthcheck()

    # Check Kimi with 60s cache
    now = time.time()
    if now - _kimi_health_cache["checked_at"] > 60:
        try:
            from app.agent.kimi_client import kimi
            await kimi.chat.completions.create(
                model=settings.kimi_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            _kimi_health_cache["ok"] = True
        except Exception as e:
            logger.warning(f"Kimi health check failed: {e}")
            _kimi_health_cache["ok"] = False
        _kimi_health_cache["checked_at"] = now

    kimi_ok = _kimi_health_cache["ok"]
    overall = "ok" if (supabase_ok and kimi_ok) else "degraded"

    return {
        "status": overall,
        "supabase": "connected" if supabase_ok else "disconnected",
        "kimi": "connected" if kimi_ok else "disconnected",
    }


# ── Route registration ──────────────────────────────────────────────────────

try:
    from app.routes.github_oauth import router as oauth_router
    app.include_router(oauth_router, prefix="/auth", tags=["auth"])
except ImportError:
    logger.warning("app.routes.github_oauth not found — skipping")

try:
    from app.routes.repos import router as repos_router
    app.include_router(repos_router, prefix="/repos", tags=["repos"])
except ImportError:
    logger.warning("app.routes.repos not found — skipping")

try:
    from app.routes.runs import router as runs_router
    app.include_router(runs_router, prefix="/runs", tags=["runs"])
except ImportError:
    logger.warning("app.routes.runs not found — skipping")

try:
    from app.webhook import router as webhook_router
    app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])
except ImportError:
    logger.warning("app.webhook not found — skipping")

try:
    from app.routes.internal import router as internal_router
    app.include_router(internal_router, prefix="/internal", tags=["internal"])
except ImportError:
    logger.warning("app.routes.internal not found — skipping")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
