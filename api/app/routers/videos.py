"""Videos library router \u2014 list previously indexed videos for browsing in the UI."""
from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import APIRouter, HTTPException

from app import jobs
from app.azure_clients import search_client
from app.config import settings
from app.models import PipelineOption
from app.storage import delete_blob_by_url, delete_blobs_with_prefix, proxy_blob_url

log = logging.getLogger(__name__)
router = APIRouter()


def _index_for(option: PipelineOption) -> str:
    if option != "option1":
        raise HTTPException(400, "Only Video Indexer is supported")
    return settings.search_index_option1


@router.get("")
def list_videos(option: PipelineOption | None = None) -> dict:
    """Return distinct (video_id, filename, scene_count, blob_url) per indexed video.

    If `option` is omitted, the Video Indexer index is queried.
    """
    options: list[PipelineOption] = [option] if option else ["option1"]
    videos: dict[str, dict] = {}
    for opt in options:
        index = _index_for(opt)
        try:
            client = search_client(index)
            # Cheap-ish: list up to 1000 docs, project minimal fields, group client-side.
            results = client.search(
                search_text="*",
                top=1000,
                select=["video_id", "filename", "blob_url", "thumbnail_url", "start_seconds", "end_seconds"],
            )
            counts: dict[str, dict] = defaultdict(
                lambda: {"scenes": 0, "options": set(), "filename": "", "blob_url": "", "thumbnail_url": "", "duration": 0.0}
            )
            for r in results:
                vid = r.get("video_id")
                if not vid:
                    continue
                rec = counts[vid]
                rec["scenes"] += 1
                rec["options"].add(opt)
                rec["filename"] = rec["filename"] or (r.get("filename") or "")
                rec["blob_url"] = rec["blob_url"] or (r.get("blob_url") or "")
                rec["thumbnail_url"] = rec["thumbnail_url"] or (r.get("thumbnail_url") or "")
                rec["duration"] = max(rec["duration"], float(r.get("end_seconds") or 0))
            for vid, rec in counts.items():
                if vid in videos:
                    videos[vid]["scenes"][opt] = rec["scenes"]
                    videos[vid]["options"].append(opt)
                    if not videos[vid].get("thumbnail_url") and rec["thumbnail_url"]:
                        videos[vid]["thumbnail_url"] = rec["thumbnail_url"]
                else:
                    videos[vid] = {
                        "video_id": vid,
                        "filename": rec["filename"],
                        "blob_url": rec["blob_url"],
                        "thumbnail_url": rec["thumbnail_url"],
                        "duration_seconds": rec["duration"],
                        "options": [opt],
                        "scenes": {opt: rec["scenes"]},
                    }
        except Exception as e:  # noqa: BLE001
            log.warning("Listing videos for %s failed: %s", opt, e)

    items = []
    for v in sorted(videos.values(), key=lambda x: x["filename"].lower()):
        play_url = proxy_blob_url(v["blob_url"]) if v["blob_url"] else ""
        thumb = proxy_blob_url(v["thumbnail_url"]) if v.get("thumbnail_url") else ""
        items.append({
            "video_id": v["video_id"],
            "filename": v["filename"],
            "duration_seconds": v["duration_seconds"],
            "options": sorted(set(v["options"])),
            "scenes": v["scenes"],
            "play_url": play_url,
            "thumbnail_url": thumb,
        })
    return {"count": len(items), "items": items}


@router.get("/jobs")
def list_jobs() -> dict:
    """Return current in-memory pipeline jobs (for status panel)."""
    return {"items": [j.model_dump() for j in jobs.list_jobs()]}


@router.get("/{video_id}/scenes")
def list_scenes(video_id: str, option: PipelineOption = "option1") -> dict:
    """Return all SceneCard documents for a video, sorted by start_seconds.

    Each row carries SAS-signed `play_url` + `thumbnail_url` ready for the UI.
    """
    if not video_id:
        raise HTTPException(400, "video_id is required")
    index = _index_for(option)
    try:
        client = search_client(index)
        results = list(client.search(
            search_text="*",
            filter=f"video_id eq '{video_id}'",
            top=1000,
            select=[
                "id", "video_id", "filename", "start_seconds", "end_seconds",
                "summary", "transcript_excerpt", "labels", "entities", "speakers",
                "thumbnail_url", "blob_url",
            ],
        ))
    except Exception as e:  # noqa: BLE001
        log.warning("list_scenes(%s, %s) failed: %s", video_id, option, e)
        raise HTTPException(500, f"Failed to load scenes: {e}")

    blob_url = next((r.get("blob_url") for r in results if r.get("blob_url")), "")
    play_url = proxy_blob_url(blob_url) if blob_url else ""

    items = []
    for r in sorted(results, key=lambda x: float(x.get("start_seconds") or 0)):
        thumb = r.get("thumbnail_url") or ""
        items.append({
            "id": r.get("id"),
            "video_id": r.get("video_id"),
            "filename": r.get("filename") or "",
            "start_seconds": float(r.get("start_seconds") or 0),
            "end_seconds": float(r.get("end_seconds") or 0),
            "summary": r.get("summary") or "",
            "transcript_excerpt": r.get("transcript_excerpt") or "",
            "labels": r.get("labels") or [],
            "entities": r.get("entities") or [],
            "speakers": r.get("speakers") or [],
            "thumbnail_url": proxy_blob_url(thumb) if thumb else "",
            "play_url": play_url,
        })
    return {"video_id": video_id, "option": option, "count": len(items), "items": items}


def _preserve_source_blob(video_id: str, blob_url: str) -> bool:
    if video_id.startswith("ref-"):
        return True
    escaped_id = video_id.replace("'", "''")
    try:
        others = search_client(settings.search_index_option1).search(
            search_text="*",
            filter=f"video_id ne '{escaped_id}'",
            select=["blob_url"],
        )
        if any(item.get("blob_url") == blob_url for item in others):
            return True
    except Exception:
        log.warning("Source reference check failed; preserving source for %s", video_id)
        return True
    return False


@router.delete("/{video_id}")
def delete_video(video_id: str, purge_storage: bool = True) -> dict:
    """Remove indexed video data while retaining referenced or shared source blobs."""
    if not video_id:
        raise HTTPException(400, "video_id is required")

    summary = {"video_id": video_id, "search_deleted": {}, "blobs_deleted": 0, "errors": []}
    blob_url_to_delete: str | None = None
    thumb_url_to_delete: str | None = None

    # 1. Look the video up in each index, collect doc ids + blob URLs, then delete docs.
    for opt in ("option1",):
        index = _index_for(opt)
        try:
            client = search_client(index)
            results = list(client.search(
                search_text="*",
                filter=f"video_id eq '{video_id}'",
                top=1000,
                select=["id", "blob_url", "thumbnail_url"],
            ))
            doc_ids = [r["id"] for r in results if r.get("id")]
            for r in results:
                if not blob_url_to_delete and r.get("blob_url"):
                    blob_url_to_delete = r["blob_url"]
                if not thumb_url_to_delete and r.get("thumbnail_url"):
                    thumb_url_to_delete = r["thumbnail_url"]
            if doc_ids:
                client.delete_documents(documents=[{"id": i} for i in doc_ids])
            summary["search_deleted"][opt] = len(doc_ids)
        except Exception as e:  # noqa: BLE001
            log.warning("Delete from %s failed: %s", opt, e)
            summary["errors"].append(f"{opt}: {e}")

    # 2. Storage cleanup
    if purge_storage:
        if (blob_url_to_delete
                and not _preserve_source_blob(video_id, blob_url_to_delete)
                and delete_blob_by_url(blob_url_to_delete)):
            summary["blobs_deleted"] += 1
        if thumb_url_to_delete and delete_blob_by_url(thumb_url_to_delete):
            summary["blobs_deleted"] += 1
        # Insights JSON live under "<video_id>/" prefix
        try:
            n = delete_blobs_with_prefix(settings.azure_storage_insights_container, f"{video_id}/")
            summary["blobs_deleted"] += n
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"insights: {e}")

    # 3. In-memory job state
    try:
        jobs.clear_video(video_id)
    except Exception:
        pass

    return summary
