import { Badge, Card, CardHeader, MessageBar, MessageBarBody, Text, tokens, makeStyles, shorthands } from "@fluentui/react-components";
import type { SearchResultItem } from "../api";

interface Props {
  items: SearchResultItem[];
  selectedId?: string;
  onSelect: (item: SearchResultItem) => void;
}

const useStyles = makeStyles({
  list: { display: "flex", flexDirection: "column", gap: "8px", overflowY: "auto", maxHeight: "60vh", paddingRight: "4px" },
  card: {
    cursor: "pointer",
    padding: "12px 14px",
    // The list scrolls (maxHeight); without this the cards shrink and clip their badges instead.
    flexShrink: 0,
    ...shorthands.borderLeft("3px", "solid", "transparent"),
    transitionProperty: "background, border-color, transform",
    transitionDuration: "120ms",
    ":hover": { background: tokens.colorNeutralBackground2Hover },
  },
  active: {
    ...shorthands.borderLeft("3px", "solid", tokens.colorBrandStroke1),
    background: tokens.colorNeutralBackground2Selected,
  },
  meta: { color: tokens.colorNeutralForeground3 },
});

function fmt(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

// Action-aware reranker verdict colors (hybrid mode only).
function matchTypeColor(matchType: SearchResultItem["match_type"]): "success" | "brand" | "warning" | "danger" | "severe" | "informative" {
  switch (matchType) {
    case "scoring_sequence": return "success";
    case "celebration_only": return "brand";
    case "attempt_only": return "severe";
    case "mention_only": return "warning";
    case "unrelated": return "danger";
    default: return "informative";
  }
}

// The verdicts describe whatever action the query asks for (a goal, a corner kick, a card, ...), so the wording stays action-neutral.
const MATCH_TYPE_LABEL: Record<NonNullable<SearchResultItem["match_type"]>, string> = {
  scoring_sequence: "action shown",
  celebration_only: "aftermath only",
  attempt_only: "attempt only",
  mention_only: "mention only",
  unrelated: "unrelated",
  uncertain: "uncertain",
};

export default function ResultsList({ items, selectedId, onSelect }: Props) {
  const cls = useStyles();
  if (!items.length) return <Text className={cls.meta}>No matches yet — run a query above.</Text>;
  // Confirmed results sort first, so none among the shown ones means the whole judged pool had none.
  const judged = items.some((it) => it.match_type);
  const noneConfirmed = judged && !items.some((it) => it.match_type === "scoring_sequence" || it.match_type === "celebration_only");
  return (
    <div className={cls.list}>
      {noneConfirmed && (
        <MessageBar intent="info">
          <MessageBarBody>No clip is confirmed to show this. These are the closest windows; the judge note under each one says why.</MessageBarBody>
        </MessageBar>
      )}
      {items.map((it) => {
        const active = it.id === selectedId;
        return (
          <Card
            key={it.id}
            onClick={() => onSelect(it)}
            className={`${cls.card} ${active ? cls.active : ""}`}
          >
            <CardHeader
              header={<Text weight="semibold">{it.filename}</Text>}
              description={
                <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                  <Text size={200} className={cls.meta}>
                    {fmt(it.start_seconds)} → {fmt(it.end_seconds)} · score {it.score.toFixed(3)}
                    {it.rerank_score != null ? ` · rerank ${it.rerank_score.toFixed(3)}` : ""}
                  </Text>
                  {it.match_type && (
                    <Badge
                      size="small"
                      appearance="tint"
                      color={matchTypeColor(it.match_type)}
                      title={it.match_reason || undefined}
                    >
                      {MATCH_TYPE_LABEL[it.match_type] ?? it.match_type.replace(/_/g, " ")}
                    </Badge>
                  )}
                  {it.visual_checked && (
                    <Badge size="small" appearance="outline" color="informative" title="Sampled video frames were examined for this result">
                      frames checked
                    </Badge>
                  )}
                  {it.origin === "tag" && (
                    <Badge size="small" appearance="outline" color="brand" title="Tagged when the video was indexed">
                      tagged
                    </Badge>
                  )}
                  {it.origin === "deep_search" && (
                    <Badge size="small" appearance="tint" color="success" title="Not tagged when the video was indexed; found at search time because its frames show the moment">
                      deep search
                    </Badge>
                  )}
                  {it.presentation === "replay" && (
                    <Badge size="small" appearance="outline" color="warning" title="The frames show a replay of the action, not its first view">
                      replay
                    </Badge>
                  )}
                  {it.origin === "deep_search" && it.presentation !== "live" && it.presentation !== "replay" && (
                    <Badge size="small" appearance="outline" color="warning" title="The frames do not clearly show the moment; the verdict rests on the commentary and text, so check the clip">
                      frames inconclusive
                    </Badge>
                  )}
                </div>
              }
            />
            {it.summary && <Text size={200} style={{ marginTop: 6, display: "block" }}>{it.summary}</Text>}
            {it.match_reason && (
              <Text size={100} className={cls.meta} style={{ marginTop: 4, display: "block", fontStyle: "italic" }}>
                Judge: {it.match_reason}
              </Text>
            )}
            {it.labels?.length > 0 && (
              <div style={{ marginTop: 8, display: "flex", gap: 4, flexWrap: "wrap" }}>
                {it.labels.slice(0, 6).map((l) => (
                  <Badge key={l} size="small" appearance="tint">{l}</Badge>
                ))}
              </div>
            )}
          </Card>
        );
      })}
    </div>
  );
}
