"""Centralized configuration loaded from environment variables."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # Azure context
    azure_subscription_id: str = Field(default="", alias="AZURE_SUBSCRIPTION_ID")
    azure_location: str = Field(default="westeurope", alias="AZURE_LOCATION")
    azure_client_id: str = Field(default="", alias="AZURE_CLIENT_ID")
    debug: bool = Field(default=False, alias="DEBUG")

    # Storage
    azure_storage_account: str = Field(default="", alias="AZURE_STORAGE_ACCOUNT")
    azure_storage_videos_container: str = Field(default="videos", alias="AZURE_STORAGE_VIDEOS_CONTAINER")
    azure_storage_insights_container: str = Field(default="insights", alias="AZURE_STORAGE_INSIGHTS_CONTAINER")
    azure_storage_thumbnails_container: str = Field(default="thumbnails", alias="AZURE_STORAGE_THUMBNAILS_CONTAINER")

    # OTP access gate. The UI requires an OTP session and protected API routes
    # validate the bearer token. ACS can be left blank for local dev; the code
    # is then logged server-side instead of emailed.
    otp_auth_enabled: bool = Field(default=True, alias="OTP_AUTH_ENABLED")
    allowed_domains: str = Field(default="example.com", alias="ALLOWED_DOMAINS")
    otp_expiry_seconds: int = Field(default=300, alias="OTP_EXPIRY_SECONDS")
    otp_length: int = Field(default=6, alias="OTP_LENGTH")
    session_token_secret: str = Field(
        default="CHANGE-ME-IN-PRODUCTION-USE-A-REAL-SECRET-KEY-HERE",
        alias="SESSION_TOKEN_SECRET",
    )
    session_token_expiry_minutes: int = Field(default=120, alias="SESSION_TOKEN_EXPIRY_MINUTES")
    acs_connection_string: str = Field(default="", alias="ACS_CONNECTION_STRING")
    acs_sender_email: str = Field(
        default="DoNotReply@<your-acs-domain>.azurecomm.net",
        alias="ACS_SENDER_EMAIL",
    )

    # Search
    azure_search_endpoint: str = Field(default="", alias="AZURE_SEARCH_ENDPOINT")
    search_index_option1: str = Field(default="moments-option1", alias="AZURE_SEARCH_INDEX_OPTION1")

    # Action-aware text-evidence reranker (hybrid mode only). Judges whether a
    # candidate's transcript/OCR/labels actually support the requested action
    # (vs. a generic object label or mere mention) before the final sort. The
    # pool is wide because the semantic reranker favours windows that TALK about
    # goals over the window where one is scored (a real goal window ranked ~33rd).
    # Fails open on any error, so disabling is only needed to cut LLM cost.
    search_action_rerank_enabled: bool = Field(default=True, alias="SEARCH_ACTION_RERANK_ENABLED")
    search_action_rerank_max_candidates: int = Field(default=40, alias="SEARCH_ACTION_RERANK_MAX_CANDIDATES")

    # Visual confirmation (hybrid mode only, subset of the action-rerank
    # candidates). Grabs 1-2 ffmpeg frames per candidate straight from its
    # source video and attaches them to the action-rerank LLM call so a
    # generic text mention of "goal" can't pass as a scoring_sequence without
    # a frame actually backing it up. Bounded tighter than the text-only tier
    # because each candidate costs an extra ffmpeg seek + vision tokens.
    search_visual_rerank_enabled: bool = Field(default=True, alias="SEARCH_VISUAL_RERANK_ENABLED")
    search_visual_rerank_max_candidates: int = Field(default=6, alias="SEARCH_VISUAL_RERANK_MAX_CANDIDATES")

    # Moment-tag deep search (hybrid retrieval, then frames): how far apart in one video two confirmed moments
    # must start to count as different ones. A celebration or replay of a goal follows it within about a
    # minute, and the earliest clip in a cluster wins, so replays fold into the first view.
    search_deep_merge_window_seconds: float = Field(default=60.0, ge=0, le=600, alias="SEARCH_DEEP_MERGE_WINDOW_SECONDS")

    # Index-time visual enrichment (Option 1): the judge model describes the frames of every scene card and tags the
    # visible events ("yellow card", "free kick"), folded into summary/labels before embedding, so event queries can
    # find windows whose speech never mentions the event (this broadcast's transcript is mostly noise). About one
    # judge call (~3.7k tokens) per card; fails open.
    visual_enrich_enabled: bool = Field(default=True, alias="VISUAL_ENRICH_ENABLED")
    visual_enrich_max_cards: int = Field(default=600, alias="VISUAL_ENRICH_MAX_CARDS")
    visual_enrich_max_frames: int = Field(default=12, alias="VISUAL_ENRICH_MAX_FRAMES")
    event_analysis_max_scenes: int = Field(default=120, ge=1, le=1000, alias="EVENT_ANALYSIS_MAX_SCENES")
    event_analysis_batch_size: int = Field(default=12, ge=1, le=100, alias="EVENT_ANALYSIS_BATCH_SIZE")
    # Scenes of the index-time event review that run at the same time (one frame extraction plus one vision-model call
    # each). A call carries 20-32 frames (about 10k tokens), so this multiplies the tokens per minute used on the judge
    # deployment: lower it if the logs show 429 rate limits. 1 reviews one scene at a time.
    event_analysis_concurrency: int = Field(default=4, ge=1, le=16, alias="EVENT_ANALYSIS_CONCURRENCY")

    # OpenAI
    azure_openai_endpoint: str = Field(default="", alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_chat_deployment: str = Field(default="gpt-4o-mini", alias="AZURE_OPENAI_CHAT_DEPLOYMENT")
    azure_openai_embedding_deployment: str = Field(default="text-embedding-3-large", alias="AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    azure_openai_api_version: str = Field(default="2024-10-21", alias="AZURE_OPENAI_API_VERSION")
    # Judge model for the action-aware rerank: it classifies every candidate from its transcript,
    # clip insights (summary/labels/OCR) and, for the best text matches, sampled video frames.
    # Empty = reuse the chat deployment on the primary account above. gpt-4o-mini cannot reliably
    # tell a goal from a saved shot in frames (measured: 40% goal recall, about half of the
    # answers "uncertain") and bills 2.8-8.5k tokens per image against a 50K TPM quota, so
    # production points this at a stronger multimodal deployment, which may live on a different
    # Azure OpenAI / Foundry account (endpoint override; the app's managed identity needs the
    # "Cognitive Services OpenAI User" role there). Reasoning-style models
    # reject `temperature` and `max_tokens`: setting a reasoning effort (low/medium/high) switches
    # the judge calls to the compatible parameter set.
    azure_openai_judge_endpoint: str = Field(default="", alias="AZURE_OPENAI_JUDGE_ENDPOINT")
    azure_openai_judge_deployment: str = Field(default="", alias="AZURE_OPENAI_JUDGE_DEPLOYMENT")
    azure_openai_judge_api_version: str = Field(default="", alias="AZURE_OPENAI_JUDGE_API_VERSION")
    azure_openai_judge_reasoning_effort: str = Field(default="", alias="AZURE_OPENAI_JUDGE_REASONING_EFFORT")

    # ── VI-poller Container Apps Job (Option 1) ────────────────────────
    # Polls Video Indexer until ingestion completes, persists the Index JSON
    # to the insights container. When empty the API falls back to in-process
    # polling (which dies if the App Service worker recycles mid-run).
    vi_poller_use_job: bool = Field(default=True, alias="VI_POLLER_USE_JOB")
    vi_poller_resource_group: str = Field(default="", alias="VI_POLLER_RESOURCE_GROUP")
    vi_poller_job_name: str = Field(default="", alias="VI_POLLER_JOB_NAME")
    vi_poller_timeout_minutes: int = Field(default=240, alias="VI_POLLER_TIMEOUT_MINUTES")

    # Video Indexer
    vi_account_id: str = Field(default="", alias="AZURE_VIDEO_INDEXER_ACCOUNT_ID")
    vi_account_name: str = Field(default="", alias="AZURE_VIDEO_INDEXER_ACCOUNT_NAME")
    vi_location: str = Field(default="westeurope", alias="AZURE_VIDEO_INDEXER_LOCATION")
    vi_resource_group: str = Field(default="rg-momentlens", alias="AZURE_VIDEO_INDEXER_RESOURCE_GROUP")
    vi_api_version: str = Field(default="2024-01-01", alias="AZURE_VIDEO_INDEXER_API_VERSION")

    # Upload limits
    max_upload_mb: int = Field(default=500, alias="MAX_UPLOAD_MB")
    max_upload_minutes: int = Field(default=30, alias="MAX_UPLOAD_MINUTES")

    # ── Job persistence ────────────────────────────────────────────────
    # When set, ProcessJob state is mirrored into an Azure Table so the API
    # survives restarts and can be scaled to >1 worker. Falls back to the
    # in-memory dict when blank (single-process dev mode).
    jobs_table_name: str = Field(default="momentlensjobs", alias="JOBS_TABLE_NAME")
    jobs_table_enabled: bool = Field(default=True, alias="JOBS_TABLE_ENABLED")

    # ── Cost telemetry rates (USD; demo defaults — adjust per agreement) ──
    cost_rate_vi_per_minute: float = Field(default=0.04, alias="COST_RATE_VI_PER_MINUTE")
    cost_rate_embedding_per_million_tokens: float = Field(default=0.13, alias="COST_RATE_EMBEDDING_PER_MILLION_TOKENS")

    @property
    def allowed_domains_list(self) -> list[str]:
        return [d.strip().lower() for d in self.allowed_domains.split(",") if d.strip()]


settings = Settings()
