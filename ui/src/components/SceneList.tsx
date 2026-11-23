import { Badge, Card, Subtitle2, Text, makeStyles, shorthands, tokens } from "@fluentui/react-components";
import { Play16Filled } from "@fluentui/react-icons";
import type { SceneItem } from "../api";

interface Props {
  scenes: SceneItem[];
  /** The video's current playback position; used to highlight the active scene. */
  currentTime?: number;
  onPick?: (scene: SceneItem) => void;
  loading?: boolean;
}

function fmt(s: number): string {
  if (!isFinite(s) || s < 0) return "0:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

const useStyles = makeStyles({
  wrap: {
    padding: 0,
    overflow: "hidden",
    display: "flex",
    flexDirection: "column",
    maxHeight: "60vh",
  },
  header: {
    padding: "10px 14px",
    borderBottom: `1px solid ${tokens.colorNeutralStroke2}`,
    display: "flex",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: "8px",
  },
  list: {
    overflowY: "auto",
    minHeight: 0,
  },
  row: {
    display: "grid",
    gridTemplateColumns: "104px 1fr",
    gap: "12px",
    padding: "10px 14px",
    cursor: "pointer",
    contentVisibility: "auto",
    containIntrinsicSize: "1px 90px",
    ...shorthands.borderBottom("1px", "solid", tokens.colorNeutralStroke3),
    transitionProperty: "background-color",
    transitionDuration: "100ms",
    ":hover": { background: tokens.colorNeutralBackground1Hover },
  },
  rowActive: {
    background: tokens.colorBrandBackground2,
    ":hover": { background: tokens.colorBrandBackground2Hover },
  },
  thumb: {
    width: "104px",
    aspectRatio: "16 / 9",
    background: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusSmall,
    overflow: "hidden",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    position: "relative",
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
  meta: { color: tokens.colorNeutralForeground3 },
  chips: { display: "flex", flexWrap: "wrap", gap: "4px", marginTop: "6px" },
  empty: { padding: "24px", textAlign: "center", color: tokens.colorNeutralForeground3 },
  playingDot: {
    display: "inline-block",
    width: "8px", height: "8px", borderRadius: "50%",
    background: tokens.colorBrandForeground1,
    marginRight: "6px",
  },
});

export default function SceneList({ scenes, currentTime, onPick, loading }: Props) {
  const cls = useStyles();
  const activeIdx = scenes.findIndex(
    (s) => currentTime != null && currentTime >= s.start_seconds && currentTime <= s.end_seconds + 0.1,
  );

  return (
    <Card className={cls.wrap}>
      <div className={cls.header}>
        <Subtitle2>Scenes ({scenes.length})</Subtitle2>
        <Text size={200} className={cls.meta}>
          {loading ? "loading…" : "click a row to play that moment"}
        </Text>
      </div>
      <div className={cls.list}>
        {scenes.length === 0 && !loading && (
          <div className={cls.empty}>No scene metadata available for this video.</div>
        )}
        {scenes.map((s, i) => {
          const active = i === activeIdx;
          const lead = (s.transcript_excerpt || "").slice(0, 200);
          return (
            <div
              key={s.id}
              data-scene-idx={i}
              className={`${cls.row} ${active ? cls.rowActive : ""}`}
              onClick={() => onPick?.(s)}
            >
              <div className={cls.thumb}>
                {s.thumbnail_url
                  ? <img className={cls.thumbImg} src={s.thumbnail_url} alt="" loading="lazy" decoding="async" />
                  : <span className={cls.thumbPlaceholder}>no thumb</span>}
                <span className={cls.timeBadge}>{fmt(s.start_seconds)}</span>
              </div>
              <div style={{ minWidth: 0 }}>
                <Text size={200} className={cls.meta}>
                  {active && <span className={cls.playingDot} />}
                  {fmt(s.start_seconds)} – {fmt(s.end_seconds)} · {(s.end_seconds - s.start_seconds).toFixed(1)}s
                </Text>
                {s.summary && (
                  <Text size={300} block style={{ marginTop: 2 }}>{s.summary}</Text>
                )}
                {lead && (
                  <Text size={200} italic block style={{ marginTop: 4, color: tokens.colorNeutralForeground3 }}>
                    “{lead}{s.transcript_excerpt.length > 200 ? "…" : ""}”
                  </Text>
                )}
                {(s.entities.length > 0 || s.labels.length > 0 || s.speakers.length > 0) && (
                  <div className={cls.chips}>
                    {s.entities.slice(0, 4).map((e) => (
                      <Badge key={`e-${e}`} appearance="tint" color="brand" size="small">{e}</Badge>
                    ))}
                    {s.labels.slice(0, 4).map((l) => (
                      <Badge key={`l-${l}`} appearance="outline" color="informative" size="small">{l}</Badge>
                    ))}
                    {s.speakers.slice(0, 2).map((sp) => (
                      <Badge key={`s-${sp}`} appearance="ghost" color="warning" size="small">
                        <Play16Filled style={{ marginRight: 2 }} />{sp}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}
