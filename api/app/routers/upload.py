"""Upload router — receives a video file and stores it in Blob."""
from __future__ import annotations

import logging
import re
import uuid
from typing import Annotated
from urllib.parse import parse_qs, urlparse

from azure.core.exceptions import AzureError, HttpResponseError, ResourceExistsError, ResourceNotFoundError
from fastapi import APIRouter, HTTPException, Path, Query, Request

from app.azure_clients import blob_service
from app.config import settings
from app.models import (
    StorageReferenceRequest,
    StoredVideo,
    StoredVideoList,
    UploadCommitRequest,
    UploadResponse,
    UploadSessionRequest,
    UploadSessionResponse,
)
from app.storage import blob_client_from_url, commit_upload_blocks, stage_upload_block
from app.validation import VideoId

log = logging.getLogger(__name__)
router = APIRouter()


# Containers accepted at upload time (Video Indexer ingests all of them directly).
_ALLOWED_EXT = {
    ".mp4", ".mov", ".mkv", ".webm", ".m4v",
    ".mpeg", ".mpg", ".ts", ".m2ts", ".mts",
}
_PROXY_BLOCK_SIZE = 4 * 1024 * 1024
_MAX_BLOCKS = 50000  # Azure Blob limit for a block blob
_VIDEO_CONTENT_TYPE = re.compile(r"video/[A-Za-z0-9.+-]{1,64}")


def _safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_")
    return base or "video"


def _check_ext(filename: str) -> str:
    if not filename:
        raise HTTPException(400, "No filename provided.")
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED_EXT:
        raise HTTPException(415, f"Unsupported extension {ext}. Allowed: {sorted(_ALLOWED_EXT)}")
    return ext


def _check_blob_name(video_id: str, blob_name: str) -> None:
    if not blob_name.startswith(f"{video_id}/") or "/" in blob_name[len(video_id) + 1:] or ".." in blob_name or "\\" in blob_name:
        raise HTTPException(400, "Invalid upload session.")
    _check_ext(blob_name.rsplit("/", 1)[-1])


@router.get("/stored", response_model=StoredVideoList)
def stored_videos(
    continuation_token: str | None = Query(default=None, max_length=8192),
) -> StoredVideoList:
    if not settings.azure_storage_account:
        raise HTTPException(503, "Video storage is not configured.")
    try:
        container = blob_service().get_container_client(settings.azure_storage_videos_container)
        pages = container.list_blobs(results_per_page=100, timeout=30).by_page(
            continuation_token=continuation_token,
        )
        items = []
        for blob in next(pages, []):
            extension = "." + blob.name.rsplit(".", 1)[-1].lower()
            if extension not in _ALLOWED_EXT or not (0 < blob.size <= settings.max_upload_mb * 1024 * 1024):
                continue
            items.append(StoredVideo(
                blob_name=blob.name,
                blob_url=container.get_blob_client(blob.name).url,
                size_bytes=blob.size,
            ))
        return StoredVideoList(
            account=settings.azure_storage_account,
            container=settings.azure_storage_videos_container,
            items=items,
            continuation_token=pages.continuation_token,
        )
    except HttpResponseError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(403, "The app cannot list stored videos.") from exc
        raise HTTPException(503, "Video storage is temporarily unavailable.") from exc
    except AzureError as exc:
        raise HTTPException(503, "Video storage is temporarily unavailable.") from exc


@router.post("/reference", response_model=UploadResponse)
def reference_video(req: StorageReferenceRequest) -> UploadResponse:
    if not settings.azure_storage_account:
        raise HTTPException(503, "Video storage is not configured.")
    try:
        parsed = urlparse(req.blob_url.strip())
        if parsed.fragment or parsed.params or any(
            key.lower() in {"snapshot", "versionid"} for key in parse_qs(parsed.query)
        ):
            raise ValueError("Only current blob URLs are supported.")
        blob = blob_client_from_url(parsed._replace(query="", fragment="").geturl())
    except ValueError as exc:
        raise HTTPException(400, "Use an HTTPS blob URL in the configured video storage account.") from exc
    if blob.container_name != settings.azure_storage_videos_container:
        raise HTTPException(400, "Use a file in the configured video container.")
    if "\\" in blob.blob_name or "\x00" in blob.blob_name or any(
        part in {".", ".."} for part in blob.blob_name.split("/")
    ):
        raise HTTPException(400, "Invalid blob path.")
    filename = blob.blob_name.rsplit("/", 1)[-1]
    _check_ext(filename)
    try:
        properties = blob.get_blob_properties(timeout=30)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, "Stored video was not found.") from exc
    except HttpResponseError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(403, "The app cannot read this stored video.") from exc
        raise HTTPException(503, "Video storage is temporarily unavailable.") from exc
    except AzureError as exc:
        raise HTTPException(503, "Video storage is temporarily unavailable.") from exc
    if properties.size <= 0:
        raise HTTPException(400, "The stored video is empty.")
    if properties.size > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB limit.")
    return UploadResponse(
        video_id=f"ref-{uuid.uuid4().hex[:12]}",
        blob_url=blob.url,
        size_bytes=properties.size,
        filename=filename,
    )


@router.post("/session", response_model=UploadSessionResponse)
async def upload_session(req: UploadSessionRequest) -> UploadSessionResponse:
    """Start an API-proxied block upload.

    This is the private-storage path: the browser sends small blocks to the
    API, and the API writes them to Blob using managed identity.
    """
    _check_ext(req.filename)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if req.size_bytes is not None and req.size_bytes > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB limit.")

    video_id = uuid.uuid4().hex[:12]
    blob_name = f"{video_id}/{_safe_name(req.filename)}"
    blob_url = (
        f"https://{settings.azure_storage_account}.blob.core.windows.net/"
        f"{settings.azure_storage_videos_container}/{blob_name}"
    )
    return UploadSessionResponse(
        video_id=video_id,
        blob_name=blob_name,
        blob_url=blob_url,
        block_size=_PROXY_BLOCK_SIZE,
    )


@router.put("/session/{video_id}/blocks/{block_index}")
async def upload_session_block(
    video_id: VideoId,
    block_index: Annotated[int, Path(ge=0, lt=_MAX_BLOCKS)],
    blob_name: Annotated[str, Query(max_length=1024)],
    request: Request,
) -> dict:
    _check_blob_name(video_id, blob_name)
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty upload block.")
    if len(data) > _PROXY_BLOCK_SIZE:
        raise HTTPException(413, f"Block exceeds {_PROXY_BLOCK_SIZE} bytes.")
    stage_upload_block(settings.azure_storage_videos_container, blob_name, block_index, data)
    return {"ok": True, "block_index": block_index}


@router.post("/session/{video_id}/commit", response_model=UploadResponse)
async def upload_session_commit(video_id: VideoId, req: UploadCommitRequest) -> UploadResponse:
    _check_blob_name(video_id, req.blob_name)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if req.size_bytes > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB limit.")
    content_type = req.content_type if _VIDEO_CONTENT_TYPE.fullmatch(req.content_type or "") else "video/mp4"
    try:
        blob_url = commit_upload_blocks(
            settings.azure_storage_videos_container, req.blob_name, req.total_blocks, content_type=content_type,
        )
    except ResourceExistsError as exc:
        raise HTTPException(409, "This upload was already completed.") from exc
    return UploadResponse(video_id=video_id, blob_url=blob_url, size_bytes=req.size_bytes, filename=req.filename)
