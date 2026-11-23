import { useEffect, useRef } from "react";
import { Body1, Card, CardHeader, Subtitle2, Text, tokens } from "@fluentui/react-components";
import type { SearchResultItem } from "../api";

interface Props {
  item: SearchResultItem | null;
  /** When true, the player will auto-pause at item.end_seconds and loop back to start. */
  loopWindow?: boolean;
  /** Called on every `timeupdate` with the current playback position (seconds). */
  onTimeUpdate?: (currentTime: number) => void;
}

function fmt(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

export default function VideoPlayer({ item, loopWindow = true, onTimeUpdate }: Props) {
  const ref = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (!item || !ref.current) return;
    const v = ref.current;
    // Strip query (SAS) so we can compare just the underlying blob URL.
    const baseOf = (u: string) => (u || "").split("?")[0].split("#")[0];
    const sameSrc = baseOf(v.currentSrc || v.src) === baseOf(item.play_url);

    let canceled = false;

    // Wait until enough data is buffered around the seek point so Chrome
    // doesn't silently drop the seek and start from byte 0.
    const seekToStart = () => {
      if (canceled) return;
      const target = item.start_seconds;
      const onSeeked = () => {
        v.removeEventListener("seeked", onSeeked);
        if (canceled) return;
        v.play().catch(() => { /* user gesture may be required */ });
      };
      v.addEventListener("seeked", onSeeked);
      try {
        v.currentTime = target;
      } catch {
        // Some browsers throw if duration not yet known — retry once on canplay.
        v.removeEventListener("seeked", onSeeked);
        v.addEventListener("canplay", () => { v.currentTime = target; }, { once: true });
      }
    };

    const onTime = () => {
      if (onTimeUpdate) onTimeUpdate(v.currentTime);
      if (loopWindow && v.currentTime >= item.end_seconds - 0.05) {
        v.pause();
        v.currentTime = item.start_seconds;
      }
    };

    if (sameSrc && v.readyState >= 2 /* HAVE_CURRENT_DATA */) {
      seekToStart();
    } else {
      // Force preload so byte-range request for the seek target is issued early.
      v.preload = "auto";
      v.src = item.play_url;
      v.load();
      // 'canplay' guarantees the browser has buffered at least a frame at the
      // current position; combined with the explicit seek+seeked pair this
      // reliably starts playback at start_seconds.
      const onCanPlay = () => {
        v.removeEventListener("canplay", onCanPlay);
        seekToStart();
      };
      v.addEventListener("canplay", onCanPlay);
    }
    v.addEventListener("timeupdate", onTime);
    return () => {
      canceled = true;
      v.removeEventListener("timeupdate", onTime);
    };
  }, [item?.id, item?.start_seconds, item?.end_seconds, item?.play_url, loopWindow, onTimeUpdate]);

  if (!item) {
    return (
      <Card style={{ padding: 24, height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <Body1 style={{ color: tokens.colorNeutralForeground3, textAlign: "center" }}>
          Select a result on the left to play it here at the matched timestamp.
        </Body1>
      </Card>
    );
  }

  return (
    <Card style={{ padding: 0, overflow: "hidden", background: tokens.colorNeutralBackground2 }}>
      <video
        ref={ref}
        controls
        playsInline
        style={{ width: "100%", display: "block", background: "#000", aspectRatio: "16 / 9", objectFit: "contain" }}
      />
      <div style={{ padding: 16 }}>
        <CardHeader
          header={<Subtitle2>{item.filename}</Subtitle2>}
          description={
            <Text size={200} style={{ color: tokens.colorNeutralForeground3 }}>
              {fmt(item.start_seconds)} → {fmt(item.end_seconds)}
              {" · "}score {item.score.toFixed(3)}
              {item.rerank_score != null ? ` · rerank ${item.rerank_score.toFixed(3)}` : ""}
              {loopWindow ? " · loops to start at end of clip" : ""}
            </Text>
          }
        />
        {item.summary && <Body1 style={{ marginTop: 12 }}>{item.summary}</Body1>}
        {item.transcript_excerpt && (
          <Text size={200} italic block style={{ marginTop: 8, color: tokens.colorNeutralForeground3 }}>
            “{item.transcript_excerpt.slice(0, 320)}{item.transcript_excerpt.length > 320 ? "…" : ""}”
          </Text>
        )}
      </div>
    </Card>
  );
}
