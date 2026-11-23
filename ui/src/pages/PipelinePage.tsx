import { useEffect, useState } from "react";
import { Dropdown, Field, Option, Subtitle1, Text, tokens, makeStyles } from "@fluentui/react-components";
import { useSearchParams } from "react-router-dom";
import type { PipelineOption, SceneItem, SearchResponse, SearchResultItem, VideoEvent, VideoLibraryItem } from "../api";
import { listScenes, listVideos } from "../api";
import QueryPane from "../components/QueryPane";
import ResultsList from "../components/ResultsList";
import ViVideoExperience from "../components/ViVideoExperience";
import SceneList from "../components/SceneList";

interface Props {
  option: PipelineOption;
  title: string;
  blurb: string;
}

const useStyles = makeStyles({
  page: {
    display: "grid",
    gridTemplateColumns: "minmax(300px, 0.8fr) minmax(0, 1.4fr)",
    gap: "24px",
    padding: "24px 32px",
    "@media (max-width: 1100px)": {
      gridTemplateColumns: "1fr",
    },
    "@media (max-width: 600px)": { padding: "16px", gap: "16px" },
  },
  left: { display: "flex", flexDirection: "column", gap: "16px", minWidth: 0 },
  meta: { color: tokens.colorNeutralForeground3 },
});

// One line saying what a search returned; for a Moment tag it also says what the deep search did, so a short
// list is never a mystery (and a cut by "Number of results" is never silent).
function describeResults(r: SearchResponse): string {
  if (!r.event_kind) return `${r.count} results for \u201c${r.query}\u201d`;
  const kind = r.event_kind.replace(/_/g, " ");
  const total = r.total_matches ?? r.count;
  const shown = total > r.count ? `${r.count} of ${total}` : `${r.count}`;
  const cut = total > r.count ? " Raise \u201cNumber of results\u201d to see all." : "";
  const deep = r.deep_search;
  if (!deep) return `${shown} tagged moments (${kind}). Turn on Visual confirmation under Advanced to also look for moments the tags missed.${cut}`;
  if (deep.failed) return `${shown} tagged moments (${kind}); the deep search was unavailable.${cut}`;
  return `${shown} moments (${kind}): ${deep.tagged} tagged + ${deep.recovered} found by deep search \u00b7 ${deep.frames_checked} clips checked against frames.${cut}`;
}

export default function PipelinePage({ option, title }: Props) {
  const cls = useStyles();
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [selected, setSelected] = useState<SearchResultItem | null>(null);
  const [scenes, setScenes] = useState<SceneItem[]>([]);
  const [scenesLoading, setScenesLoading] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [videos, setVideos] = useState<VideoLibraryItem[]>([]);
  const [sceneError, setSceneError] = useState("");
  const [params, setParams] = useSearchParams();
  const requestedVideo = params.get("video");

  useEffect(() => {
    let canceled = false;
    listVideos().then(response => { if (!canceled) setVideos(response.items); })
      .catch(reason => { if (!canceled) setSceneError(String(reason)); });
    return () => { canceled = true; };
  }, []);

  useEffect(() => {
    if (!requestedVideo) return;
    let canceled = false;
    setSceneError(""); setSelected(null); setScenes([]); setScenesLoading(true);
    listScenes(requestedVideo, option).then(response => {
      if (canceled) return;
      if (response.items[0]) pickScene(response.items[0]);
      else setSceneError("No searchable scenes for this video.");
    }).catch(reason => { if (!canceled) setSceneError(String(reason)); })
      .finally(() => { if (!canceled) setScenesLoading(false); });
    return () => { canceled = true; };
  }, [requestedVideo, option]);

  // Whenever the selected result changes to a different video, fetch its scene list.
  useEffect(() => {
    if (!selected) { setScenes([]); return; }
    let canceled = false;
    setScenesLoading(true);
    listScenes(selected.video_id, option)
      .then((r) => { if (!canceled) setScenes(r.items); })
      .catch(reason => { if (!canceled) { setScenes([]); setSceneError(String(reason)); } })
      .finally(() => { if (!canceled) setScenesLoading(false); });
    return () => { canceled = true; };
  }, [selected?.video_id, option]);

  function pickScene(s: SceneItem) {
    // Reframe the player to the clicked scene's window.
    setSelected({
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
    });
    setCurrentTime(s.start_seconds);
  }

  function pickEvent(event: VideoEvent) {
    if (!selected) return;
    setSelected({ ...selected, id: event.id, start_seconds: event.start_seconds,
      end_seconds: event.end_seconds, summary: event.evidence });
    setCurrentTime(event.start_seconds);
  }

  return (
    <div className={cls.page}>
      <div className={cls.left}>
        <div>
          <Subtitle1>{title}</Subtitle1>
        </div>
        <Field label="Indexed video"><Dropdown value={videos.find(video => video.video_id === requestedVideo)?.filename ?? "All videos"}
          selectedOptions={[requestedVideo ?? ""]}
          onOptionSelect={(_, data) => { setResults(null); setParams(data.optionValue ? { video: data.optionValue } : {}); }}>
          <Option value="">All videos</Option>
          {videos.map(video => <Option key={video.video_id} value={video.video_id}>{video.filename}</Option>)}
        </Dropdown></Field>
        {sceneError && <Text role="alert" style={{ color: tokens.colorPaletteRedForeground1 }}>{sceneError}</Text>}
        <QueryPane option={option} videoId={requestedVideo ?? undefined} availableFacets={results?.facets} onResults={(r) => { setResults(r); if (r.items[0]) setSelected(r.items[0]); }} />
        {results && (
          <Text size={200} className={cls.meta}>{describeResults(results)}</Text>
        )}
        <ResultsList items={results?.items ?? []} selectedId={selected?.id} onSelect={setSelected} />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
        <ViVideoExperience item={selected} onPickEvent={pickEvent} onTimeUpdate={setCurrentTime}
          onEnriched={() => selected && listScenes(selected.video_id, option).then(response => setScenes(response.items)).catch(reason => setSceneError(String(reason)))} />
        {selected && (
          <SceneList
            scenes={scenes}
            currentTime={currentTime}
            loading={scenesLoading}
            onPick={pickScene}
          />
        )}
      </div>
    </div>
  );
}
