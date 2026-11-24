"""MomentLens vi-poller — polls Video Indexer for one video until done.

Inputs (env vars — set per execution by the API):
    STORAGE_ACCOUNT          Azure Storage account name
    VIDEOS_CONTAINER         Container holding videos + heartbeats (default "videos")
    INSIGHTS_CONTAINER       Container for the final VI Index JSON (default "insights")
    VIDEO_ID                 MomentLens video ID (folder under VIDEOS_CONTAINER)
    VI_VIDEO_ID              VI's videoId (returned from Videos POST)
    VI_LOCATION              VI account region (e.g. "westeurope")
    VI_ACCOUNT_ID            VI account ID (GUID)
    VI_ACCOUNT_NAME          VI ARM resource name
    VI_RESOURCE_GROUP        VI ARM resource group
    VI_API_VERSION           VI ARM API version (default "2024-01-01")
    AZURE_SUBSCRIPTION_ID    Subscription ID (for ARM generateAccessToken)
    POLL_TIMEOUT_MINUTES     Max polling window (default 240)
    POLL_INTERVAL_SECONDS    Base poll interval (default 30)
    AZURE_CLIENT_ID          Managed identity client ID

Outputs (written under VIDEOS_CONTAINER/<VIDEO_ID>/):
    vi-status.json           Heartbeat: {status, state, processingProgress, vi_video_id, updated_at}
                             status ∈ submitting | running | Succeeded | Failed
    Final VI Index JSON      Written to INSIGHTS_CONTAINER as <VIDEO_ID>/insights.json
                             on success.

The API (vi_poller_client) reads vi-status.json + the Container Apps Job
execution to drive the analyze stage. App Service restarts no longer
interrupt the poll.
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone

import httpx
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vi-poller")

# ── Env ────────────────────────────────────────────────────────────────
STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT"]
VIDEOS_CONTAINER = os.environ.get("VIDEOS_CONTAINER", "videos")
INSIGHTS_CONTAINER = os.environ.get("INSIGHTS_CONTAINER", "insights")
VIDEO_ID = os.environ["VIDEO_ID"]
VI_VIDEO_ID = os.environ["VI_VIDEO_ID"]
VI_LOCATION = os.environ["VI_LOCATION"]
VI_ACCOUNT_ID = os.environ["VI_ACCOUNT_ID"]
VI_ACCOUNT_NAME = os.environ["VI_ACCOUNT_NAME"]
VI_RESOURCE_GROUP = os.environ["VI_RESOURCE_GROUP"]
VI_API_VERSION = os.environ.get("VI_API_VERSION", "2024-01-01")
SUBSCRIPTION_ID = os.environ["AZURE_SUBSCRIPTION_ID"]
POLL_TIMEOUT_MINUTES = int(os.environ.get("POLL_TIMEOUT_MINUTES", "240"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")

STATUS_BLOB_NAME = f"{VIDEO_ID}/vi-status.json"
INSIGHTS_BLOB_NAME = f"{VIDEO_ID}/insights.json"
PROMPT_BLOB_NAME = f"{VIDEO_ID}/prompt-content.json"
# When set, after Index Succeeded we trigger VI's GPT-augmented PromptContent
# (per-segment narrative summaries). Free for Paid VI accounts within their
# free tier; otherwise billed at the VI PromptContent rate. Toggle with
# PROMPT_CONTENT_ENABLED=false to skip.
PROMPT_CONTENT_ENABLED = os.environ.get("PROMPT_CONTENT_ENABLED", "true").lower() == "true"
PROMPT_CONTENT_TIMEOUT_MINUTES = int(os.environ.get("PROMPT_CONTENT_TIMEOUT_MINUTES", "10"))
# `gpt-4o` is the safest default — VI also accepts gpt-4o-mini / others if the
# tenant has them whitelisted. Empty = let VI pick its current default.
PROMPT_CONTENT_MODEL = os.environ.get("PROMPT_CONTENT_MODEL", "")

# ── Auth ───────────────────────────────────────────────────────────────
cred = ManagedIdentityCredential(client_id=AZURE_CLIENT_ID) if AZURE_CLIENT_ID else ManagedIdentityCredential()
ACCOUNT_URL = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net"
blob_service = BlobServiceClient(account_url=ACCOUNT_URL, credential=cred)
ARM_SCOPE = "https://management.azure.com/.default"
VI_API_BASE = "https://api.videoindexer.ai"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _arm_token() -> str:
    return cred.get_token(ARM_SCOPE).token


def _vi_access_token(permission: str = "Reader", scope: str = "Account") -> str:
    """Mint a VI account access token via ARM."""
    url = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{VI_RESOURCE_GROUP}"
        f"/providers/Microsoft.VideoIndexer/accounts/{VI_ACCOUNT_NAME}"
        f"/generateAccessToken?api-version={VI_API_VERSION}"
    )
    body = {"permissionType": permission, "scope": scope}
    headers = {"Authorization": f"Bearer {_arm_token()}", "Content-Type": "application/json"}
    r = httpx.post(url, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    return r.json()["accessToken"]


def _write_status(**fields) -> None:
    payload = {
        "video_id": VIDEO_ID,
        "vi_video_id": VI_VIDEO_ID,
        "updated_at": _now_iso(),
        **fields,
    }
    try:
        blob_service.get_blob_client(VIDEOS_CONTAINER, STATUS_BLOB_NAME).upload_blob(
            json.dumps(payload).encode("utf-8"), overwrite=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("status upload failed: %s", e)


def _vi_request(method: str, url: str, *, timeout: int = 60, max_retries: int = 12) -> httpx.Response:
    """Mirror api/app/pipelines/video_indexer.py::_vi_request — honour 429/503 backoff."""
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
        wait_s = min(max(wait_s, 1.0), 120.0) + random.uniform(0, 2.0)
        log.warning("VI %s -> %s (attempt %d/%d), sleeping %.1fs",
                    url.split("?")[0], r.status_code, attempt + 1, max_retries, wait_s)
        time.sleep(wait_s)
        backoff = min(backoff * 2, 120.0)
    return r


def get_index() -> dict:
    token = _vi_access_token()
    url = (
        f"{VI_API_BASE}/{VI_LOCATION}/Accounts/{VI_ACCOUNT_ID}"
        f"/Videos/{VI_VIDEO_ID}/Index?accessToken={token}&language=English"
    )
    r = _vi_request("GET", url, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_prompt_content() -> dict | None:
    """Trigger VI's PromptContent GPT augmentation and poll until ready.

    PromptContent produces per-segment narrative summaries + a global summary
    via VI's hosted LLM. The response shape is:
      { "sections": [ { "id": int, "title": str, "content": str,
                         "start": "HH:MM:SS", "end": "HH:MM:SS" } ],
        "summary": str, ... }

    Returns None on failure / timeout (best-effort — the Index JSON is the
    source of truth; PromptContent is a quality bonus).
    """
    if not PROMPT_CONTENT_ENABLED:
        return None
    base = f"{VI_API_BASE}/{VI_LOCATION}/Accounts/{VI_ACCOUNT_ID}/Videos/{VI_VIDEO_ID}/PromptContent"
    # PromptContent requires a Contributor-scoped VI token to TRIGGER the job.
    # Reads after the job exists also accept Reader, but we use Contributor
    # throughout for simplicity (the MI is already Contributor on the VI account).
    token = _vi_access_token(permission="Contributor", scope="Account")
    qs = f"?accessToken={token}"
    if PROMPT_CONTENT_MODEL:
        qs += f"&modelName={PROMPT_CONTENT_MODEL}"

    # Step 1: POST to start (idempotent — VI returns existing job if already running)
    try:
        r = _vi_request("POST", base + qs, timeout=60)
    except Exception as e:  # noqa: BLE001
        log.warning("PromptContent POST failed: %s", e)
        return None
    if r.status_code in (200, 201, 202):
        log.info("PromptContent triggered (HTTP %s)", r.status_code)
    elif r.status_code == 409:
        # Already exists — fall through to GET
        log.info("PromptContent already exists, fetching")
    else:
        log.warning("PromptContent POST -> %s: %s", r.status_code, r.text[:200])
        return None

    # Step 2: Poll GET until status == Processed (or timeout)
    deadline = time.time() + PROMPT_CONTENT_TIMEOUT_MINUTES * 60
    while time.time() < deadline:
        try:
            token = _vi_access_token()
            r = _vi_request("GET", base + f"?accessToken={token}", timeout=60)
        except Exception as e:  # noqa: BLE001
            log.warning("PromptContent GET retrying after error: %s", e)
            time.sleep(15)
            continue
        if r.status_code == 404:
            time.sleep(10)
            continue
        if r.status_code != 200:
            log.warning("PromptContent GET -> %s", r.status_code)
            time.sleep(10)
            continue
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            time.sleep(10)
            continue
        state = (data.get("state") or data.get("status") or "")
        # VI returns the section skeleton (frame IDs only) almost immediately
        # — but the LLM-generated `content` field stays null until the GPT
        # augmentation actually finishes. Treat the response as ready ONLY
        # when at least one section has non-empty `content`, OR when VI
        # explicitly reports state=Processed.
        sections = data.get("sections") or []
        has_content = any((s.get("content") or "").strip() for s in sections)
        if state == "Processed" or has_content or (data.get("summary") or "").strip():
            log.info(
                "PromptContent ready: %d sections, %d with content",
                len(sections), sum(1 for s in sections if (s.get("content") or "").strip()),
            )
            return data
        if state in ("Failed", "Error"):
            log.warning("PromptContent reported failure: %s", str(data)[:300])
            return None
        log.info("PromptContent waiting (state=%s sections=%d content=%d)",
                 state or "?", len(sections), sum(1 for s in sections if (s.get("content") or "").strip()))
        time.sleep(15)
    log.warning("PromptContent timed out after %d min", PROMPT_CONTENT_TIMEOUT_MINUTES)
    return None


def main() -> int:
    log.info("vi-poller start: video=%s vi=%s timeout=%dm",
             VIDEO_ID, VI_VIDEO_ID, POLL_TIMEOUT_MINUTES)
    _write_status(status="submitting", state="queued", processingProgress=0)

    deadline = time.time() + POLL_TIMEOUT_MINUTES * 60
    last: dict = {}
    last_status_write = 0.0
    try:
        while time.time() < deadline:
            try:
                last = get_index()
            except Exception as e:  # noqa: BLE001
                log.warning("get_index failed (will retry): %s", e)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            v = (last.get("videos") or [{}])[0]
            state = v.get("state") or last.get("state") or "queued"
            raw = v.get("processingProgress") or last.get("processingProgress") or "0%"
            try:
                pct = int(str(raw).rstrip("%"))
            except Exception:
                pct = 0
            log.info("VI state=%s progress=%d%%", state, pct)

            if time.time() - last_status_write > 8:
                _write_status(status="running", state=state, processingProgress=pct)
                last_status_write = time.time()

            if state == "Processed":
                # Persist the full Index JSON to the insights container so the
                # API can pick it up without re-fetching from VI.
                blob_service.get_blob_client(INSIGHTS_CONTAINER, INSIGHTS_BLOB_NAME).upload_blob(
                    json.dumps(last, ensure_ascii=False).encode("utf-8"), overwrite=True,
                )
                # Mark Succeeded NOW so the API's analyze stage flips to
                # `indexed` without waiting for the (sometimes slow / often
                # absent) PromptContent generation.
                _write_status(status="Succeeded", state=state, processingProgress=100,
                              insights_blob=INSIGHTS_BLOB_NAME, prompt_blob=None)

                # Best-effort PromptContent — adds GPT-augmented per-segment
                # narratives. API consumes them as the segment summary when
                # present, otherwise falls back to its own enrich_scene LLM call.
                # Short clips / no-speech clips never produce content; we just
                # leave prompt_blob=null in that case.
                pc = fetch_prompt_content()
                if pc:
                    blob_service.get_blob_client(INSIGHTS_CONTAINER, PROMPT_BLOB_NAME).upload_blob(
                        json.dumps(pc, ensure_ascii=False).encode("utf-8"), overwrite=True,
                    )
                    _write_status(status="Succeeded", state=state, processingProgress=100,
                                  insights_blob=INSIGHTS_BLOB_NAME, prompt_blob=PROMPT_BLOB_NAME)
                    log.info("vi-poller done: video=%s insights=%s prompt=%s",
                             VIDEO_ID, INSIGHTS_BLOB_NAME, PROMPT_BLOB_NAME)
                else:
                    log.info("vi-poller done (no prompt content): video=%s insights=%s",
                             VIDEO_ID, INSIGHTS_BLOB_NAME)
                return 0
            if state == "Failed":
                err = json.dumps(last)[:1000]
                _write_status(status="Failed", state=state, error=f"VI processing failed: {err}")
                log.error("VI processing failed for %s", VI_VIDEO_ID)
                return 1
            time.sleep(POLL_INTERVAL_SECONDS + random.uniform(0, 6.0))
    except Exception as e:  # noqa: BLE001
        log.exception("vi-poller crashed: %s", e)
        _write_status(status="Failed", error=f"vi-poller crash: {e}")
        return 1

    _write_status(status="Failed", error=f"poll timeout after {POLL_TIMEOUT_MINUTES} min")
    log.error("vi-poller timeout for %s", VI_VIDEO_ID)
    return 1


if __name__ == "__main__":
    sys.exit(main())
