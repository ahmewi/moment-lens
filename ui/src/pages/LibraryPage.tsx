import { useEffect, useState } from "react";
import {
  Badge, Button, Card, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, DialogTrigger,
  Spinner, Subtitle1, Subtitle2,
  Text, makeStyles, tokens,
} from "@fluentui/react-components";
import { ArrowSync24Regular, Delete24Regular, Open24Regular } from "@fluentui/react-icons";
import type { VideoLibraryItem } from "../api";
import { deleteVideo, listVideos } from "../api";
import { useNavigate } from "react-router-dom";

const useStyles = makeStyles({
  page: { padding: "24px 32px", display: "flex", flexDirection: "column", gap: "16px" },
  toolbar: { display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: "12px" },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
    gap: "16px",
  },
  card: {
    padding: "0",
    overflow: "hidden",
    transitionProperty: "transform, box-shadow",
    transitionDuration: "120ms",
    ":hover": { transform: "translateY(-2px)", boxShadow: tokens.shadow16 },
  },
  thumbWrap: {
    width: "100%",
    aspectRatio: "16 / 9",
    background: tokens.colorNeutralBackground3,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
  },
  thumb: {
    width: "100%",
    height: "100%",
    objectFit: "cover",
    display: "block",
  },
  thumbPlaceholder: {
    color: tokens.colorNeutralForeground4,
    fontSize: "12px",
  },
  body: { padding: "12px 14px 14px" },
  filename: {
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  meta: { color: tokens.colorNeutralForeground3, marginTop: "4px" },
  footer: { display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "12px" },
  empty: { color: tokens.colorNeutralForeground3, padding: "32px", textAlign: "center" },
});

function fmtDur(seconds: number): string {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

export default function LibraryPage() {
  const cls = useStyles();
  const navigate = useNavigate();
  const [items, setItems] = useState<VideoLibraryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<VideoLibraryItem | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  async function refresh() {
    setLoading(true); setErr(null);
    try {
      const r = await listVideos();
      setItems(r.items);
    } catch (e: any) { setErr(e.message || String(e)); }
    finally { setLoading(false); }
  }

  async function doDelete(v: VideoLibraryItem) {
    setDeletingId(v.video_id);
    try {
      await deleteVideo(v.video_id, true);
      setItems((arr) => arr.filter((x) => x.video_id !== v.video_id));
      setConfirmTarget(null);
    } catch (e: any) {
      setErr(`Delete failed: ${e?.message || e}`);
    } finally {
      setDeletingId(null);
    }
  }

  useEffect(() => { refresh(); }, []);

  return (
    <div className={cls.page}>
      <div className={cls.toolbar}>
        <div>
          <Subtitle1>Indexed library</Subtitle1>
          <Text block className={cls.meta}>
            Videos indexed with Azure AI Video Indexer.
          </Text>
        </div>
        <Button icon={<ArrowSync24Regular />} onClick={refresh} disabled={loading}>
          Refresh
        </Button>
      </div>

      {loading && <div className={cls.empty}><Spinner label="Loading library…" /></div>}
      {err && <Text style={{ color: tokens.colorPaletteRedForeground1 }}>{err}</Text>}
      {!loading && !err && items.length === 0 && (
        <div className={cls.empty}>
          <Text>No videos indexed yet.</Text>
        </div>
      )}

      <div className={cls.grid}>
        {items.map((v) => (
          <Card key={v.video_id} className={cls.card}>
            <div className={cls.thumbWrap}>
              {v.thumbnail_url
                ? <img className={cls.thumb} src={v.thumbnail_url} alt={v.filename} loading="lazy" />
                : <span className={cls.thumbPlaceholder}>no thumbnail</span>}
            </div>
            <div className={cls.body}>
              <Subtitle2 className={cls.filename}>{v.filename}</Subtitle2>
              <Text size={200} className={cls.meta} block>id: {v.video_id} · duration {fmtDur(v.duration_seconds)}</Text>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 10 }}>
                <Badge appearance="tint" color="brand">{v.scenes} scenes</Badge>
              </div>
              <div className={cls.footer}>
                <Text size={200} className={cls.meta}>Azure AI Video Indexer</Text>
                <div style={{ display: "flex", gap: 4 }}>
                  <Button
                    appearance="subtle"
                    icon={<Delete24Regular />}
                    title="Delete video"
                    onClick={() => setConfirmTarget(v)}
                    disabled={deletingId === v.video_id}
                  />
                  <Button
                    appearance="subtle"
                    icon={<Open24Regular />}
                    onClick={() => navigate(`/video-indexer?video=${encodeURIComponent(v.video_id)}`)}
                  >
                    View insights
                  </Button>
                </div>
              </div>
            </div>
          </Card>
        ))}
      </div>

      <Dialog open={confirmTarget !== null} onOpenChange={(_e, d) => { if (!d.open) setConfirmTarget(null); }}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Delete this video?</DialogTitle>
            <DialogContent>
              <Text block>
                This will permanently delete <b>{confirmTarget?.filename}</b> from the Video Indexer
                search index and remove its thumbnail and insights JSON from storage. Source videos
                are retained when referenced or shared.
              </Text>
              <Text block style={{ marginTop: 8, color: tokens.colorPaletteRedForeground1 }}>
                This action cannot be undone.
              </Text>
            </DialogContent>
            <DialogActions>
              <DialogTrigger disableButtonEnhancement>
                <Button appearance="secondary">Cancel</Button>
              </DialogTrigger>
              <Button
                appearance="primary"
                style={{ background: tokens.colorPaletteRedBackground3 }}
                disabled={deletingId !== null}
                onClick={() => confirmTarget && doDelete(confirmTarget)}
              >
                {deletingId ? "Deleting…" : "Delete"}
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </div>
  );
}
