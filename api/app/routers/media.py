"""Private blob media proxy for browser playback and thumbnails."""
from __future__ import annotations

import mimetypes
import re
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.storage import blob_client_from_url, served_containers

router = APIRouter()

# Browsers may only be served videos and images. Whatever a blob claims to be, anything else is sent as an opaque
# download, and every response forbids content sniffing and scripting, so an uploaded file can never run on this origin.
_INLINE_TYPES = ("video/", "image/")
_SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _parse_range(value: str | None, size: int) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = _RANGE_RE.match(value.strip())
    if not match:
        raise HTTPException(416, "Invalid Range header")
    start_s, end_s = match.groups()
    if not start_s and not end_s:
        raise HTTPException(416, "Invalid Range header")
    if start_s:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    else:
        suffix = int(end_s)
        start = max(0, size - suffix)
        end = size - 1
    if start >= size or end < start:
        raise HTTPException(416, "Requested range not satisfiable")
    end = min(end, size - 1)
    return start, end, end - start + 1


@router.get("/blob")
def stream_blob(request: Request, url: Annotated[str, Query(max_length=2048)], range_header: str | None = Header(default=None, alias="Range")):
    try:
        blob = blob_client_from_url(url, served_containers())
        props = blob.get_blob_properties()
    except ValueError as exc:
        raise HTTPException(400, "Not a playable blob of this storage account.") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, "Blob not found.") from exc

    size = int(props.size or 0)
    content_type = (props.content_settings.content_type if props.content_settings else None) or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=300",
        **_SAFE_HEADERS,
    }
    if not content_type.lower().startswith(_INLINE_TYPES):
        # Blobs copied in with other tools often carry no media type: fall back to the file extension.
        guessed = mimetypes.guess_type(blob.blob_name)[0] or ""
        if guessed.startswith(_INLINE_TYPES):
            content_type = guessed
        else:
            content_type = "application/octet-stream"
            headers["Content-Disposition"] = "attachment"
    requested = _parse_range(range_header, size)
    if requested:
        start, end, length = requested
        downloader = blob.download_blob(offset=start, length=length)
        headers.update({
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        })
        return StreamingResponse(downloader.chunks(), status_code=206, media_type=content_type, headers=headers)

    downloader = blob.download_blob()
    headers["Content-Length"] = str(size)
    return StreamingResponse(downloader.chunks(), media_type=content_type, headers=headers)
