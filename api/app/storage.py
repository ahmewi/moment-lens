"""Storage helpers for upload + SAS URL generation."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

from azure.storage.blob import (
    BlobSasPermissions,
    ContentSettings,
    UserDelegationKey,
    generate_blob_sas,
)

from app.azure_clients import blob_service
from app.config import settings

log = logging.getLogger(__name__)

_delegation_key_cache: tuple[UserDelegationKey, datetime] | None = None

# Mint user-delegation keys with a generous lifetime so they always outlive
# any SAS we issue against them. Azure storage REFUSES a SAS whose `se` is
# beyond the key's expiry, so the SAS's effective lifetime is capped by the
# key's. Maximum allowed by storage is 7 days.
_KEY_LIFETIME_HOURS = 24


def _get_user_delegation_key(min_remaining_hours: float = 0.0) -> tuple[UserDelegationKey, datetime]:
    """Get a (cached) user delegation key for SAS generation via AAD.

    Returns (key, key_expiry_utc). The cached key is reused only if it has
    enough remaining lifetime to fully cover a SAS of `min_remaining_hours`
    duration (plus a 10-minute safety margin); otherwise a fresh key is
    minted.
    """
    global _delegation_key_cache
    now = datetime.now(timezone.utc)
    required_margin = timedelta(hours=max(min_remaining_hours, 0.0)) + timedelta(minutes=10)
    if _delegation_key_cache:
        key, expiry = _delegation_key_cache
        if expiry - now > required_margin:
            return key, expiry
    start = now - timedelta(minutes=5)
    expiry = now + timedelta(hours=_KEY_LIFETIME_HOURS)
    key = blob_service().get_user_delegation_key(key_start_time=start, key_expiry_time=expiry)
    _delegation_key_cache = (key, expiry)
    return key, expiry


def upload_blob(container: str, blob_name: str, data: bytes, content_type: str = "video/mp4") -> str:
    """Upload bytes to a blob and return its full URL."""
    bs = blob_service()
    container_client = bs.get_container_client(container)
    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(
        data,
        overwrite=True,
        content_type=content_type,
    )
    return blob_client.url


def upload_stream(container: str, blob_name: str, stream, length: int, content_type: str = "video/mp4") -> str:
    bs = blob_service()
    blob_client = bs.get_container_client(container).get_blob_client(blob_name)
    blob_client.upload_blob(stream, overwrite=True, length=length, content_type=content_type)
    return blob_client.url


def _split_blob_url(blob_url: str) -> tuple[str, str] | None:
    parsed = urlparse(blob_url)
    expected_host = f"{settings.azure_storage_account}.blob.core.windows.net"
    if parsed.scheme != "https" or parsed.netloc.lower() != expected_host.lower():
        return None
    parts = parsed.path.lstrip("/").split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], unquote(parts[1])


def blob_client_from_url(blob_url: str):
    split = _split_blob_url(blob_url)
    if not split:
        raise ValueError("Blob URL is not in this storage account.")
    container, blob_name = split
    return blob_service().get_container_client(container).get_blob_client(blob_name)


def proxy_blob_url(blob_url: str) -> str:
    """Return an app-relative URL that streams a private blob through the API."""
    if not blob_url:
        return ""
    return f"/api/media/blob?url={quote(blob_url, safe='')}"


def upload_block_id(index: int) -> str:
    raw = f"block-{index:010d}".encode("ascii")
    return b64encode(raw).decode("ascii")


def stage_upload_block(container: str, blob_name: str, block_index: int, data: bytes) -> None:
    blob = blob_service().get_container_client(container).get_blob_client(blob_name)
    blob.stage_block(block_id=upload_block_id(block_index), data=data, length=len(data))


def commit_upload_blocks(container: str, blob_name: str, total_blocks: int, content_type: str = "video/mp4") -> str:
    blob = blob_service().get_container_client(container).get_blob_client(blob_name)
    block_ids = [upload_block_id(i) for i in range(total_blocks)]
    blob.commit_block_list(
        block_ids,
        content_settings=ContentSettings(content_type=content_type or "application/octet-stream"),
    )
    return blob.url


def upload_text(container: str, blob_name: str, text: str, content_type: str = "application/json") -> str:
    return upload_blob(container, blob_name, text.encode("utf-8"), content_type=content_type)


def delete_blob_by_url(blob_url: str) -> bool:
    """Delete a blob given its full URL. Returns True on success, False if missing."""
    if not blob_url:
        return False
    parsed = urlparse(blob_url.split("?", 1)[0])
    parts = parsed.path.lstrip("/").split("/", 1)
    if len(parts) != 2:
        return False
    container, blob_name = parts
    try:
        blob_service().get_container_client(container).get_blob_client(blob_name).delete_blob(
            delete_snapshots="include"
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("delete_blob_by_url failed for %s: %s", blob_url, e)
        return False


def delete_blobs_with_prefix(container: str, prefix: str) -> int:
    """Delete all blobs in a container whose name begins with `prefix`. Returns count."""
    bs = blob_service()
    cc = bs.get_container_client(container)
    deleted = 0
    try:
        for b in cc.list_blobs(name_starts_with=prefix):
            try:
                cc.get_blob_client(b.name).delete_blob(delete_snapshots="include")
                deleted += 1
            except Exception as e:  # noqa: BLE001
                log.warning("delete %s/%s failed: %s", container, b.name, e)
    except Exception as e:  # noqa: BLE001
        log.warning("list_blobs(%s, prefix=%s) failed: %s", container, prefix, e)
    return deleted


def make_sas_url(blob_url: str, hours: int = 2) -> str:
    """Generate a read-only SAS URL using user delegation (AAD-based, no account key)."""
    split = _split_blob_url(blob_url)
    if not split:
        return blob_url
    container, blob_name = split
    canonical_url = blob_client_from_url(blob_url).url
    key, key_expiry = _get_user_delegation_key(min_remaining_hours=hours)
    # Storage caps the effective SAS lifetime by the user-delegation key's
    # expiry. Clamp `se` to <= key_expiry so VI (and anything else that
    # holds the URL for a while) never sees a request rejected with 403
    # AuthenticationFailed after the key dies.
    requested_expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
    sas_expiry = min(requested_expiry, key_expiry)
    sas = generate_blob_sas(
        account_name=settings.azure_storage_account,
        container_name=container,
        blob_name=blob_name,
        user_delegation_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=sas_expiry,
    )
    return f"{canonical_url}?{sas}"


def make_upload_sas_url(container: str, blob_name: str, minutes: int = 60) -> tuple[str, str]:
    """Generate a write-only SAS URL for direct browser-to-blob uploads.

    Returns (blob_url, upload_url_with_sas). Permissions: create + write.
    """
    bs = blob_service()
    blob_client = bs.get_container_client(container).get_blob_client(blob_name)
    blob_url = blob_client.url
    hours_needed = max(minutes / 60.0, 0.0)
    key, key_expiry = _get_user_delegation_key(min_remaining_hours=hours_needed)
    requested_expiry = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    sas_expiry = min(requested_expiry, key_expiry)
    sas = generate_blob_sas(
        account_name=settings.azure_storage_account,
        container_name=container,
        blob_name=blob_name,
        user_delegation_key=key,
        permission=BlobSasPermissions(create=True, write=True),
        expiry=sas_expiry,
    )
    return blob_url, f"{blob_url}?{sas}"


def _redact_sas(value) -> str:
    """Strip SAS tokens from text bound for logs: ffmpeg's stderr tail and subprocess exception
    messages (e.g. TimeoutExpired) both embed the signed URL, whose `sig=` is a live credential."""
    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
    text = re.sub(r"(https?://[^\s'\"?]+)\?[^\s'\"]*", r"\1?<redacted>", text)
    return re.sub(r"(?i)\bsig=[^&\s'\"]+", "sig=<redacted>", text)


def extract_frame_bytes(blob_sas_url: str, at_seconds: float, max_width: int = 480, timeout: int = 12) -> bytes | None:
    """Grab one ephemeral JPEG frame from a video URL via ffmpeg.

    Unlike `extract_thumbnail`, nothing is uploaded to storage — the bytes are
    used once (e.g. attached to an LLM vision call) and discarded. Returns
    None on any failure (missing ffmpeg, bad seek, network error, timeout);
    callers must treat None as "no frame available", never as a hard error.
    """
    if not shutil.which("ffmpeg"):
        return None
    tmp = Path(tempfile.gettempdir()) / f"frame-{uuid4().hex}.jpg"
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{max(0.0, float(at_seconds)):.2f}",
            "-i", blob_sas_url,
            "-frames:v", "1",
            "-vf", f"scale={max_width}:-2",
            "-q:v", "4",
            str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            log.warning("ffmpeg frame grab failed (t=%.1f): %s", at_seconds, _redact_sas(proc.stderr[-300:]))
            return None
        return tmp.read_bytes()
    except Exception as e:  # noqa: BLE001
        log.warning("extract_frame_bytes error (t=%.1f): %s", at_seconds, _redact_sas(e))
        return None
    finally:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass


def extract_window_frames(
    blob_sas_url: str, start: float, end: float, n: int = 4, max_width: int = 640, timeout: int = 40,
    niceness: int = 0, *, keyframes_only: bool = True,
) -> list[bytes]:
    """Sample up to `n` frames spread across [start, end] with ONE ffmpeg process.

    Keyframes are the default; keyframes_only=False decodes dense temporal samples for targeted review.

    Cost is per PROCESS (TLS, container-index parse), not per frame: measured on the 3GB 1080p50
    broadcast source on a 1-vCPU host, 6 windows x 4 frames took 1.6s this way versus ~14s for 24
    one-frame processes (which also starve the web worker). Keyframes are cheap to decode and
    encoders place them on shot changes, so they also tend to be distinct views. Frames come back
    in chronological order; fewer than `n` (or none) is possible when the window has few keyframes,
    and callers fall back to `extract_frame_bytes` for those. Never raises.
    """
    duration = end - start
    if n < 1 or duration <= 0 or not shutil.which("ffmpeg"):
        return []
    step = duration / (n + 1)
    out_dir = Path(tempfile.mkdtemp(prefix="wf-"))
    try:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-skip_frame", "nokey",
            "-ss", f"{max(0.0, start + step / 2):.2f}", "-t", f"{duration - step / 2:.2f}",
            "-i", blob_sas_url,
            "-vf", f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{step:.2f})',scale={max_width}:-2",
            "-fps_mode", "vfr", "-frames:v", str(n), "-q:v", "4",
            str(out_dir / "f_%02d.jpg"),
        ]
        if not keyframes_only:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{max(0.0, start):.2f}", "-t", f"{duration:.2f}", "-i", blob_sas_url,
                "-vf", f"fps={n / duration:.6f},scale={max_width}:-2",
                "-frames:v", str(n), "-q:v", "4", str(out_dir / "f_%02d.jpg"),
            ]
        if niceness and shutil.which("nice"):
            cmd = ["nice", "-n", str(niceness), *cmd]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        frames = [p.read_bytes() for p in sorted(out_dir.glob("f_*.jpg")) if p.stat().st_size > 0]
        if proc.returncode != 0 and not frames:
            log.warning("ffmpeg window frames failed (%.0f-%.0fs): %s", start, end, _redact_sas(proc.stderr[-300:]))
        return frames
    except Exception as e:  # noqa: BLE001
        log.warning("extract_window_frames error (%.0f-%.0fs): %s", start, end, _redact_sas(e))
        return []
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def extract_thumbnail(blob_sas_url: str, video_id: str, at_seconds: float = 3.0) -> str | None:
    """Use ffmpeg to grab a frame from the video, upload it to the thumbnails
    container, and return its blob URL (no SAS). Returns None on failure.

    `blob_sas_url` must be readable directly by ffmpeg (use a SAS URL).
    """
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found on PATH; skipping thumbnail extraction.")
        return None
    tmp = Path(tempfile.gettempdir()) / f"thumb-{video_id}.jpg"
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{max(0.0, float(at_seconds)):.2f}",
            "-i", blob_sas_url,
            "-frames:v", "1",
            "-vf", "scale=480:-2",
            "-q:v", "4",
            str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            log.warning("ffmpeg thumbnail failed for %s: %s", video_id, _redact_sas(proc.stderr[-300:]))
            return None
        with tmp.open("rb") as f:
            data = f.read()
        url = upload_blob(
            container=settings.azure_storage_thumbnails_container,
            blob_name=f"{video_id}.jpg",
            data=data,
            content_type="image/jpeg",
        )
        return url
    except Exception as e:  # noqa: BLE001
        log.warning("Thumbnail extraction error for %s: %s", video_id, _redact_sas(e))
        return None
    finally:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
