# How MomentLens works

MomentLens turns a video into **searchable moments**: short windows of the video that carry what was said, what was
on screen and what happened, so a plain-language query such as *"a scored goal"* or *"the panel discussion about
transport"* lands on the right second. This page explains the pipeline behind the
[architecture diagram](architecture/architecture.svg).

## 1. Upload

The browser never talks to storage directly: the storage account is private (firewall plus private endpoints). It sends
the file to the API in 4 MiB blocks (`POST /api/upload/session`, `PUT .../blocks/{n}`, `POST .../commit`) and the API
writes them with its managed identity. A commit can never replace an existing blob. A video that is already in the
`videos` container can be referenced instead of uploaded (`POST /api/upload/reference`).

## 2. Index with Azure AI Video Indexer

`POST /api/indexing/process` starts a job and returns immediately:

1. The API signs a short-lived, read-only SAS for the blob with a user delegation key (no account key exists) and
   submits the video to Video Indexer with the **Advanced** preset and the chosen spoken language (or auto detect).
2. A manually triggered **Azure Container Apps job** (`vi-poller`) polls Video Indexer until the video is processed,
   stores the full insights JSON in `insights/<video id>/insights.json` and writes a heartbeat the API turns into the
   progress stepper. The poll runs in a job rather than in the web app, so a web app restart never interrupts a long
   indexing run. Where Video Indexer produces them, its GPT-based segment summaries are fetched too
   (`prompt-content.json`); they never block the job.
3. The API reads the insights and builds **scene cards**: one per Video Indexer shot, or sliding 20 s / 30 s windows
   (overlapping by 5 s) when a window size is chosen. A card holds the time range, a summary, the transcript excerpt,
   OCR text, labels, named entities, speakers and a keyframe thumbnail.

## 3. Enrich and embed with Azure OpenAI

* **Summaries, entities, labels.** Where Video Indexer gives no summary, a chat model writes one per scene
  (a different prompt per content type: sports, news, documentary, ...). It may only use what is in the input.
* **Visual enrichment.** The vision-capable *judge* model looks at roughly one frame every 2.5 s of every card, adds a
  one-sentence `Visual: ...` description and tags visible events (`corner kick`, `yellow card`, `slow-motion replay`).
  Speech and OCR rarely say what the camera shows, so this is what makes those moments findable. It is best effort:
  a card that cannot be described is indexed without it. Controls: `VISUAL_ENRICH_*`.
* **Embeddings.** Two vectors per card: `content_vector` (what happens) and `entity_vector` (who or what is named),
  so a query that names a person is not diluted by a query about an action.

The cards are written to the **Azure AI Search** index `moments`: BM25 keyword fields with a scoring profile that
favours entities, labels and summary over raw transcript, two HNSW vector fields and a semantic configuration.

## 4. Moment tags

A *content profile* knows which events matter for a kind of video. The built-in profile is for soccer games (goal,
foul, yellow card, red card, VAR decision, corner); other content types use the same pipeline with their own scene-card
prompts, and a profile for another domain is a small addition to `PROFILES` in
`api/app/pipelines/event_enrichment.py`.

Pressing **Extract events** (or the API call `POST /api/indexing/events/{id}/enrich`) finds candidate scenes with one
phrase query per event, has the vision model review each candidate's frames in bounded batches and writes the confirmed
first views as `event:<kind>` labels plus an `events.json` manifest. The pass is budgeted
(`EVENT_ANALYSIS_MAX_SCENES`, default 120 scenes per pass and video; each further pass reviews the next best
candidates), so it is targeted coverage, not a full-video review. Picking a **Moment tag** at search time lists the
tagged scenes with no model call at all.

## 5. Search

`POST /api/search` supports `hybrid` (default), `vector` and `keyword` modes.

1. **Query rewrite.** A chat model extracts named entities and action synonyms and writes a short expansion used as the
   embedding and semantic input.
2. **Retrieval.** BM25 over the weighted fields plus the content vector (and the entity vector when someone is named),
   fused by Azure AI Search, then reranked by its semantic ranker. Optional filters: video, content type, labels.
3. **Judge rerank** (hybrid only). The top candidates (40 by default) are classified by the judge model from their text
   and, for the best ones, 2-4 frames sampled from the clip itself: `scoring_sequence`, `celebration_only`,
   `attempt_only`, `mention_only`, `unrelated` or `uncertain`. This is what makes *"a scored goal"* return the goal
   rather than commentary about goals. Replays are recognised and badged. Any failure falls back to the ranking before
   the judge. Controls: `SEARCH_ACTION_RERANK_*`, `SEARCH_VISUAL_RERANK_*`.
4. **Deep search.** With a Moment tag and *Visual confirmation* on, the search also looks for untagged clips that the
   frames confirm, folds replays into the first view (`SEARCH_DEEP_MERGE_WINDOW_SECONDS`) and reports what it did
   (`deep_search`: tagged, recovered, judged, frames checked).

## 6. Play

The API streams the source video from private storage with HTTP range support (`/api/media/blob`), so the browser can
seek straight to the matched second. Only the `videos` and `thumbnails` containers are served, and anything that is not
a video or image is sent as an opaque download. Video Indexer's hosted player is not used: its browser-side media
requests could not reach private storage.

## Evidence and limits

* Sports events such as fouls and cards are **application-derived** (phrase search plus frame review), not native
  Video Indexer detections. Native insights and derived events are shown together and labelled.
* Frame confirmation is evidence, not a guarantee: only sampled frames are examined, and a reviewed-scene budget applies.
* The event phrases are English. Commentary in another language needs its own phrases in `event_enrichment.py`.
* The search index uses 3072-dimension vectors (`text-embedding-3-large`); changing the embedding model means changing
  `EMBEDDING_DIMS` in `api/app/pipelines/indexer.py` and recreating the index (`scripts/reset_index.py`).
* Model output can be wrong. Treat the summaries, tags and verdicts as assistance and check them against the video.

## Cost drivers

Video Indexer is billed per minute of video. Azure OpenAI is billed per token: visual enrichment is about one
vision call per scene card, event review one call per reviewed scene, and every search adds a query rewrite, one
embedding call and a judge call per candidate batch. The workbook created by `infra/` charts the estimated cost per run
(`COST_RATE_*` hold illustrative rates, replace them with your own).
