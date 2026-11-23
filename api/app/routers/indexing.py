"""Indexing router — Video Indexer ingestion, moment tags and playback metadata."""
from __future__ import annotations

import threading

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app import jobs
from app.azure_clients import search_client
from app.config import settings
from app.models import ContentType, ProcessJob, ProcessRequest, VideoIndexerLanguage
from app.pipelines.event_enrichment import PROFILES, profile_for, read_video_json
from app.pipelines.indexer import ensure_indexes
from app.pipelines.video_indexer import get_source_languages, get_vi_access_token, process_video
from app.pipelines.visual_enrich import enrich_video_events

router = APIRouter()
_enrichment_lock = threading.Lock()


class EventEnrichmentRequest(BaseModel):
    content_type: ContentType = "soccer_match"
    scene_ids: list[str] | None = Field(default=None, min_length=1, max_length=10)


def _video_metadata(video_id: str) -> tuple[dict | None, dict]:
    try:
        manifest = read_video_json(video_id, "events.json")
        raw = read_video_json(video_id, "insights.json") or {}
    except ValueError as exc:
        raise HTTPException(400, "Invalid video metadata request") from exc
    except Exception as exc:
        raise HTTPException(503, "Video metadata is temporarily unavailable") from exc
    if not manifest and not raw:
        raise HTTPException(404, "No indexed VI metadata for this video")
    return manifest, raw


@router.get("/profiles")
def profiles() -> dict:
    return {"items": [{"content_type": key, **value} for key, value in PROFILES.items()]}


def _profile_plan(content_type: ContentType) -> dict:
    if not profile_for(content_type):
        raise HTTPException(422, "No event extraction profile is configured for this content type")
    try:
        documents = search_client(settings.search_index).search(
            search_text="*", filter=f"content_type eq '{content_type}'", select=["video_id"],
        )
        video_ids = sorted({document["video_id"] for document in documents if document.get("video_id")})
        queued, skipped, active = [], [], []
        for video_id in video_ids:
            job = jobs.get_job(video_id)
            if job and job.status in ("submitted", "processing"):
                active.append(video_id)
                continue
            manifest = read_video_json(video_id, "events.json")
            if manifest and manifest.get("content_type") == content_type and (
                manifest.get("status") == "completed" or (
                    manifest.get("analysis_mode") == "query_targeted"
                    and manifest.get("analysis_version") in (1, 2)
                    and manifest.get("analysis_complete") is True
                )
            ):
                skipped.append(video_id)
            else:
                queued.append(video_id)
    except Exception as exc:
        raise HTTPException(503, "Profile analysis inventory is temporarily unavailable") from exc
    return {"content_type": content_type, "queued_video_ids": queued, "skipped_video_ids": skipped,
            "active_video_ids": active, "scene_budget": settings.event_analysis_max_scenes}


@router.get("/profiles/{content_type}/analysis")
def profile_analysis_plan(content_type: ContentType) -> dict:
    return _profile_plan(content_type)


def _enrich_profile(video_ids: list[str], content_type: ContentType, on_progress, on_status, on_stage) -> dict:
    failed, partial = [], []
    for index, video_id in enumerate(video_ids):
        on_status(stage=f"Analyzing video {index + 1}/{len(video_ids)}")
        jobs.init_stages(video_id, {"upload": "skipped"})
        jobs.update(video_id, status="processing", progress=10, error=None)
        try:
            result = enrich_video_events(
                video_id, content_type,
                on_progress=lambda progress: jobs.update(video_id, progress=progress),
                on_stage=lambda name, **fields: jobs.set_stage(video_id, name, **fields),
            )
            jobs.update(video_id, status="indexed", progress=100, insights_url=result.get("insights_url"))
            jobs.set_stage(video_id, "analyze", status="completed")
            if result.get("event_status") != "completed":
                partial.append(video_id)
        except Exception:
            failed.append(video_id)
            jobs.update(video_id, status="failed", progress=0, error="Event analysis failed; retry this video")
            jobs.set_stage(video_id, "analyze", status="failed", error="Event analysis failed")
        on_progress(int((index + 1) / len(video_ids) * 100))
    on_status(stage=f"Profile analysis finished: {len(video_ids) - len(failed)} analyzed, {len(partial)} partial, {len(failed)} failed")
    if failed:
        raise RuntimeError(f"Event analysis failed for {len(failed)} videos; successful results are preserved")
    return {"scene_count": len(video_ids)}


@router.post("/profiles/{content_type}/enrich")
def enrich_profile(content_type: ContentType) -> dict:
    with _enrichment_lock:
        group_id = f"profile-{content_type}"
        current = jobs.get_job(group_id)
        if current and current.status in ("submitted", "processing"):
            raise HTTPException(409, "This content profile is already being analyzed")
        plan = _profile_plan(content_type)
        video_ids = plan["queued_video_ids"]
        if not video_ids:
            return {**plan, "job": None}
        for video_id in video_ids:
            jobs.update(video_id, status="submitted", progress=0, error=None, stage="Queued for profile analysis")
        job = jobs.run_async(
            video_id=group_id, pass_progress_callback=True,
            target=lambda on_progress, on_status, on_stage: _enrich_profile(video_ids, content_type, on_progress, on_status, on_stage),
            initial_stages={"upload": "skipped", "analyze": "pending"},
        )
        return {**plan, "job": job.model_dump()}


@router.get("/events/{video_id}")
def events(video_id: str, response: Response) -> dict:
    manifest, raw = _video_metadata(video_id)
    video = (raw.get("videos") or [{}])[0]
    native = video.get("insights") or {}
    result = dict(manifest or {
        "video_id": video_id, "vi_video_id": video.get("id") or raw.get("id"),
        "duration_seconds": video.get("durationInSeconds") or raw.get("durationInSeconds") or 0,
        "content_type": None, "status": "needs_enrichment", "events": [], "categories": {},
        "reviewed_scenes": 0, "total_scenes": 0,
    })
    result.pop("candidates", None)
    result["raw_insights_data"] = [{"rawInsightsKey": key, "insightsItems": native[key]}
                                    for key in ("labels", "keywords", "audioEffects", "emotions", "sentiments")
                                    if isinstance(native.get(key), list) and native[key]]
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/widgets/{video_id}")
def widgets(video_id: str, response: Response) -> dict:
    manifest, raw = _video_metadata(video_id)
    video = (raw.get("videos") or [{}])[0]
    vi_id = str((manifest or {}).get("vi_video_id") or video.get("id") or raw.get("id") or "")
    if not vi_id:
        raise HTTPException(409, "VI player metadata is missing")
    try:
        token = get_vi_access_token("Reader", "Video", video_id=vi_id)
    except Exception as exc:
        raise HTTPException(503, "VI player access is temporarily unavailable") from exc
    response.headers["Cache-Control"] = "no-store"
    return {"account_id": settings.vi_account_id, "account_name": settings.vi_account_name,
            "location": settings.vi_location, "vi_video_id": vi_id,
            "access_token": token, "refresh_after_seconds": 240}


@router.post("/events/{video_id}/enrich", response_model=ProcessJob)
def enrich_events(video_id: str, req: EventEnrichmentRequest) -> ProcessJob:
    if not profile_for(req.content_type):
        raise HTTPException(422, "No event extraction profile is configured for this content type")
    _video_metadata(video_id)
    with _enrichment_lock:
        job = jobs.get_job(video_id)
        if job and job.status in ("submitted", "processing"):
            raise HTTPException(409, "This video is already processing")
        return jobs.run_async(video_id=video_id,
            target=lambda on_progress, on_status, on_stage: enrich_video_events(video_id, req.content_type, on_progress, on_stage, scene_ids=req.scene_ids),
            pass_progress_callback=True,
            initial_stages={"upload": "skipped", "analyze": "pending"})


@router.get("/languages", response_model=list[VideoIndexerLanguage])
def languages() -> list[VideoIndexerLanguage]:
    try:
        return get_source_languages()
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(503, "Video Indexer language list is temporarily unavailable.") from exc


@router.post("/process", response_model=ProcessJob)
def start(req: ProcessRequest) -> ProcessJob:
    requested_language = (req.source_language or "").strip()
    source_language = None
    if requested_language and requested_language.casefold() != "auto":
        supported = {language.code.casefold(): language.code for language in languages()}
        source_language = supported.get(requested_language.casefold())
        if source_language is None:
            raise HTTPException(422, "Unsupported Video Indexer source language.")
    ensure_indexes()
    return jobs.run_async(
        video_id=req.video_id,
        target=lambda on_progress, on_status, on_stage: process_video(
            req.video_id, req.filename, req.blob_url,
            content_type=req.content_type,
            source_language=source_language,
            indexing_window_seconds=req.indexing_window_seconds,
            on_progress=on_progress,
            on_stage=on_stage,
        ),
        pass_progress_callback=True,
        # Upload is done before /process is hit; Analyze covers the VI lifecycle.
        initial_stages={
            "upload": "skipped" if req.video_id.startswith("ref-") else "completed",
            "analyze": "pending",
        },
    )


@router.get("/status/{video_id}", response_model=ProcessJob)
def status(video_id: str) -> ProcessJob:
    job = jobs.get_job(video_id)
    if not job:
        raise HTTPException(404, "No job for this video.")
    return job
