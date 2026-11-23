"""Video Indexer ingestion pipeline.

Uses the ARM-based VI account: AAD token -> generateAccessToken -> classic VI REST.
Docs: https://learn.microsoft.com/azure/azure-video-indexer/video-indexer-use-apis
"""
from __future__ import annotations

import json
import logging
import random
import time
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import httpx

from app.azure_clients import aad_token
from app.config import settings
from app.models import SceneCard, VideoIndexerLanguage
from app.pipelines import vi_poller_client
from app.pipelines.event_enrichment import build_manifest, profile_for
from app.pipelines.indexer import upsert_scene_cards
from app.pipelines.openai_helpers import embed_texts, enrich_scene
from app.pipelines.visual_enrich import enrich_cards, enrich_video_events
from app.storage import extract_thumbnail, make_sas_url, upload_blob, upload_text
from app.telemetry import (
    estimate_embedding_cost_usd,
    estimate_vi_cost_usd,
    record_pipeline_run,
)

log = logging.getLogger(__name__)

VI_API_BASE = "https://api.videoindexer.ai"
ARM_SCOPE = "https://management.azure.com/.default"


def _vi_request(method: str, url: str, *, timeout: int = 60, max_retries: int = 12) -> httpx.Response:
    """Wrapper around httpx that retries on 429 / 503 with Retry-After backoff.

    VI throttles aggressively when many videos are submitted/polled in parallel
    (HTTP 429). We honour the Retry-After header (seconds) when present and
    otherwise back off exponentially up to 120s per attempt. With max_retries=12
    the worst case wait is several minutes — long, but always preferable to
    failing the indexing job.
    """
    backoff = 5.0
    for attempt in range(max_retries):
        r = httpx.request(method, url, timeout=timeout)
        if r.status_code not in (429, 503):
            return r
        retry_after = r.headers.get("Retry-After")
        try:
            wait_s = float(retry_after) if retry_after else backoff
        except ValueError:
            wait_s = backoff
        # add small jitter so concurrent waiters de-synchronise
        wait_s = min(max(wait_s, 1.0), 120.0) + random.uniform(0, 2.0)
        log.warning(
            "VI %s %s -> %s (attempt %d/%d), sleeping %.1fs",
            method, url.split("?")[0], r.status_code, attempt + 1, max_retries, wait_s,
        )
        time.sleep(wait_s)
        backoff = min(backoff * 2, 120.0)
    return r  # last response (still 429/503) — let caller raise_for_status


def _arm_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {aad_token(ARM_SCOPE)}", "Content-Type": "application/json"}


def get_vi_access_token(permission: str = "Contributor", scope: str = "Account", *, video_id: str | None = None) -> str:
    """Mint a VI account access token via ARM."""
    url = (
        f"https://management.azure.com/subscriptions/{settings.azure_subscription_id}"
        f"/resourceGroups/{settings.vi_resource_group}"
        f"/providers/Microsoft.VideoIndexer/accounts/{settings.vi_account_name}"
        f"/generateAccessToken?api-version={settings.vi_api_version}"
    )
    body = {"permissionType": permission, "scope": scope}
    if scope == "Video":
        if not video_id:
            raise ValueError("Video-scoped tokens require a VI video id")
        body["videoId"] = video_id
    r = httpx.post(url, headers=_arm_headers(), json=body, timeout=60)
    r.raise_for_status()
    return r.json()["accessToken"]


@lru_cache(maxsize=1)
def get_source_languages() -> list[VideoIndexerLanguage]:
    response = httpx.get(
        f"{VI_API_BASE}/{settings.vi_location}/SupportedLanguages", timeout=10,
    )
    response.raise_for_status()
    languages = [
        VideoIndexerLanguage(code=language["languageCode"], name=language["name"])
        for language in response.json()
        if language.get("isSourceLanguage") is True
    ]
    if not languages:
        raise ValueError("Video Indexer returned no source languages.")
    return sorted(languages, key=lambda language: language.name.casefold())


def _sanitize_vi_name(name: str, video_id_hint: str, max_len: int = 80) -> str:
    """VI requires name length 2..80 and disallows some chars. Truncate while
    keeping the extension and a unique suffix so re-uploads don't collide."""
    import os
    import re
    base, ext = os.path.splitext(name or "video")
    ext = ext[:8]  # keep ".mp4" etc.
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-") or "video"
    suffix = f"_{video_id_hint[:8]}"
    keep = max_len - len(ext) - len(suffix)
    if keep < 2:
        keep = 2
    return (base[:keep] + suffix + ext)[:max_len]


def upload_video(blob_sas_url: str, name: str, video_id_hint: str,
                 *, source_language: str | None = None) -> str:
    """Submit a video to VI by URL. Returns VI's videoId."""
    token = get_vi_access_token()
    safe_name = _sanitize_vi_name(name, video_id_hint)
    language = (source_language or "").strip() or "auto"
    # useManagedIdentityToDownloadVideo=true tells VI to authenticate to the
    # source storage account using its own managed identity instead of
    # treating videoUrl as anonymously fetchable. REQUIRED when the storage
    # account has its firewall enabled (publicNetworkAccess=Enabled +
    # defaultAction=Deny with VI listed in resourceAccessRules). Without
    # this flag, VI's fetch is rejected by the storage firewall and the
    # Upload-Video call returns 400.
    # Docs: https://learn.microsoft.com/azure/azure-video-indexer/storage-behind-firewall
    url = (
        f"{VI_API_BASE}/{settings.vi_location}/Accounts/{settings.vi_account_id}/Videos"
        f"?accessToken={token}&name={quote(safe_name)}&privacy=Private&videoUrl={quote(blob_sas_url, safe='')}"
        f"&externalId={quote(video_id_hint)}&language={quote(language, safe='')}&indexingPreset=Advanced"
        f"&useManagedIdentityToDownloadVideo=true"
    )
    r = _vi_request("POST", url, timeout=120)
    if r.status_code >= 400:
        log.error("VI upload failed (%s): %s", r.status_code, r.text)
        r.raise_for_status()
    return r.json()["id"]


def get_index(vi_video_id: str) -> dict[str, Any]:
    token = get_vi_access_token()
    url = (
        f"{VI_API_BASE}/{settings.vi_location}/Accounts/{settings.vi_account_id}"
        f"/Videos/{vi_video_id}/Index?accessToken={token}&language=English"
    )
    r = _vi_request("GET", url, timeout=60)
    r.raise_for_status()
    return r.json()


def wait_for_processing(
    vi_video_id: str,
    timeout_minutes: int = 60,
    poll_seconds: int = 30,
    on_progress: Any = None,
    on_stage: Any = None,
) -> dict[str, Any]:
    """Poll VI until state == Processed (or Failed). Returns final index JSON.

    Polls every `poll_seconds` (default 30s) plus 0–6s of jitter so concurrent
    videos don't all hit VI on the same tick — VI returns HTTP 429 fast otherwise.
    If `on_progress(pct)` is supplied, reports VI's `processingProgress`
    percentage scaled into the 25..85 band so the UI bar moves.
    """
    deadline = time.time() + timeout_minutes * 60
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = get_index(vi_video_id)
        v = (last.get("videos") or [{}])[0]
        state = v.get("state") or last.get("state")
        log.info("VI state for %s: %s", vi_video_id, state)
        if on_progress is not None:
            raw = (v.get("processingProgress") or last.get("processingProgress") or "0%")
            try:
                pct = int(str(raw).rstrip("%"))
            except Exception:
                pct = 0
            try:
                on_progress(25 + int(pct * 0.6))  # 25..85
            except Exception:
                pass
            if on_stage is not None:
                try:
                    on_stage("analyze", status="running", progress=pct, detail=f"VI: {state or 'queued'}")
                except Exception:
                    pass
        if state == "Processed":
            return last
        if state == "Failed":
            raise RuntimeError(f"VI processing failed: {last}")
        time.sleep(poll_seconds + random.uniform(0, 6.0))
    raise TimeoutError(f"VI processing timed out for {vi_video_id}")


def insights_to_scene_cards(
    insights: dict[str, Any], video_id: str, filename: str, blob_url: str,
    thumbnail_url: str | None = None,
    content_type: str | None = None,
    scene_thumbnails: dict[int, str] | None = None,
    prompt_content: dict[str, Any] | None = None,
    indexing_window_seconds: int = 0,
) -> list[SceneCard]:
    """Map timed VI insights to shot cards or overlapping context windows."""
    if indexing_window_seconds not in (0, 20, 30):
        raise ValueError("VI indexing window must be 0, 20, or 30 seconds.")
    scene_thumbnails = scene_thumbnails or {}
    cards: list[SceneCard] = []
    videos = insights.get("videos") or []
    if not videos:
        return cards
    v = videos[0]
    ins = v.get("insights", {})

    # Build a lookup of (start_s, end_s) -> section content from PromptContent.
    pc_sections: list[tuple[float, float, str]] = []
    if prompt_content:
        for sec in (prompt_content.get("sections") or []):
            try:
                s_str = sec.get("start") or sec.get("startTime") or "0:00:00"
                e_str = sec.get("end")   or sec.get("endTime")   or "0:00:00"
                # Reuse _to_seconds defined below — but we're in module scope
                # here, so inline the parse:
                def _ts(x: str) -> float:
                    h, m, s = (x or "0:0:0").split(":")
                    return int(h) * 3600 + int(m) * 60 + float(s)
                content = (sec.get("content") or sec.get("text") or sec.get("title") or "").strip()
                if content:
                    pc_sections.append((_ts(s_str), _ts(e_str), content))
            except Exception:  # noqa: BLE001
                continue

    transcript_items = ins.get("transcript", [])
    ocr_items = ins.get("ocr", [])
    labels_items = ins.get("labels", [])
    keywords_items = ins.get("keywords", [])
    named_people_items = ins.get("namedPeople", [])
    brands_items = ins.get("brands", [])
    shots = ins.get("shots", [])

    def _to_seconds(ts: str) -> float:
        # "0:00:12.34" -> 12.34
        if not ts:
            return 0.0
        try:
            h, m, s = ts.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        except Exception:
            return 0.0

    def _items_in_range(items: list[dict], start: float, end: float, value_key: str) -> list[str]:
        out = []
        for it in items:
            for inst in it.get("instances", []):
                s = _to_seconds(inst.get("start", ""))
                e = _to_seconds(inst.get("end", ""))
                if (e > start and s < end) or (s == e and start <= s < end):
                    val = it.get(value_key) or it.get("text") or ""
                    if val:
                        out.append(val)
        return out

    def _transcript_text_in_range(start: float, end: float) -> str:
        parts = []
        for t in transcript_items:
            for inst in t.get("instances", []):
                s = _to_seconds(inst.get("start", ""))
                e = _to_seconds(inst.get("end", ""))
                if (e > start and s < end) or (s == e and start <= s < end):
                    text = t.get("text") or ""
                    if text:
                        parts.append(text)
        return " ".join(parts)[:2000]

    shot_ranges: list[tuple[float, float, int | None]] = []
    for shot in shots:
        for inst in shot.get("instances", []):
            start = max(0.0, _to_seconds(inst.get("start", "")))
            end = _to_seconds(inst.get("end", ""))
            if end > start:
                shot_ranges.append((start, end, shot.get("id")))

    duration = float(v.get("durationInSeconds") or insights.get("durationInSeconds") or 0)
    if duration <= 0:
        duration = max((end for _, end, _ in shot_ranges), default=0.0)
    segments = shot_ranges or ([(0.0, duration, None)] if duration > 0 else [])
    if indexing_window_seconds:
        segments = []
        start = 0.0
        while start < duration:
            end = min(start + indexing_window_seconds, duration)
            overlapping = [span for span in shot_ranges if span[1] > start and span[0] < end]
            midpoint = (start + end) / 2
            thumbnail_id = min(
                overlapping, key=lambda span: abs((span[0] + span[1]) / 2 - midpoint),
            )[2] if overlapping else None
            segments.append((start, end, thumbnail_id))
            if end >= duration:
                break
            start += indexing_window_seconds - 5

    for start, end, shot_id in segments:
        transcript_excerpt = _transcript_text_in_range(start, end)
        shot_labels = sorted(set(_items_in_range(labels_items, start, end, "name")))
        shot_ocr = " ".join(_items_in_range(ocr_items, start, end, "text"))[:500]

        shot_people = sorted(set(_items_in_range(named_people_items, start, end, "name")))
        shot_brands = sorted(set(_items_in_range(brands_items, start, end, "name")))
        shot_keywords = sorted(set(_items_in_range(keywords_items, start, end, "name")))
        entities = sorted(set(shot_people + shot_brands + shot_keywords))[:30]

        pc_summary = ""
        if indexing_window_seconds:
            pc_summary = " ".join(dict.fromkeys(
                section_text for section_start, section_end, section_text in sorted(pc_sections)
                if section_end > start and section_start < end
            ))[:500]
        elif pc_sections:
            best_overlap = 0.0
            for section_start, section_end, section_text in pc_sections:
                overlap = max(0.0, min(end, section_end) - max(start, section_start))
                if overlap > best_overlap:
                    best_overlap = overlap
                    pc_summary = section_text
            pc_summary = pc_summary[:500]

        if pc_summary:
            summary = pc_summary
            llm_entities, llm_labels = [], []
        else:
            summary_input = transcript_excerpt or " ".join(shot_labels[:10])
            enriched = enrich_scene(summary_input, shot_labels, entities, shot_ocr, content_type=content_type) if summary_input else {"summary": "", "entities": [], "labels": []}
            summary = enriched.get("summary", "")
            llm_entities = [entity for entity in (enriched.get("entities") or []) if isinstance(entity, str)]
            llm_labels = [label for label in (enriched.get("labels") or []) if isinstance(label, str)]
        entities = sorted(set(entities + llm_entities))[:30]
        shot_labels = sorted(set(shot_labels + llm_labels))[:30]

        card = SceneCard(
            id=f"{video_id}-vi-{int(start*1000)}-{int(end*1000)}",
            video_id=video_id,
            filename=filename,
            start_seconds=start,
            end_seconds=end,
            duration_seconds=end - start,
            summary=summary,
            transcript_excerpt=transcript_excerpt,
            labels=shot_labels[:30],
            entities=entities,
            ocr_text=shot_ocr,
            speakers=[],
            thumbnail_url=scene_thumbnails.get(shot_id) or thumbnail_url,
            blob_url=blob_url,
            content_type=content_type,
        )
        cards.append(card)
    return cards


def process_video(video_id: str, filename: str, blob_url: str,
                  content_type: str | None = None,
                  on_progress: Any = None,
                  on_stage: Any = None,
                  source_language: str | None = None,
                  indexing_window_seconds: int = 0) -> dict[str, Any]:
    """End-to-end ingestion pipeline. Returns a status dict with VI video id and counts."""
    def _p(pct: int) -> None:
        if on_progress:
            try: on_progress(pct)
            except Exception: pass

    def _st(name: str, **fields) -> None:
        if on_stage:
            try: on_stage(name, **fields)
            except Exception: pass

    started_at = time.time()
    scene_count = 0
    duration_s = 0.0
    status_label = "failed"
    cost_usd = 0.0
    try:
        sas = make_sas_url(blob_url, hours=4)
        log.info("Submitting %s to Video Indexer (content_type=%s)...", filename, content_type)
        _p(20)
        _st("analyze", status="running", progress=0, detail="submitting to VI")
        thumb_url = extract_thumbnail(sas, video_id, at_seconds=3.0)
        vi_id = upload_video(sas, name=filename, video_id_hint=video_id,
                     source_language=source_language)
        log.info("Got VI id %s; waiting for processing...", vi_id)
        _p(25)
        _st("analyze", status="running", progress=5, detail=f"VI id {vi_id}")

        # Out-of-process polling (vi-poller Container Apps Job). Survives App
        # Service restarts; falls back to in-process polling when not configured.
        use_job = (
            settings.vi_poller_use_job
            and bool(settings.vi_poller_job_name)
            and bool(settings.vi_poller_resource_group)
        )
        if use_job:
            log.info("Using vi-poller job for VI ingestion polling")
            exec_name = vi_poller_client.start_poll(video_id, vi_id)
            _st("analyze", status="running", progress=5, detail=f"vi-poller exec={exec_name}")
            insights = vi_poller_client.wait_for_insights(
                video_id, execution_name=exec_name,
                on_progress=on_progress, on_stage=on_stage,
            )
        else:
            insights = wait_for_processing(vi_id, on_progress=on_progress, on_stage=on_stage)

        # Persist insights JSON
        _p(86)
        insights_url = upload_text(
            container=settings.azure_storage_insights_container,
            blob_name=f"{video_id}/insights.json",
            text=json.dumps(insights, ensure_ascii=False),
        )

        # PromptContent (GPT-augmented per-segment narratives) is written by
        # vi-poller into insights/<vid>/prompt-content.json when available.
        # Fetching it now is cheap (one blob GET) and skipping it just falls
        # back to per-shot enrich_scene LLM calls — no functional regression.
        prompt_content: dict[str, Any] | None = None
        try:
            prompt_content = vi_poller_client.read_prompt_content(video_id)
            if prompt_content:
                log.info("Loaded VI PromptContent (%d sections)",
                         len(prompt_content.get("sections") or []))
        except Exception as e:  # noqa: BLE001
            log.warning("PromptContent load failed (continuing without): %s", e)

        # Per-scene keyframe thumbnails (one extraction pass before mapping)
        _p(88)
        scene_thumbs = _extract_shot_thumbnails(insights, vi_id, video_id)

        cards = insights_to_scene_cards(
            insights, video_id, filename, blob_url,
            thumbnail_url=thumb_url, content_type=content_type,
            scene_thumbnails=scene_thumbs,
            prompt_content=prompt_content,
            indexing_window_seconds=indexing_window_seconds,
        )
        log.info("Built %d scene cards (with %d per-scene keyframe thumbs); embedding...",
                 len(cards), len(scene_thumbs))
        event_report = {"reviewed": 0, "candidates": []}
        if settings.visual_enrich_enabled and not profile_for(content_type) and cards:
            _st("analyze", status="running", progress=90, detail="extracting content-specific events and frame evidence")
            try:
                # The SAS minted at the start may have expired while VI was processing.
                enrich_cards(cards, make_sas_url(blob_url, hours=2))
            except Exception as e:  # noqa: BLE001
                log.warning("Visual enrichment skipped: %s", e)
        video = (insights.get("videos") or [{}])[0]
        duration_s = float(video.get("durationInSeconds") or insights.get("durationInSeconds") or 0.0)
        manifest = build_manifest(video_id, vi_id, content_type, duration_s,
                                  event_report["candidates"], event_report["reviewed"], len(cards))
        cards_by_id = {card.id: card for card in cards}
        for event in manifest["events"]:
            card = cards_by_id.get(event["scene_id"])
            if card:
                card.labels = list(dict.fromkeys([f"event:{event['kind']}", event["label"].lower()] + card.labels))[:30]
                card.summary += f"\nFirst-view {event['label']}: {event['evidence']}"
        _p(92)
        texts = [(c.summary + " " + c.transcript_excerpt + " " + " ".join(c.labels))[:8000] for c in cards]
        vectors = embed_texts(texts)
        entity_texts = [(", ".join(c.entities) or "(none)") for c in cards]
        entity_vectors = embed_texts(entity_texts)
        embeddings = {c.id: vec for c, vec in zip(cards, vectors)}
        entity_embeddings = {c.id: vec for c, vec in zip(cards, entity_vectors)}
        _p(95)
        upsert_scene_cards(settings.search_index, cards,
                           embeddings=embeddings, entity_embeddings=entity_embeddings)
        upload_text(settings.azure_storage_insights_container, f"{video_id}/events.json",
                json.dumps(manifest, ensure_ascii=False))
        if content_type and profile_for(content_type) and cards:
            enrich_video_events(video_id, content_type,
                    on_progress=lambda progress: _p(95 + int(progress / 25)), on_stage=_st)

        scene_count = len(cards)
        v = (insights.get("videos") or [{}])[0]
        duration_s = float(v.get("durationInSeconds") or insights.get("durationInSeconds") or 0.0)
        status_label = "indexed"
        cost_usd = (
            estimate_vi_cost_usd(duration_s, settings.cost_rate_vi_per_minute)
            + estimate_embedding_cost_usd(
                scene_count,
                rate_per_million_tokens=settings.cost_rate_embedding_per_million_tokens,
            )
        )
        return {
            "vi_video_id": vi_id,
            "scene_count": scene_count,
            "insights_url": insights_url,
            "estimated_cost_usd": round(cost_usd, 4),
            "video_duration_seconds": duration_s,
        }
    finally:
        try:
            record_pipeline_run(
                content_type=content_type,
                duration_seconds=duration_s,
                latency_seconds=time.time() - started_at,
                scene_count=scene_count,
                status=status_label,
                estimated_cost_usd=cost_usd,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("VI telemetry emit failed: %s", e)


# ── VI keyframe → blob thumbnail extraction ─────────────────────────────

def _extract_shot_thumbnails(
    insights: dict[str, Any], vi_video_id: str, video_id: str,
) -> dict[int, str]:
    """For each shot, fetch the middle keyframe via VI's Thumbnails API and
    upload it as a per-scene blob. Returns {shot_id: thumbnail_blob_url}.

    Best-effort: missing/failed thumbnails just fall back to the video-level
    one. Concurrency capped to keep VI happy.
    """
    out: dict[int, str] = {}
    videos = insights.get("videos") or []
    if not videos:
        return out
    shots = (videos[0].get("insights", {}) or {}).get("shots") or []
    if not shots:
        return out

    def _pick_keyframe(shot: dict) -> str | None:
        kfs = shot.get("keyFrames") or []
        if not kfs:
            return None
        # Pick the middle keyframe so the thumbnail is representative of the shot.
        kf = kfs[len(kfs) // 2]
        for inst in (kf.get("instances") or []):
            tid = inst.get("thumbnailId")
            if tid:
                return tid
        return None

    work: list[tuple[int, str]] = []
    for shot in shots:
        sid = shot.get("id")
        tid = _pick_keyframe(shot)
        if sid is None or not tid:
            continue
        work.append((int(sid), tid))

    if not work:
        return out

    log.info("Extracting %d per-shot keyframe thumbnails from VI %s", len(work), vi_video_id)

    # Mint one access token + reuse across all shot thumbnails.
    token = get_vi_access_token()
    base = (
        f"{VI_API_BASE}/{settings.vi_location}/Accounts/{settings.vi_account_id}"
        f"/Videos/{vi_video_id}/Thumbnails"
    )

    def _do(item: tuple[int, str]) -> tuple[int, str | None]:
        sid, tid = item
        url = f"{base}/{tid}?accessToken={token}&format=Jpeg"
        try:
            r = _vi_request("GET", url, timeout=30)
            if r.status_code != 200 or not r.content:
                log.warning("VI thumbnail %s -> %s", tid, r.status_code)
                return sid, None
            blob_url = upload_blob(
                container=settings.azure_storage_thumbnails_container,
                blob_name=f"{video_id}/scene-{sid}.jpg",
                data=r.content,
                content_type="image/jpeg",
            )
            return sid, blob_url
        except Exception as e:  # noqa: BLE001
            log.warning("Thumbnail fetch failed for shot %d (%s): %s", sid, tid, e)
            return sid, None

    # Modest concurrency — VI throttles aggressively.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=4) as pool:
        for fut in as_completed(pool.submit(_do, item) for item in work):
            sid, url = fut.result()
            if url:
                out[sid] = url
    log.info("Extracted %d/%d per-shot keyframe thumbnails", len(out), len(work))
    return out
