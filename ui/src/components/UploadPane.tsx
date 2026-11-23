import { useEffect, useRef, useState } from "react";
import {
  Body1, Button, Card, ProgressBar, Subtitle2, Text, tokens, makeStyles, shorthands,
  Dropdown, Option, Field, Input, Tab, TabList,
} from "@fluentui/react-components";
import {
  ArrowUpload24Regular, CheckmarkCircle24Filled, ErrorCircle24Filled, Dismiss20Regular,
  Circle20Regular, SpinnerIos20Regular, MoreCircle20Regular, ArrowClockwise20Regular,
  FolderOpen24Regular, Play24Regular, ChevronDown20Regular,
} from "@fluentui/react-icons";
import type { ContentType, ProcessJob, StageInfo, StageName, StageStatus, StoredVideo, VideoIndexerLanguage, ViIndexingWindowSeconds } from "../api";
import { CONTENT_TYPES, getStatus, getStoredVideos, getViLanguages, referenceStoredVideo, startProcess, uploadVideo } from "../api";
import SceneFilmstrip from "./SceneFilmstrip";

interface Props {
  onIndexed?: (videoId: string) => void;
}

interface UploadItem {
  key: string;
  filename: string;
  source: "upload" | "storage";
  uploadPct: number;
  job: ProcessJob | null;
  error: string | null;
  startedAt: number;        // wall-clock when we kicked off uploadVideo()
  queued: boolean;          // true while waiting behind the concurrency cap
}

// Cap concurrent in-flight uploads so we don't hammer Video Indexer (HTTP 429).
const MAX_IN_FLIGHT = 10;

const INDEXING_WINDOWS: { value: ViIndexingWindowSeconds; label: string }[] = [
  { value: 20, label: "20 seconds" },
  { value: 30, label: "30 seconds" },
  { value: 0, label: "Original VI shots" },
];

const STAGE_LABELS: Record<StageName, string> = {
  upload: "Upload",
  analyze: "Analyze",
};
const STAGE_ORDER: StageName[] = ["upload", "analyze"];

// Module-scope queue + counter — shared across all UploadPane instances and
// re-mounts (StrictMode in dev re-mounts every component once on first render,
// which would double the cap if these were per-component refs).
type VideoSource = { kind: "upload"; file: File } | { kind: "storage"; blobUrl: string; filename: string };
type Pending = { run: () => Promise<void> };
const _pending: Pending[] = [];
let _inFlight = 0;
function _pump() {
  while (_inFlight < MAX_IN_FLIGHT && _pending.length > 0) {
    const next = _pending.shift()!;
    _inFlight += 1;
    void next.run().finally(() => {
      _inFlight = Math.max(0, _inFlight - 1);
      _pump();
    });
  }
}

function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
}

const useStyles = makeStyles({
  card: { padding: "16px" },
  drop: {
    marginTop: "12px",
    padding: "20px",
    borderRadius: tokens.borderRadiusLarge,
    ...shorthands.border("1.5px", "dashed", tokens.colorNeutralStroke1),
    textAlign: "center",
    cursor: "pointer",
    transitionProperty: "border-color, background",
    transitionDuration: "120ms",
    ":hover": {
      ...shorthands.borderColor(tokens.colorBrandStroke1),
      background: tokens.colorNeutralBackground2,
    },
  },
  dropActive: {
    ...shorthands.borderColor(tokens.colorBrandStroke1),
    background: tokens.colorNeutralBackground2,
  },
  meta: { color: tokens.colorNeutralForeground3 },
  uploadRow: {
    marginTop: "12px",
    padding: "10px 12px",
    borderRadius: tokens.borderRadiusMedium,
    ...shorthands.border("1px", "solid", tokens.colorNeutralStroke2),
    background: tokens.colorNeutralBackground1,
  },
  uploadHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "8px",
  },
  stepper: {
    marginTop: "8px",
    display: "grid",
    gridTemplateColumns: "repeat(4, 1fr)",
    gap: "6px",
  },
  stageCell: {
    padding: "6px 8px",
    borderRadius: tokens.borderRadiusMedium,
    ...shorthands.border("1px", "solid", tokens.colorNeutralStroke2),
    background: tokens.colorNeutralBackground2,
    display: "flex",
    flexDirection: "column",
    gap: "2px",
    minWidth: 0,
  },
  stageHead: {
    display: "flex",
    alignItems: "center",
    gap: "4px",
  },
  stageDetail: {
    color: tokens.colorNeutralForeground3,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
});

function stageColor(s: StageStatus): string {
  switch (s) {
    case "completed": return tokens.colorPaletteGreenForeground1;
    case "running":   return tokens.colorBrandForeground1;
    case "failed":    return tokens.colorPaletteRedForeground1;
    case "skipped":   return tokens.colorNeutralForeground4;
    default:          return tokens.colorNeutralForeground3;
  }
}

function StageIcon({ status }: { status: StageStatus }) {
  const color = stageColor(status);
  if (status === "completed") return <CheckmarkCircle24Filled style={{ color, fontSize: 16 }} />;
  if (status === "failed")    return <ErrorCircle24Filled    style={{ color, fontSize: 16 }} />;
  if (status === "running")   return <SpinnerIos20Regular    style={{ color, fontSize: 16 }} />;
  if (status === "skipped")   return <MoreCircle20Regular    style={{ color, fontSize: 16 }} />;
  return <Circle20Regular style={{ color, fontSize: 16 }} />;
}

function StageStepper({ stages }: { stages: StageInfo[] }) {
  const cls = useStyles();
  // Ensure 4 cells in canonical order even if backend returns a subset.
  const byName = new Map(stages.map((s) => [s.name, s]));
  const ordered: StageInfo[] = STAGE_ORDER.map(
    (n) => byName.get(n) || ({ name: n, status: "pending" as StageStatus, progress: 0, detail: "" }),
  );
  return (
    <div className={cls.stepper}>
      {ordered.map((s) => (
        <div
          key={s.name}
          className={cls.stageCell}
          // Skipped stages are valid (e.g. Upload for a video already in storage) —
          // dim the cell so they read as "n/a" instead of "still pending".
          style={s.status === "skipped" ? { opacity: 0.55 } : undefined}
          title={`${STAGE_LABELS[s.name]} \u00b7 ${s.status}${s.detail ? ` \u00b7 ${s.detail}` : ""}`}
        >
          <div className={cls.stageHead}>
            <StageIcon status={s.status} />
            <Text size={200} weight="semibold" style={{ color: stageColor(s.status) }}>
              {STAGE_LABELS[s.name]}
            </Text>
          </div>
          {s.status === "running" && (
            <ProgressBar value={Math.max(2, s.progress) / 100} thickness="medium" />
          )}
          {s.detail && (
            <Text size={100} className={cls.stageDetail}>{s.detail}</Text>
          )}
          {s.status === "failed" && s.error && (
            <Text size={100} style={{ color: tokens.colorPaletteRedForeground1 }} truncate wrap={false}>
              {s.error}
            </Text>
          )}
        </div>
      ))}
    </div>
  );
}

export default function UploadPane({ onIndexed }: Props) {
  const cls = useStyles();
  const inputRef = useRef<HTMLInputElement>(null);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [sourceMode, setSourceMode] = useState<"upload" | "storage">("upload");
  const [blobUrl, setBlobUrl] = useState("");
  const [storedVideos, setStoredVideos] = useState<StoredVideo[]>([]);
  const [storageScope, setStorageScope] = useState("");
  const [storageCursor, setStorageCursor] = useState<string | null>(null);
  const [storageLoading, setStorageLoading] = useState(false);
  const [storageError, setStorageError] = useState("");
  const [storageReload, setStorageReload] = useState(0);
  const storageRequest = useRef(0);
  const [contentType, setContentType] = useState<ContentType>("soccer_match");
  const [sourceLanguage, setSourceLanguage] = useState("auto");
  const [indexingWindowSeconds, setIndexingWindowSeconds] = useState<ViIndexingWindowSeconds>(20);
  const [viLanguages, setViLanguages] = useState<VideoIndexerLanguage[]>([]);
  const [languagesLoading, setLanguagesLoading] = useState(false);
  const [languageError, setLanguageError] = useState(false);
  const [languageReload, setLanguageReload] = useState(0);

  useEffect(() => {
    if (sourceMode !== "storage") return;
    void loadStoredVideos();
    return () => { storageRequest.current += 1; };
  }, [sourceMode, storageReload]);

  async function loadStoredVideos(cursor?: string) {
    const requestId = ++storageRequest.current;
    setStorageLoading(true);
    setStorageError("");
    try {
      const result = await getStoredVideos(cursor);
      if (requestId !== storageRequest.current) return;
      setStoredVideos((previous) => cursor
        ? [...new Map([...previous, ...result.items].map((video) => [video.blob_url, video])).values()]
        : result.items);
      setStorageScope(`${result.account} / ${result.container}`);
      setStorageCursor(result.continuation_token);
    } catch {
      if (requestId === storageRequest.current) setStorageError("Stored videos could not be loaded.");
    } finally {
      if (requestId === storageRequest.current) setStorageLoading(false);
    }
  }

  useEffect(() => {
    let active = true;
    setLanguagesLoading(true);
    setLanguageError(false);
    getViLanguages()
      .then((languages) => { if (active) setViLanguages(languages); })
      .catch(() => { if (active) setLanguageError(true); })
      .finally(() => { if (active) setLanguagesLoading(false); });
    return () => { active = false; };
  }, [languageReload]);

  // Force re-render every second so the elapsed clock updates while Video Indexer processes.
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  // Queue of files waiting to start (gated by MAX_IN_FLIGHT — module-scope so
  // it can't be duplicated by re-mounts).

  function patch(key: string, p: Partial<UploadItem>) {
    setItems((arr) => arr.map((it) => (it.key === key ? { ...it, ...p } : it)));
  }

  async function runOne(key: string, source: VideoSource) {
    patch(key, { queued: false, startedAt: Date.now() });
    try {
      const up = source.kind === "upload"
        ? await uploadVideo(source.file, (pct) => patch(key, { uploadPct: pct }))
        : await referenceStoredVideo(source.blobUrl);
      patch(key, { filename: up.filename, uploadPct: 100 });
      const j = await startProcess({
        video_id: up.video_id,
        filename: up.filename,
        blob_url: up.blob_url,
        content_type: contentType,
        ...(sourceLanguage !== "auto" ? { source_language: sourceLanguage } : {}),
        indexing_window_seconds: indexingWindowSeconds,
      });
      patch(key, { job: j, uploadPct: 100 });
    } catch (e: any) {
      patch(key, { error: e?.message || String(e) });
    }
  }

  function startOne(source: VideoSource) {
    const filename = source.kind === "upload" ? source.file.name : source.filename;
    const key = `${filename}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setItems((arr) => [
      { key, filename, source: source.kind, uploadPct: 0, job: null, error: null, startedAt: 0, queued: true },
      ...arr,
    ]);
    _pending.push({ run: () => runOne(key, source) });
    _pump();
  }

  function onPickMany(files: FileList | File[]) {
    Array.from(files).forEach((file) => { startOne({ kind: "upload", file }); });
  }

  function startReference() {
    const selectedUrl = blobUrl.trim();
    if (!selectedUrl || referenceBusy) return;
    let filename = "Stored video";
    try { filename = decodeURIComponent(new URL(selectedUrl).pathname.split("/").pop() || filename); } catch {}
    startOne({ kind: "storage", blobUrl: selectedUrl, filename });
  }

  // Poll any in-flight jobs
  useEffect(() => {
    const inFlight = items.filter((it) => it.job && it.job.status !== "indexed" && it.job.status !== "failed");
    if (inFlight.length === 0) return;
    const t = setInterval(async () => {
      await Promise.all(inFlight.map(async (it) => {
        if (!it.job) return;
        try {
          const next = await getStatus(it.job.video_id);
          patch(it.key, { job: next });
          if (next.status === "indexed" && onIndexed) onIndexed(next.video_id);
        } catch { /* keep polling */ }
      }));
    }, 4000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items.map((i) => `${i.key}:${i.job?.status ?? "-"}`).join("|")]);

  function clearDone() {
    setItems((arr) => arr.filter((it) => !(it.job?.status === "indexed" || it.job?.status === "failed" || it.error)));
  }

  const hasDone = items.some((it) => it.job?.status === "indexed" || it.job?.status === "failed" || it.error);
  const referenceBusy = items.some((item) => item.source === "storage" && !item.job && !item.error);
  const selectedStoredVideo = storedVideos.find((video) => video.blob_url === blobUrl.trim());

  return (
    <Card className={cls.card}>
      <Subtitle2>Add videos</Subtitle2>
      <TabList
        aria-label="Video source"
        selectedValue={sourceMode}
        onTabSelect={(_event, data) => setSourceMode(data.value as "upload" | "storage")}
        size="small"
      >
        <Tab value="upload" icon={<ArrowUpload24Regular />}>Upload files</Tab>
        <Tab value="storage" icon={<FolderOpen24Regular />}>Storage</Tab>
      </TabList>
      {sourceMode === "upload" && <Body1 block className={cls.meta} style={{ marginTop: 4 }}>
        Max 10&nbsp;GB each · MP4 / MOV / MKV / WEBM / MPEG. Pick or drop multiple files — each one is indexed via{" "}
        <strong>Azure AI Video Indexer</strong>.
      </Body1>}

      <input
        ref={inputRef}
        type="file"
        accept="video/*"
        multiple
        style={{ display: "none" }}
        onChange={(e) => { if (e.target.files?.length) onPickMany(e.target.files); e.currentTarget.value = ""; }}
      />

      <div style={{ marginTop: 12 }}>
        <Field label="Content type" hint={CONTENT_TYPES.find(c => c.value === contentType)?.hint || ""}>
          <Dropdown
            value={CONTENT_TYPES.find(c => c.value === contentType)?.label || ""}
            selectedOptions={[contentType]}
            onOptionSelect={(_e, data) => {
              if (data.optionValue) setContentType(data.optionValue as ContentType);
            }}
          >
            {CONTENT_TYPES.map((ct) => (
              <Option key={ct.value} value={ct.value} text={ct.label}>
                <div>
                  <div>{ct.label}</div>
                  <div style={{ fontSize: 11, color: tokens.colorNeutralForeground3 }}>{ct.hint}</div>
                </div>
              </Option>
            ))}
          </Dropdown>
        </Field>
      </div>
      <div style={{ marginTop: 12, display: "flex", alignItems: "flex-start", gap: 8 }}>
        <Field
          label="Spoken language / dialect"
          style={{ flex: 1, minWidth: 0 }}
          validationState={languageError ? "warning" : "none"}
          validationMessage={languageError ? "Language list unavailable." : languagesLoading ? "Loading languages..." : undefined}
        >
          <Dropdown
            value={sourceLanguage === "auto" ? "Auto detect" : `${viLanguages.find((language) => language.code === sourceLanguage)?.name || sourceLanguage} (${sourceLanguage})`}
            selectedOptions={[sourceLanguage]}
            disabled={languagesLoading}
            style={{ width: "100%", minWidth: 0 }}
            button={{ style: { whiteSpace: "normal", overflowWrap: "anywhere", textAlign: "left" } }}
            listbox={{ style: { maxHeight: 320, overflowY: "auto" } }}
            onOptionSelect={(_event, data) => {
              if (data.optionValue) setSourceLanguage(data.optionValue);
            }}
          >
            <Option value="auto">Auto detect</Option>
            {viLanguages.map((language) => (
              <Option key={language.code} value={language.code}>
                {`${language.name} (${language.code})`}
              </Option>
            ))}
          </Dropdown>
        </Field>
        {languageError && (
          <Button
            appearance="subtle"
            icon={<ArrowClockwise20Regular />}
            aria-label="Retry language list"
            title="Retry language list"
            style={{ marginTop: 28 }}
            onClick={() => setLanguageReload((reload) => reload + 1)}
          />
        )}
      </div>
      <div style={{ marginTop: 12 }}>
        <Field label="Indexing window">
          <Dropdown
            value={INDEXING_WINDOWS.find((window) => window.value === indexingWindowSeconds)?.label || ""}
            selectedOptions={[String(indexingWindowSeconds)]}
            style={{ width: "100%", minWidth: 0 }}
            onOptionSelect={(_event, data) => {
              const selected = INDEXING_WINDOWS.find((window) => String(window.value) === data.optionValue);
              if (selected) setIndexingWindowSeconds(selected.value);
            }}
          >
            {INDEXING_WINDOWS.map((window) => (
              <Option key={window.value} value={String(window.value)}>{window.label}</Option>
            ))}
          </Dropdown>
        </Field>
      </div>
      {sourceMode === "upload" ? <div
        className={`${cls.drop} ${dragOver ? cls.dropActive : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault(); setDragOver(false);
          if (e.dataTransfer.files?.length) onPickMany(e.dataTransfer.files);
        }}
      >
        <ArrowUpload24Regular />
        <Body1 block style={{ marginTop: 4 }}>Drop one or more videos here, or click to browse</Body1>
      </div> : (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <Field
              label="Stored video"
              style={{ flex: 1, minWidth: 0 }}
              validationState={storageError ? "warning" : "none"}
              validationMessage={storageError || (storageLoading ? "Loading stored videos..." : undefined)}
            >
              <Dropdown
                placeholder={!storageLoading && !storedVideos.length ? "No videos listed" : "Select a video"}
                value={selectedStoredVideo?.blob_name || ""}
                selectedOptions={selectedStoredVideo ? [selectedStoredVideo.blob_url] : []}
                disabled={storageLoading || storedVideos.length === 0}
                style={{ width: "100%", minWidth: 0 }}
                button={{ style: { whiteSpace: "normal", overflowWrap: "anywhere", textAlign: "left" } }}
                listbox={{ style: { maxHeight: 320, overflowY: "auto" } }}
                onOptionSelect={(_event, data) => { if (data.optionValue) setBlobUrl(data.optionValue); }}
              >
                {storedVideos.map((video) => (
                  <Option key={video.blob_url} value={video.blob_url} text={video.blob_name}>
                    <div style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                      <div>{video.blob_name}</div>
                      <Text size={100} className={cls.meta}>{(video.size_bytes / (1024 * 1024)).toFixed(1)} MB</Text>
                    </div>
                  </Option>
                ))}
              </Dropdown>
            </Field>
            <Button
              appearance="subtle"
              icon={<ArrowClockwise20Regular />}
              aria-label="Refresh stored videos"
              title="Refresh stored videos"
              disabled={storageLoading}
              style={{ marginTop: 28 }}
              onClick={() => setStorageReload((reload) => reload + 1)}
            />
          </div>
          {storageScope && <Text size={100} className={cls.meta} style={{ overflowWrap: "anywhere" }}>{storageScope}</Text>}
          {storageCursor && (
            <Button appearance="subtle" icon={<ChevronDown20Regular />} disabled={storageLoading} onClick={() => void loadStoredVideos(storageCursor)}>
              Load more videos
            </Button>
          )}
          <Field label="Blob URL">
            <Input
              type="url"
              value={blobUrl}
              maxLength={8192}
              placeholder="https://account.blob.core.windows.net/videos/match.mp4"
              style={{ width: "100%", minWidth: 0 }}
              onChange={(_event, data) => setBlobUrl(data.value)}
              onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); startReference(); } }}
            />
          </Field>
        </div>
      )}

      <div style={{ marginTop: 12, display: "flex", gap: 8, flexWrap: "wrap" }}>
        {sourceMode === "upload" ? <Button appearance="primary" onClick={() => inputRef.current?.click()} icon={<ArrowUpload24Regular />}>
          Choose files
        </Button> : <Button appearance="primary" onClick={startReference} disabled={!blobUrl.trim() || referenceBusy} icon={<Play24Regular />}>
          {referenceBusy ? "Starting..." : "Start processing"}
        </Button>}
        {hasDone && (
          <Button appearance="subtle" icon={<Dismiss20Regular />} onClick={clearDone}>
            Clear finished
          </Button>
        )}
      </div>

      {items.map((it) => {
        const job = it.job;
        const showUploading = it.source === "upload" && it.uploadPct > 0 && it.uploadPct < 100 && !job;
        // Build the stage list rendered in the stepper. If we don't have a job
        // yet (still uploading), show a synthetic upload stage driven by the
        // browser's chunked-PUT progress; the rest stay pending. Once /process
        // returns a job the backend stages take over.
        const synthStages: StageInfo[] = job?.stages?.length
          ? job.stages
          : [
              {
                name: "upload",
                status: it.error ? "failed" : it.uploadPct >= 100 ? "completed" : it.uploadPct > 0 ? "running" : "pending",
                progress: it.uploadPct,
                detail: it.uploadPct > 0 && it.uploadPct < 100 ? `${it.uploadPct}%` : "",
                error: it.error,
              },
              { name: "analyze", status: "pending", progress: 0, detail: "" },
            ];
        return (
          <div key={it.key} className={cls.uploadRow}>
            <div className={cls.uploadHeader}>
              <Text size={300} weight="semibold" truncate wrap={false}>{it.filename}</Text>
              {it.queued && <Text size={200} className={cls.meta}>queued</Text>}
              {job && (
                <Text size={200} className={cls.meta}>
                  {job.status} · {job.progress}%
                  {it.startedAt > 0 && job.status !== "indexed" && job.status !== "failed" &&
                    ` · ${formatElapsed(Date.now() - it.startedAt)}`}
                </Text>
              )}
            </div>
            <StageStepper stages={it.source === "storage"
              ? synthStages.map((stage) => stage.name === "upload" ? { ...stage, status: "skipped", detail: "Stored video", error: null } : stage)
              : synthStages} />
            {showUploading && (
              <div style={{ marginTop: 6 }}>
                <Text size={200}>Uploading… {it.uploadPct}%</Text>
                <ProgressBar value={it.uploadPct / 100} />
              </div>
            )}
            {job && (
              <div style={{ marginTop: 6 }}>
                {job.status === "indexed" && (
                  <Text size={200} style={{ color: tokens.colorPaletteGreenForeground1, marginTop: 4, display: "flex", alignItems: "center", gap: 4 }}>
                    <CheckmarkCircle24Filled /> Indexed — run a query below.
                  </Text>
                )}
                {job.status === "failed" && (
                  <Text size={200} style={{ color: tokens.colorPaletteRedForeground1, marginTop: 4, display: "flex", alignItems: "flex-start", gap: 4 }}>
                    <ErrorCircle24Filled /> {job.error}
                  </Text>
                )}
              </div>
            )}
            {it.error && (
              <Text size={200} style={{ color: tokens.colorPaletteRedForeground1, marginTop: 6 }}>{it.error}</Text>
            )}
            {job?.status === "indexed" && (
              <SceneFilmstrip
                videoId={job.video_id}
                filename={it.filename}
              />
            )}
          </div>
        );
      })}
    </Card>
  );
}
