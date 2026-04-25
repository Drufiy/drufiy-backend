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
    Any run stuck in 'diagnosing' or 'applying' for > 5 minutes was almost
    certainly abandoned when the server died mid-pipeline.  Reset them to
    'pending' so the next webhook event (or a manual retry) can re-queue them.
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
            ids = [r["id"] for r in stuck.data]
            supabase.table("ci_runs").update({
                "status": "pending",
                "error_message": "Auto-recovered after server restart (was stuck in-progress)",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).in_("id", ids).execute()
            logger.info(f"Recovered {len(ids)} stuck run(s): {ids}")
        else:
            logger.info("No stuck runs found — clean startup")
    except Exception as e:
        logger.warning(f"Stuck-run recovery failed (non-fatal): {e}")


# ── Fix 3: pre-warm Kimi so the first real diagnosis isn't slow ───────────────

async def _prewarm_kimi():
    """
    Send a trivial 1-token completion to Kimi at startup.
    This establishes the HTTP connection pool and warms any provider-side
    caching, so the first real diagnosis doesn't pay a cold-start penalty.
    """
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Drufiy backend starting")

    # Fix 2: sync recovery (fast DB query, fine on startup path)
    _recover_stuck_runs()

    # Fix 3: async pre-warm — fire and forget, don't block startup
    asyncio.create_task(_prewarm_kimi())

    yield
    logger.info("Drufiy backend shutting down")


app = FastAPI(title="Drufiy Backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
