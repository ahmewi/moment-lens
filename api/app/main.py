"""MomentLens FastAPI app entrypoint."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.pipelines.indexer import ensure_indexes
from app.routers import indexing, media, search, upload, videos
from app.telemetry import configure as configure_telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("momentlens")

# Initialise App Insights / OTel as early as possible so the rest of the app
# (and uvicorn loggers) is auto-instrumented. No-op when the connection string
# env var isn't set.
configure_telemetry()



def _ensure_indexes_quietly() -> None:
    try:
        ensure_indexes()
    except Exception as exc:  # noqa: BLE001
        # Search or its role assignment may not be ready yet right after a deployment; the first indexing
        # request creates the index again.
        log.warning("Could not create the search index at startup: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # In the background, so that the web app answers health checks while Azure AI Search is reached.
    threading.Thread(target=_ensure_indexes_quietly, daemon=True, name="ensure-indexes").start()
    yield


app = FastAPI(title="MomentLens API", version="0.1.0", lifespan=lifespan)

# CORS: the UI is served from the same origin; only the local dev servers need an allowance.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "region": settings.azure_location,
        "index": settings.search_index,
    }


@app.get("/api/config")
def config() -> dict:
    """Public config (no secrets) the UI needs."""
    return {
        "maxUploadMB": settings.max_upload_mb,
        "maxUploadMinutes": settings.max_upload_minutes,
        "videoIndexer": {
            "accountName": settings.vi_account_name,
            "location": settings.vi_location,
        },
    }


app.include_router(media.router,   prefix="/api/media",   tags=["media"])
app.include_router(upload.router,  prefix="/api/upload",  tags=["upload"])
app.include_router(indexing.router, prefix="/api/indexing", tags=["indexing"])
app.include_router(search.router,  prefix="/api/search",  tags=["search"])
app.include_router(videos.router,  prefix="/api/videos",  tags=["library"])


NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def mount_ui(application: FastAPI, static_dir: Path) -> bool:
    """Serve the built React UI (created at Docker build time) from `static_dir`.

    Returns False when there is no build, so the API can run on its own.
    """
    if not (static_dir / "index.html").exists():
        return False
    root = static_dir.resolve()
    application.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @application.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        # API and docs paths are handled before this; everything else -> SPA index.html
        if full_path.startswith(("api/", "docs", "openapi.json")):
            return {"detail": "not found"}
        # Serve any real file that ships under static/ (logo, favicon, robots.txt, etc.), but never
        # anything outside it: resolve first so "..", encoded separators and symlinks cannot escape.
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        # index.html must never be cached — it references hashed asset filenames
        # that change on every deploy. Without no-store, browsers serve a stale
        # index.html that points at a JS bundle that no longer exists.
        return FileResponse(root / "index.html", headers=NO_CACHE_HEADERS)

    return True


STATIC_DIR = Path(__file__).parent / "static"
if not mount_ui(app, STATIC_DIR):
    log.warning("Static UI directory %s not found — running API-only.", STATIC_DIR)
