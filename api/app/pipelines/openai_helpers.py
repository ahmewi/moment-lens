"""Embeddings + LLM helpers (Azure OpenAI)."""
from __future__ import annotations

import base64
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NamedTuple

from app.azure_clients import judge_client, openai_client
from app.config import settings

log = logging.getLogger(__name__)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns list of vectors."""
    if not texts:
        return []
    client = openai_client()
    # Azure embeddings: batch up to 16 by default
    out: list[list[float]] = []
    BATCH = 16
    for i in range(0, len(texts), BATCH):
        batch = [t[:8000] if t else " " for t in texts[i:i + BATCH]]
        resp = client.embeddings.create(input=batch, model=settings.azure_openai_embedding_deployment)
        out.extend([d.embedding for d in resp.data])
    return out


# Hidden reasoning counts against `max_completion_tokens`, so a reasoning model needs headroom above the answer budget.
_CHAT_REASONING_HEADROOM = 1500


def _chat_params(max_tokens: int, temperature: float) -> dict:
    """Sampling and length parameters for a call to the `chat` deployment.

    Classic chat models take `temperature` and `max_tokens`. Reasoning-style models reject both, so setting
    AZURE_OPENAI_CHAT_REASONING_EFFORT switches to `max_completion_tokens` and `reasoning_effort`.
    """
    effort = settings.azure_openai_chat_reasoning_effort.strip().lower()
    if effort:
        return {"max_completion_tokens": max_tokens + _CHAT_REASONING_HEADROOM, "reasoning_effort": effort}
    return {"temperature": temperature, "max_tokens": max_tokens}


SCENE_CARD_SYSTEM_PROMPT = """You are a sports/broadcast video analyst.
Given a transcript window and detected entities/labels for a video segment, produce a concise scene card as JSON.
Be neutral, factual, and short. Do not invent facts not supported by the inputs."""


def summarize_scene(transcript: str, labels: list[str], entities: list[str], ocr: str = "") -> str:
    """Use the chat model to produce a 1-2 sentence summary."""
    client = openai_client()
    user = json.dumps({
        "transcript_excerpt": transcript[:2000],
        "labels": labels[:30],
        "entities": entities[:30],
        "ocr_text": ocr[:500],
    }, ensure_ascii=False)
    resp = client.chat.completions.create(
        model=settings.azure_openai_chat_deployment,
        messages=[
            {"role": "system", "content": SCENE_CARD_SYSTEM_PROMPT + "\nReturn JSON: {\"summary\": str}"},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        **_chat_params(max_tokens=200, temperature=0.2),
    )
    try:
        data = json.loads(resp.choices[0].message.content or "{}")
        return data.get("summary", "")[:500]
    except Exception:
        return ""

ENRICH_SYSTEM_PROMPT = """You are a sports/broadcast video analyst. Given a transcript window plus
any provided detected labels/entities/OCR for ONE video segment, produce a precise scene card.

Rules:
- Use ONLY information present in the inputs. Do NOT invent player names, teams, or actions.
- "entities": ONLY proper nouns ACTUALLY mentioned/visible in this segment (player names like
  "Sam Rivera", team names like "Northfield FC", coaches, brands on jerseys/boards). Prefer the
  full canonical name when known. Empty list is fine.
- "labels": short generic action/scene tags (e.g. "goal", "free kick", "penalty", "save",
  "celebration", "tackle", "offside", "corner kick", "red card", "interview"). 1-3 words each.
- "summary": 1-2 sentence neutral factual description of WHAT IS HAPPENING in this segment.
  No speculation."""

# News / politics variant — extracts personas, news topic and a factual description.
# Used when content_type == "news_politics".
ENRICH_NEWS_SYSTEM_PROMPT = """You are a news and current-affairs video analyst. Given a transcript
window plus any provided detected labels/entities/OCR for ONE video segment, produce a precise
news scene card.

Rules:
- Use ONLY information present in the inputs. Do NOT invent names, organizations, or events.
- "entities": named PERSONAS and bodies ACTUALLY mentioned/visible — politicians, officials,
  spokespeople, anchors, reporters, organizations, governments, parties, countries, and places
  (e.g. "Jordan Reyes", "City Council", "National Assembly", "Norway"). Prefer the full
  canonical name. Empty list is fine.
- "labels": short news topic/category tags describing the story (e.g. "diplomacy", "elections",
  "economy", "conflict", "summit", "legislation", "protest", "press conference", "interview",
  "breaking news"). 1-3 words each.
- "summary": 2-3 sentence neutral, factual description of the NEWS STORY in this segment — who is
  involved, what is happening, and any stated outcome or context. No opinion, no speculation."""


# Visual scene-description variant — used for content_type in {"news", "documentary"}.
# Emphasises a richer description of WHAT IS HAPPENING ON SCREEN (people's actions, scene
# events, and notable physical objects like airplanes, ships, vehicles, animals, etc.) rather
# than political personas alone. Useful for news b-roll, packages, and documentaries where
# the editorial value is in the visual story, not just named individuals.
ENRICH_VISUAL_SCENE_SYSTEM_PROMPT = """You are a video scene-description analyst for news and
documentary content. Given a transcript window plus any provided detected labels/entities/OCR
for ONE video segment, produce a precise, visually rich scene card.

Rules:
- Use ONLY information present in the inputs. Do NOT invent names, places, or events.
- "summary": 3-5 sentences of neutral, factual description of WHAT IS VISUALLY HAPPENING in
  this segment. Cover (a) the setting / location (studio, street, war zone, savanna, lab,
  port, etc.), (b) WHO is on screen and what they are physically DOING — actions, gestures,
  interactions (e.g. "an anchor reads from a desk", "a reporter walks through rubble while
  speaking to camera", "a herd of elephants drinks at a watering hole", "a fighter jet
  takes off from a carrier deck"), and (c) any NOTABLE OBJECTS or vehicles visible
  (airplanes, helicopters, ships, tanks, microphones, podiums, flags, animals, instruments,
  banners, machinery, etc.). Stay strictly factual.
- "entities": named proper nouns ACTUALLY mentioned or clearly visible in this segment —
  people (anchors, reporters, officials, experts, public figures), organizations,
  governments, named places, species, named events. Use the canonical full form when
  known. Empty list is fine.
- "labels": short generic tags describing the scene content. Mix three flavours: people
  actions ("interview", "press conference", "patrolling", "fleeing"), notable objects
  ("airplane", "warship", "elephant", "drone", "microscope"), and setting / scene type
  ("studio", "aerial footage", "archival", "wildlife", "war zone", "lab", "rally"). 1-3
  words each, up to 10 labels."""


def enrich_scene(
    transcript: str,
    labels: list[str],
    entities: list[str],
    ocr: str = "",
    content_type: str | None = None,
) -> dict:
    """LLM-extract {summary, entities, labels} for a scene. Resilient to failures.

    `content_type` selects the extraction lens:
    - "news" / "documentary" → visual scene-description prompt (people actions, notable
      objects, setting). Richer "what is happening on screen" output.
    - "news_politics" → persona + news-topic prompt (politically focused).
    - everything else → sports/broadcast prompt.
    """
    client = openai_client()
    if content_type in {"news", "documentary"}:
        system_prompt = ENRICH_VISUAL_SCENE_SYSTEM_PROMPT
    elif content_type == "news_politics":
        system_prompt = ENRICH_NEWS_SYSTEM_PROMPT
    else:
        system_prompt = ENRICH_SYSTEM_PROMPT
    user = json.dumps({
        "transcript_excerpt": transcript[:2000],
        "detected_labels": labels[:30],
        "detected_entities": entities[:30],
        "ocr_text": ocr[:500],
    }, ensure_ascii=False)
    try:
        # Visual scene-description prompts ask for 3-5 sentences; give them headroom.
        is_visual = content_type in {"news", "documentary"}
        max_tokens = 700 if is_visual else 350
        summary_cap = 1200 if is_visual else 500
        resp = client.chat.completions.create(
            model=settings.azure_openai_chat_deployment,
            messages=[
                {"role": "system", "content": system_prompt
                    + '\nReturn JSON exactly: {"summary": str, "entities": [str], "labels": [str]}'},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            **_chat_params(max_tokens=max_tokens, temperature=0.1),
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        return {
            "summary": str(data.get("summary", ""))[:summary_cap],
            "entities": [str(x)[:80] for x in (data.get("entities") or []) if x][:20],
            "labels":   [str(x)[:60] for x in (data.get("labels")   or []) if x][:20],
        }
    except Exception as e:  # noqa: BLE001
        log.warning("enrich_scene failed: %s", e)
        return {"summary": "", "entities": [], "labels": []}


QUERY_REWRITE_SYSTEM_PROMPT = """You rewrite a short user search query for a sports video search engine.
Output JSON. Be conservative and faithful to the user's intent.

Fields:
- "entities": proper nouns the user clearly named (people, teams, brands). Use the canonical
  full form when obvious (e.g. "rivera" -> "Sam Rivera"). Empty list if user named nobody.
- "actions":  small list of action/event terms implied by the query (e.g. for "scoring":
  ["goal","scores","scored","scoring","finish","strike"]). Only list synonyms of the action,
  not generic sport terms.
- "expanded_query": a SHORT natural-language phrase combining everything (used as the
  embedding/semantic input). Example: "Sam Rivera scoring a goal".
- "must_entities": subset of entities the result MUST mention to be relevant (typically the
  same as entities). Empty if no entity was named."""


def rewrite_query(query: str) -> dict:
    """Return {entities, actions, expanded_query, must_entities}. Best-effort; resilient."""
    q = (query or "").strip()
    if not q:
        return {"entities": [], "actions": [], "expanded_query": "", "must_entities": []}
    client = openai_client()
    try:
        resp = client.chat.completions.create(
            model=settings.azure_openai_chat_deployment,
            messages=[
                {"role": "system", "content": QUERY_REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": q},
            ],
            response_format={"type": "json_object"},
            **_chat_params(max_tokens=250, temperature=0.0),
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        return {
            "entities":       [str(x)[:80] for x in (data.get("entities")       or []) if x][:8],
            "actions":        [str(x)[:40] for x in (data.get("actions")        or []) if x][:12],
            "expanded_query": str(data.get("expanded_query") or q)[:300],
            "must_entities":  [str(x)[:80] for x in (data.get("must_entities")  or []) if x][:5],
        }
    except Exception as e:  # noqa: BLE001
        log.warning("rewrite_query failed: %s", e)
        return {"entities": [], "actions": [], "expanded_query": q, "must_entities": []}


RERANK_SYSTEM_PROMPT = """You are a sports/broadcast video search quality judge. You are given a
user's search query and one or more candidate video clips. Each candidate has machine-generated text
evidence (summary, transcript excerpt, detected labels, detected entities, OCR text) and, for some
candidates, image frames sampled from the clip. Decide, for EACH candidate, whether the clip actually
contains what the user asked for.

Text evidence rules:
- Summaries and labels are machine-generated and often wrong. A generic object label (for example
  "goal" meaning a goalpost, not a scoring event) is NOT proof the action happened. A summary that
  claims the action must be backed by the transcript, OCR text, frames or a specific action label;
  otherwise treat the claim as unverified.
- Excited commentary is not proof that the action happened: commentators narrate shots, near-misses and
  saves in the same tone as goals. Count commentary as evidence only when it states the outcome (for a goal
  query: "goal", "he scores", "it's 1-0", "equalizer"), not when it merely describes the attempt or a chance.
- Never invent evidence that isn't present.

Classify "match_type" as exactly one of:
- "scoring_sequence": the requested action itself happens in the clip (for a goal query: the ball goes
  into the net, a replay of that goal, or commentary narrating the goal as it happens).
- "celebration_only": only the AFTERMATH of the action is in the clip (for a goal query: players
  celebrating, a GOAL banner, a scoreboard change, or reaction to a goal that just happened) without
  the ball going in.
- "attempt_only": an attempt at the action occurs but fails (for a goal query: a shot or header that is
  saved, blocked, hits the post or bar, goes wide or over, is offside, or is disallowed).
- "mention_only": the clip only talks about the action, the people, statistics, history or predictions
  (studio, interview, commentary about other moments) without the action or its aftermath happening in
  it.
- "unrelated": the clip does not support the query at all.
- "uncertain": the evidence is too ambiguous or insufficient to decide.

Image frames (only for candidates that have them):
- Frames are samples from the clip in chronological order. Treat what they show as direct evidence: the
  ball in the net, the net bulging, a replay of a goal, a GOAL banner or scoreboard change, players
  celebrating or hugging after scoring => goal evidence. A shot saved by the goalkeeper, blocked, off the
  woodwork, wide or over with no celebration => attempt_only. Studio, interview, graphics, stats boards,
  crowd, bench, or players just standing or walking => no action.
- Frames are sparse and can miss a split-second moment, so frames without goal evidence do not prove that
  no goal happened: if they are inconclusive, keep the verdict the text supports. But when the frames
  clearly show something different from what the text claims (for example the text claims a goal but the
  frames show only a saved shot, or a studio), trust the frames.
- For a candidate with frames also give "presentation": "live" when the frames show the action as it
  first happens, "replay" when they show a repeat of it (replay or logo-wipe graphics, slow motion, a
  second angle of the same action, a highlights package), "unknown" when the frames do not clearly show
  the action (the verdict then rests on the text). Omit it for candidates without frames.

"entity_match": true only if the query names a specific person/team/brand AND the evidence confirms that
exact entity is involved in this candidate; false if the query names an entity but the evidence clearly
points to a DIFFERENT entity; null if the query named no entity or the evidence doesn't mention any
relevant entity.
"evidence": a short phrase (max 15 words) copied from the candidate's text inputs, or a brief factual
description of what the frames show. Empty string if none.
"reason": one short sentence (max 20 words) explaining the classification. Keep both fields short: the
answer is read by people scanning many results and long answers slow every search down.

Return a JSON object with the EXACT key "results": a list with ONE entry per candidate id you were given,
each shaped {"id": str, "match_type": str, "entity_match": bool or null, "evidence": str, "reason": str}
plus "presentation" for candidates with frames.
Use the candidate ids EXACTLY as given. Do not invent ids. Do not omit any candidate id."""

_RERANK_SUMMARY_CHARS = 300
_RERANK_TRANSCRIPT_CHARS = 400
_RERANK_OCR_CHARS = 150
_RERANK_VALID_MATCH_TYPES = {
    "scoring_sequence", "celebration_only", "attempt_only", "mention_only", "unrelated", "uncertain",
}
_RERANK_VALID_PRESENTATIONS = {"live", "replay", "unknown"}
# Text-only candidates are judged in small batches: a single 20-candidate call with images dropped
# 5-7 of the 20 verdicts in production, while small text-only batches return every id. Output tokens
# dominate latency, so more, smaller parallel calls finish sooner (measured on a pool of 40: 8 per
# call = 10.4s for the whole stage, 4 per call = 7.3s). The pool of 40 then needs 10 workers.
_RERANK_BATCH_SIZE = 4
_RERANK_MAX_WORKERS = 12
# The SDK default is 10 minutes; a hung judge call must not hold a search request that long.
_RERANK_TIMEOUT_S = 45.0
# Reasoning models count their hidden reasoning against `max_completion_tokens`; without headroom
# a long deliberation would leave an empty answer. The cap is only an upper bound, not a cost.
_RERANK_REASONING_HEADROOM = 2000


class _JudgeTarget(NamedTuple):
    client: Any
    model: str
    detail: str | None  # image `detail` for frames
    reasoning_effort: str  # "" = classic chat parameters (temperature / max_tokens)


def _judge_target() -> _JudgeTarget:
    """Resolve the model that judges candidates (text evidence AND frames) from settings."""
    model = settings.azure_openai_judge_deployment or settings.azure_openai_chat_deployment
    dedicated = model != settings.azure_openai_chat_deployment or bool(settings.azure_openai_judge_endpoint)
    # detail="high" on a <=640px frame is a single/double tile (~255-425 tokens on gpt-4o, ~290 on
    # newer vision models). On gpt-4o-mini the same setting bills ~8.5k tokens per frame, so the fallback
    # to the chat model must use "low" (~2.8k).
    overridden = bool(settings.azure_openai_judge_endpoint or settings.azure_openai_judge_api_version)
    return _JudgeTarget(
        client=judge_client() if overridden else openai_client(),
        model=model,
        detail="high" if dedicated else "low",
        reasoning_effort=settings.azure_openai_judge_reasoning_effort.strip().lower() if dedicated else "",
    )


def _completion_params(target: _JudgeTarget, n_candidates: int) -> dict:
    answer_budget = 150 * n_candidates + 200
    if target.reasoning_effort:
        # Reasoning-style models: no `temperature` other than the default, no `max_tokens`.
        return {
            "max_completion_tokens": answer_budget + _RERANK_REASONING_HEADROOM,
            "reasoning_effort": target.reasoning_effort,
        }
    return {"temperature": 0.0, "max_tokens": answer_budget}


def _candidate_payload(c: dict) -> dict | None:
    cid = str(c.get("id") or "")
    if not cid:
        return None
    return {
        "id": cid,
        "summary": str(c.get("summary") or "")[:_RERANK_SUMMARY_CHARS],
        "transcript_excerpt": str(c.get("transcript_excerpt") or "")[:_RERANK_TRANSCRIPT_CHARS],
        "labels": list(c.get("labels") or [])[:10],
        "entities": list(c.get("entities") or [])[:10],
        "ocr_text": str(c.get("ocr_text") or "")[:_RERANK_OCR_CHARS],
    }


def _judge_call(
    target: _JudgeTarget,
    q: str,
    rewrite: dict,
    payloads: list[dict],
    frames: dict[str, list[bytes]],
) -> dict[str, dict]:
    """One chat call judging `payloads`. Fails open: returns {} on any error."""
    model = target.model
    valid_ids = {p["id"] for p in payloads}
    preamble = {
        "query": q,
        "named_entities": rewrite.get("entities") or [],
        "action_terms": rewrite.get("actions") or [],
    }
    user_content: str | list[dict]
    if any(frames.get(p["id"]) for p in payloads):
        # Each candidate's text block is immediately followed by its own frames so the
        # model can associate every image with the right id.
        content: list[dict] = [{"type": "text", "text": json.dumps(preamble, ensure_ascii=False)}]
        for p in payloads:
            content.append({"type": "text", "text": json.dumps({"candidate": p}, ensure_ascii=False)})
            for frame_bytes in frames.get(p["id"]) or []:
                image = {"url": "data:image/jpeg;base64," + base64.b64encode(frame_bytes).decode("ascii")}
                if target.detail:
                    image["detail"] = target.detail
                content.append({"type": "image_url", "image_url": image})
        user_content = content
    else:
        user_content = json.dumps({**preamble, "candidates": payloads}, ensure_ascii=False)
    started = time.monotonic()
    try:
        resp = target.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RERANK_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            timeout=_RERANK_TIMEOUT_S,
            **_completion_params(target, len(payloads)),
        )
        choice = resp.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            log.warning("rerank judge answer truncated by the token cap (%s, %d candidates)", model, len(payloads))
        usage = getattr(resp, "usage", None)
        log.info(
            "rerank judge %s: %d candidate(s), %d with frames, %.1fs, tokens prompt=%s completion=%s reasoning=%s",
            model, len(payloads), sum(1 for p in payloads if frames.get(p["id"])), time.monotonic() - started,
            getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
            getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None),
        )
        data = json.loads(choice.message.content or "{}")
        out: dict[str, dict] = {}
        for entry in (data.get("results") or []):
            if not isinstance(entry, dict):
                continue
            cid = str(entry.get("id") or "")
            if cid not in valid_ids or cid in out:
                continue
            match_type = str(entry.get("match_type") or "").strip().lower()
            if match_type not in _RERANK_VALID_MATCH_TYPES:
                match_type = "uncertain"
            entity_match = entry.get("entity_match")
            if not isinstance(entity_match, bool):
                entity_match = None
            out[cid] = {
                "match_type": match_type,
                "entity_match": entity_match,
                "evidence": str(entry.get("evidence") or "")[:300],
                "reason": str(entry.get("reason") or "")[:300],
            }
            presentation = str(entry.get("presentation") or "").strip().lower()
            # Only candidates judged from frames can have one; an absent/invalid value is simply left out.
            if presentation in _RERANK_VALID_PRESENTATIONS and frames.get(cid):
                out[cid]["presentation"] = presentation
        return out
    except Exception as e:  # noqa: BLE001
        ids = [p["id"] for p in payloads]
        log.warning("rerank_action_candidates call failed (%s, %d candidates, ids=%s): %s",
                    model, len(payloads), ids[:3] + (["..."] if len(ids) > 3 else []), e)
        return {}


def rerank_action_candidates(
    query: str,
    rewrite: dict,
    candidates: list[dict],
    frames: dict[str, list[bytes]] | None = None,
) -> dict[str, dict]:
    """Judge whether each candidate's evidence supports the query's action/entity.

    `candidates` is a list of dicts with at least an "id" plus any of
    "summary"/"transcript_excerpt"/"labels"/"entities"/"ocr_text". `frames`, if given, maps a
    candidate id to raw JPEG bytes sampled from that candidate's own time range.

    Candidates WITH frames are judged one per call (so every image is unambiguously tied to one
    candidate and no verdict can be dropped); candidates without are judged from text in small
    batches. Both use the judge deployment (`AZURE_OPENAI_JUDGE_*`, falling back to the chat
    deployment) and all calls run concurrently.
    Returns {id: {"match_type", "entity_match", "evidence", "reason"}} containing ONLY ids from
    the input; ids the model skipped are simply absent.

    Fails open per call: a failed call just leaves its candidates without a verdict. Callers
    must treat a missing id as "no verdict" and keep the prior ranking for it.
    """
    q = (query or "").strip()
    payloads = [p for p in (_candidate_payload(c) for c in candidates) if p]
    if not q or not payloads:
        return {}
    frames = frames or {}
    target = _judge_target()

    jobs: list[list[dict]] = [[p] for p in payloads if frames.get(p["id"])]
    text_only = [p for p in payloads if not frames.get(p["id"])]
    jobs += [text_only[i:i + _RERANK_BATCH_SIZE] for i in range(0, len(text_only), _RERANK_BATCH_SIZE)]
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(jobs), _RERANK_MAX_WORKERS)) as pool:
        futures = [pool.submit(_judge_call, target, q, rewrite, batch, frames) for batch in jobs]
        for fut in futures:
            for cid, verdict in fut.result().items():
                out.setdefault(cid, verdict)
    return out
