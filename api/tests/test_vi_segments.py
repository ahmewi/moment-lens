import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import ProcessJob
from app.pipelines import video_indexer
from app.routers import option1


def instance(start, end):
    return {"start": str(timedelta(seconds=start)), "end": str(timedelta(seconds=end))}


def insights(duration=55):
    return {"videos": [{
        "durationInSeconds": duration,
        "insights": {"shots": [
            {"id": start // 3, "instances": [instance(start, min(start + 3, duration))]}
            for start in range(0, int(duration), 3)
        ]},
    }]}


class VideoIndexerSegmentTests(unittest.TestCase):
    def setUp(self):
        enrichment = patch.object(
            video_indexer, "enrich_scene",
            side_effect=lambda text, labels, entities, ocr, **kwargs: {
                "summary": text, "labels": [], "entities": [],
            },
        )
        self.enrich = enrichment.start()
        self.addCleanup(enrichment.stop)

    def cards(self, source, **kwargs):
        return video_indexer.insights_to_scene_cards(
            source, "video-test", "match.mp4", "https://example.test/match.mp4", **kwargs,
        )

    def test_original_mode_preserves_shot_boundaries_and_ids(self):
        cards = self.cards(insights(9))
        self.assertEqual([(card.start_seconds, card.end_seconds) for card in cards],
                         [(0, 3), (3, 6), (6, 9)])
        self.assertEqual(cards[0].id, "video-test-vi-0-3000")

    def test_windows_overlap_and_stop_at_video_end(self):
        for size, expected in (
            (20, [(0, 20), (15, 35), (30, 50), (45, 55)]),
            (30, [(0, 30), (25, 55)]),
        ):
            with self.subTest(size=size):
                cards = self.cards(insights(), indexing_window_seconds=size)
                self.assertEqual([(card.start_seconds, card.end_seconds) for card in cards], expected)
                self.assertTrue(all(card.duration_seconds <= size for card in cards))
                self.assertEqual(len({card.id for card in cards}), len(cards))

    def test_window_enrichment_combines_evidence_from_adjacent_shots(self):
        source = insights()
        evidence = source["videos"][0]["insights"]
        evidence["transcript"] = [
            {"text": "Build-up", "instances": [instance(1, 2)]},
            {"text": "Scoring action", "instances": [instance(11, 12)]},
            {"text": "Unrelated interview", "instances": [instance(40, 41)]},
        ]
        evidence["labels"] = [{"name": "football", "instances": [instance(8, 9)]}]
        evidence["namedPeople"] = [{"name": "Player", "instances": [instance(11, 12)]}]
        evidence["brands"] = [{"name": "Sponsor", "instances": [instance(14, 15)]}]
        evidence["keywords"] = [{"name": "Free kick", "instances": [instance(6, 7)]}]
        evidence["ocr"] = [{"text": "1-0", "instances": [instance(18, 19)]}]
        first = self.cards(source, indexing_window_seconds=20)[0]
        self.assertEqual(first.transcript_excerpt, "Build-up Scoring action")
        self.assertEqual(first.summary, "Build-up Scoring action")
        self.assertEqual(first.labels, ["football"])
        self.assertEqual(first.entities, ["Free kick", "Player", "Sponsor"])
        self.assertEqual(first.ocr_text, "1-0")
        self.assertEqual(self.enrich.call_args_list[0].args[0], "Build-up Scoring action")

    def test_overlap_keeps_boundary_event_without_leaking_future_evidence(self):
        source = insights(35)
        source["videos"][0]["insights"]["transcript"] = [
            {"text": "Boundary event", "instances": [instance(19, 21)]},
            {"text": "After first window", "instances": [instance(20, 22)]},
            {"text": "End marker", "instances": [instance(35, 35)]},
        ]
        cards = self.cards(source, indexing_window_seconds=20)
        self.assertEqual(cards[0].transcript_excerpt, "Boundary event")
        self.assertEqual(cards[1].transcript_excerpt, "Boundary event After first window")

    def test_thumbnail_comes_from_shot_near_window_midpoint(self):
        cards = self.cards(
            insights(), indexing_window_seconds=20,
            scene_thumbnails={3: "thumb-first", 8: "thumb-second"},
            thumbnail_url="fallback",
        )
        self.assertEqual(cards[0].thumbnail_url, "thumb-first")
        self.assertEqual(cards[1].thumbnail_url, "thumb-second")
        self.assertEqual(cards[-1].thumbnail_url, "fallback")

    def test_window_combines_overlapping_prompt_sections(self):
        source = insights(20)
        prompt = {"sections": [
            {"start": "0:00:10", "end": "0:00:19", "content": "Shot and outcome."},
            {"start": "0:00:00", "end": "0:00:09", "content": "Attacking build-up."},
            {"start": "0:00:20", "end": "0:00:40", "content": "Unrelated scene."},
        ]}
        card = self.cards(source, indexing_window_seconds=20, prompt_content=prompt)[0]
        self.assertEqual(card.summary, "Attacking build-up. Shot and outcome.")
        self.enrich.assert_not_called()

    def test_fractional_short_video_has_one_clamped_window(self):
        for size in (20, 30):
            with self.subTest(size=size):
                cards = self.cards(insights(2.5), indexing_window_seconds=size)
                self.assertEqual([(card.start_seconds, card.end_seconds) for card in cards], [(0, 2.5)])

    def test_no_shots_uses_video_or_root_duration(self):
        for source in (
            {"videos": [{"durationInSeconds": 40.5, "insights": {}}]},
            {"durationInSeconds": 40.5, "videos": [{"insights": {}}]},
        ):
            with self.subTest(source=source):
                cards = self.cards(source, indexing_window_seconds=20)
                self.assertEqual([(card.start_seconds, card.end_seconds) for card in cards],
                                 [(0, 20), (15, 35), (30, 40.5)])
                self.assertEqual(self.cards(source)[0].duration_seconds, 40.5)

    def test_missing_duration_uses_last_shot_end(self):
        source = insights(35)
        del source["videos"][0]["durationInSeconds"]
        cards = self.cards(source, indexing_window_seconds=30)
        self.assertEqual([(card.start_seconds, card.end_seconds) for card in cards], [(0, 30), (25, 35)])

    def test_empty_video_produces_no_cards(self):
        for source in ({}, {"videos": [{"insights": {}}]}):
            self.assertEqual(self.cards(source, indexing_window_seconds=20), [])

    def test_invalid_window_is_rejected(self):
        for size in (-1, 5, 25, 60):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.cards(insights(), indexing_window_seconds=size)


    def test_pipeline_embeds_and_indexes_combined_windows(self):
        source = insights(35)
        source["videos"][0]["insights"]["transcript"] = [
            {"text": "Build-up", "instances": [instance(1, 2)]},
            {"text": "Scoring action", "instances": [instance(11, 12)]},
        ]
        with (
            patch.object(video_indexer.settings, "vi_poller_use_job", False),
            patch.object(video_indexer.settings, "visual_enrich_enabled", False),
            patch.object(video_indexer, "make_sas_url", return_value="https://example.test/video"),
            patch.object(video_indexer, "extract_thumbnail", return_value=None),
            patch.object(video_indexer, "upload_video", return_value="vi-test") as upload,
            patch.object(video_indexer, "wait_for_processing", return_value=source),
            patch.object(video_indexer, "upload_text", return_value="https://example.test/insights"),
            patch.object(video_indexer.vi_poller_client, "read_prompt_content", return_value=None),
            patch.object(video_indexer, "_extract_shot_thumbnails", return_value={}),
            patch.object(video_indexer, "embed_texts", side_effect=lambda texts: [[1.0] for text in texts]) as embed,
            patch.object(video_indexer, "upsert_scene_cards") as upsert,
            patch.object(video_indexer, "record_pipeline_run"),
        ):
            result = video_indexer.process_video(
                "test-video", "match.mp4", "https://example.test/video",
                source_language="da-DK", indexing_window_seconds=20,
            )
            cards = upsert.call_args.args[1]
            self.assertEqual([(card.start_seconds, card.end_seconds) for card in cards], [(0, 20), (15, 35)])
            self.assertEqual(result["scene_count"], 2)
            self.assertIn("Build-up Scoring action", embed.call_args_list[0].args[0][0])
            self.assertEqual(upload.call_args.kwargs["source_language"], "da-DK")


class SceneCardWriteTests(unittest.TestCase):
    def test_batches_are_bounded_and_per_document_failures_are_errors(self):
        from app.models import SceneCard
        from app.pipelines import indexer
        cards = [SceneCard(id=f"card-{number}", video_id="video", filename="match.mp4", option="option1",
                           start_seconds=number, end_seconds=number + 1, duration_seconds=1,
                           blob_url="https://example.test/video") for number in range(101)]
        client = Mock()
        client.upload_documents.side_effect = lambda documents: [Mock(succeeded=True) for _ in documents]
        with patch.object(indexer, "search_client", return_value=client):
            indexer.upsert_scene_cards("index", cards, {})
            self.assertEqual([len(call.kwargs["documents"]) for call in client.upload_documents.call_args_list], [100, 1])
            client.upload_documents.side_effect = None
            client.upload_documents.return_value = [Mock(succeeded=False)]
            with self.assertRaisesRegex(RuntimeError, "Search did not accept"):
                indexer.upsert_scene_cards("index", cards[:1], {})


class VideoIndexerVisualEnrichmentTests(unittest.TestCase):
    def run_pipeline(self, *, enabled=True, enrich=None, content_type=None):
        source = insights(35)
        source["videos"][0]["insights"]["transcript"] = [{"text": "Whistle", "instances": [instance(1, 2)]}]
        with (
            patch.object(video_indexer.settings, "vi_poller_use_job", False),
            patch.object(video_indexer.settings, "visual_enrich_enabled", enabled),
            patch.object(video_indexer, "make_sas_url", return_value="https://example.test/sas") as sas,
            patch.object(video_indexer, "extract_thumbnail", return_value=None),
            patch.object(video_indexer, "upload_video", return_value="vi-test"),
            patch.object(video_indexer, "wait_for_processing", return_value=source),
            patch.object(video_indexer, "upload_text", return_value="https://example.test/insights"),
            patch.object(video_indexer.vi_poller_client, "read_prompt_content", return_value=None),
            patch.object(video_indexer, "_extract_shot_thumbnails", return_value={}),
            patch.object(video_indexer, "enrich_scene", side_effect=lambda text, *a, **k: {"summary": text, "labels": [], "entities": []}),
            patch.object(video_indexer, "embed_texts", side_effect=lambda texts: [[1.0] for _ in texts]) as embed,
            patch.object(video_indexer, "upsert_scene_cards") as upsert,
            patch.object(video_indexer, "record_pipeline_run"),
            patch.object(video_indexer, "enrich_cards", side_effect=enrich) as enrich_mock,
            patch.object(video_indexer, "enrich_video_events", side_effect=lambda *args, **kwargs: self.assertTrue(upsert.called)) as targeted,
        ):
            result = video_indexer.process_video("test-video", "match.mp4", "https://example.test/video",
                                                 indexing_window_seconds=20, content_type=content_type)
        self.targeted = targeted
        return result, upsert, embed, enrich_mock, sas

    def test_cards_are_enriched_before_they_are_embedded_and_indexed(self):
        def enrich(cards, url, *, event_report=None):
            for card in cards:
                card.summary = "Visual: referee shows a card.\n" + card.summary
                card.labels = ["yellow card"] + card.labels
            return len(cards)

        result, upsert, embed, enrich_mock, sas = self.run_pipeline(enrich=enrich)
        cards = upsert.call_args.args[1]
        self.assertTrue(all(c.summary.startswith("Visual: referee shows a card.") for c in cards))
        self.assertIn("yellow card", embed.call_args_list[0].args[0][0])
        self.assertEqual(result["scene_count"], 2)
        # A fresh SAS is minted for the frames: the one from the start can expire while VI processes.
        self.assertEqual(enrich_mock.call_args.args[1], "https://example.test/sas")
        self.assertEqual(sas.call_args_list[-1].kwargs, {"hours": 2})

    def test_it_is_skipped_when_disabled(self):
        _, upsert, _, enrich_mock, _ = self.run_pipeline(enabled=False)
        enrich_mock.assert_not_called()
        self.assertEqual(len(upsert.call_args.args[1]), 2)

    def test_soccer_profile_activates_extraction_independently_of_generic_visual_setting(self):
        _, upsert, _, enrich_mock, _ = self.run_pipeline(enabled=False, content_type="soccer_match")
        enrich_mock.assert_not_called()
        self.targeted.assert_called_once()
        self.assertEqual(self.targeted.call_args.args, ("test-video", "soccer_match"))
        card = upsert.call_args.args[1][0]
        self.assertNotIn("event:goal", card.labels)

    def test_a_failing_enrichment_never_fails_the_pipeline(self):
        result, upsert, _, _, _ = self.run_pipeline(enrich=RuntimeError("boom"))
        self.assertEqual(result["scene_count"], 2)
        self.assertEqual(len(upsert.call_args.args[1]), 2)


class VideoIndexerWindowRequestTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(option1.router, prefix="/api/option1")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.request = {
            "video_id": "test-video", "filename": "match.mp4", "blob_url": "https://example.test/video",
        }

    def test_window_choices_and_legacy_default_reach_pipeline(self):
        for fields, expected in (({}, 0), ({"indexing_window_seconds": 0}, 0),
                                 ({"indexing_window_seconds": 20}, 20),
                                 ({"indexing_window_seconds": 30}, 30)):
            with (
                self.subTest(fields=fields),
                patch.object(option1, "ensure_indexes"),
                patch.object(option1.jobs, "run_async", return_value=ProcessJob(
                    video_id="test-video", option="option1", status="submitted",
                )) as run,
                patch.object(option1, "process_video") as process,
            ):
                response = self.client.post("/api/option1/process", json={**self.request, **fields})
                self.assertEqual(response.status_code, 200)
                run.call_args.kwargs["target"](Mock(), Mock(), Mock())
                self.assertEqual(process.call_args.kwargs["indexing_window_seconds"], expected)

    def test_invalid_windows_cannot_start_jobs(self):
        for size in (-1, 5, 25, 60, "20", "invalid", None):
            with (
                self.subTest(size=size),
                patch.object(option1, "ensure_indexes") as indexes,
                patch.object(option1.jobs, "run_async") as run,
            ):
                response = self.client.post(
                    "/api/option1/process", json={**self.request, "indexing_window_seconds": size},
                )
                self.assertEqual(response.status_code, 422)
                indexes.assert_not_called()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
