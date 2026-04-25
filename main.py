from contextlib import asynccontextmanager
import logging
import json

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app import db


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Drufiy backend starting")
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


@app.get("/health/deep")
async def health_deep():
    healthy = await db.healthcheck()
    return {
        "status": "ok" if healthy else "degraded",
        "supabase": "connected" if healthy else "disconnected",
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
