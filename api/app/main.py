"""MomentLens FastAPI app entrypoint."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import media, option1, search, upload, videos
from app.telemetry import configure as configure_telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("momentlens")

# Initialise App Insights / OTel as early as possible so the rest of the app
# (and uvicorn loggers) is auto-instrumented. No-op when the connection string
# env var isn't set.
configure_telemetry()

app = FastAPI(title="MomentLens API", version="0.1.0")

# CORS — App Service serves UI from same origin, but allow localhost dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "region": settings.azure_location,
        "indexes": {
            "option1": settings.search_index_option1,
        },
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
app.include_router(option1.router, prefix="/api/option1", tags=["option1-vi"])
app.include_router(search.router,  prefix="/api/search",  tags=["search"])
app.include_router(videos.router,  prefix="/api/videos",  tags=["library"])


# Serve built React UI from /app/static (created at Docker build time)
STATIC_DIR = Path(__file__).parent / "static"
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}
if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        # API and docs paths are handled before this; everything else -> SPA index.html
        if full_path.startswith(("api/", "docs", "openapi.json")):
            return {"detail": "not found"}
        # Serve any real file that ships under static/ (logo, favicon, robots.txt, etc.)
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        # index.html must never be cached — it references hashed asset filenames
        # that change on every deploy. Without no-store, browsers serve a stale
        # index.html that points at a JS bundle that no longer exists.
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE_HEADERS)
else:
    log.warning("Static UI directory %s not found — running API-only.", STATIC_DIR)
