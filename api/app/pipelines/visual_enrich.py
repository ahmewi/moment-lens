"""Index-time visual enrichment: describe what the frames of each scene card show.

Speech and OCR rarely mention what the camera shows (a referee's card, a replay, a corner kick), and some broadcasts have a
transcript that is mostly noise, so such windows cannot be found from text. This step has the judge model describe the sampled
frames of every card and tag the visible events; both become searchable text (summary, labels) before the card is embedded.

Run against an already-indexed video (no Video Indexer re-run):
    python3 /app/app/pipelines/visual_enrich.py backfill <video_id> [--apply] [--index NAME]
    python3 /app/app/pipelines/visual_enrich.py restore <video_id> [--index NAME]
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from typing import Any, NamedTuple

from app.azure_clients import blob_service, search_client
from app.config import settings
from app.models import SceneCard
from app.odata import quote
from app.pipelines.event_enrichment import build_manifest, event_prompt, first_view_events, parse_events, profile_for, read_video_json
from app.pipelines.openai_helpers import _completion_params, _judge_target, embed_texts
from app.storage import extract_window_frames, make_sas_url, upload_text

log = logging.getLogger(__name__)

VISUAL_PREFIX = "Visual: "
VISUAL_SYSTEM_PROMPT = """You write search metadata for one clip of a video from frames sampled in chronological order. Describe ONLY what is visibly shown; never guess names, scores, teams or events you cannot see. Return a JSON object {"description": str, "events": [str]}.
"description": one factual sentence (max 35 words) with the setting or phase of play, who is visible (players, referee, goalkeeper, coaches, crowd, presenters) and the main action.
"events": 0-6 short lowercase tags (1-3 words) for notable events that are visible, for example "yellow card", "red card", "free kick", "corner kick", "penalty kick", "goal celebration", "shot saved", "shot wide", "foul", "injured player", "substitution", "var review", "offside flag", "slow-motion replay", "studio", "interview". Use other short tags for events that are not in this list. Do not tag the scoreboard or the crowd unless the crowd is the main subject.
Use "yellow card" or "red card" only when the referee clearly holds a card up (a player in a yellow kit is not a card), "goal celebration" only for players celebrating a goal, and "slow-motion replay" only when replay graphics or slow motion are visible. Use an empty list when nothing notable is visible."""

_SECONDS_PER_FRAME = 2.5  # about one keyframe per GOP: denser sampling only repeats views
_FRAME_WIDTH = 640
_MAX_EVENTS = 6
_MAX_DESCRIPTION_CHARS = 300
_MAX_LABELS = 30
_BATCH = 24  # cards whose frames are held in memory at once
_EXTRACT_WORKERS = 2  # ffmpeg is CPU-bound and the web worker shares the vCPU
_DESCRIBE_WORKERS = 4  # keeps a long video under the deployment's per-minute token quota
_ATTEMPTS = 4
_REQUEST_TIMEOUT_S = 90.0
_RETRYABLE = re.compile(r"Error code: (429|5\d\d)")
_VISUAL_PREFIX_RE = re.compile(r"^Visual: [^\n]*\n?")
_BACKUP_BLOB = "{video_id}/pre-visual-enrich.json"
_DOC_FIELDS = [
    "id", "video_id", "filename", "start_seconds", "end_seconds", "duration_seconds", "summary",
    "transcript_excerpt", "labels", "entities", "ocr_text", "speakers", "thumbnail_url", "blob_url", "content_type",
]


def _frame_count(duration: float) -> int:
    return max(2, min(settings.visual_enrich_max_frames, round(duration / _SECONDS_PER_FRAME)))


def normalize_events(raw: Any) -> list[str]:
    """Lowercase, de-duplicate and bound the model's event tags (1-3 words, letters/digits/hyphens)."""
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        tag = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 \-]", "", str(item).lower())).strip()
        if tag and len(tag) <= 30 and len(tag.split()) <= 3 and tag not in out:
            out.append(tag)
    return out[:_MAX_EVENTS]


def strip_visual(summary: str) -> str:
    return _VISUAL_PREFIX_RE.sub("", summary or "", count=1)


def apply_visual_metadata(card: SceneCard, description: str, events: list[str]) -> None:
    """Put the description in front of the summary (so a truncated judge payload still sees it) and the tags first in labels."""
    base = strip_visual(card.summary)
    card.summary = f"{VISUAL_PREFIX}{description}\n{base}".strip() if description else base
    known = set(events)
    card.labels = (events + [label for label in card.labels if label.lower() not in known])[:_MAX_LABELS]


def describe_frames(target, frames: list[bytes], start: float, end: float, *,
                    content_type: str | None = None, evidence: str = "", scene_id: str = "",
                    context_window: tuple[float, float] | None = None,
                    frame_times: list[float] | None = None) -> dict | None:
    """One judge call -> {"description", "events"}, or None on any failure (the card is then simply left unchanged)."""
    if frame_times is not None and len(frame_times) != len(frames):
        raise ValueError("Frame times must match the supplied frames")
    content: list[dict] = [{"type": "text", "text": f"Clip window {start:g}s-{end:g}s. {len(frames)} frames follow."}]
    if context_window:
        content[0]["text"] = (
            f"{len(frames)} chronological frames cover {context_window[0]:g}s-{context_window[1]:g}s. "
            f"Only annotate events and describe actions INSIDE the target scene {start:g}s-{end:g}s. "
            "Surrounding frames establish context and first presentation, not additional events. "
            "Never assign an event from another scene to this target."
        )
    profile = profile_for(content_type)
    if profile:
        content[0]["text"] += f"\nUntrusted timed scene evidence:\n{evidence[:4000]}"
    for index, frame in enumerate(frames):
        if frame_times is not None:
            content.append({"type": "text", "text": f"Approximate sample time: {frame_times[index]:.3f}s."})
        image = {"url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")}
        if target.detail:
            image["detail"] = target.detail
        content.append({"type": "image_url", "image_url": image})
    for attempt in range(_ATTEMPTS):
        try:
            resp = target.client.chat.completions.create(
                model=target.model,
                messages=[{"role": "system", "content": VISUAL_SYSTEM_PROMPT + event_prompt(content_type or "")}, {"role": "user", "content": content}],
                response_format={"type": "json_object"},
                timeout=_REQUEST_TIMEOUT_S,
                **_completion_params(target, 8 if profile else 2),
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            description = re.sub(r"\s+", " ", str(data.get("description") or "")).strip()[:_MAX_DESCRIPTION_CHARS]
            events = normalize_events(data.get("events"))
            info = {"description": description, "events": events}
            if profile:
                info["custom_events"] = parse_events(data.get("custom_events"), content_type=content_type,
                                                     start=start, end=end, scene_id=scene_id)
                info["events_reviewed"] = data.get("events_reviewed") is True and isinstance(data.get("custom_events"), list)
                if context_window and info["events_reviewed"] and len(info["custom_events"]) != len(data["custom_events"]):
                    info["events_reviewed"] = False
                info["event_review_reason"] = str(data.get("event_review_reason") or "")[:500]
            return info if description or events or profile else None
        except Exception as e:  # noqa: BLE001
            if _RETRYABLE.search(str(e)) and attempt < _ATTEMPTS - 1:
                time.sleep(5 * (attempt + 1))
                continue
            log.warning("Visual description failed for %.0f-%.0fs: %s", start, end, str(e)[:200])
            return None
    return None


def enrich_cards(cards: list[SceneCard], source_url: str, *, target=None, event_report: dict | None = None) -> int:
    """Describe the frames of up to `visual_enrich_max_cards` cards and fold the result into summary/labels in place.

    `source_url` must be readable by ffmpeg (a SAS URL). Returns the number of cards changed. Best effort: a card whose frames
    can't be read or described is left as it was.
    """
    if not cards or not source_url:
        return 0
    todo = cards if event_report is not None else cards[: max(0, settings.visual_enrich_max_cards)]
    if len(todo) < len(cards):
        log.warning("Visual enrichment limited to the first %d of %d scene cards", len(todo), len(cards))
    target = target or _judge_target()
    started = time.monotonic()
    changed = 0

    def frames_of(card: SceneCard) -> list[bytes]:
        return extract_window_frames(
            source_url, card.start_seconds, card.end_seconds, _frame_count(card.duration_seconds), _FRAME_WIDTH, niceness=10,
        )

    def describe(item: tuple[SceneCard, list[bytes]]) -> dict | None:
        card, frames = item
        return describe_frames(target, frames, card.start_seconds, card.end_seconds,
                               content_type=card.content_type,
                               evidence=f"Transcript: {card.transcript_excerpt}\nOCR: {card.ocr_text}\nVI summary: {card.summary}",
                               scene_id=card.id) if frames else None

    with ThreadPoolExecutor(_EXTRACT_WORKERS) as extract_pool, ThreadPoolExecutor(_DESCRIBE_WORKERS) as describe_pool:
        for i in range(0, len(todo), _BATCH):
            framed = list(zip(todo[i:i + _BATCH], extract_pool.map(frames_of, todo[i:i + _BATCH])))
            for (card, _), info in zip(framed, describe_pool.map(describe, framed)):
                if info:
                    apply_visual_metadata(card, info["description"], info["events"])
                    changed += 1
                    if event_report is not None and info.get("events_reviewed"):
                        event_report["reviewed"] += 1
                        event_report.setdefault("reviewed_scene_ids", []).append(card.id)
                        event_report["candidates"].extend(info.get("custom_events", []))
    log.info("Visual enrichment: %d/%d scene cards in %.0fs", changed, len(todo), time.monotonic() - started)
    return changed


# ── Backfill of an already-indexed video ────────────────────────────────────────


def _card_from_doc(doc: dict) -> SceneCard:
    return SceneCard(
        id=doc["id"], video_id=doc["video_id"], filename=doc.get("filename") or "",
        start_seconds=float(doc.get("start_seconds") or 0), end_seconds=float(doc.get("end_seconds") or 0),
        duration_seconds=float(doc.get("duration_seconds") or 0), summary=doc.get("summary") or "",
        transcript_excerpt=doc.get("transcript_excerpt") or "", labels=list(doc.get("labels") or []),
        entities=list(doc.get("entities") or []), ocr_text=doc.get("ocr_text") or "", speakers=list(doc.get("speakers") or []),
        thumbnail_url=doc.get("thumbnail_url"), blob_url=doc.get("blob_url") or "", content_type=doc.get("content_type"),
    )


def _embedding_text(card: SceneCard) -> str:
    # Same composition as video_indexer.process_video, so backfilled and freshly indexed cards embed alike.
    return (card.summary + " " + card.transcript_excerpt + " " + " ".join(card.labels))[:8000]


def _candidate_batches(client, cards: list[SceneCard], content_type: str, seen: set[str]):
    profile = profile_for(content_type)
    if not profile or not cards:
        return
    budget = settings.event_analysis_max_scenes
    by_id = {card.id: card for card in cards}
    positions = {card.id: index for index, card in enumerate(cards)}
    query_filter = f"video_id eq '{quote(cards[0].video_id)}'"
    if seen:
        excluded = quote("|".join(sorted(seen)))
        query_filter += f" and not search.in(id, '{excluded}', '|')"
    pools = [iter(list(islice(client.search(
        search_text=query, query_type="simple", search_mode="any",
        search_fields=["summary", "labels", "transcript_excerpt", "ocr_text"],
        filter=query_filter, select=["id"], top=budget,
    ), budget))) for query in profile["queries"]]
    queued = set(seen)
    batch = []
    selected = 0
    while pools and selected < budget:
        remaining = []
        for pool in pools:
            hit = next(pool, None)
            if hit is None:
                continue
            remaining.append(pool)
            card = by_id.get(hit.get("id"))
            if not card or card.id in queued:
                continue
            index = positions[card.id]
            neighbors = [card] + [cards[position] for position in (index - 1, index + 1)
                                  if 0 <= position < len(cards)
                                  and abs(cards[position].start_seconds - card.start_seconds) <= 30]
            for candidate in neighbors:
                if candidate.id in queued or selected >= budget:
                    continue
                queued.add(candidate.id)
                selected += 1
                batch.append(candidate)
                if len(batch) == settings.event_analysis_batch_size:
                    yield batch
                    batch = []
        pools = remaining
    if batch:
        yield batch


class _SceneReview(NamedTuple):
    """What reviewing one scene produced. The caller applies it to the report, in scene order."""
    card: SceneCard
    start: float
    end: float
    frame_count: int
    requested_frames: int
    frame_width: int
    frame_times: list[float] | None
    info: dict | None
    extract_seconds: float
    model_seconds: float


def _review_scene(card: SceneCard, source_url: str, inventory: list[SceneCard], window: tuple[float, float], target,
                  extract_slots: threading.BoundedSemaphore, *, detailed: bool) -> _SceneReview:
    """Extract the frames of one scene and have the judge review them.

    It only reads (its own card and the shared inventory), so scenes can be reviewed at the same time."""
    start, end = window
    frame_width = 960 if detailed else _FRAME_WIDTH
    frame_times = None
    with extract_slots:  # decoding is CPU-bound and the web worker shares the vCPU; the judge call below only waits
        extraction_started = time.monotonic()
        if detailed:
            segments = [(start, card.start_seconds, 2),
                        (card.start_seconds, card.end_seconds, min(46, max(2, math.ceil(card.duration_seconds * 3)))),
                        (card.end_seconds, end, 2)]
            segments = [segment for segment in segments if segment[1] > segment[0]]
            requested_frames = sum(segment[2] for segment in segments)
            frames = []
            frame_times = []
            extraction_complete = True
            for segment_start, segment_end, count in segments:
                chunk = extract_window_frames(source_url, segment_start, segment_end, count,
                                              frame_width, niceness=10, keyframes_only=False)
                extraction_complete = extraction_complete and bool(chunk)
                frames.extend(chunk)
                frame_times.extend(segment_start + (index + 0.5) * (segment_end - segment_start) / count
                                   for index in range(len(chunk)))
            if not extraction_complete:
                frames = []
                frame_times = []
        else:
            requested_frames = min(32, max(2, math.ceil(end - start)))
            frames = extract_window_frames(source_url, start, end, requested_frames,
                                           frame_width, niceness=10, keyframes_only=False)
        extract_seconds = time.monotonic() - extraction_started
    evidence = "\n".join(
        f"{item.start_seconds:g}-{item.end_seconds:g}s: Transcript: {item.transcript_excerpt}; "
        f"OCR: {item.ocr_text}; Unverified metadata: {item.summary}"
        for item in [card] + [item for item in inventory if item.id != card.id
                             and item.start_seconds < end and start < item.end_seconds]
    )
    model_started = time.monotonic()
    info = describe_frames(target, frames, card.start_seconds, card.end_seconds,
                           content_type=card.content_type, evidence=evidence, scene_id=card.id,
                           context_window=(start, end), frame_times=frame_times) if frames else None
    return _SceneReview(card, start, end, len(frames), requested_frames, frame_width, frame_times, info,
                        extract_seconds, time.monotonic() - model_started)


def _apply_review(review: _SceneReview, report: dict, *, detailed: bool) -> None:
    card, info = review.card, review.info
    audit = {"context_start_seconds": review.start, "context_end_seconds": review.end,
             "annotation_start_seconds": card.start_seconds, "annotation_end_seconds": card.end_seconds,
             "frame_count": review.frame_count, "sampling": "focused_context" if detailed else "dense_context",
             "requested_frame_count": review.requested_frames, "frame_width": review.frame_width,
             "frame_times_seconds": review.frame_times,
             "reviewed": bool(info and info.get("events_reviewed")),
             "reason": "frame_extraction_failed" if not review.frame_count else "model_review_failed",
             "model_reason": (info or {}).get("event_review_reason", "")}
    report["event_reviews"][card.id] = audit
    if not audit["reviewed"]:
        if info:
            audit["reason"] = "review_not_confirmed"
        return
    events = info.get("custom_events", [])
    audit["reason"] = ("eligible_event" if first_view_events(events) else
                       "no_supported_event" if not events else
                       "not_first_view" if all(event.presentation != "live" for event in events) else
                       "below_confidence")
    audit["candidate_events"] = [event.model_dump() for event in events]
    report["candidates"] = [event for event in report["candidates"] if event.scene_id != card.id] + events
    apply_visual_metadata(card, info["description"], info["events"])
    if card.id not in report["reviewed_scene_ids"]:
        report["reviewed_scene_ids"].append(card.id)
        report["reviewed"] += 1
    report["revalidated_scene_ids"].append(card.id)


def _revalidate_cards(cards: list[SceneCard], source_url: str, inventory: list[SceneCard],
                      duration: float, report: dict, *, detailed: bool = False) -> None:
    """Review `cards` with up to `event_analysis_concurrency` scenes in flight (one in this thread when that is 1).

    Results are applied in card order whichever review finishes first, so the report comes out exactly as when the
    scenes are reviewed one by one."""
    target = _judge_target()
    windows = [(max(0.0, card.start_seconds - 8), min(duration, card.end_seconds + 8)) for card in cards]
    if any(end - start > 90 for start, end in windows):  # before any frame is extracted or model is called
        raise ValueError("Revalidation context must not exceed 90 seconds")
    workers = max(1, min(settings.event_analysis_concurrency, len(cards)))
    extract_slots = threading.BoundedSemaphore(min(_EXTRACT_WORKERS, workers))

    def review(item: tuple[SceneCard, tuple[float, float]]) -> _SceneReview:
        return _review_scene(item[0], source_url, inventory, item[1], target, extract_slots, detailed=detailed)

    started = time.monotonic()
    pool = ThreadPoolExecutor(workers, thread_name_prefix="event-review") if workers > 1 else None
    extract_seconds = model_seconds = 0.0
    try:
        for result in (pool.map(review, zip(cards, windows)) if pool else map(review, zip(cards, windows))):
            _apply_review(result, report, detailed=detailed)
            extract_seconds += result.extract_seconds
            model_seconds += result.model_seconds
    finally:
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
    log.info("Event review: %d scenes in %.0fs, %d at a time (%.0fs of frame extraction and %.0fs of model calls in total)",
             len(cards), time.monotonic() - started, workers, extract_seconds, model_seconds)


def enrich_video_events(video_id: str, content_type: str, on_progress=None, on_stage=None, *,
                        scene_ids: list[str] | None = None) -> dict:
    if not profile_for(content_type):
        raise ValueError("No event extraction profile is configured")
    raw = read_video_json(video_id, "insights.json")
    if not raw:
        raise ValueError("Native VI metadata is unavailable; index this video first")
    video = (raw.get("videos") or [{}])[0]
    vi_id = str(video.get("id") or raw.get("id") or "")
    if not vi_id:
        raise ValueError("VI video id is unavailable")
    client = search_client(settings.search_index)
    cards = sorted(_fetch_cards(client, video_id), key=lambda card: (card.start_seconds, card.id))
    if not cards:
        raise ValueError("No VI scenes are indexed for this video")
    if on_stage:
        on_stage("analyze", status="running", detail="revalidating selected scenes" if scene_ids is not None else "retrieving event candidates", progress=10)
    duration = float(video.get("durationInSeconds") or raw.get("durationInSeconds") or max(card.end_seconds for card in cards))
    cards_by_id = {card.id: card for card in cards}
    if scene_ids is not None and (not scene_ids or len(scene_ids) > 10 or any(scene_id not in cards_by_id for scene_id in scene_ids)):
        raise ValueError("Select 1-10 indexed scenes belonging to this video")
    previous = read_video_json(video_id, "events.json") or {}
    if previous.get("content_type") != content_type:
        previous = {}
    seen = set(previous.get("reviewed_scene_ids") or []) & cards_by_id.keys()
    if previous.get("status") == "completed":
        seen = set(cards_by_id)
    candidates = []
    for item in previous.get("candidates", previous.get("events", [])):
        card = cards_by_id.get(item.get("scene_id")) if isinstance(item, dict) else None
        if card:
            candidates.extend(parse_events([item], content_type=content_type,
                              start=card.start_seconds, end=card.end_seconds, scene_id=card.id))
    report = {"reviewed": len(seen), "reviewed_scene_ids": sorted(seen), "candidates": candidates,
              "revalidated_scene_ids": [], "event_reviews": dict(previous.get("event_reviews") or {})}
    initial_reviewed = len(seen)
    attempted = 0
    source_url = None
    accepted_keys = {(event["scene_id"], event["kind"]) for event in previous.get("events", [])}

    def snapshot(finished: bool) -> dict:
        manifest = build_manifest(video_id, vi_id, content_type, duration,
                                  report["candidates"], report["reviewed"], len(cards))
        successful_count = len(report["revalidated_scene_ids"]) if scene_ids is not None else report["reviewed"] - initial_reviewed
        manifest.update(analysis_mode="query_targeted", analysis_version=2,
                analysis_complete=finished and successful_count == attempted,
                        reviewed_scene_ids=sorted(set(report["reviewed_scene_ids"])),
                        attempted_scenes=attempted, scene_budget=settings.event_analysis_max_scenes,
                        stop_reason="scene_budget" if attempted >= settings.event_analysis_max_scenes else "candidates_exhausted",
                        legacy_reviewed_scenes=previous.get("legacy_reviewed_scenes", previous.get("reviewed_scenes", 0)))
        if report["event_reviews"]:
            manifest["event_reviews"] = report["event_reviews"]
            for scene_id in report["revalidated_scene_ids"]:
                audit = manifest["event_reviews"][scene_id]
                audit["accepted_kinds"] = [event["kind"] for event in manifest["events"] if event["scene_id"] == scene_id]
                if audit["reason"] in ("eligible_event", "accepted", "deduplicated"):
                    audit["reason"] = "accepted" if audit["accepted_kinds"] else "deduplicated"
        if scene_ids is not None:
            manifest["revalidation_scene_ids"] = list(dict.fromkeys(scene_ids))
            manifest["stop_reason"] = "selected_scenes_reviewed"
        return manifest

    batches = ([cards_by_id[scene_id] for scene_id in dict.fromkeys(scene_ids)],) if scene_ids is not None else _candidate_batches(client, cards, content_type, seen)
    for batch in batches:
        working = [card.model_copy(deep=True) for card in batch]
        for card in working:
            card.content_type = content_type
            card.labels = [label for label in card.labels if not label.startswith("event:")]
            card.summary = re.sub(r"\nFirst-view [^\n]*", "", card.summary)
        source_url = source_url or make_sas_url(cards[0].blob_url, hours=4)
        attempted += len(working)
        tracking_key = "revalidated_scene_ids"
        before = set(report[tracking_key])
        _revalidate_cards(working, source_url, cards, duration, report, detailed=scene_ids is not None)
        successful = [card for card in working if card.id in set(report[tracking_key]) - before]
        manifest = snapshot(False)
        next_keys = {(event["scene_id"], event["kind"]) for event in manifest["events"]}
        successful_by_id = {card.id: card for card in successful}
        for scene_id, kind in accepted_keys - next_keys:
            if scene_id in cards_by_id and scene_id not in successful_by_id:
                card = cards_by_id[scene_id].model_copy(deep=True)
                successful_by_id[scene_id] = card
                successful.append(card)
            card = successful_by_id.get(scene_id)
            if card:
                card.labels = [label for label in card.labels if label != f"event:{kind}"]
                label = profile_for(content_type)["events"][kind]
                card.summary = re.sub(rf"\nFirst-view {re.escape(label)}:[^\n]*", "", card.summary)
        for event in manifest["events"]:
            card = next((card for card in successful if card.id == event["scene_id"]), None)
            if card:
                card.labels = list(dict.fromkeys([f"event:{event['kind']}", event["label"].lower()] + card.labels))[:30]
                card.summary += f"\nFirst-view {event['label']}: {event['evidence']}"
        if successful:
            _merge(client, successful, content_type=content_type)
            cards_by_id.update({card.id: card for card in successful})
            accepted_keys = next_keys
            upload_text(settings.azure_storage_insights_container, f"{video_id}/events.json",
                        json.dumps(manifest, ensure_ascii=False))
        progress = 10 + int(75 * attempted / settings.event_analysis_max_scenes)
        if on_stage:
            validated = len(report["revalidated_scene_ids"]) if scene_ids is not None else report["reviewed"] - initial_reviewed
            on_stage("analyze", status="running", progress=progress,
                     detail=f"validated {validated}/{attempted} candidate scenes (budget {settings.event_analysis_max_scenes})")
        if on_progress:
            on_progress(progress)
    manifest = snapshot(True)
    if on_progress:
        on_progress(90)
    url = upload_text(settings.azure_storage_insights_container, f"{video_id}/events.json", json.dumps(manifest, ensure_ascii=False))
    return {"vi_video_id": vi_id, "scene_count": len(cards), "insights_url": url, "event_status": manifest["status"]}


def _merge(client, cards: list[SceneCard], *, content_type: str | None = None) -> None:
    vectors = embed_texts([_embedding_text(c) for c in cards])
    if len(vectors) != len(cards):
        raise ValueError("Embedding count does not match scene count")
    updates = [
        {"id": c.id, "summary": c.summary, "labels": c.labels, "content_vector": v} for c, v in zip(cards, vectors)
    ]
    if content_type is not None:
        for update in updates:
            update["content_type"] = content_type
    for i in range(0, len(updates), 100):
        batch = updates[i:i + 100]
        results = client.merge_or_upload_documents(documents=batch)
        if len(results) != len(batch) or any(result.succeeded is not True for result in results):
            raise RuntimeError("Search did not accept all scene enrichment updates")


def _fetch_cards(client, video_id: str) -> list[SceneCard]:
    docs = client.search(search_text="*", filter=f"video_id eq '{quote(video_id)}'", select=_DOC_FIELDS)
    return sorted((_card_from_doc(d) for d in docs), key=lambda c: c.start_seconds)


def _backup_blob(video_id: str):
    container = blob_service().get_container_client(settings.azure_storage_insights_container)
    return container.get_blob_client(_BACKUP_BLOB.format(video_id=video_id))


def backfill(video_id: str, *, index_name: str | None = None, apply: bool = False) -> dict:
    """Enrich the cards of an indexed video in place. Without `apply` nothing is written.

    The first applied run stores the original summary/labels in insights/<video_id>/pre-visual-enrich.json so `restore` can undo
    it; later runs start again from that backup, so tags of an earlier run never pile up in the labels.
    """
    index = index_name or settings.search_index
    client = search_client(index)
    cards = _fetch_cards(client, video_id)
    result = {"index": index, "cards": len(cards), "enriched": 0, "applied": False}
    if not cards:
        return result
    backup_blob = _backup_blob(video_id)
    backup = json.loads(backup_blob.download_blob().readall()) if backup_blob.exists() else None
    if backup:
        for card in cards:
            if card.id in backup:
                card.summary, card.labels = backup[card.id]["summary"], list(backup[card.id]["labels"])
    originals = {c.id: (c.summary, list(c.labels)) for c in cards}
    enrich_cards(cards, make_sas_url(cards[0].blob_url, hours=3))
    changed = [c for c in cards if (c.summary, c.labels) != originals[c.id]]
    result["enriched"] = len(changed)
    if not apply or not changed:
        return result
    if backup is None:
        upload_text(
            settings.azure_storage_insights_container, _BACKUP_BLOB.format(video_id=video_id),
            json.dumps({cid: {"summary": summary, "labels": labels} for cid, (summary, labels) in originals.items()}, ensure_ascii=False),
        )
    _merge(client, changed)
    result["applied"] = True
    return result


def restore(video_id: str, *, index_name: str | None = None) -> int:
    """Put the pre-enrichment summary/labels (and matching embeddings) back. Returns the number of cards restored."""
    client = search_client(index_name or settings.search_index)
    backup = json.loads(_backup_blob(video_id).download_blob().readall())
    cards = [c for c in _fetch_cards(client, video_id) if c.id in backup]
    for card in cards:
        card.summary, card.labels = backup[card.id]["summary"], backup[card.id]["labels"]
    if cards:
        _merge(client, cards)
    return len(cards)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("backfill", "restore"):
        p = sub.add_parser(name)
        p.add_argument("video_id")
        p.add_argument("--index")
    sub.choices["backfill"].add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "backfill":
        print(json.dumps(backfill(args.video_id, index_name=args.index, apply=args.apply)))
    else:
        print(json.dumps({"restored": restore(args.video_id, index_name=args.index)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
