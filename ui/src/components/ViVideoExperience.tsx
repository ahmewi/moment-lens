import { useEffect, useId, useRef, useState } from "react";
import {
  Badge, Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Dropdown, Field, Option, Spinner, Subtitle2, Tab, TabList, Text, makeStyles, tokens,
} from "@fluentui/react-components";
import { ArrowSync20Regular, Play20Regular, Sparkle20Regular } from "@fluentui/react-icons";
import type { ContentType, ProcessJob, ProfileAnalysisPlan, SearchResultItem, VideoEvent, VideoEvents } from "../api";
import { CONTENT_TYPES, analyzeContentProfile, enrichVideoEvents, getContentProfiles, getProfileAnalysisPlan, getStatus, getVideoEvents, getViWidgetAccess } from "../api";
import VideoPlayer from "./VideoPlayer";

const VI_ORIGIN = "https://www.videoindexer.ai";
const WIDGET_SRC = "https://unpkg.com/@azure/video-indexer-widgets@1.0.5/dist/global.min.js";
const WIDGET_SRI = "sha384-cu7/tKb1iaDSbDk90D6Q7ppWFRDz6CPWTN5HmpKyPadwrI6yBNEioqeuRmnLc5Ql";
const COLORS: Record<string, string> = {
  goal: "#107C10", foul: "#C15B16", yellow_card: "#C19C00", red_card: "#C4314B",
  var_decision: "#0078D4", corner: "#6B5B95",
};
type WidgetApi = { CustomInsights: new (id: string, options: object, config: object) => { render: () => void } };
let widgetPromise: Promise<WidgetApi> | null = null;

function loadWidgets(): Promise<WidgetApi> {
  const api = () => (window as unknown as { vi?: { widgets: WidgetApi } }).vi?.widgets;
  if (api()) return Promise.resolve(api()!);
  if (!widgetPromise) widgetPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = WIDGET_SRC;
    script.integrity = WIDGET_SRI;
    script.crossOrigin = "anonymous";
    script.onload = () => api() ? resolve(api()!) : reject(new Error("VI widget API unavailable"));
    script.onerror = () => { script.remove(); widgetPromise = null; reject(new Error("VI widgets could not load")); };
    document.head.appendChild(script);
  });
  return widgetPromise;
}

function timestamp(seconds: number) {
  return `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds % 3600 / 60)).padStart(2, "0")}:${(seconds % 60).toFixed(3).padStart(6, "0")}`;
}

function clock(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", gap: "12px", minWidth: 0 },
  player: { width: "100%", aspectRatio: "16 / 9", border: "0", display: "block", backgroundColor: "#111" },
  row: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" },
  toolbar: { display: "flex", gap: "12px", alignItems: "end", flexWrap: "wrap" },
  muted: { color: tokens.colorNeutralForeground3 },
  error: { color: tokens.colorPaletteRedForeground1 },
  widget: { position: "relative", minHeight: "500px", overflow: "hidden", borderRadius: "8px", border: `1px solid ${tokens.colorNeutralStroke2}` },
  timeline: { position: "relative", height: "40px", backgroundColor: tokens.colorNeutralBackground3, borderRadius: "4px", margin: "4px 8px" },
  marker: { position: "absolute", top: "6px", width: "28px", height: "28px", minWidth: "28px", padding: "0", transform: "translateX(-50%)" },
  event: { display: "flex", gap: "10px", alignItems: "flex-start", padding: "10px 0", borderBottom: `1px solid ${tokens.colorNeutralStroke2}` },
  evidence: { display: "flex", flexDirection: "column", gap: "3px", minWidth: 0, overflowWrap: "anywhere" },
});

interface Props {
  item: SearchResultItem | null;
  onPickEvent: (event: VideoEvent) => void;
  onTimeUpdate: (seconds: number) => void;
  onEnriched?: () => void;
}

export default function ViVideoExperience({ item, onPickEvent, onTimeUpdate, onEnriched }: Props) {
  const cls = useStyles();
  const hostId = useId().replace(/:/g, "");
  const hostRef = useRef<HTMLDivElement>(null);
  const playerRef = useRef<HTMLIFrameElement>(null);
  const sourceRef = useRef<HTMLDivElement>(null);
  const startRef = useRef(item?.start_seconds ?? 0);
  const enrichedRef = useRef(onEnriched);
  enrichedRef.current = onEnriched;
  const [events, setEvents] = useState<VideoEvents | null>(null);
  const [playerUrl, setPlayerUrl] = useState("");
  const [playerMode, setPlayerMode] = useState("source");
  const [playerRevision, setPlayerRevision] = useState(0);
  const [profile, setProfile] = useState<ContentType>("soccer_match");
  const [kind, setKind] = useState("all");
  const [job, setJob] = useState<ProcessJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [profilePlan, setProfilePlan] = useState<ProfileAnalysisPlan | null>(null);
  const [profiles, setProfiles] = useState<ContentType[] | null>(null);
  const [profileError, setProfileError] = useState("");
  const [error, setError] = useState("");
  const [playerError, setPlayerError] = useState("");
  const [widgetError, setWidgetError] = useState("");
  const [revision, setRevision] = useState(0);
  const videoId = item?.video_id;
  const supported = profiles?.includes(profile) ?? false;

  useEffect(() => {
    let cancelled = false;
    getContentProfiles().then(response => { if (!cancelled) setProfiles(response.items.map(entry => entry.content_type)); })
      .catch(reason => { if (!cancelled) setProfileError(String(reason)); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    startRef.current = item?.start_seconds ?? 0;
    playerRef.current?.contentWindow?.postMessage({ time: startRef.current }, VI_ORIGIN);
  }, [item?.id, item?.start_seconds]);

  useEffect(() => {
    let cancelled = false;
    setEvents(null); setError(""); setKind("all"); setJob(null);
    if (videoId) getVideoEvents(videoId).then(data => {
      if (cancelled) return;
      setEvents(data);
      setProfile(data.content_type ?? "soccer_match");
    }).catch(reason => { if (!cancelled) setError(String(reason)); });
    return () => { cancelled = true; };
  }, [videoId, revision]);

  useEffect(() => { setPlayerMode("source"); }, [videoId]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    setPlayerUrl(""); setPlayerError("");
    if (playerMode !== "vi") return;
    async function refresh() {
      if (!videoId) return;
      try {
        const access = await getViWidgetAccess(videoId);
        if (cancelled) return;
        setPlayerError("");
        const params = new URLSearchParams({ accessToken: access.access_token, location: access.location,
          accountName: access.account_name, t: String(startRef.current), autoplay: "false", showCaptions: "true", captions: "en-US" });
        setPlayerUrl(`${VI_ORIGIN}/embed/player/${encodeURIComponent(access.account_id)}/${encodeURIComponent(access.vi_video_id)}?${params}`);
        timer = setTimeout(refresh, access.refresh_after_seconds * 1000);
      } catch (reason) {
        if (!cancelled) { setPlayerError(String(reason)); timer = setTimeout(refresh, 30000); }
      }
    }
    void refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [videoId, playerRevision, playerMode]);

  useEffect(() => {
    let cancelled = false;
    const host = hostRef.current;
    if (!host || !events || events.duration_seconds <= 0) return;
    setWidgetError("");
    let renderedWidth = 0;
    function render() {
      const width = host?.clientWidth ?? 0;
      if (width <= 0 || width === renderedWidth) return;
      renderedWidth = width;
      void loadWidgets().then(api => {
        if (cancelled || !host || !events) return;
        host.replaceChildren();
        const filtered = events.events.filter(event => kind === "all" || event.kind === kind);
        const groups = Object.entries(events.categories).map(([key, label], index) => ({
          id: index + 1, text: label, color: COLORS[key] ?? "#0078D4",
          instances: filtered.filter(event => event.kind === key).map(event => ({
            start: timestamp(event.start_seconds), end: timestamp(event.end_seconds),
            adjustedStart: timestamp(event.start_seconds), adjustedEnd: timestamp(event.end_seconds), confidence: event.confidence,
          })),
        })).filter(group => group.instances.length);
        new api.CustomInsights(hostId, { width: Math.max(240, host.clientWidth), height: 520 }, {
          duration: events.duration_seconds, viInsightsKeys: [], rawInsightsData: events.raw_insights_data,
          customData: groups.length ? [{ title: "First-view match events", key: "matchEvents", presets: ["all"], type: "capsule", items: groups }] : [],
          preset: "all", style: { theme: "Default" },
        }).render();
      }).catch(reason => { if (!cancelled) setWidgetError(String(reason)); });
    }
    render();
    const observer = new ResizeObserver(render);
    observer.observe(host);
    return () => { cancelled = true; observer.disconnect(); host.replaceChildren(); };
  }, [events, kind, hostId]);

  useEffect(() => {
    function receive(message: MessageEvent) {
      const insightFrame = hostRef.current?.querySelector("iframe");
      const fromInsights = message.source === insightFrame?.contentWindow;
      const fromPlayer = message.source === playerRef.current?.contentWindow;
      if (message.origin !== VI_ORIGIN || (!fromInsights && !fromPlayer) || !message.data) return;
      const time = message.data.time ?? message.data.currentTime;
      if (typeof time !== "number" || !Number.isFinite(time) || time < 0 || time > (events?.duration_seconds ?? 0)) return;
      onTimeUpdate(time);
      if (fromInsights && message.data.time !== undefined) {
        startRef.current = time;
        const sourcePlayer = sourceRef.current?.querySelector("video");
        if (sourcePlayer) {
          sourcePlayer.currentTime = time;
          void sourcePlayer.play().catch(() => {});
        }
        playerRef.current?.contentWindow?.postMessage({ time }, VI_ORIGIN);
        insightFrame?.contentWindow?.postMessage({ confirm: true, time }, VI_ORIGIN);
      }
    }
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [events?.duration_seconds, onTimeUpdate]);

  useEffect(() => {
    if (!videoId || !job || !["submitted", "processing"].includes(job.status)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await getStatus(job!.video_id);
        if (cancelled) return;
        if (next.status === "indexed") { setJob(next); setRevision(value => value + 1); enrichedRef.current?.(); }
        else if (next.status === "failed") { setJob(next); setError(next.error ?? "Event extraction failed"); }
        else { timer = setTimeout(poll, 3000); }
      } catch (reason) { if (!cancelled) { setError(String(reason)); setJob(null); } }
    }
    timer = setTimeout(poll, 1000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [job?.video_id, job?.status, videoId]);

  async function extract() {
    if (!videoId) return;
    setError(""); setStarting(true);
    try { setJob(await enrichVideoEvents(videoId, profile)); }
    catch (reason) { setError(String(reason)); }
    finally { setStarting(false); }
  }

  async function previewProfile() {
    setError(""); setStarting(true);
    try { setProfilePlan(await getProfileAnalysisPlan(profile)); }
    catch (reason) { setError(String(reason)); }
    finally { setStarting(false); }
  }

  async function analyzeProfile() {
    if (!profilePlan) return;
    setStarting(true); setError("");
    try {
      const result = await analyzeContentProfile(profilePlan.content_type);
      setJob(result.job); setProfilePlan(null);
    } catch (reason) { setError(String(reason)); }
    finally { setStarting(false); }
  }

  if (!item) return <div className={cls.root}><Subtitle2>Video insights</Subtitle2><Text className={cls.muted}>No video selected.</Text></div>;
  const visible = events?.events.filter(event => kind === "all" || event.kind === kind) ?? [];
  const busy = starting || (job && ["submitted", "processing"].includes(job.status));
  const lanes: VideoEvent[][] = [];
  const position = (event: VideoEvent) => Math.min(96, Math.max(4, event.start_seconds / Math.max(1, events?.duration_seconds ?? 0) * 100));
  for (const event of [...visible].sort((first, second) => first.start_seconds - second.start_seconds)) {
    const lane = lanes.find(row => position(event) - position(row[row.length - 1]) >= 12);
    if (lane) lane.push(event);
    else lanes.push([event]);
  }

  return <div className={cls.root}>
    <div className={cls.row}><Subtitle2>{item.filename}</Subtitle2><Badge appearance="tint">Video Indexer Advanced</Badge></div>
    <TabList selectedValue={playerMode} onTabSelect={(_, data) => setPlayerMode(String(data.value))}>
      <Tab value="source">Source video</Tab><Tab value="vi" disabled title="Hosted VI playback cannot reach private storage">VI player</Tab>
    </TabList>
    <Text size={200} className={cls.muted}>Hosted VI playback is unavailable with private storage.</Text>
    {playerMode === "vi" ? <>
      {playerUrl ? <iframe ref={playerRef} className={cls.player} src={playerUrl} title="Azure AI Video Indexer player"
        allow="autoplay; fullscreen; local-network-access" allowFullScreen
        onLoad={() => playerRef.current?.contentWindow?.postMessage({ time: startRef.current }, VI_ORIGIN)} />
        : !playerError && <Spinner label="Loading VI player" />}
      {playerError && <div className={cls.row}><Text role="alert" className={cls.error}>{playerError}</Text>
        <Button icon={<ArrowSync20Regular />} aria-label="Retry VI player access" title="Retry VI player access" onClick={() => setPlayerRevision(value => value + 1)} /></div>}
    </> : <div ref={sourceRef}><VideoPlayer item={item} loopWindow={false} onTimeUpdate={onTimeUpdate} /></div>}
    {item.match_reason && <Text size={200}>{item.visual_checked ? "Frames checked: " : "Text evidence: "}{item.match_reason}</Text>}
    <div className={cls.toolbar}>
      <Field label="Content profile"><Dropdown disabled={!!busy} value={CONTENT_TYPES.find(type => type.value === profile)?.label ?? profile}
        selectedOptions={[profile]} onOptionSelect={(_, data) => data.optionValue && setProfile(data.optionValue as ContentType)}>
        {CONTENT_TYPES.map(type => <Option key={type.value} value={type.value}>{type.label}</Option>)}
      </Dropdown></Field>
      <Button icon={<Sparkle20Regular />} onClick={extract} disabled={!supported || !!busy}>Extract events</Button>
      <Button icon={<Sparkle20Regular />} onClick={previewProfile} disabled={!supported || !!busy}>Analyze profile</Button>
      <Button icon={<ArrowSync20Regular />} aria-label="Refresh events" title="Refresh events" onClick={() => setRevision(value => value + 1)} disabled={!!busy} />
    </div>
    {profileError && <Text role="alert" className={cls.error}>Content profiles unavailable: {profileError}</Text>}
    {profiles && !supported && <Text size={200} className={cls.muted}>Custom event profile not configured.</Text>}
    {busy && <Spinner label="Analyzing moments" />}
    {job?.video_id.startsWith("profile-") && <Text size={200}>{job.stage ?? "Profile analysis queued"} · {job.progress}%</Text>}
    {profilePlan && <Dialog open onOpenChange={(_, data) => { if (!data.open && !starting) setProfilePlan(null); }}>
      <DialogSurface><DialogBody>
        <DialogTitle>Analyze {CONTENT_TYPES.find(type => type.value === profilePlan?.content_type)?.label ?? "content profile"} videos</DialogTitle>
        <DialogContent>{profilePlan?.queued_video_ids.length ?? 0} videos to analyze; {profilePlan?.skipped_video_ids.length ?? 0} analyzed previously; {profilePlan?.active_video_ids.length ?? 0} already processing. Up to {profilePlan?.scene_budget ?? 120} candidate scenes per video. Model usage is charged.</DialogContent>
        <DialogActions>
          <Button disabled={starting} onClick={() => setProfilePlan(null)}>Cancel</Button>
          <Button appearance="primary" icon={<Sparkle20Regular />} disabled={starting || !profilePlan?.queued_video_ids.length} onClick={analyzeProfile}>Analyze</Button>
        </DialogActions>
      </DialogBody></DialogSurface>
    </Dialog>}
    {error && <Text role="alert" className={cls.error}>{error}</Text>}
    {events && <>
      <div className={cls.row}><Subtitle2>Match events</Subtitle2><Badge appearance="tint">LLM enrichment</Badge><Text size={200} className={cls.muted}>First-view only</Text></div>
      <Text size={200} className={cls.muted}>{events.status === "needs_enrichment" ? "Not analysed for custom events yet."
        : events.status === "not_applicable" ? "Native insights only."
        : events.analysis_mode === "query_targeted" ? `${events.reviewed_scenes}/${events.total_scenes} scenes reviewed · targeted coverage${
            events.stop_reason === "scene_budget" ? ` · scene budget (${events.scene_budget ?? events.attempted_scenes}) reached, so moments in other scenes can be untagged. Run Extract events again to review more, or search with Visual confirmation on to find them`
            : events.analysis_complete ? " · pass finished" : " · incomplete pass"}.`
        : `${events.reviewed_scenes}/${events.total_scenes} scenes reviewed${events.status === "partial" ? " (partial coverage)" : ""}.`}</Text>
      <Field label="Event type"><Dropdown value={kind === "all" ? "All events" : events.categories[kind]} selectedOptions={[kind]}
        onOptionSelect={(_, data) => data.optionValue && setKind(data.optionValue)}>
        <Option value="all">All events</Option>
        {Object.entries(events.categories).map(([key, label]) => <Option key={key} value={key}>{label}</Option>)}
      </Dropdown></Field>
      {lanes.map((lane, index) => <div key={index} className={cls.timeline} aria-label={`First-view event timeline row ${index + 1}`}>
        {lane.map(event => <Button key={event.id} className={cls.marker} icon={<Play20Regular />}
          style={{ left: `${position(event)}%`, color: COLORS[event.kind] }}
          title={`${event.label} ${clock(event.start_seconds)}`} aria-label={`${event.label} at ${clock(event.start_seconds)}`}
          onClick={() => onPickEvent(event)} />)}
      </div>)}
      {!visible.length && <Text size={200} className={cls.muted}>No confirmed first-view events in this selection.</Text>}
      {visible.map(event => <div key={event.id} className={cls.event}>
        <Button icon={<Play20Regular />} appearance="subtle" aria-label={`Play ${event.label} at ${clock(event.start_seconds)}`} onClick={() => onPickEvent(event)} />
        <div className={cls.evidence}><Text weight="semibold">{event.label} · {clock(event.start_seconds)}{event.subject ? ` · ${event.subject}` : ""}</Text>
          <Text size={200}>{event.evidence}</Text><Text size={100} className={cls.muted}>Model confidence {Math.round(event.confidence * 100)}%</Text></div>
      </div>)}
      <Subtitle2>VI insights and enriched events</Subtitle2>
      {widgetError && <Text role="alert" className={cls.error}>{widgetError}</Text>}
      <div className={cls.widget}><div ref={hostRef} id={hostId} /></div>
    </>}
  </div>;
}
