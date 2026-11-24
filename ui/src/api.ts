export type ViIndexingWindowSeconds = 0 | 20 | 30;

export type ContentType =
  | "soccer_match"
  | "tennis_match"
  | "talk_show"
  | "music_event"
  | "movie_scene"
  | "news_politics"
  | "news"
  | "documentary"
  | "generic";

export const CONTENT_TYPES: { value: ContentType; label: string; hint: string }[] = [
  { value: "soccer_match", label: "Soccer game", hint: "Goals, fouls, cards, VAR decisions, corners" },
  { value: "tennis_match", label: "Tennis match",            hint: "serves · rallies · winners · score" },
  { value: "talk_show",    label: "Talk show / panel",       hint: "topics · speakers · sentiment" },
  { value: "music_event",  label: "Music event / concert",   hint: "songs · performers · mood" },
  { value: "movie_scene",  label: "Movie / TV scene",        hint: "characters · setting · tone" },
  { value: "news_politics",label: "News / Politics",         hint: "personas · news topic · story details" },
  { value: "news",         label: "News (visual)",           hint: "scene description · people actions · notable objects" },
  { value: "documentary",  label: "Documentary",             hint: "narrative scenes · subjects · environments · objects" },
  { value: "generic", label: "Generic", hint: "Native VI insights; no custom event profile yet" },
];

export interface UploadResponse {
  video_id: string;
  blob_url: string;
  size_bytes: number;
  filename: string;
}

export interface StoredVideo {
  blob_name: string;
  blob_url: string;
  size_bytes: number;
}

export interface StoredVideoList {
  account: string;
  container: string;
  items: StoredVideo[];
  continuation_token: string | null;
}

export interface VideoIndexerLanguage {
  code: string;
  name: string;
}

export interface ProcessJob {
  video_id: string;
  status: "uploaded" | "submitted" | "processing" | "indexed" | "failed";
  job_ref?: string | null;
  progress: number;
  error?: string | null;
  insights_url?: string | null;
  stage?: string | null;
  stages?: StageInfo[];
}

export type StageName = "upload" | "analyze";
export type StageStatus = "pending" | "running" | "completed" | "failed" | "skipped";

export interface StageInfo {
  name: StageName;
  status: StageStatus;
  progress: number;
  detail: string;
  started_at?: string | null;
  updated_at?: string | null;
  error?: string | null;
}

export interface SearchResultItem {
  id: string;
  video_id: string;
  filename: string;
  start_seconds: number;
  end_seconds: number;
  score: number;
  rerank_score?: number | null;
  summary: string;
  transcript_excerpt: string;
  labels: string[];
  entities?: string[];
  ocr_text?: string;
  thumbnail_url?: string | null;
  blob_url: string;
  play_url: string;
  // Action-aware reranker verdict (hybrid mode only). Absent / null when the
  // reranker didn't run or didn't classify this result.
  match_type?: "scoring_sequence" | "celebration_only" | "attempt_only" | "mention_only" | "unrelated" | "uncertain" | null;
  entity_match?: boolean | null;
  match_reason?: string | null;
  // True when sampled video frames (not just text) were examined for this result.
  visual_checked?: boolean;
  // From the frames: whether the clip shows the action as it first happens or a repeat of it.
  presentation?: "live" | "replay" | "unknown" | null;
  // Moment-tag searches: "tag" = tagged at index time, "deep_search" = not tagged, but its frames show the moment.
  origin?: "tag" | "deep_search" | null;
}

export interface SearchFilters {
  video_ids?: string[];
  content_types?: string[];
  labels?: string[];
}

export interface FacetValue {
  value: string;
  count: number;
}

export interface DeepSearchStats {
  tagged: number;
  recovered: number;
  candidates_judged: number;
  frames_checked: number;
  failed: boolean;
}

export interface SearchResponse {
  query: string;
  count: number;
  items: SearchResultItem[];
  event_kind?: string | null;
  facets?: Record<string, FacetValue[]>;
  // Moment-tag searches only.
  deep_search?: DeepSearchStats | null;
  total_matches?: number | null;
}

export interface VideoLibraryItem {
  video_id: string;
  filename: string;
  duration_seconds: number;
  scenes: number;
  play_url: string;
  thumbnail_url?: string;
}

export interface SceneItem {
  id: string;
  video_id: string;
  filename: string;
  start_seconds: number;
  end_seconds: number;
  summary: string;
  transcript_excerpt: string;
  labels: string[];
  entities: string[];
  speakers: string[];
  thumbnail_url?: string;
  play_url: string;
}

async function jsonReq<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    credentials: "same-origin",
    ...init,
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
  }
  return r.json() as Promise<T>;
}

export function getStoredVideos(continuationToken?: string) {
  const query = continuationToken ? `?continuation_token=${encodeURIComponent(continuationToken)}` : "";
  return jsonReq<StoredVideoList>(`/api/upload/stored${query}`);
}

export function referenceStoredVideo(blobUrl: string) {
  return jsonReq<UploadResponse>("/api/upload/reference", {
    method: "POST",
    body: JSON.stringify({ blob_url: blobUrl }),
  });
}

export async function uploadVideo(file: File, onProgress?: (pct: number) => void): Promise<UploadResponse> {
  const session = await jsonReq<{
    video_id: string;
    blob_name: string;
    blob_url: string;
    block_size: number;
  }>("/api/upload/session", {
    method: "POST",
    body: JSON.stringify({
      filename: file.name,
      content_type: file.type || "video/mp4",
      size_bytes: file.size,
    }),
  });

  const blockSize = session.block_size || 4 * 1024 * 1024;
  const totalBlocks = Math.max(1, Math.ceil(file.size / blockSize));
  const contentType = file.type || "video/mp4";

  async function putBlock(i: number, blob: Blob): Promise<void> {
    const url = `/api/upload/session/${encodeURIComponent(session.video_id)}/blocks/${i}?blob_name=${encodeURIComponent(session.blob_name)}`;
    let lastErr: unknown = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await new Promise<void>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("PUT", url);
          xhr.setRequestHeader("Content-Type", "application/octet-stream");
          xhr.upload.onprogress = (e) => {
            if (!e.lengthComputable || !onProgress) return;
            const done = i * blockSize + e.loaded;
            onProgress(Math.min(99, Math.round((done / file.size) * 100)));
          };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) resolve();
            else reject(new Error(`Block ${i + 1}/${totalBlocks} failed ${xhr.status}: ${xhr.responseText || xhr.statusText}`));
          };
          xhr.onerror = () => reject(new Error(`Network error on block ${i + 1}/${totalBlocks}`));
          xhr.send(blob);
        });
        return;
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
  }

  for (let i = 0; i < totalBlocks; i++) {
    const start = i * blockSize;
    const end = Math.min(start + blockSize, file.size);
    await putBlock(i, file.slice(start, end));
  }

  const committed = await jsonReq<UploadResponse>(`/api/upload/session/${encodeURIComponent(session.video_id)}/commit`, {
    method: "POST",
    body: JSON.stringify({
      blob_name: session.blob_name,
      filename: file.name,
      size_bytes: file.size,
      content_type: contentType,
      total_blocks: totalBlocks,
    }),
  });

  if (onProgress) onProgress(100);
  return committed;
}

export interface AppConfig {
  maxUploadMB: number;
  maxUploadMinutes: number;
}

export function getConfig() {
  return jsonReq<AppConfig>("/api/config");
}

export function getViLanguages() {
  return jsonReq<VideoIndexerLanguage[]>("/api/indexing/languages");
}

export function startProcess(body: { video_id: string; filename: string; blob_url: string; content_type?: ContentType; source_language?: string; indexing_window_seconds?: ViIndexingWindowSeconds }) {
  return jsonReq<ProcessJob>("/api/indexing/process", { method: "POST", body: JSON.stringify(body) });
}

export function getStatus(videoId: string) {
  return jsonReq<ProcessJob>(`/api/indexing/status/${encodeURIComponent(videoId)}`);
}

export function search(body: {
  query: string; top: number; clip_duration_seconds: number; mode?: string;
  event_kind?: string;
  filters?: SearchFilters; visual_rerank_enabled?: boolean; visual_rerank_max_candidates?: number;
  visual_rerank_frames?: number;
}) {
  return jsonReq<SearchResponse>("/api/search", { method: "POST", body: JSON.stringify({ mode: "hybrid", ...body }) });
}

export function listVideos() {
  return jsonReq<{ count: number; items: VideoLibraryItem[] }>("/api/videos");
}

export function deleteVideo(videoId: string, purgeStorage = true) {
  return jsonReq<{ video_id: string; search_deleted: number; blobs_deleted: number; errors: string[] }>(
    `/api/videos/${encodeURIComponent(videoId)}?purge_storage=${purgeStorage}`,
    { method: "DELETE" },
  );
}

export function listScenes(videoId: string) {
  return jsonReq<{ video_id: string; count: number; items: SceneItem[] }>(
    `/api/videos/${encodeURIComponent(videoId)}/scenes`,
  );
}

export function listJobs() {
  return jsonReq<{ items: ProcessJob[] }>("/api/videos/jobs");
}

export interface VideoEvent {
  id: string;
  kind: string;
  label: string;
  start_seconds: number;
  end_seconds: number;
  confidence: number;
  evidence: string;
  presentation: "live";
  subject: string;
  scene_id: string;
  source: "llm_enrichment";
}

export interface VideoEvents {
  video_id: string;
  vi_video_id: string | null;
  content_type: ContentType | null;
  duration_seconds: number;
  status: "completed" | "partial" | "needs_enrichment" | "not_applicable";
  reviewed_scenes: number;
  total_scenes: number;
  analysis_mode?: "query_targeted";
  analysis_complete?: boolean;
  scene_budget?: number;
  attempted_scenes?: number;
  // Why the pass ended: "scene_budget" means scenes beyond the budget were never reviewed.
  stop_reason?: "scene_budget" | "candidates_exhausted" | "selected_scenes_reviewed";
  categories: Record<string, string>;
  events: VideoEvent[];
  raw_insights_data: { rawInsightsKey: string; insightsItems: unknown[] }[];
}

export interface ViWidgetAccess {
  account_id: string;
  account_name: string;
  location: string;
  vi_video_id: string;
  access_token: string;
  refresh_after_seconds: number;
}

export function getVideoEvents(videoId: string) {
  return jsonReq<VideoEvents>(`/api/indexing/events/${encodeURIComponent(videoId)}`);
}

export function getContentProfiles() {
  return jsonReq<{ items: { content_type: ContentType; name: string; events: Record<string, string> }[] }>("/api/indexing/profiles");
}

export interface ProfileAnalysisPlan {
  content_type: ContentType;
  scene_budget?: number;
  queued_video_ids: string[];
  skipped_video_ids: string[];
  active_video_ids: string[];
}

export function getProfileAnalysisPlan(contentType: ContentType) {
  return jsonReq<ProfileAnalysisPlan>(`/api/indexing/profiles/${encodeURIComponent(contentType)}/analysis`);
}

export function analyzeContentProfile(contentType: ContentType) {
  return jsonReq<ProfileAnalysisPlan & { job: ProcessJob | null }>(`/api/indexing/profiles/${encodeURIComponent(contentType)}/enrich`, { method: "POST" });
}

export function getViWidgetAccess(videoId: string) {
  return jsonReq<ViWidgetAccess>(`/api/indexing/widgets/${encodeURIComponent(videoId)}`);
}

export function enrichVideoEvents(videoId: string, contentType: ContentType) {
  return jsonReq<ProcessJob>(`/api/indexing/events/${encodeURIComponent(videoId)}/enrich`, {
    method: "POST", body: JSON.stringify({ content_type: contentType }),
  });
}
