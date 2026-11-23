import { useEffect, useState } from "react";
import {
  Badge, Button, Field, Slider, Switch, Textarea, Text, Spinner, makeStyles,
  Dropdown, Option, RadioGroup, Radio, tokens,
} from "@fluentui/react-components";
import { Search24Regular, Filter24Regular, Dismiss12Regular, Settings24Regular } from "@fluentui/react-icons";
import type { FacetValue, SearchFilters, SearchResponse } from "../api";
import { getContentProfiles, search } from "../api";

interface Props {
  onResults: (r: SearchResponse) => void;
  defaultQuery?: string;
  videoId?: string;
  // Facets observed from the LAST result set, used to populate filter chips.
  availableFacets?: Record<string, FacetValue[]>;
}

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", gap: "12px" },
  row: { display: "flex", gap: "16px", flexWrap: "wrap" },
  block: { minWidth: "180px", flex: 1 },
  hint: { color: tokens.colorNeutralForeground3 },
  facetGroup: { display: "flex", flexDirection: "column", gap: "4px" },
  chips: { display: "flex", flexWrap: "wrap", gap: "4px" },
  chip: { cursor: "pointer", userSelect: "none" },
  facetHeader: { display: "flex", alignItems: "center", gap: "6px", color: tokens.colorNeutralForeground3 },
});

const FACET_LABEL: Record<string, string> = {
  video_id: "Video",
  filename: "File",
  content_type: "Content type",
  labels: "Label",
};

const FACET_TARGET: Record<string, keyof SearchFilters> = {
  video_id: "video_ids",
  content_type: "content_types",
  labels: "labels",
  // filename is informational only (not a filterable field on the index)
};

export default function QueryPane({ onResults, defaultQuery = "", availableFacets, videoId }: Props) {
  const cls = useStyles();
  const [q, setQ] = useState(defaultQuery);
  const [top, setTop] = useState(5);
  const [duration, setDuration] = useState(15);
  const [mode, setMode] = useState<"hybrid" | "vector" | "keyword">("hybrid");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [filters, setFilters] = useState<SearchFilters>({});
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [visualRerankEnabled, setVisualRerankEnabled] = useState(true);
  const [visualMaxCandidates, setVisualMaxCandidates] = useState(6);
  const [visualFrames, setVisualFrames] = useState(0); // 0 = automatic (2-4 per clip, by clip length)
  const [eventKind, setEventKind] = useState("");
  const [eventOptions, setEventOptions] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    getContentProfiles().then(response => {
      if (!cancelled) setEventOptions(Object.assign({}, ...response.items.map(profile => profile.events)));
    }).catch(error => { if (!cancelled) setErr(String(error)); });
    return () => { cancelled = true; };
  }, []);

  function toggleFilter(key: keyof SearchFilters, value: string) {
    setFilters((cur) => {
      const list = (cur[key] as string[] | undefined) || [];
      const next = list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
      return { ...cur, [key]: next };
    });
  }

  function clearFilters() { setFilters({}); }

  function isActive(key: keyof SearchFilters, value: string): boolean {
    const list = (filters[key] as string[] | undefined) || [];
    return list.includes(value);
  }

  async function go() {
    if (!q.trim() && !eventKind) return;
    setBusy(true); setErr(null);
    try {
      const cleaned: SearchFilters = {
        video_ids: videoId ? [videoId] : filters.video_ids?.length ? filters.video_ids : undefined,
        content_types: filters.content_types?.length ? filters.content_types : undefined,
        labels: filters.labels?.length ? filters.labels : undefined,
      };
      const r = await search({
        query: q, top, clip_duration_seconds: duration, mode, filters: cleaned, event_kind: eventKind || undefined,
        visual_rerank_enabled: visualRerankEnabled,
        visual_rerank_max_candidates: visualMaxCandidates,
        visual_rerank_frames: visualFrames || undefined,
      });
      onResults(r);
    } catch (e: any) { setErr(e.message || String(e)); }
    finally { setBusy(false); }
  }

  const activeCount =
    (filters.video_ids?.length || 0) +
    (filters.content_types?.length || 0) +
    (filters.labels?.length || 0);

  return (
    <div className={cls.root}>
      <Field label="Moment tag"><Dropdown value={eventOptions[eventKind] ?? "Any moment"} selectedOptions={[eventKind]}
        onOptionSelect={(_, data) => { setEventKind(data.optionValue ?? ""); if (data.optionValue) setQ(""); }}>
        <Option value="">Any moment</Option>
        {Object.entries(eventOptions).map(([kind, label]) => <Option key={kind} value={kind}>{label}</Option>)}
      </Dropdown></Field>
      <Field label="Natural-language query" hint="Describe the moment you want to find.">
        <Textarea
          disabled={!!eventKind}
          value={q}
          onChange={(_, d) => setQ(d.value)}
          placeholder='e.g. "Rivera free kick goal" or "panel discussion about referee decision"'
          rows={2}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) go(); }}
        />
      </Field>
      <div className={cls.row}>
        <div className={cls.block}>
          <Field label={`Number of results: ${top}`}>
            <Slider min={1} max={20} value={top} onChange={(_, d) => setTop(d.value)} />
          </Field>
        </div>
        <div className={cls.block}>
          <Field label={`Clip duration: ${duration}s`}>
            <Slider min={5} max={60} step={5} value={duration} onChange={(_, d) => setDuration(d.value)} />
          </Field>
        </div>
      </div>
      <Field label="Search mode">
        <RadioGroup disabled={!!eventKind} value={mode} onChange={(_, d) => setMode(d.value as any)} layout="horizontal">
          <Radio value="hybrid" label="Hybrid (BM25 + vector + semantic re-rank)" />
          <Radio value="vector" label="Vector only" />
          <Radio value="keyword" label="Keyword only" />
        </RadioGroup>
      </Field>

      <div className={cls.facetGroup}>
        <div className={cls.facetHeader}>
          <Settings24Regular />
          <Text size={200} weight="semibold">Advanced</Text>
          <Button size="small" appearance="subtle" onClick={() => setShowAdvanced((v) => !v)}>
            {showAdvanced ? "Hide" : "Show"}
          </Button>
        </div>
        {showAdvanced && (
          <div className={cls.row} style={{ marginTop: 4 }}>
            <div className={cls.block}>
              <Field label="Visual confirmation" hint={eventKind
                ? "Besides the tagged moments, checks the best-matching untagged clips against real video frames and adds those that show the moment, so events the tagging missed are found. Slower; uses model calls."
                : "Re-checks the clips that look like goals against real video frames, so a shot that was saved or a recap about a goal ranks below an actual goal (hybrid mode only). A plain query such as \u201cgoals\u201d is treated as a Moment tag, where this check also finds moments the tags missed. More accurate; adds several seconds per search."}>
                <Switch
                  checked={visualRerankEnabled}
                  onChange={(_, d) => setVisualRerankEnabled(d.checked)}
                  label={visualRerankEnabled ? "On" : "Off"}
                />
              </Field>
            </div>
            {visualRerankEnabled && (
              <>
                <div className={cls.block}>
                  <Field label={`Candidate clips to check: up to ${visualMaxCandidates}`} hint="How many different moments get their video frames examined (with a Moment tag: besides the tagged ones). More candidates find more missed moments but take longer.">
                    <Slider min={0} max={20} value={visualMaxCandidates} onChange={(_, d) => setVisualMaxCandidates(d.value)} />
                  </Field>
                </div>
                <div className={cls.block}>
                  <Field label={`Frames per clip: ${visualFrames === 0 ? "automatic (2\u20134)" : visualFrames}`} hint={`Still images taken from each candidate clip, so up to ${visualMaxCandidates * (visualFrames || 4)} images are examined per search. More frames make a short moment, such as the ball crossing the line, less likely to be missed.`}>
                    <Slider min={0} max={12} value={visualFrames} onChange={(_, d) => setVisualFrames(d.value === 1 ? 2 : d.value)} />
                  </Field>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {availableFacets && Object.keys(availableFacets).length > 0 && (
        <div className={cls.facetGroup}>
          <div className={cls.facetHeader}>
            <Filter24Regular />
            <Text size={200} weight="semibold">Filters</Text>
            {activeCount > 0 && (
              <Button size="small" appearance="subtle" onClick={clearFilters} icon={<Dismiss12Regular />}>
                Clear ({activeCount})
              </Button>
            )}
          </div>
          {Object.entries(availableFacets).map(([key, values]) => {
            if (!values || values.length === 0) return null;
            if (key === "video_id" && videoId) return null;
            const targetKey = FACET_TARGET[key];
            if (!targetKey) return null;
            return (
              <div key={key} style={{ marginTop: 4 }}>
                <Text size={200} className={cls.hint}>{FACET_LABEL[key] || key}</Text>
                <div className={cls.chips} style={{ marginTop: 2 }}>
                  {values.slice(0, 12).map((fv) => {
                    const active = isActive(targetKey, fv.value);
                    return (
                      <Badge
                        key={fv.value}
                        size="small"
                        appearance={active ? "filled" : "tint"}
                        color={active ? "brand" : "informative"}
                        className={cls.chip}
                        onClick={() => toggleFilter(targetKey, fv.value)}
                      >
                        {fv.value} · {fv.count}
                      </Badge>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Button appearance="primary" onClick={go} disabled={busy || (!q.trim() && !eventKind)} icon={busy ? <Spinner size="tiny" /> : <Search24Regular />}>
          {busy ? "Searching…" : "Search"}
        </Button>
        <Text size={200} className={cls.hint}>Tip: press Ctrl+Enter to search</Text>
      </div>
      {err && <Text style={{ color: tokens.colorPaletteRedForeground1 }}>{err}</Text>}
    </div>
  );
}
