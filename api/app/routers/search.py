"""Search router — smart hybrid query over the moments index.

Quality controls:
  * LLM query rewrite extracts named entities (e.g. "Rivera" -> "Sam Rivera"),
    action synonyms ("scoring" -> goal/scores/scored/finish/strike), and a
    natural-language expansion used as the embedding/semantic input.
  * BM25 keyword query is built in Lucene full-syntax to require entity tokens
    (when present) and OR-expand action terms.
  * A pre-filter (`search.ismatchscoring`) requires the entity to appear in
    metadata or transcript fields. If that yields no hits we gracefully fall
    back to the unfiltered hybrid query.
  * Index uses a scoring profile that boosts entity / label / summary matches
    over raw transcript text.
  * Semantic reranker pulls a >=50 candidate pool and we drop any whose
    @search.reranker_score is below `min_rerank_score` to filter out weak
    semantic matches.
  * Intent-aware vectors: content_vector is queried with the action/scene
    description; entity_vector is queried with ONLY the named entities and is
    skipped entirely when none were named, instead of both fields sharing one
    diluted embedding.
  * Action-aware reranker (hybrid only), two stages. Stage 1 (text): an LLM
    judges a WIDE pool of top candidates (default 40) in small parallel batches,
    classifying each as scoring_sequence / celebration_only / attempt_only /
    mention_only / unrelated from its transcript/OCR/labels, because the
    semantic reranker favours windows that talk ABOUT goals over the window
    where one is scored. Stage 2 (visual, optional): for the few candidates
    stage 1 believes show the action (plus undecided ones while slots remain),
    2-4 ffmpeg frames from the candidate's own time range are judged by the same
    model (one call per candidate) so a hallucinated "goal scored" summary or a
    saved shot is demoted. Every stage
    fails open: a missing verdict is neutral ("uncertain") and never ranks
    below a candidate the judge affirmatively called a mere mention.
  * Moment tags: `event_kind` (or a plain query such as "goals") lists the cards that carry the
    index-time `event:<kind>` tag without calling any model. When a request explicitly turns visual
    confirmation on, `_deep_search` also recovers untagged moments (the index-time pass is budgeted) by
    running this same pipeline for the event and keeping the clips whose frames confirm it.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any

from azure.search.documents.models import VectorizedQuery
from fastapi import APIRouter, HTTPException

from app.azure_clients import search_client
from app.config import settings
from app.models import DeepSearchStats, FacetValue, SearchRequest, SearchResponse, SearchResultItem
from app.odata import quote
from app.pipelines.event_enrichment import EVENT_SEARCH_QUERIES, PROFILES
from app.pipelines.indexer import SCORING_PROFILE, SEMANTIC_CONFIG
from app.pipelines.openai_helpers import embed_texts, rerank_action_candidates, rewrite_query
from app.storage import extract_frame_bytes, extract_window_frames, make_sas_url, proxy_blob_url

log = logging.getLogger(__name__)
router = APIRouter()

SELECT_FIELDS = [
    "id", "video_id", "filename", "start_seconds", "end_seconds",
    "summary", "transcript_excerpt", "labels", "entities", "speakers",
    "thumbnail_url", "blob_url", "ocr_text",
]

# Reranker @search.reranker_score is in [0, 4]. ~1.0 is a soft "this is
# at least somewhat relevant" floor that filters obvious off-topic results
# while still surfacing fuzzy matches.
MIN_RERANK_SCORE = 1.0

# Action-aware rank order (highest first). A candidate with NO verdict (the judge
# skipped it or a call failed) is treated exactly like "uncertain": unknown is
# not a negative, so it must rank above anything the judge affirmatively called
# a non-match. (Previously unclassified ranked BELOW "mention_only", so verdicts
# the model dropped silently pushed real goal windows out of the top results.)
_MATCH_TYPE_RANK = {
    "scoring_sequence": 5,
    "celebration_only": 4,
    "uncertain": 3,
    "attempt_only": 2,
    "mention_only": 1,
    "unrelated": 0,
}
_UNKNOWN_RANK = _MATCH_TYPE_RANK["uncertain"]
# Candidates beyond the judged pool were never evaluated: no evidence either way, and they are
# semantically weaker than the pool. They share the judged non-match tier so they sort after every
# unknown/positive candidate, and within that tier fall back to semantic order.
_UNJUDGED_RANK = _MATCH_TYPE_RANK["mention_only"]
_ENTITY_MISMATCH_PENALTY = 10
# Types worth spending frames on first: the text judge believes the action/aftermath is in the clip.
_VISUAL_CHECK_TYPES = frozenset({"scoring_sequence", "celebration_only"})


def _action_rank(match_type: str | None, entity_match: bool | None, requires_entity: bool) -> int:
    """Pure ranking helper — isolated so it's unit-testable without network calls."""
    rank = _MATCH_TYPE_RANK.get(match_type or "", _UNKNOWN_RANK)
    if requires_entity and entity_match is False:
        rank -= _ENTITY_MISMATCH_PENALTY
    return rank


def _final_sort_key(it: SearchResultItem, requires_entity: bool, unjudged_ids: frozenset[str] = frozenset()) -> tuple:
    """Verdict tier first; within a tier, results whose frames were examined beat
    text-only ones (they survived scrutiny); then the semantic reranker, then BM25/vector."""
    rank = _UNJUDGED_RANK if it.id in unjudged_ids else _action_rank(it.match_type, it.entity_match, requires_entity)
    return (
        rank,
        1 if it.visual_checked else 0,
        it.rerank_score if it.rerank_score is not None else 0,
        it.score,
    )


def _apply_verdicts(items: list[SearchResultItem], verdicts: dict[str, dict], *, visual: bool) -> int:
    """Copy judge verdicts onto the results. A visual verdict that came back
    "uncertain" (inconclusive frames) never overwrites the text verdict."""
    applied = 0
    for it in items:
        verdict = verdicts.get(it.id)
        if not verdict:
            continue
        if visual and verdict.get("match_type") == "uncertain":
            continue
        it.match_type = verdict.get("match_type")
        it.entity_match = verdict.get("entity_match")
        it.match_reason = verdict.get("reason")
        if visual:
            it.visual_checked = True
            it.presentation = verdict.get("presentation")
        applied += 1
    return applied


def _interleave_by_video(items: list[SearchResultItem]) -> list[SearchResultItem]:
    """Round-robin across videos, keeping each video's own order, so one video (say the first half of
    a match) cannot take every slot of a shared pool before the other gets any."""
    by_video: dict[str, list[SearchResultItem]] = {}
    for it in items:
        by_video.setdefault(it.video_id, []).append(it)
    return [it for group in zip_longest(*by_video.values()) for it in group if it is not None]


def _visual_targets(
    items: list[SearchResultItem], limit: int, requires_entity: bool, *, balance_by_video: bool = False,
) -> list[SearchResultItem]:
    """Up to `limit` candidates to check against their frames, in the order they would currently
    be ranked: first those the text judge believes show the action, then, with any spare slots,
    the ones it could not decide ("uncertain" or no verdict). Garbled or missing commentary is
    common, and a real goal whose transcript says nothing is exactly what the frames can settle;
    the frame calls run in parallel, so spare slots cost no extra latency. Entity mismatches are
    skipped (they are demoted anyway). With `balance_by_video` the videos take turns, each in its own
    priority order, so no video is left without frame checks while another has several."""
    if limit <= 0:
        return []
    eligible = [it for it in items if not (requires_entity and it.entity_match is False)]
    positives = [it for it in eligible if it.match_type in _VISUAL_CHECK_TYPES]
    undecided = [it for it in eligible if it.match_type is None or it.match_type == "uncertain"]
    positives.sort(key=lambda it: _final_sort_key(it, requires_entity), reverse=True)
    undecided.sort(key=lambda it: _final_sort_key(it, requires_entity), reverse=True)
    ordered = positives + undecided
    return (_interleave_by_video(ordered) if balance_by_video else ordered)[:limit]


def _effective_visual_rerank(req: SearchRequest) -> tuple[bool, int]:
    """Resolve (enabled, max_candidates) from the request's advanced override,
    falling back to the global Settings defaults when the request didn't set it."""
    enabled = settings.search_visual_rerank_enabled if req.visual_rerank_enabled is None else req.visual_rerank_enabled
    max_candidates = (
        settings.search_visual_rerank_max_candidates
        if req.visual_rerank_max_candidates is None
        else req.visual_rerank_max_candidates
    )
    return enabled, max(0, max_candidates)


_MAX_CONFIRMATION_FRAMES = 4
_SECONDS_PER_FRAME = 6.0
# Windows longer than this are never decoded densely for a user-chosen frame count (the decode would
# read the whole window); they fall back to the keyframe fast path.
_DENSE_MAX_WINDOW_SECONDS = 45.0
# 640px is two 512px tiles on gpt-4o (~425 tokens/frame) and keeps enough detail for a
# broadcast wide shot; far cheaper than the previous detail="auto" billing (~8.5k on mini).
_FRAME_WIDTH = 640


def _frame_timestamps(
    start: float, end: float, max_frames: int = _MAX_CONFIRMATION_FRAMES, *, exact: bool = False,
) -> list[float]:
    """Evenly spaced sample points inside a candidate's clip range for visual
    confirmation. A short raw VI shot (2-4s) still gets 2 points, but a long
    windowed scene card (20-30s, common when a video was indexed with
    indexing_window_seconds set) gets up to `max_frames` so a few-second
    decisive moment (the actual goal, as opposed to the attempt/celebration/
    commentary around it) isn't likely to be missed entirely. With `exact`
    (the user chose a frame count) `max_frames` is the number of points
    whatever the clip length."""
    duration = max(0.0, end - start)
    if duration <= 0:
        return [max(0.0, start)]
    n = max_frames if exact else max(2, min(max_frames, round(duration / _SECONDS_PER_FRAME) + 1))
    return [start + duration * (i + 1) / (n + 1) for i in range(n)]


def _frames_for_window(sas_url: str, start: float, end: float, frames: int | None = None) -> list[bytes]:
    """Frames spread across one candidate's real scene window, chronological.

    Fast path: ONE ffmpeg process per window (keyframes) — the cost is per process, so this is ~9x
    cheaper than one process per frame on a 1-vCPU host. Fallback for windows with too few
    keyframes (short raw shots, unusual encodes): precise per-timestamp grabs.

    `frames` is a user-chosen count. Keyframes can be sparser than that, so the window is decoded
    densely instead (still one process)."""
    dense = bool(frames) and end - start <= _DENSE_MAX_WINDOW_SECONDS
    timestamps = _frame_timestamps(start, end, frames, exact=True) if frames else _frame_timestamps(start, end)
    options = {"keyframes_only": False} if dense else {}
    found = extract_window_frames(sas_url, start, end, len(timestamps), _FRAME_WIDTH, **options)
    if len(found) >= min(2, len(timestamps)):
        return found
    # The fallback is one process per frame, so it keeps to the automatic 2-4 even for a larger chosen count.
    return [f for f in (extract_frame_bytes(sas_url, t, _FRAME_WIDTH) for t in _frame_timestamps(start, end)) if f]


def _gather_confirmation_frames(
    items: list[SearchResultItem],
    scene_end_by_id: dict[str, float] | None = None,
    frames_per_candidate: int | None = None,
) -> dict[str, list[bytes]]:
    """Best-effort: grab 2-4 confirmation frames (scaled with scene duration,
    see _frame_timestamps; exactly `frames_per_candidate` when the user chose
    one) per candidate straight from its source video (not
    the precomputed scene thumbnail, so frames always land inside this exact
    candidate's time range).

    IMPORTANT: `it.end_seconds` is the PLAYBACK window (start + the user's
    clip_duration_seconds, which can pad a 2-4s shot out to 60s for a nicer
    player experience) — sampling frames from it can land well past the
    actual evidence into unrelated footage. `scene_end_by_id`, when given,
    is the real indexed scene/shot end and is what frame timestamps should
    be based on; it falls back to `it.end_seconds` if missing.

    Every failure mode (no blob_url, SAS minting failure, ffmpeg missing/
    timing out/erroring) is swallowed per-candidate — a candidate simply gets
    no entry (or fewer frames) rather than the whole search request failing.
    Callers must treat a missing/empty entry as "judge this one from text
    evidence alone", never as an error.
    """
    scene_end_by_id = scene_end_by_id or {}
    tasks: list[tuple[str, str, float, float]] = []
    sas_cache: dict[str, str] = {}
    for it in items:
        if not it.blob_url:
            continue
        sas = sas_cache.get(it.blob_url)
        if sas is None:
            try:
                sas = make_sas_url(it.blob_url)
            except Exception as e:  # noqa: BLE001
                log.warning("make_sas_url failed for visual confirmation: %s", e)
                sas = ""
            sas_cache[it.blob_url] = sas
        if not sas:
            continue
        tasks.append((it.id, sas, it.start_seconds, scene_end_by_id.get(it.id, it.end_seconds)))
    frames: dict[str, list[bytes]] = {}
    if not tasks:
        return frames
    # One ffmpeg per candidate; 6 at once is plenty (more only starves the web worker's CPU).
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(_frames_for_window, sas, start, end, frames_per_candidate): cid for cid, sas, start, end in tasks
        }
        for fut, cid in futures.items():
            try:
                found = fut.result(timeout=75)
            except Exception as e:  # noqa: BLE001
                log.warning("Frame extraction failed for %s: %s", cid, e)
                found = []
            if found:
                frames[cid] = found
    return frames


def _judge_input(it: SearchResultItem) -> dict:
    return {
        "id": it.id,
        "summary": it.summary,
        "transcript_excerpt": it.transcript_excerpt,
        "labels": it.labels,
        "entities": it.entities,
        "ocr_text": it.ocr_text,
    }


def _run_action_rerank(
    req: SearchRequest,
    rewrite: dict,
    items: list[SearchResultItem],
    scene_end_by_id: dict[str, float],
    requires_entity: bool,
    stats: dict[str, int] | None = None,
    balance_visual_by_video: bool = False,
) -> frozenset[str]:
    """Two-stage action-aware rerank; sets match_type/entity_match/match_reason/visual_checked in place.

    Stage 1 (text): judge the top `search_action_rerank_max_candidates` results from their text
    evidence. Stage 2 (frames, optional): for the best `visual_max` of those the text judge believes
    show the action (scoring/celebration), then any it could not decide, pull frames from their real
    scene window and re-judge them with the same model. A failed or inconclusive stage leaves the
    earlier verdict intact.

    Returns the ids of results that were never submitted to the judge (beyond the pool). When `stats`
    is given it receives `judged` (candidates the text judge answered for) and `frames` (candidates whose
    frames the judge answered for; extracted frames whose judge call failed do not count).
    `balance_visual_by_video` spreads the frame checks over the videos of a shared pool.
    """
    candidate_slice = items[: max(0, settings.search_action_rerank_max_candidates)]
    unjudged = frozenset(it.id for it in items[len(candidate_slice):])
    if not candidate_slice:
        return unjudged
    t0 = time.monotonic()
    text_verdicts = rerank_action_candidates(req.query, rewrite, [_judge_input(it) for it in candidate_slice])
    n_text = _apply_verdicts(candidate_slice, text_verdicts, visual=False)
    t_text = time.monotonic() - t0

    visual_enabled, visual_max = _effective_visual_rerank(req)
    targets = (
        _visual_targets(candidate_slice, visual_max, requires_entity, balance_by_video=balance_visual_by_video)
        if visual_enabled else []
    )
    n_frames = n_visual = n_answered = 0
    t_visual = 0.0
    if targets:
        t1 = time.monotonic()
        try:
            # Only passed when the user chose a count, so the automatic path keeps its two-argument call.
            gather_options = {"frames_per_candidate": req.visual_rerank_frames} if req.visual_rerank_frames else {}
            frames = _gather_confirmation_frames(targets, scene_end_by_id, **gather_options)
        except Exception as e:  # noqa: BLE001
            log.warning("Visual confirmation frame gathering failed: %s", e)
            frames = {}
        n_frames = len(frames)
        with_frames = [it for it in targets if frames.get(it.id)]
        if with_frames:
            visual_verdicts = rerank_action_candidates(
                req.query, rewrite, [_judge_input(it) for it in with_frames], frames=frames,
            )
            n_visual = _apply_verdicts(with_frames, visual_verdicts, visual=True)
            n_answered = len(visual_verdicts)
        t_visual = time.monotonic() - t1
    log.info(
        "Action-aware rerank q=%r: text %d/%d verdicts in %.1fs; visual %d verdicts from %d/%d candidates "
        "with frames in %.1fs",
        req.query, n_text, len(candidate_slice), t_text, n_visual, n_frames, len(targets), t_visual,
    )
    if stats is not None:
        stats.update(judged=n_text, frames=n_answered)
    return unjudged


def _select_embedding_texts(expanded_query: str, entities: list[str]) -> tuple[str, str | None]:
    """Split one query into (content_text, entity_text) for separate vector fields.

    `entity_text` is None when no entities were named, so the caller skips the
    entity_vector query entirely instead of diluting it with action-only text.
    """
    content_text = (expanded_query or "").strip()
    entity_text = ", ".join(e.strip() for e in entities if e and e.strip())
    return content_text, (entity_text or None)


def _build_vector_queries(
    content_vector: list[float] | None,
    entity_vector: list[float] | None,
    k: int,
) -> list[VectorizedQuery]:
    """Build only the vector queries we actually have embeddings for."""
    queries: list[VectorizedQuery] = []
    if content_vector:
        queries.append(VectorizedQuery(vector=content_vector, k_nearest_neighbors=k, fields="content_vector"))
    if entity_vector:
        queries.append(VectorizedQuery(vector=entity_vector, k_nearest_neighbors=k, fields="entity_vector"))
    return queries

ENTITY_FIELDS_FOR_FILTER = "entities,summary,transcript_excerpt,ocr_text,labels"


_TOKEN_RE = re.compile(r"[A-Za-z0-9'\u00C0-\u017F]+")


def _lucene_escape(token: str) -> str:
    """Escape Lucene reserved chars in a single token."""
    return re.sub(r'([+\-!(){}\[\]\^"~*?:\\/&|])', r"\\\1", token)


def _surname(name: str) -> str:
    parts = [p for p in _TOKEN_RE.findall(name) if p]
    return parts[-1] if parts else name


def _build_keyword_query(rewrite: dict, raw_query: str) -> str:
    """Compose a Lucene full-syntax query.

    - Required: each must_entity (matched on its surname OR full form, OR'd)
    - Optional but boosted: action synonyms
    - Always include the raw query terms so we don't lose the user's exact intent
    """
    parts: list[str] = []
    must_entities = rewrite.get("must_entities") or []
    for ent in must_entities:
        ent = (ent or "").strip()
        if not ent:
            continue
        full = _lucene_escape(ent)
        sn = _lucene_escape(_surname(ent))
        if sn.lower() == full.lower():
            parts.append(f"+({sn})")
        else:
            parts.append(f'+("{full}" OR {sn})')
    actions = rewrite.get("actions") or []
    if actions:
        action_terms = " OR ".join(_lucene_escape(a) for a in actions if a)
        if action_terms:
            parts.append(f"({action_terms})")
    if not parts:
        return raw_query
    parts.append(_lucene_escape(raw_query))
    return " ".join(parts)


def _build_entity_filter(rewrite: dict) -> str | None:
    must_entities = rewrite.get("must_entities") or []
    if not must_entities:
        return None
    clauses: list[str] = []
    for ent in must_entities:
        ent = (ent or "").strip()
        if not ent:
            continue
        sn = _surname(ent)
        full_lit = ent.replace("'", "''")
        sn_lit = sn.replace("'", "''")
        if sn.lower() == ent.lower():
            clauses.append(
                f"search.ismatchscoring('{sn_lit}', '{ENTITY_FIELDS_FOR_FILTER}', 'simple', 'any')"
            )
        else:
            clauses.append(
                "("
                f"search.ismatchscoring('\"{full_lit}\"', '{ENTITY_FIELDS_FOR_FILTER}', 'full', 'any')"
                f" or search.ismatchscoring('{sn_lit}', '{ENTITY_FIELDS_FOR_FILTER}', 'simple', 'any')"
                ")"
            )
    if not clauses:
        return None
    return " and ".join(clauses)


def _build_user_filter(filters: Any) -> str | None:
    """Combine UI filter chips into an OData filter string.

    `filters` is a `SearchFilters | None` (Pydantic). Each chip group within
    a category is OR'd; categories are AND'd.
    """
    if not filters:
        return None
    parts: list[str] = []
    if filters.video_ids:
        ors = " or ".join(f"video_id eq '{quote(v)}'" for v in filters.video_ids)
        parts.append(f"({ors})")
    if filters.content_types:
        ors = " or ".join(f"content_type eq '{quote(v)}'" for v in filters.content_types)
        parts.append(f"({ors})")
    if filters.labels:
        # `labels` is a Collection(Edm.String) — use any() lambdas
        ors = " or ".join(f"l eq '{quote(v)}'" for v in filters.labels)
        parts.append(f"labels/any(l: {ors})")
    return " and ".join(parts) if parts else None


def _combine_filters(*clauses: str | None) -> str | None:
    """AND together any non-empty filter clauses."""
    real = [c for c in clauses if c]
    if not real:
        return None
    if len(real) == 1:
        return real[0]
    return " and ".join(f"({c})" for c in real)


def _hits_to_items(
    results: Any, clip_duration: int,
) -> tuple[list[SearchResultItem], dict[str, list[str]], dict[str, float]]:
    items: list[SearchResultItem] = []
    speakers_by_id: dict[str, list[str]] = {}
    # The real indexed scene/shot end (NOT padded by clip_duration) — used to
    # keep visual-confirmation frame sampling inside the actual evidence
    # window instead of the padded playback window. See _gather_confirmation_frames.
    scene_end_by_id: dict[str, float] = {}
    for r in results:
        # Always play the requested length: VI shots are often only 2-4s long.
        clip_start = max(0.0, float(r.get("start_seconds") or 0))
        clip_end = clip_start + clip_duration
        scene_end = max(clip_start, float(r.get("end_seconds") or clip_start))

        blob_url = r.get("blob_url") or ""
        # IMPORTANT: do NOT append a #t= media fragment here. The browser's
        # built-in fragment seeking races with our explicit currentTime= seek
        # in the player and on Chrome the explicit seek is silently dropped.
        # The player handles seeking itself via start_seconds.
        play_url = proxy_blob_url(blob_url) if blob_url else ""

        item = SearchResultItem(
            id=r.get("id"),
            video_id=r.get("video_id"),
            filename=r.get("filename") or "",
            start_seconds=clip_start,
            end_seconds=clip_end,
            score=float(r.get("@search.score") or 0),
            rerank_score=float(r["@search.reranker_score"]) if r.get("@search.reranker_score") is not None else None,
            summary=r.get("summary") or "",
            transcript_excerpt=r.get("transcript_excerpt") or "",
            labels=r.get("labels") or [],
            entities=r.get("entities") or [],
            ocr_text=r.get("ocr_text") or "",
            thumbnail_url=proxy_blob_url(r.get("thumbnail_url") or ""),
            blob_url=blob_url,
            play_url=play_url,
        )
        items.append(item)
        speakers_by_id[item.id] = list(r.get("speakers") or [])
        scene_end_by_id[item.id] = scene_end
    return items, speakers_by_id, scene_end_by_id


def _drop_overlapping(
    items: list[SearchResultItem], scene_end_by_id: dict[str, float] | None = None,
) -> list[SearchResultItem]:
    """Keep only the best-ranked clip when hits from the same video overlap in time.

    By default the padded playback windows are compared (any overlap). With `scene_end_by_id` the real indexed
    scene windows are, and only a scene that mostly (over half of the shorter one) covers another counts: windowed
    indexing overlaps neighbouring scenes by a few seconds, and a long clip duration pads playback windows, so
    neither may make two distinct moments look like one."""
    def end_of(it: SearchResultItem) -> float:
        return scene_end_by_id.get(it.id, it.end_seconds) if scene_end_by_id else it.end_seconds

    def overlaps(a: SearchResultItem, b: SearchResultItem) -> bool:
        shared = min(end_of(a), end_of(b)) - max(a.start_seconds, b.start_seconds)
        if shared <= 0:
            return False
        if not scene_end_by_id:
            return True
        return shared > 0.5 * min(end_of(a) - a.start_seconds, end_of(b) - b.start_seconds)

    kept: list[SearchResultItem] = []
    for it in items:
        if not any(k.video_id == it.video_id and overlaps(k, it) for k in kept):
            kept.append(it)
    return kept


def _requested_event(req: SearchRequest) -> str | None:
    categories = {kind: label for profile in PROFILES.values() for kind, label in profile["events"].items()}
    if req.event_kind:
        if req.event_kind not in categories:
            raise HTTPException(422, "Unknown moment tag")
        return req.event_kind
    query = " ".join(req.query.casefold().strip(" .!?").split())
    for article in ("a ", "an ", "the ", "all "):
        if query.startswith(article):
            query = query[len(article):]
            break
    for kind, label in categories.items():
        if query in {kind.replace("_", " "), label.casefold(), label.casefold() + "s"}:
            return kind
    if "goal" in categories and query in {"scored goal", "scored goals"}:
        return "goal"
    return None


def _chronological(it: SearchResultItem) -> tuple[str, float]:
    return it.filename.casefold(), it.start_seconds


def _event_label(kind: str) -> str:
    return next((p["events"][kind] for p in PROFILES.values() if kind in p["events"]), kind.replace("_", " "))


# `top` of the internal hybrid request behind a deep search: 5x that many (100) candidates are retrieved.
_DEEP_POOL_TOP = 20
# Azure AI Search returns at most this many documents per request: the ceiling for a tag lookup.
_MAX_TAGGED_HITS = 1000
# The longest clip a search may return (SearchRequest.clip_duration_seconds is capped at the same value).
_MAX_CLIP_SECONDS = 60.0


def _same_moment(it: SearchResultItem, anchors: list[SearchResultItem], window: float) -> bool:
    """True when `it` starts within `window` seconds of an anchor in the same video."""
    return any(a.video_id == it.video_id and abs(a.start_seconds - it.start_seconds) < window for a in anchors)


def _deep_search(
    req: SearchRequest, kind: str, known: list[SearchResultItem], max_candidates: int,
) -> tuple[list[SearchResultItem], dict[str, float], dict[str, int]]:
    """Find moments of `kind` that the index-time tags missed.

    The index-time analysis reviews a budgeted, text-selected set of scenes per video, so real events can stay
    untagged (say a goal whose commentary ranked below the budget). This retrieves the best-matching clips for the
    event with the same hybrid search as a typed query, drops those that are the same moment as one already
    `known`, judges the rest from their text and checks the best `max_candidates` of them (spread over the
    videos) against their frames.

    Returns the clips whose frames confirm the action (earliest first inside a cluster, so a replay folds into
    the first view), their scene ends and the judge's stats."""
    query = EVENT_SEARCH_QUERIES.get(kind) or _event_label(kind).lower()
    deep_req = req.model_copy(update={
        "query": query, "mode": "hybrid", "event_kind": None, "top": _DEEP_POOL_TOP,
        "visual_rerank_enabled": True, "visual_rerank_max_candidates": max_candidates,
    })
    rewrite = {"entities": [], "actions": [], "expanded_query": query, "must_entities": []}
    pool = _retrieve_pool(deep_req, rewrite)
    window = settings.search_deep_merge_window_seconds
    fresh = _interleave_by_video([it for it in pool.items if not _same_moment(it, known, window)])
    stats = {"judged": 0, "frames": 0}
    _run_action_rerank(deep_req, rewrite, fresh, pool.scene_end_by_id, False, stats=stats, balance_visual_by_video=True)
    if fresh and not stats["judged"] and not stats["frames"]:
        # Not "no moments found": the judge model answered for no candidate at all (outage, quota).
        raise RuntimeError("the judge model returned no verdict for any candidate")
    confirmed = sorted(
        (it for it in fresh if it.match_type == "scoring_sequence" and it.visual_checked),
        key=lambda it: (it.video_id, it.start_seconds),
    )
    recovered: list[SearchResultItem] = []
    for it in confirmed:
        if not _same_moment(it, recovered, window):
            it.origin = "deep_search"
            # The frames were sampled across the whole scene, so the clip has to reach its end or the moment
            # that was seen could lie outside the clip that is played.
            scene_end = pool.scene_end_by_id.get(it.id, it.end_seconds)
            it.end_seconds = max(it.end_seconds, min(scene_end, it.start_seconds + _MAX_CLIP_SECONDS))
            recovered.append(it)
    return recovered, pool.scene_end_by_id, stats


def _search_tagged(req: SearchRequest, kind: str) -> SearchResponse:
    client = search_client(settings.search_index)
    event_filter = f"labels/any(label: label eq 'event:{quote(kind)}')"
    combined_filter = _combine_filters(event_filter, _build_user_filter(req.filters))
    try:
        # Every tagged hit, not an arbitrary 50: a `*` search has no order, so a smaller cut would drop moments
        # before the chronological sort and could even hide a tagged moment from the deep search's exclusion.
        results = client.search(
            search_text="*", filter=combined_filter, top=_MAX_TAGGED_HITS, select=SELECT_FIELDS,
            facets=["video_id,count:50", "content_type,count:20", "labels,count:30", "filename,count:50"],
        )
        hits = list(results)
        try:
            raw_facets = results.get_facets() or {}
        except Exception:
            raw_facets = {}
    except Exception as exc:
        raise HTTPException(503, "Tagged moment search is temporarily unavailable") from exc
    tagged_items, _, scene_end_by_id = _hits_to_items(hits, req.clip_duration_seconds)
    tagged_items.sort(key=_chronological)
    for it in tagged_items:
        it.origin = "tag"
    facets = {name: [FacetValue(value=str(value["value"]), count=int(value.get("count") or 0))
                     for value in values if value.get("value") is not None]
              for name, values in raw_facets.items()}
    tagged = _drop_overlapping(tagged_items, scene_end_by_id)

    # The tags come from a budgeted index-time pass, so they can miss moments. With visual confirmation switched on
    # for this request (never by default: it makes model calls) also look for untagged ones in the frames.
    recovered: list[SearchResultItem] = []
    stats: DeepSearchStats | None = None
    _, max_candidates = _effective_visual_rerank(req)
    if req.visual_rerank_enabled is True and max_candidates > 0 and settings.search_action_rerank_enabled:
        try:
            recovered, found_ends, judge = _deep_search(req, kind, tagged_items, max_candidates)
            scene_end_by_id.update(found_ends)
            stats = DeepSearchStats(tagged=len(tagged), recovered=len(recovered),
                                    candidates_judged=judge["judged"], frames_checked=judge["frames"])
        except Exception as exc:  # noqa: BLE001
            log.warning("Deep search for %r failed; returning the tagged moments only: %s", kind, exc)
            stats = DeepSearchStats(tagged=len(tagged), recovered=0, candidates_judged=0, frames_checked=0, failed=True)
        else:
            log.info("Deep search %r: %d tagged + %d recovered (%d judged, %d with frames)",
                     kind, len(tagged), len(recovered), stats.candidates_judged, stats.frames_checked)
    moments = _drop_overlapping(sorted(tagged + recovered, key=_chronological), scene_end_by_id)
    selected = moments[:req.top]
    return SearchResponse(query=req.query, event_kind=kind, count=len(selected), items=selected,
                          facets=facets, deep_search=stats, total_matches=len(moments))


@dataclass
class _Pool:
    """Candidates retrieved for one hybrid/vector/keyword query, before any judging."""
    items: list[SearchResultItem]
    speakers_by_id: dict[str, list[str]] = field(default_factory=dict)
    scene_end_by_id: dict[str, float] = field(default_factory=dict)
    raw_facets: dict[str, list[dict]] = field(default_factory=dict)
    used_filter: bool = False


def _retrieve_pool(req: SearchRequest, rewrite: dict) -> _Pool:
    """Steps 2-6 of a search: embed, retrieve a wide candidate pool, apply the reranker threshold."""
    index_name = settings.search_index
    client = search_client(index_name)
    expanded = rewrite.get("expanded_query") or req.query

    # 2. Embed content vs. entity SEPARATELY (intent-aware vectors). content_vector
    # gets the action/scene description; entity_vector gets ONLY the named entities
    # and is skipped entirely when none were named — a pure action query like
    # "goals being scored" no longer gets diluted by an entity vector with nothing
    # meaningful to match against.
    content_vector: list[float] | None = None
    entity_vector: list[float] | None = None
    if req.mode in ("vector", "hybrid"):
        content_text, entity_text = _select_embedding_texts(expanded, rewrite.get("entities") or [])
        texts_to_embed = [content_text] + ([entity_text] if entity_text else [])
        try:
            vecs = embed_texts(texts_to_embed)
            content_vector = vecs[0] if vecs else None
            entity_vector = vecs[1] if entity_text and len(vecs) > 1 else None
        except Exception as e:  # noqa: BLE001
            log.warning("Embedding failed; falling back to keyword: %s", e)
            content_vector = None
            entity_vector = None

    # 3. Build candidate pool — pull more, rerank, trim. Azure recommends k +
    # maxTextRecallSize sum to >= 50 so the semantic ranker gets a full pool.
    candidates = max(req.top * 5, 50)

    base_kwargs: dict[str, Any] = {
        "top": candidates,
        "select": SELECT_FIELDS,
        "include_total_count": True,
        "scoring_profile": SCORING_PROFILE,
        # Always request facets so the UI can populate filter chips from
        # the actual content of the matched result set.
        "facets": [
            "video_id,count:50",
            "content_type,count:20",
            "labels,count:30",
            "filename,count:50",
        ],
    }

    if req.mode == "hybrid":
        base_kwargs["search_text"] = _build_keyword_query(rewrite, req.query)
        base_kwargs["query_type"] = "semantic"
        base_kwargs["semantic_configuration_name"] = SEMANTIC_CONFIG
        base_kwargs["query_caption"] = "extractive"
        base_kwargs["query_answer"] = "extractive"
        base_kwargs["query_language"] = "en-us"
    elif req.mode == "keyword":
        base_kwargs["search_text"] = req.query
        base_kwargs["query_type"] = "full"
    else:  # vector
        base_kwargs["search_text"] = "*"

    if req.mode in ("vector", "hybrid"):
        # Advanced #6: multi-vector — fan the query out across both fields.
        # Azure AI Search merges results across vector queries via RRF
        # automatically, which boosts entity precision without sacrificing
        # broader content recall. Each field now gets its OWN embedding
        # (see step 2) instead of the same text duplicated across both.
        vector_queries = _build_vector_queries(content_vector, entity_vector, candidates)
        if vector_queries:
            base_kwargs["vector_queries"] = vector_queries

    # 4. First attempt: with entity pre-filter (when applicable) plus user filters
    entity_filter = _build_entity_filter(rewrite) if req.mode == "hybrid" else None
    user_filter = _build_user_filter(req.filters)
    combined_filter = _combine_filters(entity_filter, user_filter)
    items: list[SearchResultItem] = []
    speakers_by_id: dict[str, list[str]] = {}
    scene_end_by_id: dict[str, float] = {}
    used_filter = False
    raw_facets: dict[str, list[dict]] = {}
    if combined_filter:
        kwargs = dict(base_kwargs)
        kwargs["filter"] = combined_filter
        try:
            results = client.search(**kwargs)
            hits = list(results)
            try:
                raw_facets = results.get_facets() or {}
            except Exception:
                raw_facets = {}
            items, speakers_by_id, scene_end_by_id = _hits_to_items(hits, req.clip_duration_seconds)
            used_filter = True
            log.info("Filtered search returned %d hits (filter=%s)", len(items), combined_filter)
        except Exception as e:  # noqa: BLE001
            log.warning("Filtered search failed (%s); will retry without entity pre-filter", e)
            items = []

    # 5. Fallback: drop only the LLM-derived entity pre-filter; keep the user's explicit filters.
    if not items:
        kwargs = dict(base_kwargs)
        if user_filter:
            kwargs["filter"] = user_filter
        try:
            results = client.search(**kwargs)
            hits = list(results)
            try:
                raw_facets = results.get_facets() or {}
            except Exception:
                raw_facets = {}
        except Exception as e:  # noqa: BLE001
            log.exception("Search failed")
            raise HTTPException(500, f"Search failed: {e}")
        items, speakers_by_id, scene_end_by_id = _hits_to_items(hits, req.clip_duration_seconds)

    # 6. Apply reranker threshold (only meaningful when semantic was used)
    if req.mode == "hybrid" and items:
        scored = [it for it in items if (it.rerank_score is None or it.rerank_score >= MIN_RERANK_SCORE)]
        if scored:
            items = scored
    return _Pool(items, speakers_by_id, scene_end_by_id, raw_facets, used_filter)


@router.post("", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    event_kind = _requested_event(req)
    if event_kind:
        return _search_tagged(req, event_kind)
    if not req.query.strip():
        raise HTTPException(400, "Empty query")

    # 1. LLM-rewrite the query (only for hybrid; keyword/vector stay literal)
    rewrite = rewrite_query(req.query) if req.mode == "hybrid" else {
        "entities": [], "actions": [], "expanded_query": req.query, "must_entities": [],
    }
    log.info("Search rewrite: %r -> %s", req.query, rewrite)

    # 2-6. Retrieve the candidate pool.
    pool = _retrieve_pool(req, rewrite)
    items, speakers_by_id, scene_end_by_id = pool.items, pool.speakers_by_id, pool.scene_end_by_id
    raw_facets, used_filter = pool.raw_facets, pool.used_filter

    # 7. Action-aware reranker (hybrid only, fails open). Stage 1 asks a chat model to
    # judge a wide pool from TEXT evidence; stage 2 checks the few it believes with
    # real video frames. See `_run_action_rerank`. On any failure every affected
    # candidate keeps match_type=None, which ranks as "uncertain" (neutral).
    requires_entity = bool(rewrite.get("must_entities"))
    rerank_ran = req.mode == "hybrid" and bool(items) and settings.search_action_rerank_enabled
    unjudged_ids: frozenset[str] = frozenset()
    if rerank_ran:
        try:
            unjudged_ids = _run_action_rerank(req, rewrite, items, scene_end_by_id, requires_entity)
        except Exception as e:  # noqa: BLE001
            log.warning("Action-aware rerank failed unexpectedly; keeping semantic order: %s", e)

    # 8. Speaker-aware boost (advanced #4) — when the query named entities AND
    # those names appear in a scene's diarized speakers list, the scene is
    # likely an interview/quote BY that person, not just commentary about them.
    must_entities_lc = {(e or "").lower() for e in (rewrite.get("must_entities") or [])}
    if req.mode == "hybrid" and must_entities_lc:
        for it in items:
            spk_lc = {s.lower() for s in speakers_by_id.get(it.id, [])}
            # Match if any entity surname/full-name token appears in any speaker label
            hit = any(any(tok in s for tok in must_entities_lc) for s in spk_lc) if spk_lc else False
            if hit:
                if it.rerank_score is not None:
                    it.rerank_score = min(4.0, it.rerank_score * 1.25)
                it.score = it.score * 1.25

    if req.mode == "hybrid" and items:
        items.sort(key=lambda it: _final_sort_key(it, requires_entity, unjudged_ids), reverse=True)

    if rerank_ran:
        log.info("Final order (* = frames checked): %s", "; ".join(
            f"{it.video_id[:6]}@{int(it.start_seconds)}s={it.match_type or '-'}{'*' if it.visual_checked else ''}"
            for it in items[:10]
        ))

    items = _drop_overlapping(items)[: req.top]

    # Convert raw facet payloads into typed FacetValue lists for the UI.
    facets: dict[str, list[FacetValue]] = {}
    for name, values in (raw_facets or {}).items():
        facets[name] = [
            FacetValue(value=str(v.get("value")), count=int(v.get("count") or 0))
            for v in values
            if v.get("value") is not None
        ]

    log.info(
        "Search done: q=%r mode=%s used_filter=%s returned=%d facet_groups=%d",
        req.query, req.mode, used_filter, len(items), len(facets),
    )
    return SearchResponse(query=req.query, count=len(items), items=items, facets=facets)
