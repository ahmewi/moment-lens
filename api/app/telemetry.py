"""Application Insights / OpenTelemetry telemetry helpers.

Configures `azure-monitor-opentelemetry` once at import time when the
APPLICATIONINSIGHTS_CONNECTION_STRING env var is set, then exposes a small
typed surface for emitting per-pipeline-run cost/latency metrics.

Cost model: the rates are illustrative defaults (USD) for Video Indexer
(per video minute) and embeddings (per million tokens). Set the
`COST_RATE_*` variables (see `app.config`) to your own agreement. The figures
are estimates only (invoiced cost varies with commitments, region and support
tier), but they are good enough for a relative dashboard.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_configured = False
_pipeline_runs = None      # type: ignore[var-annotated]
_pipeline_latency = None   # type: ignore[var-annotated]
_pipeline_cost = None      # type: ignore[var-annotated]
_pipeline_scenes = None    # type: ignore[var-annotated]


def configure() -> None:
    """Idempotent App Insights configuration. Safe to call multiple times."""
    global _configured, _pipeline_runs, _pipeline_latency, _pipeline_cost, _pipeline_scenes
    if _configured:
        return
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        log.info("APPLICATIONINSIGHTS_CONNECTION_STRING not set; telemetry disabled.")
        _configured = True
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import metrics

        configure_azure_monitor(connection_string=conn, logger_name="momentlens")

        meter = metrics.get_meter("momentlens.pipelines")
        _pipeline_runs    = meter.create_counter("momentlens.pipeline.runs",
                                                 description="Pipeline runs (success or failure)")
        _pipeline_latency = meter.create_histogram("momentlens.pipeline.latency_seconds",
                                                   unit="s",
                                                   description="End-to-end pipeline latency")
        _pipeline_cost    = meter.create_histogram("momentlens.pipeline.cost_usd",
                                                   unit="USD",
                                                   description="Estimated per-run cost (USD)")
        _pipeline_scenes  = meter.create_histogram("momentlens.pipeline.scene_count",
                                                   description="Scene cards produced per run")
        log.info("Application Insights telemetry configured.")
    except Exception as e:  # noqa: BLE001
        log.warning("App Insights configuration failed; continuing without telemetry: %s", e)
    finally:
        _configured = True


# ── Cost estimators ─────────────────────────────────────────────────────

def estimate_vi_cost_usd(duration_seconds: float, rate_per_min: float) -> float:
    return max(0.0, (duration_seconds / 60.0) * rate_per_min)


def estimate_embedding_cost_usd(scene_count: int, rate_per_million_tokens: float,
                                 avg_tokens_per_scene: float = 600.0) -> float:
    """Embedding spend = (scenes × avg_tokens × 2 vectors) × rate.

    We embed two vectors per scene (content_vector + entity_vector) so the
    per-scene token count is doubled. 600 tokens is a conservative average for
    the content_embedding_text in this codebase (~3-4 short paragraphs).
    """
    if scene_count <= 0:
        return 0.0
    tokens = scene_count * avg_tokens_per_scene * 2
    return (tokens / 1_000_000.0) * rate_per_million_tokens


# ── Public emit ─────────────────────────────────────────────────────────

def record_pipeline_run(
    *,
    content_type: Optional[str],
    duration_seconds: float,
    latency_seconds: float,
    scene_count: int,
    status: str,
    estimated_cost_usd: float = 0.0,
) -> None:
    """Emit one set of custom metrics for a finished pipeline run.

    Always safe to call even when telemetry is disabled (no-op fast path).
    `duration_seconds` is the *video* duration; `latency_seconds` is wall-clock.
    """
    if not _configured:
        configure()
    if _pipeline_runs is None:
        return
    attrs = {
        "content_type": content_type or "unknown",
        "status": status,
    }
    try:
        _pipeline_runs.add(1, attributes=attrs)
        _pipeline_latency.record(max(0.0, latency_seconds), attributes=attrs)
        _pipeline_cost.record(max(0.0, estimated_cost_usd), attributes=attrs)
        if scene_count:
            _pipeline_scenes.record(scene_count, attributes=attrs)
        log.info(
            "telemetry: status=%s scenes=%d duration=%.1fs latency=%.1fs cost=$%.4f",
            status, scene_count, duration_seconds, latency_seconds, estimated_cost_usd,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("record_pipeline_run failed: %s", e)
