import { useEffect, useState } from "react";
import {
  Body1, Button, Card, Dialog, DialogBody, DialogContent, DialogSurface, DialogTitle, DialogTrigger,
  Spinner, Subtitle2, Text, Tooltip, makeStyles, shorthands, tokens,
} from "@fluentui/react-components";
import { ChevronDown16Regular, ChevronUp16Regular, Dismiss20Regular } from "@fluentui/react-icons";
import type { SceneItem, SearchResultItem } from "../api";
import { listScenes } from "../api";
import VideoPlayer from "./VideoPlayer";
import SceneList from "./SceneList";

interface Props {
  videoId: string;
  filename: string;
  /** Auto-collapse the strip when scene count exceeds this. */
  collapseThreshold?: number;
}

function fmt(s: number): string {
  if (!isFinite(s) || s < 0) return "0:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

const useStyles = makeStyles({
  wrap: { padding: "12px 14px 14px", marginTop: "12px" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" },
  meta: { color: tokens.colorNeutralForeground3 },
  strip: {
    marginTop: "10px",
    display: "flex",
    gap: "8px",
    overflowX: "auto",
    overflowY: "hidden",
    paddingBottom: "6px",
    scrollSnapType: "x proximity",
  },
  tile: {
    flex: "0 0 auto",
    width: "140px",
    cursor: "pointer",
    scrollSnapAlign: "start",
    contentVisibility: "auto",
    containIntrinsicSize: "140px 110px",
    display: "flex",
    flexDirection: "column",
    gap: "2px",
    transitionProperty: "transform",
    transitionDuration: "100ms",
    ":hover": { transform: "translateY(-2px)" },
  },
  thumb: {
    width: "100%",
    aspectRatio: "16 / 9",
    background: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusMedium,
    overflow: "hidden",
    position: "relative",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    ...shorthands.border("1px", "solid", tokens.colorNeutralStroke2),
  },
  thumbImg: { width: "100%", height: "100%", objectFit: "cover", display: "block" },
  thumbPlaceholder: { color: tokens.colorNeutralForeground4, fontSize: "10px" },
  timeBadge: {
    position: "absolute",
    bottom: "4px",
    right: "4px",
    padding: "0 6px",
    borderRadius: tokens.borderRadiusSmall,
    background: "rgba(0,0,0,0.65)",
    color: "#fff",
    fontSize: "10px",
    lineHeight: "16px",
  },
  caption: {
    fontSize: "11px",
    color: tokens.colorNeutralForeground2,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  loading: { padding: "12px 0", color: tokens.colorNeutralForeground3, display: "flex", alignItems: "center", gap: "8px" },
  dialogSurface: { maxWidth: "1100px", width: "min(96vw, 1100px)" },
  dialogGrid: {
    display: "grid",
    gridTemplateColumns: "minmax(0, 1.5fr) minmax(280px, 1fr)",
    gap: "16px",
    "@media (max-width: 900px)": { gridTemplateColumns: "1fr" },
  },
});

function sceneToItem(s: SceneItem): SearchResultItem {
  return {
    id: s.id,
    video_id: s.video_id,
    filename: s.filename,
    start_seconds: s.start_seconds,
    end_seconds: s.end_seconds,
    score: 0,
    summary: s.summary,
    transcript_excerpt: s.transcript_excerpt,
    labels: s.labels,
    thumbnail_url: s.thumbnail_url,
    blob_url: "",
    play_url: s.play_url,
  };
}

export default function SceneFilmstrip({
  videoId, filename, collapseThreshold = 60,
}: Props) {
  const cls = useStyles();
  const [scenes, setScenes] = useState<SceneItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<SceneItem | null>(null);
  const [currentTime, setCurrentTime] = useState(0);

  useEffect(() => {
    let canceled = false;
    setLoading(true); setErr(null);
    listScenes(videoId)
      .then((r) => { if (!canceled) { setScenes(r.items); setExpanded(r.items.length <= collapseThreshold); } })
      .catch((e) => { if (!canceled) setErr(e?.message || String(e)); })
      .finally(() => { if (!canceled) setLoading(false); });
    return () => { canceled = true; };
  }, [videoId, collapseThreshold]);

  const tooBig = scenes.length > collapseThreshold;
  const visible = expanded ? scenes : scenes.slice(0, collapseThreshold);

  return (
    <Card className={cls.wrap}>
      <div className={cls.header}>
        <div>
          <Subtitle2>Scenes</Subtitle2>
          <Text size={200} block className={cls.meta}>
            {loading ? "loading…" : `${scenes.length} scene${scenes.length === 1 ? "" : "s"} extracted · click any thumbnail to play`}
          </Text>
        </div>
        {tooBig && (
          <Button
            appearance="subtle"
            size="small"
            icon={expanded ? <ChevronUp16Regular /> : <ChevronDown16Regular />}
            onClick={() => setExpanded((e) => !e)}
          >
            {expanded ? "Show less" : `Show all ${scenes.length}`}
          </Button>
        )}
      </div>

      {loading && <div className={cls.loading}><Spinner size="tiny" /> loading scenes…</div>}
      {err && <Text size={200} style={{ color: tokens.colorPaletteRedForeground1 }}>{err}</Text>}
      {!loading && !err && scenes.length === 0 && (
        <Body1 className={cls.meta} style={{ marginTop: 8 }}>No scenes were extracted.</Body1>
      )}

      {visible.length > 0 && (
        <div className={cls.strip}>
          {visible.map((s) => (
            <Tooltip
              key={s.id}
              relationship="description"
              content={
                <div style={{ maxWidth: 320 }}>
                  <Text size={200} weight="semibold">{fmt(s.start_seconds)} – {fmt(s.end_seconds)}</Text>
                  {s.summary && <Text size={200} block style={{ marginTop: 4 }}>{s.summary}</Text>}
                  {s.entities.length > 0 && (
                    <Text size={100} block style={{ marginTop: 4, color: tokens.colorNeutralForeground3 }}>
                      👤 {s.entities.slice(0, 4).join(" · ")}
                    </Text>
                  )}
                  {s.labels.length > 0 && (
                    <Text size={100} block style={{ color: tokens.colorNeutralForeground3 }}>
                      🏷 {s.labels.slice(0, 4).join(" · ")}
                    </Text>
                  )}
                </div>
              }
            >
              <div className={cls.tile} onClick={() => { setSelected(s); setCurrentTime(s.start_seconds); }}>
                <div className={cls.thumb}>
                  {s.thumbnail_url
                    ? <img className={cls.thumbImg} src={s.thumbnail_url} alt="" loading="lazy" decoding="async" />
                    : <span className={cls.thumbPlaceholder}>no thumb</span>}
                  <span className={cls.timeBadge}>{fmt(s.start_seconds)}</span>
                </div>
                <span className={cls.caption}>{s.summary || `Scene ${fmt(s.start_seconds)}`}</span>
              </div>
            </Tooltip>
          ))}
        </div>
      )}

      <Dialog
        open={selected !== null}
        onOpenChange={(_e, d) => { if (!d.open) setSelected(null); }}
      >
        <DialogSurface className={cls.dialogSurface}>
          <DialogBody>
            <DialogTitle
              action={
                <DialogTrigger action="close" disableButtonEnhancement>
                  <Button appearance="subtle" icon={<Dismiss20Regular />} aria-label="Close" />
                </DialogTrigger>
              }
            >
              {filename}
            </DialogTitle>
            <DialogContent>
              <div className={cls.dialogGrid}>
                <VideoPlayer
                  item={selected ? sceneToItem(selected) : null}
                  loopWindow={false}
                  onTimeUpdate={setCurrentTime}
                />
                <SceneList
                  scenes={scenes}
                  currentTime={currentTime}
                  onPick={(s) => { setSelected(s); setCurrentTime(s.start_seconds); }}
                />
              </div>
            </DialogContent>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </Card>
  );
}
