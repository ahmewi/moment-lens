"""VI-poller client — drives the `momentlens vi-poller` Container Apps Job.

Why this exists
---------------
Video Indexer ingestion takes ~1× video duration (sometimes longer when VI
is busy). Polling that loop in-process means an App Service worker recycle
mid-run leaves the analyze stage hung and the Search index empty. Moving
the poll into a dedicated job:
  * survives App Service restarts,
  * persists the final Index JSON to the insights container,
  * lets the API resume by reading a cheap heartbeat blob.

Lifecycle
---------
1. `start_poll(video_id, vi_video_id)`         → POST `<jobId>/start`
2. `wait_for_insights(video_id, exec_name)`    → polls `<video_id>/vi-status.json`
                                                 until status=Succeeded/Failed,
                                                 returns the parsed Index JSON.

The job runs under the same UAMI as the API, which already has VI Reader
and Storage Blob Data Contributor.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from azure.core.exceptions import ResourceNotFoundError

from app.azure_clients import aad_token, blob_service
from app.config import settings

log = logging.getLogger(__name__)

ARM_SCOPE = "https://management.azure.com/.default"
ARM_BASE = "https://management.azure.com"
JOB_API_VERSION = "2025-07-01"

_TEMPLATE_CACHE: dict[str, Any] | None = None


def _job_resource_id() -> str:
    if not (settings.azure_subscription_id
            and settings.vi_poller_resource_group
            and settings.vi_poller_job_name):
        raise RuntimeError(
            "vi-poller job is not configured (AZURE_SUBSCRIPTION_ID, "
            "VI_POLLER_RESOURCE_GROUP, VI_POLLER_JOB_NAME must be set)."
        )
    return (
        f"/subscriptions/{settings.azure_subscription_id}"
        f"/resourceGroups/{settings.vi_poller_resource_group}"
        f"/providers/Microsoft.App/jobs/{settings.vi_poller_job_name}"
    )


def _arm_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {aad_token(ARM_SCOPE)}",
        "Content-Type": "application/json",
    }


def _template_container() -> dict[str, Any]:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE
    url = f"{ARM_BASE}{_job_resource_id()}?api-version={JOB_API_VERSION}"
    r = httpx.get(url, headers=_arm_headers(), timeout=30)
    r.raise_for_status()
    containers = r.json().get("properties", {}).get("template", {}).get("containers", [])
    if not containers:
        raise RuntimeError("vi-poller job has no containers in its template.")
    _TEMPLATE_CACHE = containers[0]
    return _TEMPLATE_CACHE


def start_poll(video_id: str, vi_video_id: str) -> str:
    """Trigger one execution of the vi-poller job."""
    url = f"{ARM_BASE}{_job_resource_id()}/start?api-version={JOB_API_VERSION}"

    base = _template_container()
    overrides = {
        "VIDEO_ID": video_id,
        "VI_VIDEO_ID": vi_video_id,
    }
    merged_env: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in base.get("env", []) or []:
        name = e.get("name")
        if not name:
            continue
        if name in overrides:
            merged_env.append({"name": name, "value": overrides[name]})
        else:
            merged_env.append(e)
        seen.add(name)
    for k, v in overrides.items():
        if k not in seen:
            merged_env.append({"name": k, "value": v})

    container_override: dict[str, Any] = {
        "name": base.get("name", "vi-poller"),
        "image": base["image"],
        "env": merged_env,
    }
    if base.get("resources"):
        container_override["resources"] = base["resources"]
    if base.get("command"):
        container_override["command"] = base["command"]
    if base.get("args"):
        container_override["args"] = base["args"]

    body = {"containers": [container_override]}
    log.info("vi-poller start: video=%s vi=%s", video_id, vi_video_id)
    r = httpx.post(url, headers=_arm_headers(), json=body, timeout=60)
    if r.status_code not in (200, 202):
        log.error("vi-poller start failed: %s %s", r.status_code, r.text)
        r.raise_for_status()
    try:
        data = r.json()
    except Exception:
        data = {}
    exec_name = data.get("name") or r.headers.get("x-ms-request-id") or "(unknown)"
    log.info("vi-poller execution: %s", exec_name)
    return exec_name


def _execution_status(execution_name: str) -> str:
    if not execution_name:
        return ""
    url = (
        f"{ARM_BASE}{_job_resource_id()}/executions/{execution_name}"
        f"?api-version={JOB_API_VERSION}"
    )
    try:
        r = httpx.get(url, headers=_arm_headers(), timeout=30)
        if r.status_code == 404:
            return ""
        r.raise_for_status()
        body = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("vi-poller execution status failed for %s: %s", execution_name, e)
        return ""
    return str(
        body.get("properties", {}).get("status")
        or body.get("status")
        or ""
    )


def _read_status(video_id: str) -> dict[str, Any] | None:
    cc = blob_service().get_container_client(settings.azure_storage_videos_container)
    try:
        data = cc.get_blob_client(f"{video_id}/vi-status.json").download_blob().readall()
    except ResourceNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read vi-status for %s: %s", video_id, e)
        return None
    try:
        return json.loads(data)
    except Exception:  # noqa: BLE001
        return None


def read_insights(video_id: str) -> dict[str, Any]:
    """Fetch the persisted VI Index JSON for a completed video."""
    cc = blob_service().get_container_client(settings.azure_storage_insights_container)
    data = cc.get_blob_client(f"{video_id}/option1.json").download_blob().readall()
    return json.loads(data)


def read_prompt_content(video_id: str) -> dict[str, Any] | None:
    """Fetch the optional PromptContent JSON (GPT segment summaries).

    Returns None when the poller didn't produce one (PromptContent disabled,
    failed, or timed out — Index is the source of truth, this is a quality
    bonus). Callers must handle None.
    """
    cc = blob_service().get_container_client(settings.azure_storage_insights_container)
    try:
        data = cc.get_blob_client(f"{video_id}/option1-prompt.json").download_blob().readall()
    except ResourceNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read PromptContent for %s: %s", video_id, e)
        return None
    try:
        return json.loads(data)
    except Exception:  # noqa: BLE001
        return None


def wait_for_insights(
    video_id: str,
    execution_name: str | None = None,
    timeout_minutes: int | None = None,
    on_progress: Any = None,
    on_stage: Any = None,
) -> dict[str, Any]:
    """Block until the vi-poller execution finishes. Returns parsed Index JSON.

    Raises RuntimeError on failure / timeout. Reports VI processingProgress
    via on_progress (scaled to 25..85 to match the legacy in-process band)
    and on_stage("analyze", …) for the UI stepper.
    """
    timeout = (timeout_minutes or settings.vi_poller_timeout_minutes) * 60
    deadline = time.time() + timeout
    failure_streak = 0
    poll_interval = 10.0

    while time.time() < deadline:
        status = _read_status(video_id)
        if status:
            s = (status.get("status") or "")
            state = status.get("state") or "queued"
            pct = int(status.get("processingProgress") or 0)
            if on_progress:
                try: on_progress(25 + int(pct * 0.6))
                except Exception: pass
            if on_stage:
                try: on_stage("analyze", status="running", progress=pct, detail=f"VI: {state}")
                except Exception: pass
            if s == "Succeeded":
                if on_stage:
                    try: on_stage("analyze", status="running", progress=100, detail="VI: Processed")
                    except Exception: pass
                return read_insights(video_id)
            if s == "Failed":
                err = status.get("error") or "vi-poller reported failure"
                if on_stage:
                    try: on_stage("analyze", status="failed", error=err)
                    except Exception: pass
                raise RuntimeError(f"VI processing failed: {err}")

        if execution_name:
            exec_status = _execution_status(execution_name)
            if exec_status in ("Failed", "Stopped", "Degraded"):
                failure_streak += 1
                if failure_streak >= 2:
                    err = (status.get("error") if status else None) \
                          or f"vi-poller execution {exec_status}"
                    if on_stage:
                        try: on_stage("analyze", status="failed", error=err)
                        except Exception: pass
                    raise RuntimeError(f"vi-poller {execution_name}: {exec_status} — {err}")
            else:
                failure_streak = 0

        time.sleep(poll_interval)

    raise TimeoutError(
        f"vi-poller timed out after "
        f"{(timeout_minutes or settings.vi_poller_timeout_minutes)} min"
    )
