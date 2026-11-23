"""Job tracker with optional Azure Table persistence.

Behaviour:
  * Always maintains an in-process cache so reads stay sub-millisecond and the
    background pipeline threads are unaffected by Table latency / outages.
  * When `JOBS_TABLE_ENABLED=true` AND `AZURE_STORAGE_ACCOUNT` is configured,
    every `update()` mirrors the row into Azure Table Storage, and the cache
    is back-filled at startup from whatever the table already holds. This lets
    the API survive App Service restarts and run with multiple workers without
    losing visibility into in-flight or recently completed jobs.

Schema (one row per video):
  PartitionKey = video_id
  RowKey       = "job"
  + every other ProcessJob field, JSON-strings for None where needed.

Failures talking to the table are logged and swallowed — the in-memory state
stays correct so callers never see an exception just because the table is
temporarily unreachable.
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import settings
from app.models import (
    STAGE_ORDER,
    ProcessingStatus,
    ProcessJob,
    StageInfo,
    StageName,
    StageStatus,
)

log = logging.getLogger(__name__)

_ROW_KEY = "job"

_jobs: dict[str, ProcessJob] = {}
_lock = threading.Lock()
_table_loaded = False
_table_load_lock = threading.Lock()


# ── Table-storage backing (best-effort) ─────────────────────────────────

def _table_enabled() -> bool:
    return bool(settings.jobs_table_enabled and settings.azure_storage_account and settings.jobs_table_name)


_table_client = None
_table_client_lock = threading.Lock()


def _table():
    """Return a TableClient for the jobs table, creating the table if needed."""
    global _table_client
    if not _table_enabled():
        return None
    if _table_client is not None:
        return _table_client
    with _table_client_lock:
        if _table_client is not None:
            return _table_client
        try:
            from app.azure_clients import table_service
            svc = table_service()
            try:
                svc.create_table_if_not_exists(settings.jobs_table_name)
            except Exception as e:  # noqa: BLE001
                log.warning("create_table_if_not_exists(%s) ignored: %s", settings.jobs_table_name, e)
            _table_client = svc.get_table_client(settings.jobs_table_name)
            log.info("Job table backing enabled: %s", settings.jobs_table_name)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not initialise job table client: %s", e)
            _table_client = None
    return _table_client


def _job_to_entity(job: ProcessJob) -> dict[str, Any]:
    return {
        "PartitionKey": job.video_id,
        "RowKey": _ROW_KEY,
        "status": job.status,
        "job_ref": job.job_ref or "",
        "progress": int(job.progress),
        "error": (job.error or "")[:32000],  # Table string column max
        "insights_url": job.insights_url or "",
        "stage": job.stage or "",
        "stages_json": json.dumps([s.model_dump() for s in job.stages])[:32000],
    }


def _entity_to_job(e: dict[str, Any]) -> ProcessJob:
    raw_stages = e.get("stages_json") or "[]"
    stages: list[StageInfo] = []
    try:
        for s in json.loads(raw_stages) or []:
            try:
                stages.append(StageInfo(**s))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        stages = []
    return ProcessJob(
        video_id=e["PartitionKey"],
        status=e.get("status") or "uploaded",
        job_ref=e.get("job_ref") or None,
        progress=int(e.get("progress") or 0),
        error=e.get("error") or None,
        insights_url=e.get("insights_url") or None,
        stage=e.get("stage") or None,
        stages=stages,
    )


def _persist(job: ProcessJob) -> None:
    tc = _table()
    if tc is None:
        return
    try:
        tc.upsert_entity(_job_to_entity(job))
    except Exception as e:  # noqa: BLE001
        log.warning("Job upsert failed for %s: %s", job.video_id, e)


def _delete_persisted(video_id: str) -> None:
    tc = _table()
    if tc is None:
        return
    try:
        tc.delete_entity(partition_key=video_id, row_key=_ROW_KEY)
    except Exception as e:  # noqa: BLE001
        log.warning("Job delete failed for %s: %s", video_id, e)


def _load_table_into_cache() -> None:
    """Hydrate the in-memory cache from the table once per process."""
    global _table_loaded
    if _table_loaded:
        return
    with _table_load_lock:
        if _table_loaded:
            return
        tc = _table()
        if tc is None:
            _table_loaded = True
            return
        try:
            count = 0
            with _lock:
                for e in tc.list_entities():
                    if e.get("RowKey") != _ROW_KEY:
                        continue
                    try:
                        job = _entity_to_job(e)
                        _jobs[job.video_id] = job
                        count += 1
                    except Exception as ex:  # noqa: BLE001
                        log.warning("Skipping malformed job entity: %s", ex)
            log.info("Loaded %d job(s) from table %s", count, settings.jobs_table_name)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not load jobs from table: %s", e)
        finally:
            _table_loaded = True


# ── Public API ──────────────────────────────────────────────────────────

def get_job(video_id: str) -> ProcessJob | None:
    _load_table_into_cache()
    with _lock:
        return _jobs.get(video_id)


def list_jobs() -> list[ProcessJob]:
    _load_table_into_cache()
    with _lock:
        return list(_jobs.values())


def clear_video(video_id: str) -> int:
    """Remove the job entry for a given video (memory + table). Returns the number removed."""
    _load_table_into_cache()
    with _lock:
        removed = 1 if _jobs.pop(video_id, None) is not None else 0
    _delete_persisted(video_id)
    return removed


def update(video_id: str, **changes) -> ProcessJob:
    _load_table_into_cache()
    with _lock:
        job = _jobs.get(video_id) or ProcessJob(video_id=video_id, status="uploaded")
        data = job.model_dump()
        data.update(changes)
        job = ProcessJob(**data)
        _jobs[video_id] = job
    _persist(job)
    return job


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_stages(video_id: str, initial: dict[StageName, StageStatus]) -> ProcessJob:
    """Create a fresh ordered stage list. `initial` maps stage → starting status.
    Stages not in `initial` default to "pending"."""
    now = _now_iso()
    stages = [
        StageInfo(
            name=n,
            status=initial.get(n, "pending"),
            progress=100 if initial.get(n) == "completed" else 0,
            updated_at=now,
            started_at=now if initial.get(n) in ("running", "completed") else None,
        )
        for n in STAGE_ORDER
    ]
    return update(video_id, stages=stages)


def set_stage(
    video_id: str,
    name: StageName,
    *,
    status: StageStatus | None = None,
    progress: int | None = None,
    detail: str | None = None,
    error: str | None = None,
) -> ProcessJob:
    """Update one stage in-place. Auto-stamps started_at when first transitioning
    to running, and updated_at on every call."""
    _load_table_into_cache()
    now = _now_iso()
    with _lock:
        job = _jobs.get(video_id) or ProcessJob(video_id=video_id, status="uploaded")
        stages = list(job.stages) if job.stages else [
            StageInfo(name=n) for n in STAGE_ORDER
        ]
        for i, s in enumerate(stages):
            if s.name != name:
                continue
            data = s.model_dump()
            if status is not None:
                data["status"] = status
                if status == "running" and not data.get("started_at"):
                    data["started_at"] = now
                if status == "completed":
                    data["progress"] = 100
            if progress is not None:
                data["progress"] = max(0, min(100, int(progress)))
            if detail is not None:
                data["detail"] = detail[:200]
            if error is not None:
                data["error"] = error[:1000]
            data["updated_at"] = now
            stages[i] = StageInfo(**data)
            break
        job_data = job.model_dump()
        job_data["stages"] = [s.model_dump() for s in stages]
        job = ProcessJob(**job_data)
        _jobs[video_id] = job
    _persist(job)
    return job


def run_async(
    video_id: str,
    target: Callable[..., dict],
    initial_status: ProcessingStatus = "submitted",
    pass_progress_callback: bool = False,
    initial_stages: dict[StageName, StageStatus] | None = None,
) -> ProcessJob:
    """Kick off a pipeline in a background thread with status tracking.

    If `pass_progress_callback` is True, `target` is invoked with three keyword
    arguments:
      * `on_progress: Callable[[int], None]` — incremental percent (clamped 15..95).
      * `on_status:   Callable[..., None]`   — sets arbitrary ProcessJob fields
        (currently `stage` and `job_ref`).
      * `on_stage:    Callable[..., None]`   — update one stage; signature
        `on_stage(name, status=None, progress=None, detail=None, error=None)`.

    `initial_stages` seeds the per-file stepper. Stages omitted default to
    "pending". Typical usage:
      `initial_stages={"upload": "completed", "analyze": "pending"}`
    """
    if initial_stages:
        init_stages(video_id, initial_stages)
    job = update(video_id, status=initial_status, progress=10, error=None, stage=None)

    def _on_progress(pct: int) -> None:
        clamped = max(15, min(95, int(pct)))
        update(video_id, status="processing", progress=clamped)

    def _on_status(**fields) -> None:
        allowed = {"stage", "job_ref"}
        clean = {k: v for k, v in fields.items() if k in allowed}
        if clean:
            update(video_id, **clean)

    def _on_stage(name: StageName, **fields) -> None:
        try:
            set_stage(video_id, name, **fields)
        except Exception as e:  # noqa: BLE001
            log.warning("on_stage(%s) failed: %s", name, e)

    def _runner() -> None:
        try:
            update(video_id, status="processing", progress=15)
            if pass_progress_callback:
                result = target(on_progress=_on_progress, on_status=_on_status, on_stage=_on_stage)
            else:
                update(video_id, status="processing", progress=40)
                result = target()
            update(
                video_id,
                status="indexed",
                progress=100,
                insights_url=result.get("insights_url"),
                job_ref=str(result.get("vi_video_id") or result.get("scene_count") or ""),
            )
            # Mark the analyze stage completed if the pipeline didn't already.
            try:
                j = _jobs.get(video_id)
                if j:
                    last_unfinished = next(
                        (s.name for s in reversed(j.stages)
                         if s.status not in ("completed", "skipped", "failed")),
                        None,
                    )
                    if last_unfinished:
                        set_stage(video_id, last_unfinished, status="completed")
            except Exception:  # noqa: BLE001
                pass
            log.info("Pipeline completed for %s: %s", video_id, result)
        except Exception as e:  # noqa: BLE001
            log.exception("Pipeline failed for %s", video_id)
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"
            update(video_id, status="failed", progress=0, error=err)
            # Mark the first non-completed stage as failed for the stepper.
            try:
                j = _jobs.get(video_id)
                if j:
                    first_open = next(
                        (s.name for s in j.stages if s.status in ("pending", "running")),
                        None,
                    )
                    if first_open:
                        set_stage(video_id, first_open, status="failed", error=str(e))
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_runner, daemon=True, name=f"pipe-{video_id}")
    t.start()
    return job
