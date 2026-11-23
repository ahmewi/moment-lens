"""Pydantic models for API requests/responses."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.validation import VIDEO_ID_PATTERN

ProcessingStatus = Literal["uploaded", "submitted", "processing", "indexed", "failed"]

StageName = Literal["upload", "analyze"]
StageStatus = Literal["pending", "running", "completed", "failed", "skipped"]
STAGE_ORDER: tuple[StageName, ...] = ("upload", "analyze")


class StageInfo(BaseModel):
    """Per-file lifecycle stage. Surfaced to the UI as a stepper."""
    name: StageName
    status: StageStatus = "pending"
    progress: int = 0           # 0..100 within this stage
    detail: str = ""            # e.g. "indexing", "frame review 3/12"
    started_at: Optional[str] = None  # ISO8601
    updated_at: Optional[str] = None  # ISO8601
    error: Optional[str] = None

ContentType = Literal[
    "soccer_match",
    "soccer_slim",
    "tennis_match",
    "talk_show",
    "music_event",
    "movie_scene",
    "news_politics",
    "news",
    "documentary",
    "generic",
]


class UploadResponse(BaseModel):
    video_id: str
    blob_url: str
    size_bytes: int
    filename: str


class StorageReferenceRequest(BaseModel):
    blob_url: str = Field(min_length=1, max_length=8192)


class StoredVideo(BaseModel):
    blob_name: str
    blob_url: str
    size_bytes: int


class StoredVideoList(BaseModel):
    account: str
    container: str
    items: list[StoredVideo]
    continuation_token: Optional[str] = None


class UploadSessionRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: Optional[str] = Field(default=None, max_length=128)
    size_bytes: Optional[int] = Field(default=None, ge=0)


class UploadSessionResponse(BaseModel):
    video_id: str
    blob_name: str
    blob_url: str
    block_size: int


class UploadCommitRequest(BaseModel):
    blob_name: str = Field(max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    content_type: Optional[str] = Field(default=None, max_length=128)
    total_blocks: int = Field(ge=1, le=50000)


class ProcessRequest(BaseModel):
    video_id: str = Field(pattern=VIDEO_ID_PATTERN)
    filename: str = Field(min_length=1, max_length=512)
    blob_url: str = Field(min_length=1, max_length=2048)
    content_type: ContentType = "soccer_match"
    source_language: Optional[str] = Field(default=None, max_length=32)
    indexing_window_seconds: Literal[0, 20, 30] = 0


class VideoIndexerLanguage(BaseModel):
    code: str
    name: str


class ProcessJob(BaseModel):
    video_id: str
    status: ProcessingStatus
    job_ref: Optional[str] = None  # provider-specific job/asset id
    progress: int = 0  # 0-100
    error: Optional[str] = None
    insights_url: Optional[str] = None  # blob URL of stored insights JSON
    # Human-readable phase ("indexing", "reviewing frames", ...).
    # Used by the UI to surface what the pipeline is currently doing.
    stage: Optional[str] = None
    # Per-file lifecycle stages (Upload → Analyze). Stages that don't apply
    # (for example Upload for a video already in storage) stay status="skipped".
    stages: list[StageInfo] = Field(default_factory=list)


class SceneCard(BaseModel):
    """Unified scene/moment representation pushed to Search."""
    id: str
    video_id: str
    filename: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    summary: str = ""
    transcript_excerpt: str = ""
    labels: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    ocr_text: str = ""
    speakers: list[str] = Field(default_factory=list)
    thumbnail_url: Optional[str] = None
    blob_url: str = ""  # source video blob URL
    content_type: Optional[str] = None


class SearchFilters(BaseModel):
    """Optional facet-style filters applied alongside the user query."""
    video_ids: list[str] = Field(default_factory=list)
    content_types: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str
    event_kind: Optional[str] = Field(default=None, max_length=64)
    top: int = Field(default=5, ge=1, le=20)
    clip_duration_seconds: int = Field(default=15, ge=5, le=60)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    filters: Optional[SearchFilters] = None
    # Advanced, per-request overrides of the global visual-confirmation
    # defaults (see Settings.search_visual_rerank_*). None means "use the
    # server default" so existing callers are unaffected.
    visual_rerank_enabled: Optional[bool] = None
    visual_rerank_max_candidates: Optional[int] = Field(default=None, ge=0, le=20)
    # Frames sampled from each clip that is confirmed visually. None = automatic (2-4, by clip length).
    visual_rerank_frames: Optional[int] = Field(default=None, ge=2, le=12)


class SearchResultItem(BaseModel):
    id: str
    video_id: str
    filename: str
    start_seconds: float
    end_seconds: float
    score: float
    rerank_score: Optional[float] = None
    summary: str
    transcript_excerpt: str
    labels: list[str]
    entities: list[str] = Field(default_factory=list)
    ocr_text: str = ""
    thumbnail_url: Optional[str]
    blob_url: str
    play_url: str  # SAS URL with #t= fragment
    # Action-aware text-evidence reranker verdict (hybrid mode only). None
    # when the reranker didn't run or didn't classify this candidate — treat
    # as "no verdict", never as "unrelated".
    match_type: Optional[str] = None
    entity_match: Optional[bool] = None
    match_reason: Optional[str] = None
    # True when sampled video frames were examined for this result (not just text).
    visual_checked: bool = False
    # From the frames (judge): the clip shows the action as it first happens ("live"), as a repeat of it ("replay"),
    # or not clearly ("unknown": the verdict then rests on the text).
    presentation: Optional[Literal["live", "replay", "unknown"]] = None
    # Moment-tag searches only: "tag" = carries the event tag written at index time, "deep_search" = found at
    # query time by checking untagged clips against their frames.
    origin: Optional[Literal["tag", "deep_search"]] = None


class FacetValue(BaseModel):
    value: str
    count: int


class DeepSearchStats(BaseModel):
    """What a Moment-tag search did beyond the index-time tags (see search._deep_search)."""
    tagged: int  # moments carrying the index-time tag
    recovered: int  # additional moments confirmed from frames at query time
    candidates_judged: int  # untagged clips whose text evidence the judge answered for
    frames_checked: int  # candidates whose frames the judge answered for
    failed: bool = False  # the extra lookup errored, so only the index-time tags are shown


class SearchResponse(BaseModel):
    query: str
    count: int
    items: list[SearchResultItem]
    event_kind: Optional[str] = None
    facets: dict[str, list[FacetValue]] = Field(default_factory=dict)
    deep_search: Optional[DeepSearchStats] = None
    # Moment-tag searches: moments found before the "Number of results" cut, so a cut is never silent.
    total_matches: Optional[int] = None
