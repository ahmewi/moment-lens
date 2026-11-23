import json
import re
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from app.config import settings
from app.models import SceneCard
from app.pipelines import visual_enrich as ve
from app.pipelines.event_enrichment import PROFILES, SOCCER_EVENTS, build_manifest, first_view_events, parse_events


def make_card(id_="c1", start=0.0, end=30.0, summary="Original summary.", labels=None):
    return SceneCard(
        id=id_, video_id="v1", filename="match.mp4", option="option1", start_seconds=start, end_seconds=end,
        duration_seconds=end - start, summary=summary, transcript_excerpt="talk", labels=["football"] if labels is None else labels,
        entities=[], ocr_text="", blob_url="https://acct.blob.core.windows.net/videos/v1/match.mp4",
    )


def chat_response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def make_target(create, *, effort="low"):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return SimpleNamespace(client=client, model="judge-model", detail="high", reasoning_effort=effort)


def answer(description="A referee shows a yellow card to a player.", events=("yellow card",)):
    return chat_response(json.dumps({"description": description, "events": list(events)}))


class NormalizeEventsTests(unittest.TestCase):
    def test_custom_events_reject_invalid_types_timestamps_and_untrusted_source(self):
        valid = {"kind": "yellow_card", "start_seconds": 10, "end_seconds": 12,
                 "confidence": 0.95, "evidence": "Referee visibly raises a yellow card.", "presentation": "live"}
        events = parse_events([valid, {**valid, "kind": "invented"}, {**valid, "end_seconds": 50},
                               {**valid, "confidence": float("nan")}, {**valid, "presentation": "guessed"}],
                              content_type="soccer_match", start=0, end=30, scene_id="scene")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].label, "Yellow card")
        self.assertEqual(events[0].scene_id, "scene")

    def test_replays_unknowns_low_confidence_and_overlapping_duplicates_are_hidden(self):
        live = {"kind": "goal", "start_seconds": 10, "end_seconds": 15,
                "confidence": 0.95, "evidence": "Ball crosses the line; referee awards the goal.", "presentation": "live"}
        candidates = parse_events([live, {**live, "start_seconds": 12},
                                   {**live, "start_seconds": 20, "end_seconds": 25, "presentation": "replay"},
                                   {**live, "presentation": "unknown"}, {**live, "confidence": 0.5},
                                   {**live, "start_seconds": 40, "end_seconds": 45}],
                                  content_type="soccer_match", start=0, end=60, scene_id="scene")
        self.assertEqual([event.start_seconds for event in first_view_events(candidates)], [10, 40])
        manifest = build_manifest("video", "vi-video", "soccer_match", 60, candidates, 1, 2)
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(len(manifest["events"]), 2)
        self.assertEqual(len(manifest["candidates"]), 6)
        self.assertEqual(build_manifest("video", "vi", "generic", 60, [], 0, 2)["status"], "not_applicable")

    def test_tags_are_lowercased_deduplicated_and_bounded(self):
        raw = ["Yellow Card", "yellow card", " Free-Kick! ", "a b c d", "x" * 31, "", 7, "corner kick", "foul", "replay", "var review", "offside"]
        self.assertEqual(
            ve.normalize_events(raw),
            ["yellow card", "free-kick", "7", "corner kick", "foul", "replay"],
        )

    def test_repeated_goal_clock_is_suppressed_across_nonoverlapping_windows(self):
        goal = {"kind": "goal", "start_seconds": 10, "end_seconds": 15, "match_clock_seconds": 1234,
                "confidence": 0.95, "evidence": "Ball crosses the line; referee awards the goal.", "presentation": "live"}
        events = parse_events([goal, {**goal, "start_seconds": 40, "end_seconds": 45, "match_clock_seconds": 1236},
                               {**goal, "start_seconds": 50, "end_seconds": 55, "match_clock_seconds": 1400}],
                              content_type="soccer_match", start=0, end=60, scene_id="scene")
        self.assertEqual([event.start_seconds for event in first_view_events(events)], [10, 50])

    def test_anything_but_a_list_gives_no_tags(self):
        for raw in (None, "yellow card", {"a": 1}, 3):
            self.assertEqual(ve.normalize_events(raw), [])


class ApplyVisualMetadataTests(unittest.TestCase):
    def test_description_goes_first_in_the_summary_and_tags_first_in_the_labels(self):
        card = make_card(labels=["Football", "Grass"])
        ve.apply_visual_metadata(card, "Referee raises a card.", ["yellow card", "football"])
        self.assertEqual(card.summary, "Visual: Referee raises a card.\nOriginal summary.")
        self.assertEqual(card.labels, ["yellow card", "football", "Grass"])

    def test_applying_twice_replaces_the_earlier_description(self):
        card = make_card()
        ve.apply_visual_metadata(card, "First.", ["foul"])
        ve.apply_visual_metadata(card, "Second.", ["corner kick"])
        self.assertEqual(card.summary, "Visual: Second.\nOriginal summary.")

    def test_tags_only_leave_the_summary_alone(self):
        card = make_card()
        ve.apply_visual_metadata(card, "", ["foul"])
        self.assertEqual(card.summary, "Original summary.")
        self.assertEqual(card.labels, ["foul", "football"])

    def test_labels_are_capped_with_the_new_tags_kept(self):
        card = make_card(labels=[f"label{i}" for i in range(40)])
        ve.apply_visual_metadata(card, "x", ["yellow card"])
        self.assertEqual(len(card.labels), 30)
        self.assertEqual(card.labels[0], "yellow card")


class DescribeFramesTests(unittest.TestCase):
    def test_soccer_profile_returns_structured_events_and_uses_timed_evidence(self):
        event = {"kind": "red_card", "start_seconds": 15, "end_seconds": 18,
                 "confidence": 0.92, "evidence": "Referee raises a red card above his head.", "presentation": "live"}
        create = Mock(return_value=chat_response(json.dumps({
            "description": "Referee displays a red card.", "events": ["red card"],
            "custom_events": [event], "events_reviewed": True,
        })))
        info = ve.describe_frames(make_target(create), [b"frame"], 10, 30,
                                  content_type="soccer_match", evidence="a red card", scene_id="scene")
        self.assertTrue(info["events_reviewed"])
        self.assertEqual(info["custom_events"][0].kind, "red_card")
        messages = create.call_args.kwargs["messages"]
        self.assertIn("VAR decision", messages[0]["content"])
        self.assertIn("replay", messages[0]["content"])
        self.assertIn("a red card", messages[1]["content"][0]["text"])

    def test_legacy_response_cannot_claim_complete_event_coverage(self):
        info = ve.describe_frames(make_target(Mock(return_value=answer())), [b"frame"], 0, 30,
                                  content_type="soccer_match")
        self.assertFalse(info["events_reviewed"])
        self.assertEqual(info["custom_events"], [])

    def test_parses_description_and_events_and_sends_every_frame_with_the_target_detail(self):
        create = Mock(return_value=answer(description="  A referee   shows a card. ", events=["Yellow Card"]))
        info = ve.describe_frames(make_target(create), [b"f1", b"f2"], 10.0, 40.0)
        self.assertEqual(info, {"description": "A referee shows a card.", "events": ["yellow card"]})
        content = create.call_args.kwargs["messages"][1]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "image_url", "image_url"])
        self.assertEqual(content[1]["image_url"]["detail"], "high")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_reasoning_judges_get_reasoning_parameters(self):
        create = Mock(return_value=answer())
        ve.describe_frames(make_target(create, effort="low"), [b"f"], 0.0, 30.0)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertIn("max_completion_tokens", kwargs)
        self.assertNotIn("temperature", kwargs)

    def test_nonuniform_frames_have_explicit_sample_times(self):
        create = Mock(return_value=answer())
        ve.describe_frames(make_target(create), [b"before", b"inside"], 10, 30,
                           context_window=(2, 38), frame_times=[4, 10.25])
        content = create.call_args.kwargs["messages"][1]["content"]
        self.assertEqual(content[1]["text"], "Approximate sample time: 4.000s.")
        self.assertEqual(content[3]["text"], "Approximate sample time: 10.250s.")
        self.assertEqual(len([part for part in content if part["type"] == "image_url"]), 2)
        with self.assertRaisesRegex(ValueError, "Frame times must match"):
            ve.describe_frames(make_target(create), [b"frame"], 10, 30, frame_times=[])

    def test_unusable_answers_give_none(self):
        for content in ("not json", "{}", json.dumps({"description": "", "events": []})):
            with self.subTest(content=content):
                self.assertIsNone(ve.describe_frames(make_target(Mock(return_value=chat_response(content))), [b"f"], 0, 30))

    def test_a_non_retryable_error_is_not_retried(self):
        create = Mock(side_effect=RuntimeError("Error code: 400 - content safety"))
        self.assertIsNone(ve.describe_frames(make_target(create), [b"f"], 0, 30))
        self.assertEqual(create.call_count, 1)

    def test_rate_limits_are_retried_after_a_pause(self):
        create = Mock(side_effect=[RuntimeError("Error code: 429 - slow down"), answer()])
        with patch.object(ve.time, "sleep") as sleep:
            info = ve.describe_frames(make_target(create), [b"f"], 0, 30)
        self.assertEqual(info["events"], ["yellow card"])
        self.assertEqual(create.call_count, 2)
        sleep.assert_called_once()

    def test_it_gives_up_after_the_attempt_budget(self):
        create = Mock(side_effect=RuntimeError("Error code: 429 - slow down"))
        with patch.object(ve.time, "sleep"):
            self.assertIsNone(ve.describe_frames(make_target(create), [b"f"], 0, 30))
        self.assertEqual(create.call_count, ve._ATTEMPTS)


class EnrichCardsTests(unittest.TestCase):
    def test_profile_event_analysis_reviews_all_cards_beyond_generic_budget(self):
        cards = [make_card(id_=f"card-{index}") for index in range(5)]
        for card in cards:
            card.content_type = "soccer_match"
        report = {"reviewed": 0, "candidates": []}
        info = {"description": "A player takes a corner.", "events": [], "custom_events": [], "events_reviewed": True}
        with (patch.object(settings, "visual_enrich_max_cards", 2),
              patch.object(ve, "extract_window_frames", return_value=[b"frame"]),
              patch.object(ve, "describe_frames", return_value=info) as describe):
            changed = ve.enrich_cards(cards, "https://example.test/match.mp4", target=Mock(), event_report=report)
        self.assertEqual(changed, 5)
        self.assertEqual(describe.call_count, 5)
        self.assertEqual(report["reviewed"], 5)

    def run_enrich(self, cards, frames=lambda *a, **k: [b"f1", b"f2"], create=None, **setting_overrides):
        create = create or Mock(return_value=answer())
        patches = [patch.object(settings, name, value) for name, value in setting_overrides.items()]
        patches.append(patch.object(ve, "extract_window_frames", side_effect=frames))
        for p in patches:
            p.start()
        try:
            count = ve.enrich_cards(cards, "https://sas.example/video", target=make_target(create))
        finally:
            for p in patches:
                p.stop()
        return count, create

    def test_cards_get_a_visual_summary_and_tags(self):
        cards = [make_card("a"), make_card("b", start=25, end=55)]
        count, _ = self.run_enrich(cards)
        self.assertEqual(count, 2)
        for card in cards:
            self.assertTrue(card.summary.startswith("Visual: A referee shows a yellow card to a player."))
            self.assertEqual(card.labels[0], "yellow card")

    def test_frame_count_follows_the_card_duration_and_is_bounded(self):
        calls = []

        def frames(url, start, end, n, width, **kwargs):
            calls.append((round(end - start), n, width, kwargs))
            return [b"f"]

        cards = [make_card("long", end=30), make_card("short", start=0, end=4), make_card("tiny", start=0, end=1)]
        self.run_enrich(cards, frames=frames, visual_enrich_max_frames=12)
        # (duration, frames): roughly one keyframe per 2.5s, at least 2, at most the configured maximum.
        self.assertEqual(sorted((c[0], c[1]) for c in calls), [(1, 2), (4, 2), (30, 12)])
        self.assertTrue(all(c[2] == 640 and c[3] == {"niceness": 10} for c in calls))
        calls.clear()
        self.run_enrich([make_card("long", end=30)], frames=frames, visual_enrich_max_frames=6)
        self.assertEqual(calls[0][1], 6)

    def test_a_card_the_model_cannot_describe_is_left_untouched(self):
        cards = [make_card("a"), make_card("b")]
        create = Mock(side_effect=RuntimeError("Error code: 400 - content safety"))
        count, _ = self.run_enrich(cards, create=create)
        self.assertEqual(count, 0)
        self.assertEqual([c.summary for c in cards], ["Original summary."] * 2)
        self.assertEqual([c.labels for c in cards], [["football"]] * 2)

    def test_a_card_whose_frames_cannot_be_read_skips_the_model_call(self):
        create = Mock(return_value=answer())
        count, _ = self.run_enrich([make_card()], frames=lambda *a, **k: [], create=create)
        self.assertEqual(count, 0)
        create.assert_not_called()

    def test_the_card_cap_limits_how_many_are_enriched(self):
        cards = [make_card(f"c{i}", start=i * 25, end=i * 25 + 30) for i in range(5)]
        count, _ = self.run_enrich(cards, visual_enrich_max_cards=2)
        self.assertEqual(count, 2)
        self.assertTrue(cards[1].summary.startswith("Visual:"))
        self.assertFalse(cards[2].summary.startswith("Visual:"))

    def test_nothing_to_do_makes_no_calls(self):
        create = Mock()
        for cards, url in (([], "https://sas.example/v"), ([make_card()], "")):
            self.assertEqual(ve.enrich_cards(cards, url, target=make_target(create)), 0)
        create.assert_not_called()


def doc(card, **extra):
    return {**card.model_dump(), **extra}


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.cards = [make_card("c1", 0, 30), make_card("c2", 25, 55)]
        self.client = MagicMock()
        self.client.search.return_value = [doc(c) for c in reversed(self.cards)]
        self.client.merge_or_upload_documents.side_effect = lambda documents: [Mock(succeeded=True) for _ in documents]
        self.backup_exists = False
        self.container = MagicMock()
        self.container.get_blob_client.side_effect = lambda name: Mock(
            exists=lambda: self.backup_exists,
            download_blob=lambda: Mock(readall=lambda: self.stored["text"].encode()),
        )
        self.stored = {}
        for p in (
            patch.object(ve, "search_client", return_value=self.client),
            patch.object(ve, "blob_service", return_value=Mock(get_container_client=lambda name: self.container)),
            patch.object(ve, "make_sas_url", return_value="https://sas.example/v"),
            patch.object(ve, "upload_text", side_effect=lambda c, n, text, **k: self.stored.update(text=text)) ,
            patch.object(ve, "embed_texts", side_effect=lambda texts: [[float(len(t))] for t in texts]),
            patch.object(ve, "extract_window_frames", return_value=[b"f"]),
            patch.object(ve, "_judge_target", return_value=make_target(Mock(return_value=answer()))),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.upload = ve.upload_text

    def test_a_dry_run_reports_but_writes_nothing(self):
        result = ve.backfill("v1")
        self.assertEqual(result, {"index": settings.search_index_option1, "cards": 2, "enriched": 2, "applied": False})
        self.client.merge_or_upload_documents.assert_not_called()
        self.upload.assert_not_called()

    def test_apply_backs_up_the_originals_then_merges_summary_labels_and_a_new_vector(self):
        result = ve.backfill("v1", apply=True)
        self.assertTrue(result["applied"])
        backup = json.loads(self.stored["text"])
        self.assertEqual(backup["c1"], {"summary": "Original summary.", "labels": ["football"]})
        self.assertEqual(self.upload.call_args.args[:2], ("insights", "v1/pre-visual-enrich.json"))
        updates = self.client.merge_or_upload_documents.call_args.kwargs["documents"]
        self.assertEqual([u["id"] for u in updates], ["c1", "c2"])
        self.assertEqual(set(updates[0]), {"id", "summary", "labels", "content_vector"})
        self.assertTrue(updates[0]["summary"].startswith("Visual: "))
        self.assertEqual(updates[0]["labels"][0], "yellow card")
        self.assertEqual(len(updates[0]["content_vector"]), 1)

    def test_an_existing_backup_is_never_overwritten(self):
        self.backup_exists = True
        self.stored["text"] = json.dumps({"c1": {"summary": "Original summary.", "labels": ["football"]}})
        ve.backfill("v1", apply=True)
        self.upload.assert_not_called()
        self.client.merge_or_upload_documents.assert_called_once()

    def test_a_rerun_starts_from_the_backup_so_old_tags_do_not_pile_up(self):
        # The index already holds the output of an earlier run (tags + description); the backup holds the pristine text.
        enriched = make_card("c1", summary="Visual: Old description.\nOriginal summary.", labels=["stale tag", "football"])
        self.client.search.return_value = [doc(enriched)]
        self.backup_exists = True
        self.stored["text"] = json.dumps({"c1": {"summary": "Original summary.", "labels": ["football"]}})
        ve.backfill("v1", apply=True)
        update = self.client.merge_or_upload_documents.call_args.kwargs["documents"][0]
        self.assertEqual(update["summary"], "Visual: A referee shows a yellow card to a player.\nOriginal summary.")
        self.assertEqual(update["labels"], ["yellow card", "football"])
        self.upload.assert_not_called()

    def test_a_video_without_cards_is_a_no_op(self):
        self.client.search.return_value = []
        self.assertEqual(ve.backfill("v1", apply=True), {"index": settings.search_index_option1, "cards": 0, "enriched": 0, "applied": False})

    def test_restore_puts_the_original_text_back(self):
        enriched = [make_card("c1", summary="Visual: x\nOriginal summary.", labels=["yellow card", "football"])]
        self.client.search.return_value = [doc(c) for c in enriched]
        self.stored["text"] = json.dumps({"c1": {"summary": "Original summary.", "labels": ["football"]}})
        self.assertEqual(ve.restore("v1"), 1)
        update = self.client.merge_or_upload_documents.call_args.kwargs["documents"][0]
        self.assertEqual((update["id"], update["summary"], update["labels"]), ("c1", "Original summary.", ["football"]))


class CandidateRetrievalTests(unittest.TestCase):
    def test_candidates_are_scoped_unique_budgeted_and_expand_to_neighbors(self):
        cards = [make_card(f"c{index}", index * 10, index * 10 + 10) for index in range(10)]
        client = Mock()
        client.search.return_value = [{"id": "outside-video"}, {"id": "c4"}, {"id": "c7"}]
        with (patch.object(settings, "event_analysis_max_scenes", 4),
              patch.object(settings, "event_analysis_batch_size", 2)):
            batches = list(ve._candidate_batches(client, cards, "soccer_match", {"c4"}))
        selected = [card.id for batch in batches for card in batch]
        self.assertEqual(selected, ["c7", "c6", "c8"])
        self.assertTrue(all(len(batch) <= 2 for batch in batches))
        self.assertEqual(client.search.call_count, 6)
        for call in client.search.call_args_list:
            self.assertIn("video_id eq 'v1'", call.kwargs["filter"])
            self.assertIn("not search.in(id, 'c4', '|')", call.kwargs["filter"])
            self.assertNotEqual(call.kwargs["search_text"], "*")
            self.assertEqual(call.kwargs["top"], 4)

    def test_candidate_budget_includes_context_and_has_no_full_scan_fallback(self):
        cards = [make_card(f"c{index}", index * 10, index * 10 + 10) for index in range(10)]
        client = Mock()
        client.search.return_value = [{"id": card.id} for card in cards]
        with patch.object(settings, "event_analysis_max_scenes", 4):
            selected = [card.id for batch in ve._candidate_batches(client, cards, "soccer_match", set()) for card in batch]
        self.assertEqual(len(selected), 4)
        self.assertEqual(len(set(selected)), 4)
        client.search.return_value = []
        self.assertEqual(list(ve._candidate_batches(client, cards, "soccer_match", set())), [])


class EventQueryTests(unittest.TestCase):
    """Candidate retrieval runs one Azure AI Search simple-syntax query per event."""

    def queries(self):
        return dict(zip(SOCCER_EVENTS, PROFILES["soccer_match"]["queries"]))

    def test_every_event_is_searched_with_context_phrases_not_single_keywords(self):
        self.assertEqual(list(self.queries()), list(SOCCER_EVENTS))
        for kind, query in self.queries().items():
            with self.subTest(kind=kind):
                phrases = re.findall(r'"([^"]+)"', query)
                self.assertGreaterEqual(len(phrases), 8)
                self.assertTrue(all(len(phrase.split()) > 1 for phrase in phrases))
                # Simple syntax: quotes balanced, and no operator outside a phrase that would change what matches.
                self.assertEqual(query.count('"') % 2, 0)
                self.assertIsNone(re.search(r"[+|\-*()~\\]", re.sub(r'"[^"]*"', " ", query)))

    def test_corner_is_only_searched_as_a_phrase_because_goals_are_described_as_into_the_far_corner(self):
        bare_words = re.sub(r'"[^"]*"', " ", self.queries()["corner"]).split()
        self.assertNotIn("corner", [word.lower() for word in bare_words])


class ParallelEventReviewTests(unittest.TestCase):
    def cards(self, count):
        return [make_card(f"c{index}", index * 10, index * 10 + 10) for index in range(count)]

    @staticmethod
    def info(scene_id, events=()):
        return {"description": f"Review of {scene_id}.", "events": ["goal celebration"] if events else [],
                "custom_events": list(events), "events_reviewed": True, "event_review_reason": ""}

    def review(self, cards, *, describe, extract=None, workers=4, duration=1000):
        report = {"reviewed": 0, "reviewed_scene_ids": [], "candidates": [], "revalidated_scene_ids": [], "event_reviews": {}}
        with (patch.object(settings, "event_analysis_concurrency", workers),
              patch.object(ve, "_judge_target", return_value=Mock()),
              patch.object(ve, "extract_window_frames", side_effect=extract or (lambda *args, **kwargs: [b"frame"])),
              patch.object(ve, "describe_frames", side_effect=describe)):
            ve._revalidate_cards(cards, "source", cards, duration, report)
        return report

    def test_scenes_are_reviewed_at_the_same_time(self):
        rendezvous = threading.Barrier(2, timeout=3)  # a review only gets past it while another one is in flight

        def describe(target, frames, start, end, **kwargs):
            rendezvous.wait()
            return self.info(kwargs["scene_id"])

        report = self.review(self.cards(4), describe=describe, workers=2)
        self.assertEqual(report["reviewed_scene_ids"], ["c0", "c1", "c2", "c3"])

    def test_results_are_applied_in_scene_order_whichever_review_finishes_first(self):
        cards = self.cards(3)
        last_scene_done = threading.Event()

        def describe(target, frames, start, end, **kwargs):
            scene = kwargs["scene_id"]
            if scene == "c0":
                self.assertTrue(last_scene_done.wait(3))  # the first scene is the slowest
            if scene == "c2":
                last_scene_done.set()
            return None if scene == "c1" else self.info(scene)

        report = self.review(cards, describe=describe, workers=3)
        self.assertEqual(report["reviewed_scene_ids"], ["c0", "c2"])
        self.assertEqual(list(report["event_reviews"]), ["c0", "c1", "c2"])
        self.assertEqual(report["event_reviews"]["c1"]["reason"], "model_review_failed")
        self.assertEqual([card.summary.split("\n")[0] for card in cards],
                         ["Visual: Review of c0.", "Original summary.", "Visual: Review of c2."])

    def test_parallel_review_builds_the_same_report_as_one_scene_at_a_time(self):
        def describe(target, frames, start, end, **kwargs):
            scene = kwargs["scene_id"]
            time.sleep(0.01 * (5 - int(scene[1:])))  # later scenes finish first
            if scene in ("c1", "c4"):
                return None
            goal = parse_events([{"kind": "goal", "start_seconds": start + 1, "end_seconds": start + 3, "confidence": 0.9,
                                  "evidence": "Ball crosses the line and the goal is awarded.", "presentation": "live"}],
                                content_type="soccer_match", start=start, end=end, scene_id=scene)
            return self.info(scene, goal if scene in ("c2", "c5") else ())

        outcomes = []
        for workers in (1, 4):
            cards = self.cards(6)
            report = self.review(cards, describe=describe, workers=workers)
            outcomes.append((report, [(card.summary, card.labels) for card in cards]))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0]["reviewed_scene_ids"], ["c0", "c2", "c3", "c5"])
        self.assertEqual([event.scene_id for event in outcomes[0][0]["candidates"]], ["c2", "c5"])

    def test_one_at_a_time_stays_in_the_calling_thread(self):
        threads = []

        def describe(target, frames, start, end, **kwargs):
            threads.append(threading.current_thread())
            return self.info(kwargs["scene_id"])

        self.review(self.cards(3), describe=describe, workers=1)
        self.assertEqual(threads, [threading.current_thread()] * 3)

    def test_frame_extraction_is_capped_below_the_number_of_reviews_in_flight(self):
        lock = threading.Lock()
        running = peak = 0

        def extract(*args, **kwargs):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1
            return [b"frame"]

        self.review(self.cards(8), extract=extract, workers=6,
                    describe=lambda target, frames, start, end, **kwargs: self.info(kwargs["scene_id"]))
        self.assertLessEqual(peak, ve._EXTRACT_WORKERS)

    def test_a_context_that_is_too_long_fails_before_any_frames_or_model_calls(self):
        cards = [make_card("ok", 0, 10), make_card("long", 100, 190)]
        extract, describe = Mock(return_value=[b"frame"]), Mock()
        with (patch.object(settings, "event_analysis_concurrency", 4), patch.object(ve, "_judge_target", return_value=Mock()),
              patch.object(ve, "extract_window_frames", extract), patch.object(ve, "describe_frames", describe)):
            with self.assertRaisesRegex(ValueError, "must not exceed 90 seconds"):
                ve._revalidate_cards(cards, "source", cards, 1000, {"event_reviews": {}})
        extract.assert_not_called()
        describe.assert_not_called()


class EventEnrichmentWriteTests(unittest.TestCase):
    def test_completed_scene_revalidation_uses_dense_context_and_preserves_other_events(self):
        cards = [make_card("target", 20, 30), make_card("other", 50, 60)]
        old = {"kind": "yellow_card", "start_seconds": 52, "end_seconds": 55, "confidence": 0.95,
               "evidence": "The referee clearly displays a yellow card.", "presentation": "live", "scene_id": "other"}
        previous = {"content_type": "soccer_match", "status": "completed", "reviewed_scenes": 2, "events": [old]}
        events = parse_events([{"kind": "corner", "start_seconds": 22, "end_seconds": 25, "confidence": 0.9,
                                "evidence": "A live corner kick is delivered into the penalty area.", "presentation": "live"}],
                              content_type="soccer_match", start=20, end=30, scene_id="target")
        info = {"description": "A corner is taken.", "events": ["corner kick"], "custom_events": events,
                "events_reviewed": True, "event_review_reason": "Original delivery precedes the replay."}
        client = Mock()
        client.merge_or_upload_documents.return_value = [Mock(succeeded=True)]
        with (patch.object(ve, "read_video_json", side_effect=lambda video, name: {"id": "vi", "durationInSeconds": 60} if name == "option1.json" else previous),
              patch.object(ve, "search_client", return_value=client), patch.object(ve, "_fetch_cards", return_value=cards),
              patch.object(ve, "make_sas_url", return_value="source"), patch.object(ve, "_judge_target", return_value=Mock()),
              patch.object(ve, "extract_window_frames", return_value=[b"frame"]) as extract,
              patch.object(ve, "describe_frames", return_value=info) as describe,
              patch.object(ve, "embed_texts", return_value=[[1.0]]), patch.object(ve, "upload_text", return_value="manifest") as upload):
            ve.enrich_video_events("v1", "soccer_match", scene_ids=["target"])
        self.assertEqual(extract.call_args_list, [
            unittest.mock.call("source", 12, 20, 2, 960, niceness=10, keyframes_only=False),
            unittest.mock.call("source", 20, 30, 30, 960, niceness=10, keyframes_only=False),
            unittest.mock.call("source", 30, 38, 2, 960, niceness=10, keyframes_only=False),
        ])
        self.assertEqual(describe.call_args.kwargs["context_window"], (12, 38))
        self.assertEqual(describe.call_args.kwargs["frame_times"], [14, 20 + 1 / 6, 32])
        manifest = json.loads(upload.call_args.args[2])
        self.assertEqual(manifest["event_reviews"]["target"]["requested_frame_count"], 34)
        self.assertEqual(manifest["event_reviews"]["target"]["frame_width"], 960)
        self.assertEqual(manifest["reviewed_scenes"], 2)
        self.assertEqual({event["kind"] for event in manifest["events"]}, {"corner", "yellow_card"})
        self.assertEqual(manifest["event_reviews"]["target"]["reason"], "accepted")
        self.assertTrue(manifest["analysis_complete"])
        self.assertEqual(client.merge_or_upload_documents.call_args.kwargs["documents"][0]["id"], "target")

    def test_detailed_revalidation_frame_budget_is_capped(self):
        card = make_card("target", 10, 80)
        with (patch.object(ve, "_judge_target", return_value=Mock()),
              patch.object(ve, "extract_window_frames", return_value=[]) as extract):
            ve._revalidate_cards([card], "source", [card], 100, {"event_reviews": {}}, detailed=True)
        self.assertEqual(sum(call.args[3] for call in extract.call_args_list), 50)
        self.assertEqual(extract.call_args_list[1],
                 unittest.mock.call("source", 10, 80, 46, 960, niceness=10, keyframes_only=False))

    def test_failed_revalidation_preserves_existing_tag_and_records_failure(self):
        card = make_card("target")
        old = {"kind": "corner", "start_seconds": 2, "end_seconds": 5, "confidence": 0.9,
               "evidence": "A corner kick is taken in the original live view.", "presentation": "live", "scene_id": "target"}
        previous = {"content_type": "soccer_match", "status": "completed", "events": [old]}
        client = Mock()
        with (patch.object(ve, "read_video_json", side_effect=lambda video, name: {"id": "vi"} if name == "option1.json" else previous),
              patch.object(ve, "search_client", return_value=client), patch.object(ve, "_fetch_cards", return_value=[card]),
              patch.object(ve, "make_sas_url", return_value="source"), patch.object(ve, "_judge_target", return_value=Mock()),
              patch.object(ve, "extract_window_frames", return_value=[]), patch.object(ve, "embed_texts") as embed,
              patch.object(ve, "upload_text", return_value="manifest") as upload):
            ve.enrich_video_events("v1", "soccer_match", scene_ids=["target"])
        embed.assert_not_called()
        client.merge_or_upload_documents.assert_not_called()
        manifest = json.loads(upload.call_args.args[2])
        self.assertEqual(len(manifest["events"]), 1)
        self.assertFalse(manifest["analysis_complete"])
        self.assertEqual(manifest["event_reviews"]["target"]["reason"], "frame_extraction_failed")

    def test_context_frames_cannot_create_events_outside_target_annotation_window(self):
        outside = {"kind": "corner", "start_seconds": 12, "end_seconds": 15, "confidence": 0.95,
                   "evidence": "A corner delivery is visible in the preceding scene.", "presentation": "live"}
        create = Mock(return_value=chat_response(json.dumps({"description": "Players wait.", "events": [],
                       "custom_events": [outside], "events_reviewed": True, "event_review_reason": "Context only."})))
        info = ve.describe_frames(make_target(create), [b"frame"], 20, 30, content_type="soccer_match", context_window=(12, 38))
        self.assertEqual(info["custom_events"], [])
        self.assertFalse(info["events_reviewed"])
        self.assertEqual(info["event_review_reason"], "Context only.")
        self.assertIn("12s-38s", create.call_args.kwargs["messages"][1]["content"][0]["text"])

    def test_earlier_first_view_removes_stale_canonical_tag_from_prior_scene(self):
        cards = [make_card("early", 0, 10), make_card("late", 30, 40)]
        cards[1].labels.append("event:goal")
        cards[1].summary += "\nFirst-view Goal: Prior occurrence."
        event = {"kind": "goal", "start_seconds": 32, "end_seconds": 35, "confidence": 0.95,
                 "evidence": "Ball crosses the line and referee awards the goal.", "presentation": "live",
                 "match_clock_seconds": 600, "scene_id": "late"}
        previous = {"content_type": "soccer_match", "status": "partial", "events": [event],
                    "candidates": [event], "reviewed_scene_ids": ["late"]}
        earlier = parse_events([{**event, "start_seconds": 2, "end_seconds": 5}],
                               content_type="soccer_match", start=0, end=10, scene_id="early")
        info = {"description": "A goal is scored.", "events": [], "custom_events": earlier, "events_reviewed": True}
        client = Mock()
        client.merge_or_upload_documents.side_effect = lambda documents: [Mock(succeeded=True) for _ in documents]
        with (patch.object(ve, "read_video_json", side_effect=lambda video, name: {"id": "vi-test"} if name == "option1.json" else previous),
              patch.object(ve, "search_client", return_value=client),
              patch.object(ve, "_fetch_cards", return_value=cards),
              patch.object(ve, "_candidate_batches", return_value=iter([[cards[0]]])),
              patch.object(ve, "make_sas_url", return_value="source"),
              patch.object(ve, "_judge_target", return_value=Mock()),
              patch.object(ve, "extract_window_frames", return_value=[b"frame"]),
              patch.object(ve, "describe_frames", return_value=info),
              patch.object(ve, "embed_texts", side_effect=lambda texts: [[1.0] for _ in texts]),
              patch.object(ve, "upload_text", return_value="manifest") as upload):
            ve.enrich_video_events("v1", "soccer_match")
        updates = {item["id"]: item for item in client.merge_or_upload_documents.call_args.kwargs["documents"]}
        self.assertIn("event:goal", updates["early"]["labels"])
        self.assertNotIn("event:goal", updates["late"]["labels"])
        self.assertNotIn("First-view Goal:", updates["late"]["summary"])
        self.assertEqual(json.loads(upload.call_args.args[2])["events"][0]["scene_id"], "early")

    def test_targeted_passes_resume_preserve_events_and_only_embed_reviewed_candidates(self):
        cards = [make_card(f"c{index}", index * 10, index * 10 + 10) for index in range(6)]
        old_event = {"kind": "goal", "start_seconds": 52, "end_seconds": 55, "confidence": 0.95,
                     "evidence": "Ball crosses the line and referee awards the goal.", "presentation": "live",
                     "scene_id": "c5"}
        stored = {"content_type": "soccer_match", "status": "partial", "reviewed_scene_ids": ["c5"],
                  "reviewed_scenes": 1, "candidates": [old_event]}
        raw = {"videos": [{"id": "vi-test", "durationInSeconds": 60}]}
        client = Mock()
        client.search.return_value = [{"id": "c2"}]
        client.merge_or_upload_documents.side_effect = lambda documents: [Mock(succeeded=True) for _ in documents]
        info = {"description": "Players on the pitch.", "events": [], "custom_events": [], "events_reviewed": True}
        def save(container, name, text):
            stored.clear()
            stored.update(json.loads(text))
            return "https://example.test/manifest"
        with (patch.object(ve, "read_video_json", side_effect=lambda video, name: raw if name == "option1.json" else dict(stored)),
              patch.object(ve, "search_client", return_value=client),
              patch.object(ve, "_fetch_cards", return_value=cards),
              patch.object(ve, "make_sas_url", return_value="https://example.test/source"),
              patch.object(ve, "extract_window_frames", return_value=[b"frame"]) as extract,
              patch.object(ve, "describe_frames", return_value=info),
              patch.object(ve, "_judge_target", return_value=Mock()),
              patch.object(ve, "embed_texts", side_effect=lambda texts: [[1.0] for _ in texts]) as embed,
              patch.object(ve, "upload_text", side_effect=save),
              patch.object(settings, "event_analysis_max_scenes", 2),
              patch.object(settings, "event_analysis_batch_size", 1)):
            ve.enrich_video_events("v1", "soccer_match")
            self.assertEqual(stored["analysis_mode"], "query_targeted")
            self.assertEqual(stored["analysis_version"], 2)
            self.assertTrue(all(call.kwargs["keyframes_only"] is False for call in extract.call_args_list))
            self.assertEqual(stored["stop_reason"], "scene_budget")
            self.assertTrue(stored["analysis_complete"])
            self.assertEqual(stored["reviewed_scene_ids"], ["c1", "c2", "c5"])
            self.assertEqual(stored["status"], "partial")
            self.assertEqual(stored["events"][0]["scene_id"], "c5")
            client.search.return_value = [{"id": "c4"}]
            ve.enrich_video_events("v1", "soccer_match")
            self.assertEqual(stored["reviewed_scene_ids"], ["c1", "c2", "c3", "c4", "c5"])
            self.assertEqual(stored["reviewed_scenes"], 5)
            self.assertEqual(sum(len(call.args[0]) for call in embed.call_args_list), 4)
        written = [item["id"] for call in client.merge_or_upload_documents.call_args_list for item in call.kwargs["documents"]]
        self.assertEqual(written, ["c2", "c1", "c4", "c3"])
        self.assertEqual(len(stored["events"]), 1)

    def test_empty_candidate_results_never_trigger_full_scan_or_embeddings(self):
        cards = [make_card()]
        with (patch.object(ve, "read_video_json", side_effect=lambda video, name: {"id": "vi-test"} if name == "option1.json" else None),
              patch.object(ve, "search_client", return_value=Mock()),
              patch.object(ve, "_fetch_cards", return_value=cards),
              patch.object(ve, "_candidate_batches", return_value=iter([])),
              patch.object(ve, "enrich_cards") as enrich,
              patch.object(ve, "embed_texts") as embed,
              patch.object(ve, "upload_text", return_value="manifest") as upload):
            ve.enrich_video_events("v1", "soccer_match")
        enrich.assert_not_called()
        embed.assert_not_called()
        manifest = json.loads(upload.call_args.args[2])
        self.assertEqual(manifest["reviewed_scenes"], 0)
        self.assertEqual(manifest["status"], "partial")
        self.assertTrue(manifest["analysis_complete"])

    def test_failed_frame_validation_stays_retryable_without_claiming_review(self):
        card = make_card()
        with (patch.object(ve, "read_video_json", side_effect=lambda video, name: {"id": "vi-test"} if name == "option1.json" else None),
              patch.object(ve, "search_client", return_value=Mock()),
              patch.object(ve, "_fetch_cards", return_value=[card]),
              patch.object(ve, "_candidate_batches", return_value=iter([[card]])),
              patch.object(ve, "make_sas_url", return_value="source"),
              patch.object(ve, "extract_window_frames", return_value=[]),
              patch.object(ve, "embed_texts") as embed,
              patch.object(ve, "upload_text", return_value="manifest") as upload):
            ve.enrich_video_events("v1", "soccer_match")
        embed.assert_not_called()
        manifest = json.loads(upload.call_args.args[2])
        self.assertFalse(manifest["analysis_complete"])
        self.assertEqual(manifest["reviewed_scene_ids"], [])
        self.assertEqual(manifest["attempted_scenes"], 1)

    def run_enrichment(self, *, failed_write=False):
        card = make_card()
        card.content_type = "soccer_match"
        raw = {"videos": [{"id": "vi-test", "durationInSeconds": 30}]}
        candidates = parse_events([
            {"kind": "goal", "start_seconds": 4, "end_seconds": 8, "confidence": 0.95,
             "evidence": "Ball visibly crosses the line and the referee awards the goal.", "presentation": "live"},
            {"kind": "goal", "start_seconds": 20, "end_seconds": 24, "confidence": 0.98,
             "evidence": "Slow motion replay shows the same goal.", "presentation": "replay"},
        ], content_type="soccer_match", start=0, end=30, scene_id=card.id)
        def enrich(cards, url, inventory, duration, event_report, *, detailed=False):
            self.assertFalse(detailed)
            event_report.update(reviewed=1, candidates=candidates, reviewed_scene_ids=[card.id], revalidated_scene_ids=[card.id])
        client = Mock()
        client.merge_or_upload_documents.return_value = [Mock(succeeded=not failed_write)]
        with (patch.object(ve, "read_video_json", return_value=raw),
              patch.object(ve, "search_client", return_value=client),
              patch.object(ve, "_fetch_cards", return_value=[card]),
              patch.object(ve, "_candidate_batches", return_value=iter([[card]])),
              patch.object(ve, "make_sas_url", return_value="https://example.test/source"),
              patch.object(ve, "_revalidate_cards", side_effect=enrich),
              patch.object(ve, "embed_texts", return_value=[[1.0]]),
              patch.object(ve, "upload_text", return_value="https://example.test/insights") as upload):
            if failed_write:
                with self.assertRaisesRegex(RuntimeError, "Search did not accept"):
                    ve.enrich_video_events("v1", "soccer_match")
                upload.assert_not_called()
                return
            ve.enrich_video_events("v1", "soccer_match")
            manifest = json.loads(upload.call_args.args[2])
            self.assertEqual(len(manifest["events"]), 1)
            self.assertEqual(len(manifest["candidates"]), 2)
            update = client.merge_or_upload_documents.call_args.kwargs["documents"][0]
            self.assertEqual(update["content_type"], "soccer_match")
            self.assertIn("event:goal", update["labels"])
            self.assertIn("First-view Goal:", update["summary"])

    def test_reenrichment_persists_first_view_search_metadata_and_replay_audit(self):
        self.run_enrichment()

    def test_failed_search_write_cannot_publish_a_completed_manifest(self):
        self.run_enrichment(failed_write=True)


if __name__ == "__main__":
    unittest.main()
