import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from azure.search.documents.models import VectorizedQuery
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.models import SearchFilters, SearchRequest, SearchResultItem
from app.pipelines import openai_helpers
from app.routers import search as search_router


def _fake_chat_response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TaggedSearchTests(unittest.TestCase):
    def test_simple_moment_queries_resolve_to_profile_tags(self):
        for query, expected in [("a corner", "corner"), ("corners", "corner"), ("a foul", "foul"),
                                ("yellow cards", "yellow_card"), ("A scored goal", "goal")]:
            with self.subTest(query=query):
                self.assertEqual(search_router._requested_event(SearchRequest(query=query)), expected)
        self.assertIsNone(search_router._requested_event(SearchRequest(query="Rivera near the corner flag")))

    def test_unknown_explicit_tag_is_rejected(self):
        with self.assertRaises(Exception) as caught:
            search_router.search(SearchRequest(query="", event_kind="invented"))
        self.assertEqual(caught.exception.status_code, 422)

    def test_tag_search_preserves_filters_and_does_not_call_models(self):
        client = Mock()
        client.search.return_value = [{"id": "corner-1", "video_id": "video-1", "filename": "match.mp4",
                                       "start_seconds": 20, "end_seconds": 25, "labels": ["event:corner"],
                                       "summary": "A corner is taken", "transcript_excerpt": "", "blob_url": ""}]
        with patch.object(search_router, "search_client", return_value=client), \
             patch.object(search_router, "rewrite_query") as rewrite, \
             patch.object(search_router, "embed_texts") as embed, \
             patch.object(search_router, "_run_action_rerank") as rerank:
            result = search_router.search(SearchRequest(query="a corner", filters=SearchFilters(
                video_ids=["video-1"], content_types=["soccer_match"])))
        self.assertEqual(result.event_kind, "corner")
        self.assertEqual(result.count, 1)
        arguments = client.search.call_args.kwargs
        self.assertIn("event:corner", arguments["filter"])
        self.assertIn("video-1", arguments["filter"])
        self.assertIn("soccer_match", arguments["filter"])
        self.assertEqual(arguments["search_text"], "*")
        rewrite.assert_not_called()
        embed.assert_not_called()
        rerank.assert_not_called()

    def test_no_tagged_matches_returns_empty_without_falling_back(self):
        client = Mock()
        client.search.return_value = []
        with patch.object(search_router, "search_client", return_value=client), \
             patch.object(search_router, "rewrite_query") as rewrite:
            result = search_router.search(SearchRequest(query="", event_kind="corner"))
        self.assertEqual(result.count, 0)
        rewrite.assert_not_called()

    def test_two_tagged_moments_close_together_are_not_merged_by_the_playback_padding(self):
        # With a 60s clip the playback windows of goals 20s apart overlap, but they are two scenes.
        def goal(id_, start, end=None):
            return {"id": id_, "video_id": "video-1", "filename": "half.mp4", "start_seconds": start,
                    "end_seconds": start + 4 if end is None else end, "labels": ["event:goal"], "summary": "",
                    "transcript_excerpt": "", "blob_url": ""}

        client = Mock()
        client.search.return_value = [goal("g1", 100), goal("g2", 120)]
        with patch.object(search_router, "search_client", return_value=client):
            result = search_router.search(SearchRequest(query="", event_kind="goal", clip_duration_seconds=60))
        self.assertEqual([item.id for item in result.items], ["g1", "g2"])

    def test_neighbouring_indexing_windows_are_two_moments_but_a_scene_covering_another_is_one(self):
        # Windowed indexing overlaps neighbouring scenes by 5s; that is not the same moment.
        def goal(id_, start, end):
            return {"id": id_, "video_id": "video-1", "filename": "half.mp4", "start_seconds": start,
                    "end_seconds": end, "labels": ["event:goal"], "summary": "", "transcript_excerpt": "",
                    "blob_url": ""}

        client = Mock()
        client.search.return_value = [goal("w1", 100, 120), goal("w2", 115, 135), goal("same", 117, 137)]
        with patch.object(search_router, "search_client", return_value=client):
            result = search_router.search(SearchRequest(query="", event_kind="goal"))
        self.assertEqual([item.id for item in result.items], ["w1", "w2"])

    def test_a_cut_by_the_number_of_results_is_reported(self):
        client = Mock()
        client.search.return_value = [
            {"id": f"c{i}", "video_id": "video-1", "filename": "half.mp4", "start_seconds": i * 100,
             "end_seconds": i * 100 + 4, "labels": ["event:corner"], "summary": "", "transcript_excerpt": "",
             "blob_url": ""} for i in range(3)
        ]
        with patch.object(search_router, "search_client", return_value=client):
            result = search_router.search(SearchRequest(query="", event_kind="corner", top=2))
        self.assertEqual((result.count, result.total_matches), (2, 3))
        # Every tagged hit is retrieved: the cut happens after the chronological sort, not in the lookup.
        self.assertEqual(client.search.call_args.kwargs["top"], 1000)


def _moment(id_, start, *, video="video-1", filename="half-1.mp4", labels=None, rerank=2.0, end=None):
    return {"id": id_, "video_id": video, "filename": filename, "start_seconds": float(start),
            "end_seconds": float(start) + 4.0 if end is None else float(end), "@search.score": 1.0,
            "@search.reranker_score": rerank, "summary": "", "transcript_excerpt": "", "labels": labels or [],
            "entities": [], "ocr_text": "", "speakers": [], "thumbnail_url": "",
            "blob_url": f"https://example.test/videos/{video}/match.mp4"}


def _verdict(match_type, **extra):
    return {"match_type": match_type, "entity_match": None, "evidence": "", "reason": f"{match_type} reason", **extra}


class MomentTagDeepSearchTests(unittest.TestCase):
    """A Moment-tag search with visual confirmation switched on also finds moments the index-time tags missed
    (the reported case: "goals" over two halves returned the 2 tagged goals of 4)."""

    def setUp(self):
        app = FastAPI()
        app.include_router(search_router.router, prefix="/api/search")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        for name, value in (("search_action_rerank_enabled", True), ("search_action_rerank_max_candidates", 40),
                            ("search_deep_merge_window_seconds", 60.0)):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_search(self, tagged, pool, text=None, visual=None, **request):
        client = Mock()
        client.search.side_effect = [_FakeSearchResults(tagged), _FakeSearchResults(pool)]
        self.search_client = client

        def judge(query, rewrite, candidates, frames=None):
            return (visual or {}) if frames else (text or {})

        gather = Mock(side_effect=lambda items, scene_ends, **kwargs: {item.id: [b"frame"] for item in items})
        with (patch.object(search_router, "search_client", return_value=client),
              patch.object(search_router, "rewrite_query") as rewrite,
              patch.object(search_router, "embed_texts", side_effect=lambda texts: [[1.0]] * len(texts)) as embed,
              patch.object(search_router, "_gather_confirmation_frames", gather),
              patch.object(search_router, "rerank_action_candidates", side_effect=judge) as rerank):
            response = self.client.post("/api/search", json={"query": "goals", "top": 10, **request})
        return response, SimpleNamespace(gather=gather, rewrite=rewrite, embed=embed, rerank=rerank)

    def test_untagged_goals_are_recovered_from_their_frames_and_labelled(self):
        tagged = [_moment("goal-1", 100, labels=["event:goal"])]
        pool = [
            _moment("same-moment", 130),  # 30s after the tagged goal: its celebration, not a new moment
            _moment("missed-goal", 600),
            _moment("replay", 640),  # 40s later: folded into the earlier clip
            _moment("inconclusive", 900),  # text says goal, frames cannot confirm
            _moment("saved-shot", 1200),
            _moment("half2-goal", 50, video="video-2", filename="half-2.mp4"),
        ]
        judged = ["missed-goal", "replay", "inconclusive", "saved-shot", "half2-goal"]
        text = {id_: _verdict("scoring_sequence") for id_ in judged}
        visual = {
            "missed-goal": _verdict("scoring_sequence", presentation="live"),
            "replay": _verdict("scoring_sequence", presentation="replay"),
            "inconclusive": _verdict("uncertain"),
            "saved-shot": _verdict("attempt_only"),
            "half2-goal": _verdict("scoring_sequence"),
        }
        response, calls = self.run_search(tagged, pool, text, visual, visual_rerank_enabled=True,
                                          visual_rerank_max_candidates=5, visual_rerank_frames=6)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([(it["id"], it["origin"]) for it in body["items"]],
                         [("goal-1", "tag"), ("missed-goal", "deep_search"), ("half2-goal", "deep_search")])
        self.assertTrue(body["items"][1]["visual_checked"])
        self.assertEqual(body["items"][1]["presentation"], "live")
        self.assertEqual(body["deep_search"], {"tagged": 1, "recovered": 2, "candidates_judged": 5,
                                               "frames_checked": 5, "failed": False})
        self.assertEqual(body["total_matches"], 3)
        # Tag searches have no query text, so the event's own description drives retrieval; no model rewrite.
        calls.rewrite.assert_not_called()
        hybrid = self.search_client.search.call_args_list[1].kwargs
        self.assertEqual(hybrid["query_type"], "semantic")
        self.assertIn("goal", hybrid["search_text"])
        # Frame checks are not spent on the tagged moment's own aftermath, and honour the chosen count.
        self.assertEqual({it.id for it in calls.gather.call_args.args[0]}, set(judged))
        self.assertEqual(calls.gather.call_args.kwargs, {"frames_per_candidate": 6})
        self.assertNotIn("same-moment", [c["id"] for c in calls.rerank.call_args_list[0].args[2]])

    def test_frame_checks_are_shared_between_the_videos(self):
        # The first half's candidates all outrank the second half's: unbalanced, they would take every slot.
        pool = [_moment(f"h1-{i}", 100 * (i + 1), rerank=3.5) for i in range(4)] + [
            _moment("h2-0", 300, video="video-2", filename="half-2.mp4", rerank=2.0)]
        text = {item["id"]: _verdict("scoring_sequence") for item in pool}
        response, calls = self.run_search([], pool, text, {}, visual_rerank_enabled=True,
                                          visual_rerank_max_candidates=3)
        self.assertEqual(response.status_code, 200)
        self.assertIn("h2-0", {it.id for it in calls.gather.call_args.args[0]})

    def test_a_failing_deep_search_returns_the_tagged_moments_and_says_so(self):
        tagged = [_moment("goal-1", 100, labels=["event:goal"])]
        with patch.object(search_router, "_retrieve_pool", side_effect=RuntimeError("search is down")):
            response, _ = self.run_search(tagged, [], visual_rerank_enabled=True)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([it["id"] for it in body["items"]], ["goal-1"])
        self.assertTrue(body["deep_search"]["failed"])

    def test_a_judge_that_answers_for_no_candidate_is_a_failed_deep_search_not_an_empty_one(self):
        pool = [_moment("a", 300), _moment("b", 900)]
        response, _ = self.run_search([], pool, text={}, visual={}, visual_rerank_enabled=True)
        self.assertTrue(response.json()["deep_search"]["failed"])

    def test_frames_the_judge_never_answered_for_are_not_reported_as_checked(self):
        pool = [_moment("a", 300), _moment("b", 900)]
        text = {"a": _verdict("scoring_sequence"), "b": _verdict("scoring_sequence")}
        response, calls = self.run_search([], pool, text=text, visual={}, visual_rerank_enabled=True)
        calls.gather.assert_called_once()  # frames were extracted ...
        stats = response.json()["deep_search"]
        self.assertEqual((stats["candidates_judged"], stats["frames_checked"], stats["recovered"], stats["failed"]),
                         (2, 0, 0, False))  # ... but nothing was confirmed from them

    def test_a_recovered_clip_reaches_the_end_of_the_scene_whose_frames_were_checked(self):
        pool = [_moment("window", 200, end=230), _moment("long-shot", 400, end=700)]
        text = {"window": _verdict("scoring_sequence"), "long-shot": _verdict("scoring_sequence")}
        response, _ = self.run_search([], pool, text=text, visual=dict(text), visual_rerank_enabled=True,
                                      clip_duration_seconds=15)
        ends = {it["id"]: it["end_seconds"] for it in response.json()["items"]}
        self.assertEqual(ends, {"window": 230.0, "long-shot": 460.0})  # a scene's own end, never beyond 60s

    def test_it_makes_no_model_calls_unless_visual_confirmation_is_explicitly_on(self):
        tagged = [_moment("goal-1", 100, labels=["event:goal"])]
        for options in ({}, {"visual_rerank_enabled": False},
                        {"visual_rerank_enabled": True, "visual_rerank_max_candidates": 0}):
            with self.subTest(options=options):
                response, calls = self.run_search(tagged, [], **options)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()["deep_search"])
                for model_call in (calls.rewrite, calls.embed, calls.rerank, calls.gather):
                    model_call.assert_not_called()
        with patch.object(settings, "search_action_rerank_enabled", False):
            response, calls = self.run_search(tagged, [], visual_rerank_enabled=True)
        calls.rerank.assert_not_called()
        self.assertIsNone(response.json()["deep_search"])


class SelectEmbeddingTextsTests(unittest.TestCase):
    def test_entities_are_joined_into_a_separate_entity_text(self):
        content, entity = search_router._select_embedding_texts(
            "Sam Rivera scoring a football goal", ["Sam Rivera", "", "  ", "Northfield FC"],
        )
        self.assertEqual(content, "Sam Rivera scoring a football goal")
        self.assertEqual(entity, "Sam Rivera, Northfield FC")

    def test_no_entities_returns_none_so_entity_vector_is_skipped(self):
        content, entity = search_router._select_embedding_texts("goals being scored", [])
        self.assertEqual(content, "goals being scored")
        self.assertIsNone(entity)

    def test_blank_only_entities_return_none(self):
        _, entity = search_router._select_embedding_texts("goals being scored", ["", "   "])
        self.assertIsNone(entity)


class BuildVectorQueriesTests(unittest.TestCase):
    def test_no_vectors_produces_no_queries(self):
        self.assertEqual(search_router._build_vector_queries(None, None, 50), [])

    def test_content_only(self):
        queries = search_router._build_vector_queries([1.0, 2.0], None, 50)
        self.assertEqual(len(queries), 1)
        self.assertIsInstance(queries[0], VectorizedQuery)
        self.assertEqual(queries[0].fields, "content_vector")
        self.assertEqual(queries[0].vector, [1.0, 2.0])
        self.assertEqual(queries[0].k_nearest_neighbors, 50)

    def test_entity_only(self):
        queries = search_router._build_vector_queries(None, [3.0], 50)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].fields, "entity_vector")

    def test_both_vectors_produce_two_distinct_queries(self):
        queries = search_router._build_vector_queries([1.0], [2.0], 25)
        self.assertEqual([q.fields for q in queries], ["content_vector", "entity_vector"])
        self.assertEqual([q.vector for q in queries], [[1.0], [2.0]])
        self.assertTrue(all(q.k_nearest_neighbors == 25 for q in queries))


class ActionRankTests(unittest.TestCase):
    def test_known_match_types_rank_in_expected_order(self):
        ordered = ("scoring_sequence", "celebration_only", "uncertain", "attempt_only", "mention_only", "unrelated")
        ranks = [search_router._action_rank(match_type, None, False) for match_type in ordered]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertEqual(len(set(ranks)), len(ranks))

    def test_unknown_or_missing_match_type_ranks_as_uncertain(self):
        uncertain = search_router._action_rank("uncertain", None, False)
        self.assertEqual(search_router._action_rank(None, None, False), uncertain)
        self.assertEqual(search_router._action_rank("not_a_real_type", None, False), uncertain)

    def test_a_candidate_without_a_verdict_outranks_one_judged_mention_only(self):
        # Regression: verdicts the model dropped used to rank BELOW mention_only, which silently
        # pushed real goal windows out of the top results.
        self.assertGreater(
            search_router._action_rank(None, None, False),
            search_router._action_rank("mention_only", None, False),
        )
        self.assertGreater(
            search_router._action_rank(None, None, False),
            search_router._action_rank("attempt_only", None, False),
        )

    def test_entity_mismatch_only_penalized_when_query_requires_an_entity(self):
        top = search_router._action_rank("scoring_sequence", None, False)
        self.assertEqual(
            search_router._action_rank("scoring_sequence", False, requires_entity=True),
            top - search_router._ENTITY_MISMATCH_PENALTY,
        )
        self.assertEqual(search_router._action_rank("scoring_sequence", False, requires_entity=False), top)
        self.assertEqual(search_router._action_rank("scoring_sequence", True, requires_entity=True), top)
        self.assertEqual(search_router._action_rank("scoring_sequence", None, requires_entity=True), top)


def _item(id_, *, match_type=None, visual_checked=False, rerank=1.0, score=1.0, entity_match=None, start=0.0, end=5.0,
          blob_url="https://acct.blob.core.windows.net/videos/a.mp4"):
    return SearchResultItem(
        id=id_, video_id=f"v-{id_}", filename="a.mp4", start_seconds=start, end_seconds=end, score=score,
        rerank_score=rerank, summary="", transcript_excerpt="", labels=[], thumbnail_url=None,
        blob_url=blob_url, play_url="", match_type=match_type, visual_checked=visual_checked,
        entity_match=entity_match,
    )


class FinalSortKeyTests(unittest.TestCase):
    def test_frame_checked_results_beat_text_only_ones_within_the_same_verdict_tier(self):
        checked = _item("checked", match_type="scoring_sequence", visual_checked=True, rerank=1.0)
        text_only = _item("text", match_type="scoring_sequence", visual_checked=False, rerank=3.0)
        ordered = sorted([text_only, checked], key=lambda it: search_router._final_sort_key(it, False), reverse=True)
        self.assertEqual([it.id for it in ordered], ["checked", "text"])

    def test_verdict_tier_dominates_the_frame_checked_tiebreak(self):
        celebration_checked = _item("celebration", match_type="celebration_only", visual_checked=True)
        scoring_text = _item("scoring", match_type="scoring_sequence", visual_checked=False)
        ordered = sorted([celebration_checked, scoring_text],
                         key=lambda it: search_router._final_sort_key(it, False), reverse=True)
        self.assertEqual([it.id for it in ordered], ["scoring", "celebration"])

    def test_results_never_submitted_to_the_judge_rank_with_non_matches_not_above_unknowns(self):
        skipped = _item("skipped", match_type=None, rerank=1.0)              # submitted, no verdict
        mention = _item("mention", match_type="mention_only", rerank=2.0)    # judged non-match
        never = _item("never", match_type=None, rerank=1.5)                  # beyond the judged pool
        unjudged = frozenset({"never"})
        ordered = sorted([never, mention, skipped],
                         key=lambda it: search_router._final_sort_key(it, False, unjudged), reverse=True)
        # unknown > (judged non-match ~ unjudged, semantic order within the tier)
        self.assertEqual([it.id for it in ordered], ["skipped", "mention", "never"])


class ApplyVerdictsTests(unittest.TestCase):
    def _verdict(self, match_type, reason="r"):
        return {"match_type": match_type, "entity_match": None, "evidence": "", "reason": reason}

    def test_text_verdicts_are_copied_and_do_not_mark_frames_checked(self):
        items = [_item("a"), _item("b")]
        applied = search_router._apply_verdicts(items, {"a": self._verdict("mention_only", "talk")}, visual=False)
        self.assertEqual(applied, 1)
        self.assertEqual(items[0].match_type, "mention_only")
        self.assertEqual(items[0].match_reason, "talk")
        self.assertFalse(items[0].visual_checked)
        self.assertIsNone(items[1].match_type)

    def test_visual_verdicts_overwrite_the_text_verdict_and_mark_frames_checked(self):
        item = _item("a", match_type="scoring_sequence")
        search_router._apply_verdicts([item], {"a": self._verdict("attempt_only", "saved")}, visual=True)
        self.assertEqual(item.match_type, "attempt_only")
        self.assertTrue(item.visual_checked)

    def test_inconclusive_visual_verdict_keeps_the_text_verdict(self):
        item = _item("a", match_type="scoring_sequence")
        applied = search_router._apply_verdicts([item], {"a": self._verdict("uncertain")}, visual=True)
        self.assertEqual(applied, 0)
        self.assertEqual(item.match_type, "scoring_sequence")
        self.assertFalse(item.visual_checked)


class VisualTargetsTests(unittest.TestCase):
    def test_text_positive_candidates_come_first_best_first_then_undecided_ones(self):
        items = [
            _item("mention", match_type="mention_only", rerank=3.9),
            _item("celebration", match_type="celebration_only", rerank=3.0),
            _item("scoring-low", match_type="scoring_sequence", rerank=1.0),
            _item("unjudged", match_type=None, rerank=3.5),
            _item("scoring-high", match_type="scoring_sequence", rerank=2.0),
            _item("uncertain", match_type="uncertain", rerank=2.5),
            _item("attempt", match_type="attempt_only", rerank=3.8),
        ]
        targets = search_router._visual_targets(items, limit=10, requires_entity=False)
        # Mentions and attempts are never worth frames; undecided ones follow the positives.
        self.assertEqual(
            [it.id for it in targets],
            ["scoring-high", "scoring-low", "celebration", "unjudged", "uncertain"],
        )

    def test_spare_slots_go_to_undecided_candidates_but_never_displace_a_positive(self):
        items = [
            _item("uncertain-a", match_type="uncertain", rerank=3.9),
            _item("scoring", match_type="scoring_sequence", rerank=0.5),
            _item("uncertain-b", match_type="uncertain", rerank=2.0),
        ]
        self.assertEqual([it.id for it in search_router._visual_targets(items, 1, requires_entity=False)], ["scoring"])
        self.assertEqual(
            [it.id for it in search_router._visual_targets(items, 2, requires_entity=False)],
            ["scoring", "uncertain-a"],
        )

    def test_limit_bounds_the_selection_and_zero_selects_nothing(self):
        items = [_item(f"s{i}", match_type="scoring_sequence", rerank=float(i)) for i in range(5)]
        self.assertEqual(len(search_router._visual_targets(items, limit=2, requires_entity=False)), 2)
        self.assertEqual(search_router._visual_targets(items, limit=0, requires_entity=False), [])

    def test_entity_mismatches_are_skipped_when_the_query_requires_an_entity(self):
        items = [
            _item("wrong", match_type="scoring_sequence", entity_match=False),
            _item("wrong-undecided", match_type="uncertain", entity_match=False),
            _item("right", match_type="scoring_sequence", entity_match=True),
        ]
        self.assertEqual([it.id for it in search_router._visual_targets(items, 5, requires_entity=True)], ["right"])
        self.assertEqual(len(search_router._visual_targets(items, 5, requires_entity=False)), 3)

    def test_balancing_gives_the_videos_turns_each_in_its_own_priority_order(self):
        def item(id_, video, rerank, match_type="scoring_sequence"):
            result = _item(id_, match_type=match_type, rerank=rerank)
            result.video_id = video
            return result

        items = [item("a1", "A", 3.0), item("a2", "A", 2.9), item("a3", "A", 2.8), item("b1", "B", 1.0),
                 item("a-uncertain", "A", 3.5, "uncertain"), item("b-uncertain", "B", 0.5, "uncertain")]
        plain = [it.id for it in search_router._visual_targets(items, 6, requires_entity=False)]
        balanced = [it.id for it in search_router._visual_targets(items, 6, requires_entity=False, balance_by_video=True)]
        self.assertEqual(plain, ["a1", "a2", "a3", "b1", "a-uncertain", "b-uncertain"])
        # B's undecided candidate takes its turn before A's third positive.
        self.assertEqual(balanced, ["a1", "b1", "a2", "b-uncertain", "a3", "a-uncertain"])


class EffectiveVisualRerankTests(unittest.TestCase):
    def _req(self, **overrides):
        return SearchRequest(query="goal", **overrides)

    def test_request_overrides_take_precedence_over_settings(self):
        with (
            patch.object(settings, "search_visual_rerank_enabled", True),
            patch.object(settings, "search_visual_rerank_max_candidates", 6),
        ):
            enabled, max_candidates = search_router._effective_visual_rerank(
                self._req(visual_rerank_enabled=False, visual_rerank_max_candidates=2),
            )
        self.assertFalse(enabled)
        self.assertEqual(max_candidates, 2)

    def test_unset_request_fields_fall_back_to_settings_defaults(self):
        with (
            patch.object(settings, "search_visual_rerank_enabled", True),
            patch.object(settings, "search_visual_rerank_max_candidates", 6),
        ):
            enabled, max_candidates = search_router._effective_visual_rerank(self._req())
        self.assertTrue(enabled)
        self.assertEqual(max_candidates, 6)

    def test_partial_override_only_replaces_the_set_field(self):
        with (
            patch.object(settings, "search_visual_rerank_enabled", True),
            patch.object(settings, "search_visual_rerank_max_candidates", 6),
        ):
            enabled, max_candidates = search_router._effective_visual_rerank(
                self._req(visual_rerank_max_candidates=10),
            )
        self.assertTrue(enabled)
        self.assertEqual(max_candidates, 10)


class FrameTimestampsTests(unittest.TestCase):
    def test_short_duration_returns_two_points(self):
        points = search_router._frame_timestamps(10.0, 13.0)
        self.assertEqual(len(points), 2)
        self.assertTrue(all(10.0 < p < 13.0 for p in points))
        self.assertLess(points[0], points[1])

    def test_frame_count_scales_up_with_longer_durations(self):
        # Windowed scene cards (20-30s, common with indexing_window_seconds
        # set) must get more than 2 samples or a short decisive moment
        # anywhere in the window can be missed entirely.
        self.assertEqual(len(search_router._frame_timestamps(0.0, 10.0)), 3)
        self.assertEqual(len(search_router._frame_timestamps(0.0, 18.0)), 4)

    def test_frame_count_is_capped_at_max_frames_for_very_long_durations(self):
        points = search_router._frame_timestamps(0.0, 30.0)
        self.assertEqual(len(points), search_router._MAX_CONFIRMATION_FRAMES)
        points60 = search_router._frame_timestamps(0.0, 60.0)
        self.assertEqual(len(points60), search_router._MAX_CONFIRMATION_FRAMES)

    def test_max_frames_override_allows_more_samples_for_long_durations(self):
        points = search_router._frame_timestamps(0.0, 30.0, max_frames=6)
        self.assertEqual(len(points), 6)

    def test_a_chosen_frame_count_is_exact_even_for_a_short_shot(self):
        points = search_router._frame_timestamps(10.0, 13.0, 8, exact=True)
        self.assertEqual(len(points), 8)
        self.assertEqual(points, sorted(points))
        self.assertTrue(all(10.0 < p < 13.0 for p in points))

    def test_points_are_strictly_increasing_and_inside_the_range(self):
        points = search_router._frame_timestamps(5.0, 29.0)
        self.assertEqual(points, sorted(points))
        self.assertTrue(all(5.0 < p < 29.0 for p in points))

    def test_zero_or_negative_duration_returns_a_single_clamped_point(self):
        self.assertEqual(search_router._frame_timestamps(5.0, 5.0), [5.0])
        self.assertEqual(search_router._frame_timestamps(5.0, 2.0), [5.0])
        self.assertEqual(search_router._frame_timestamps(-3.0, -3.0), [0.0])


class GatherConfirmationFramesTests(unittest.TestCase):
    SAS = "https://sas.example/a.mp4?sig=x"

    def _item(self, id_, blob_url="https://acct.blob.core.windows.net/videos/a.mp4", start=0.0, end=5.0):
        return SearchResultItem(
            id=id_, video_id=f"v-{id_}", filename="a.mp4", start_seconds=start, end_seconds=end,
            score=1.0, summary="", transcript_excerpt="", labels=[], thumbnail_url=None,
            blob_url=blob_url, play_url="",
        )

    def _patched(self, window=None, single=None):
        """Patch SAS minting plus both frame extractors (window fast path, per-timestamp fallback)."""
        return (
            patch.object(search_router, "make_sas_url", return_value=self.SAS),
            patch.object(search_router, "extract_window_frames", **({"side_effect": window} if callable(window) else {"return_value": window or []})),
            patch.object(search_router, "extract_frame_bytes", **({"side_effect": single} if callable(single) else {"return_value": single})),
        )

    def test_no_blob_url_yields_no_frames(self):
        sas, window, single = self._patched()
        with sas as sas_mock, window as window_mock, single as single_mock:
            result = search_router._gather_confirmation_frames([self._item("a", blob_url="")])
        self.assertEqual(result, {})
        sas_mock.assert_not_called()
        window_mock.assert_not_called()
        single_mock.assert_not_called()

    def test_sas_minting_failure_is_swallowed_and_skips_that_item(self):
        with patch.object(search_router, "make_sas_url", side_effect=RuntimeError("no creds")):
            result = search_router._gather_confirmation_frames([self._item("a")])
        self.assertEqual(result, {})

    def test_one_ffmpeg_process_per_candidate_is_the_fast_path(self):
        # 30s window -> 4 frames from ONE window call; the per-frame fallback must not run.
        item = self._item("a", start=100.0, end=130.0)
        sas, window, single = self._patched(window=[b"f1", b"f2", b"f3", b"f4"])
        with sas, window as window_mock, single as single_mock:
            result = search_router._gather_confirmation_frames([item], scene_end_by_id={"a": 130.0})
        self.assertEqual(result, {"a": [b"f1", b"f2", b"f3", b"f4"]})
        window_mock.assert_called_once_with(self.SAS, 100.0, 130.0, 4, search_router._FRAME_WIDTH)
        single_mock.assert_not_called()

    def test_a_chosen_frame_count_decodes_the_window_densely_in_one_process(self):
        item = self._item("a", start=100.0, end=120.0)
        sas, window, single = self._patched(window=[b"f"] * 8)
        with sas, window as window_mock, single as single_mock:
            result = search_router._gather_confirmation_frames([item], {"a": 120.0}, frames_per_candidate=8)
        self.assertEqual(len(result["a"]), 8)
        window_mock.assert_called_once_with(self.SAS, 100.0, 120.0, 8, search_router._FRAME_WIDTH, keyframes_only=False)
        single_mock.assert_not_called()

    def test_a_chosen_frame_count_never_decodes_a_very_long_window_densely(self):
        item = self._item("a", start=0.0, end=300.0)
        sas, window, single = self._patched(window=[b"f"] * 8)
        with sas, window as window_mock, single:
            search_router._gather_confirmation_frames([item], {"a": 300.0}, frames_per_candidate=8)
        window_mock.assert_called_once_with(self.SAS, 0.0, 300.0, 8, search_router._FRAME_WIDTH)

    def test_a_failed_dense_decode_falls_back_to_a_bounded_number_of_precise_grabs(self):
        item = self._item("a", start=100.0, end=120.0)
        sas, window, single = self._patched(window=[], single=b"precise")
        with sas, window, single as single_mock:
            result = search_router._gather_confirmation_frames([item], {"a": 120.0}, frames_per_candidate=12)
        self.assertEqual(single_mock.call_count, len(result["a"]))
        self.assertLessEqual(single_mock.call_count, search_router._MAX_CONFIRMATION_FRAMES)

    def test_too_few_window_frames_fall_back_to_precise_per_timestamp_grabs(self):
        item = self._item("a", start=10.0, end=13.0)
        sas, window, single = self._patched(window=[b"only-one"], single=b"precise")
        with sas, window, single as single_mock:
            result = search_router._gather_confirmation_frames([item], scene_end_by_id={"a": 13.0})
        self.assertEqual(result, {"a": [b"precise", b"precise"]})
        sampled = sorted(call.args[1] for call in single_mock.call_args_list)
        self.assertTrue(all(10.0 <= t <= 13.0 for t in sampled), sampled)

    def test_extraction_failures_return_no_entry_for_that_candidate(self):
        sas, window, single = self._patched(window=[], single=None)
        with sas, window, single:
            result = search_router._gather_confirmation_frames([self._item("a")])
        self.assertEqual(result, {})

    def test_an_exception_in_one_candidate_does_not_lose_the_others(self):
        def window(url, start, end, n, width):
            if start == 0.0:
                raise RuntimeError("ffmpeg exploded")
            return [b"ok1", b"ok2"]

        sas, win, single = self._patched(window=window, single=None)
        with sas, win, single:
            result = search_router._gather_confirmation_frames([
                self._item("bad", start=0.0, end=5.0), self._item("good", start=50.0, end=55.0),
            ])
        self.assertEqual(result, {"good": [b"ok1", b"ok2"]})

    def test_successful_frames_are_collected_per_candidate_id(self):
        sas, window, single = self._patched(window=[b"a", b"b"])
        with sas, window, single:
            result = search_router._gather_confirmation_frames([self._item("a"), self._item("b")])
        self.assertEqual(set(result.keys()), {"a", "b"})
        self.assertEqual(result["a"], [b"a", b"b"])

    def test_sas_url_is_minted_once_per_distinct_blob_url(self):
        sas, window, single = self._patched(window=[b"a", b"b"])
        with sas as sas_mock, window, single:
            search_router._gather_confirmation_frames([
                self._item("a", blob_url="https://acct.blob.core.windows.net/videos/shared.mp4"),
                self._item("b", blob_url="https://acct.blob.core.windows.net/videos/shared.mp4"),
            ])
        sas_mock.assert_called_once_with("https://acct.blob.core.windows.net/videos/shared.mp4")

    def test_uses_the_natural_scene_end_not_the_clip_duration_padded_end(self):
        # A 3s shot (start=10, natural end=13) padded for playback out to 60s
        # (end_seconds=70) must still sample frames from within 10..13, not
        # from the padded 10..70 window — otherwise frames land in whatever
        # unrelated footage follows the actual shot.
        item = self._item("a", start=10.0, end=70.0)
        sas, window, single = self._patched(window=[b"a", b"b"])
        with sas, window as window_mock, single:
            search_router._gather_confirmation_frames([item], scene_end_by_id={"a": 13.0})
        _, start, end, _, _ = window_mock.call_args.args
        self.assertEqual((start, end), (10.0, 13.0))

    def test_missing_scene_end_entry_falls_back_to_item_end_seconds(self):
        item = self._item("a", start=0.0, end=5.0)
        sas, window, single = self._patched(window=[b"a", b"b"])
        with sas, window as window_mock, single:
            search_router._gather_confirmation_frames([item], scene_end_by_id={})
        _, start, end, _, _ = window_mock.call_args.args
        self.assertEqual((start, end), (0.0, 5.0))


class RerankActionCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            {"id": "a", "summary": "Player strikes the ball into the net", "transcript_excerpt": "",
             "labels": ["goal"], "entities": ["Sam Rivera"], "ocr_text": ""},
            {"id": "b", "summary": "Post-match interview about today's goals", "transcript_excerpt": "",
             "labels": ["interview"], "entities": [], "ocr_text": ""},
        ]

    def test_empty_query_or_candidates_short_circuits_without_calling_the_model(self):
        with patch.object(openai_helpers, "openai_client") as client:
            self.assertEqual(openai_helpers.rerank_action_candidates("", {}, self.candidates), {})
            self.assertEqual(openai_helpers.rerank_action_candidates("goal", {}, []), {})
            self.assertEqual(openai_helpers.rerank_action_candidates("goal", {}, [{"summary": "no id"}]), {})
            client.assert_not_called()

    def test_valid_response_is_parsed_and_keyed_by_id(self):
        content = json.dumps({"results": [
            {"id": "a", "match_type": "scoring_sequence", "entity_match": True,
             "evidence": "strikes the ball into the net", "reason": "Shows the goal"},
            {"id": "b", "match_type": "mention_only", "entity_match": None,
             "evidence": "", "reason": "Only discusses goals"},
        ]})
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(content)
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("rivera scoring a goal", {"entities": ["Sam Rivera"]}, self.candidates)
        self.assertEqual(result["a"], {
            "match_type": "scoring_sequence", "entity_match": True,
            "evidence": "strikes the ball into the net", "reason": "Shows the goal",
        })
        self.assertEqual(result["b"]["match_type"], "mention_only")
        self.assertIsNone(result["b"]["entity_match"])

    def test_hallucinated_and_duplicate_ids_are_dropped(self):
        content = json.dumps({"results": [
            {"id": "a", "match_type": "scoring_sequence", "entity_match": True, "evidence": "e1", "reason": "r1"},
            {"id": "a", "match_type": "unrelated", "entity_match": False, "evidence": "e2", "reason": "r2"},
            {"id": "does-not-exist", "match_type": "scoring_sequence", "entity_match": True, "evidence": "e3", "reason": "r3"},
        ]})
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(content)
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates)
        self.assertEqual(set(result.keys()), {"a"})
        self.assertEqual(result["a"]["match_type"], "scoring_sequence")  # first occurrence wins

    def test_partial_coverage_is_allowed_missing_ids_simply_absent(self):
        content = json.dumps({"results": [
            {"id": "a", "match_type": "scoring_sequence", "entity_match": True, "evidence": "", "reason": ""},
        ]})
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(content)
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates)
        self.assertEqual(set(result.keys()), {"a"})

    def test_invalid_match_type_is_coerced_to_uncertain_and_bad_entity_match_to_none(self):
        content = json.dumps({"results": [
            {"id": "a", "match_type": "TOTALLY_MADE_UP", "entity_match": "yes", "evidence": "", "reason": ""},
        ]})
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(content)
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates)
        self.assertEqual(result["a"]["match_type"], "uncertain")
        self.assertIsNone(result["a"]["entity_match"])

    def test_presentation_is_kept_only_for_candidates_judged_from_frames(self):
        content = json.dumps({"results": [
            {"id": "a", "match_type": "scoring_sequence", "entity_match": None, "evidence": "", "reason": "",
             "presentation": "Replay"},
            {"id": "b", "match_type": "mention_only", "entity_match": None, "evidence": "", "reason": "",
             "presentation": "live"},
        ]})
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(content)
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates, frames={"a": [b"frame"]})
        self.assertEqual(result["a"]["presentation"], "replay")
        self.assertNotIn("presentation", result["b"])

    def test_malformed_json_fails_open_to_empty_dict(self):
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response("not valid json")
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates)
        self.assertEqual(result, {})

    def test_model_call_exception_fails_open_to_empty_dict(self):
        fake_client = Mock()
        fake_client.chat.completions.create.side_effect = RuntimeError("transient failure")
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates)
        self.assertEqual(result, {})

    def test_long_fields_are_truncated_before_being_sent_to_the_model(self):
        long_candidates = [{
            "id": "a", "summary": "s" * 1000, "transcript_excerpt": "t" * 1000,
            "labels": [f"label{i}" for i in range(50)], "entities": [f"entity{i}" for i in range(50)],
            "ocr_text": "o" * 1000,
        }]
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(json.dumps({"results": []}))
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            openai_helpers.rerank_action_candidates("goal", {}, long_candidates)
        sent = json.loads(fake_client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        sent_candidate = sent["candidates"][0]
        self.assertEqual(len(sent_candidate["summary"]), openai_helpers._RERANK_SUMMARY_CHARS)
        self.assertEqual(len(sent_candidate["transcript_excerpt"]), openai_helpers._RERANK_TRANSCRIPT_CHARS)
        self.assertEqual(len(sent_candidate["ocr_text"]), openai_helpers._RERANK_OCR_CHARS)
        self.assertEqual(len(sent_candidate["labels"]), 10)
        self.assertEqual(len(sent_candidate["entities"]), 10)

    def test_no_frames_sends_the_plain_json_string_payload_unchanged(self):
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(json.dumps({"results": []}))
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            openai_helpers.rerank_action_candidates("goal", {}, self.candidates, frames={})
        content = fake_client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIsInstance(content, str)

    def _calls_by_kind(self, fake_client):
        """Split recorded chat calls into (frame calls, text calls) by message shape."""
        frame_calls, text_calls = [], []
        for call in fake_client.chat.completions.create.call_args_list:
            content = call.kwargs["messages"][1]["content"]
            (frame_calls if isinstance(content, list) else text_calls).append(call)
        return frame_calls, text_calls

    def test_a_candidate_with_frames_gets_its_own_call_with_images_after_its_text(self):
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(json.dumps({"results": []}))
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            openai_helpers.rerank_action_candidates(
                "goal", {}, self.candidates, frames={"a": [b"frame1", b"frame2"]},
            )
        frame_calls, text_calls = self._calls_by_kind(fake_client)
        self.assertEqual(len(frame_calls), 1)
        self.assertEqual(len(text_calls), 1)
        # Frame call: preamble, candidate "a" text, then its 2 images. Candidate "b" is NOT in it.
        content = frame_calls[0].kwargs["messages"][1]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "text", "image_url", "image_url"])
        self.assertIn('"a"', content[1]["text"])
        self.assertNotIn('"b"', content[1]["text"])
        self.assertTrue(all(
            part["image_url"]["url"].startswith("data:image/jpeg;base64,")
            for part in content if part["type"] == "image_url"
        ))
        # Text call: only the frame-less candidate "b".
        self.assertEqual([c["id"] for c in json.loads(text_calls[0].kwargs["messages"][1]["content"])["candidates"]], ["b"])

    def _run_with_judge_settings(self, *, frames=None, candidates=None, **judge_settings):
        """Run one rerank with the given `azure_openai_judge_*` settings; returns (fake_client, judge_client_fake)."""
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(json.dumps({"results": []}))
        judge_fake = Mock()
        judge_fake.chat.completions.create.return_value = _fake_chat_response(json.dumps({"results": []}))
        values = {
            "azure_openai_chat_deployment": "chat-model",
            "azure_openai_judge_endpoint": "",
            "azure_openai_judge_deployment": "",
            "azure_openai_judge_api_version": "",
            "azure_openai_judge_reasoning_effort": "",
        }
        values.update(judge_settings)
        patches = [patch.object(settings, name, value) for name, value in values.items()]
        patches += [
            patch.object(openai_helpers, "openai_client", return_value=fake_client),
            patch.object(openai_helpers, "judge_client", return_value=judge_fake),
        ]
        for p in patches:
            p.start()
        try:
            openai_helpers.rerank_action_candidates("goal", {}, candidates or self.candidates, frames=frames or {})
        finally:
            for p in patches:
                p.stop()
        return fake_client, judge_fake

    def test_without_judge_settings_both_stages_use_the_chat_deployment_with_low_detail_and_classic_params(self):
        fake_client, judge_fake = self._run_with_judge_settings(frames={"a": [b"frame1"]})
        judge_fake.chat.completions.create.assert_not_called()
        frame_calls, text_calls = self._calls_by_kind(fake_client)
        self.assertEqual(frame_calls[0].kwargs["model"], "chat-model")
        self.assertEqual(text_calls[0].kwargs["model"], "chat-model")
        image = [p for p in frame_calls[0].kwargs["messages"][1]["content"] if p["type"] == "image_url"][0]
        # gpt-4o bills ~255 tokens for a 640px "high" frame; gpt-4o-mini bills ~8.5k, so it must use "low".
        self.assertEqual(image["image_url"]["detail"], "low")
        for call in (frame_calls[0], text_calls[0]):
            self.assertEqual(call.kwargs["temperature"], 0.0)
            self.assertIn("max_tokens", call.kwargs)
            self.assertNotIn("max_completion_tokens", call.kwargs)
            self.assertNotIn("reasoning_effort", call.kwargs)

    def test_a_dedicated_judge_deployment_judges_text_and_frames_with_high_detail_images(self):
        fake_client, judge_fake = self._run_with_judge_settings(
            frames={"a": [b"frame1"]}, azure_openai_judge_deployment="judge-model",
        )
        frame_calls, text_calls = self._calls_by_kind(fake_client)
        self.assertEqual(frame_calls[0].kwargs["model"], "judge-model")
        self.assertEqual(text_calls[0].kwargs["model"], "judge-model")
        image = [p for p in frame_calls[0].kwargs["messages"][1]["content"] if p["type"] == "image_url"][0]
        self.assertEqual(image["image_url"]["detail"], "high")
        judge_fake.chat.completions.create.assert_not_called()  # same endpoint: the primary client is used

    def test_a_judge_endpoint_override_uses_the_judge_client_and_not_the_primary_one(self):
        fake_client, judge_fake = self._run_with_judge_settings(
            frames={"a": [b"frame1"]},
            azure_openai_judge_endpoint="https://foundry.example/", azure_openai_judge_deployment="judge-model",
        )
        fake_client.chat.completions.create.assert_not_called()
        models = {c.kwargs["model"] for c in judge_fake.chat.completions.create.call_args_list}
        self.assertEqual(models, {"judge-model"})
        self.assertEqual(judge_fake.chat.completions.create.call_count, 2)  # one frame call + one text call

    def test_a_judge_api_version_override_alone_also_selects_the_judge_client(self):
        fake_client, judge_fake = self._run_with_judge_settings(azure_openai_judge_api_version="2025-04-01-preview")
        fake_client.chat.completions.create.assert_not_called()
        judge_fake.chat.completions.create.assert_called_once()

    def test_reasoning_effort_switches_to_reasoning_compatible_parameters(self):
        # A reasoning-style judge rejects `max_tokens` and any non-default `temperature` with HTTP 400.
        _, judge_fake = self._run_with_judge_settings(
            frames={"a": [b"frame1"]},
            azure_openai_judge_endpoint="https://foundry.example/", azure_openai_judge_deployment="judge-model",
            azure_openai_judge_reasoning_effort=" Low ",
        )
        calls = judge_fake.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call.kwargs["reasoning_effort"], "low")
            self.assertNotIn("temperature", call.kwargs)
            self.assertNotIn("max_tokens", call.kwargs)
            # Hidden reasoning tokens count against the cap, so it must exceed the visible-answer budget.
            self.assertGreater(call.kwargs["max_completion_tokens"], 150 + 200 + 1000)

    def test_reasoning_effort_is_ignored_when_the_judge_falls_back_to_the_chat_model(self):
        # A reasoning effort configured without a dedicated judge must not be sent to gpt-4o-mini.
        fake_client, _ = self._run_with_judge_settings(azure_openai_judge_reasoning_effort="low")
        for call in fake_client.chat.completions.create.call_args_list:
            self.assertNotIn("reasoning_effort", call.kwargs)
            self.assertEqual(call.kwargs["temperature"], 0.0)

    def test_an_answer_cut_off_by_the_token_cap_is_logged_and_fails_open(self):
        truncated = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""), finish_reason="length")], usage=None,
        )
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = truncated
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            with self.assertLogs(openai_helpers.log, level="WARNING") as logs:
                result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates[:1])
        self.assertEqual(result, {})
        self.assertTrue(any("truncated" in line for line in logs.output))


    def test_text_only_candidates_are_judged_in_small_batches_and_merged(self):
        batch = openai_helpers._RERANK_BATCH_SIZE
        candidates = [{"id": f"c{i}", "summary": "s"} for i in range(batch * 2 + 3)]

        def fake_create(**kwargs):
            sent = json.loads(kwargs["messages"][1]["content"])["candidates"]
            self.assertLessEqual(len(sent), batch)
            return _fake_chat_response(json.dumps({"results": [
                {"id": c["id"], "match_type": "mention_only", "entity_match": None, "evidence": "", "reason": ""}
                for c in sent
            ]}))

        fake_client = Mock()
        fake_client.chat.completions.create.side_effect = fake_create
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, candidates)
        self.assertEqual(fake_client.chat.completions.create.call_count, 3)
        self.assertEqual(set(result), {c["id"] for c in candidates})

    def test_one_failed_batch_does_not_discard_verdicts_from_the_others(self):
        batch = openai_helpers._RERANK_BATCH_SIZE
        candidates = [{"id": f"c{i}", "summary": "s"} for i in range(batch + 2)]

        def fake_create(**kwargs):
            sent = json.loads(kwargs["messages"][1]["content"])["candidates"]
            if sent[0]["id"] == "c0":
                raise RuntimeError("transient failure")
            return _fake_chat_response(json.dumps({"results": [
                {"id": c["id"], "match_type": "scoring_sequence", "entity_match": None, "evidence": "", "reason": ""}
                for c in sent
            ]}))

        fake_client = Mock()
        fake_client.chat.completions.create.side_effect = fake_create
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, candidates)
        self.assertEqual(set(result), {f"c{batch}", f"c{batch + 1}"})

    def test_ids_a_call_was_not_given_are_dropped_even_if_valid_elsewhere(self):
        # The frame call for "a" must not be able to emit a verdict for "b".
        content = json.dumps({"results": [
            {"id": "a", "match_type": "scoring_sequence", "entity_match": None, "evidence": "net", "reason": "frames"},
            {"id": "b", "match_type": "scoring_sequence", "entity_match": None, "evidence": "net", "reason": "leak"},
        ]})
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(content)
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates[:1], frames={"a": [b"f"]})
        self.assertEqual(set(result), {"a"})

    def test_candidates_without_frames_are_still_judged_when_others_have_frames(self):
        def fake_create(**kwargs):
            content = kwargs["messages"][1]["content"]
            if isinstance(content, list):
                results = [{"id": "a", "match_type": "scoring_sequence", "entity_match": None,
                            "evidence": "frame shows the net bulging", "reason": "visual confirms"}]
            else:
                results = [{"id": "b", "match_type": "mention_only", "entity_match": None,
                            "evidence": "", "reason": "text only"}]
            return _fake_chat_response(json.dumps({"results": results}))

        fake_client = Mock()
        fake_client.chat.completions.create.side_effect = fake_create
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates, frames={"a": [b"frame1"]})
        self.assertEqual(result["a"]["match_type"], "scoring_sequence")
        self.assertEqual(result["b"]["match_type"], "mention_only")

    def test_visual_call_still_fails_open_on_model_exception(self):
        fake_client = Mock()
        fake_client.chat.completions.create.side_effect = RuntimeError("transient failure")
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates, frames={"a": [b"frame1"]})
        self.assertEqual(result, {})

    def test_attempt_only_is_a_valid_verdict(self):
        content = json.dumps({"results": [
            {"id": "a", "match_type": "attempt_only", "entity_match": None, "evidence": "saved", "reason": "saved shot"},
        ]})
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(content)
        with patch.object(openai_helpers, "openai_client", return_value=fake_client):
            result = openai_helpers.rerank_action_candidates("goal", {}, self.candidates[:1])
        self.assertEqual(result["a"]["match_type"], "attempt_only")

    def test_system_prompt_defines_every_verdict_and_how_to_weigh_frames(self):
        prompt = openai_helpers.RERANK_SYSTEM_PROMPT
        for match_type in openai_helpers._RERANK_VALID_MATCH_TYPES:
            self.assertIn(f'"{match_type}"', prompt)
        # Sparse frames must not be able to veto a clear text narration, but must be able to
        # override a text claim they clearly contradict.
        self.assertIn("do not prove", prompt)
        self.assertIn("trust the frames", prompt)
        # Excited commentary that only describes a shot ("land it in the far corner" on a miss) must
        # not count as a goal (without this rule such a miss was judged a goal in 37-75% of runs).
        self.assertIn("Excited commentary is not proof", prompt)


def _hit(id_, *, score, rerank_score, summary="", transcript_excerpt="", labels=None, entities=None, ocr_text=""):
    # Distinct video_id per hit so `_drop_overlapping` (same-video, overlapping
    # time range) never collapses these into one result — these tests are
    # about cross-scene ranking, not the overlap-dedup behavior.
    return {
        "id": id_, "video_id": f"video-{id_}", "filename": "match.mp4",
        "start_seconds": 0.0, "end_seconds": 5.0,
        "@search.score": score, "@search.reranker_score": rerank_score,
        "summary": summary, "transcript_excerpt": transcript_excerpt,
        "labels": labels or [], "entities": entities or [], "ocr_text": ocr_text,
        "speakers": [], "thumbnail_url": "", "blob_url": f"https://example.test/videos/video-{id_}/match.mp4",
    }


class _FakeSearchResults(list):
    def get_facets(self):
        return {}


class SearchActionRerankIntegrationTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(search_router.router, prefix="/api/search")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        enabled = patch.object(settings, "search_action_rerank_enabled", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        max_candidates = patch.object(settings, "search_action_rerank_max_candidates", 20)
        max_candidates.start()
        self.addCleanup(max_candidates.stop)
        # These tests only care about the text-evidence path (they patch
        # rerank_action_candidates wholesale); disable visual confirmation so
        # no real ffmpeg/storage calls are attempted in this environment.
        visual_off = patch.object(settings, "search_visual_rerank_enabled", False)
        visual_off.start()
        self.addCleanup(visual_off.stop)

    def request(self, query="goals being scored", mode="hybrid", top=5):
        return self.client.post("/api/search", json={
            "query": query, "top": top,
            "clip_duration_seconds": 15, "mode": mode,
        })

    def run_search(self, hits, rewrite, embed_side_effect, verdicts):
        mock_search_client = Mock()
        mock_search_client.search.return_value = _FakeSearchResults(hits)
        with (
            patch.object(search_router, "search_client", return_value=mock_search_client),
            patch.object(search_router, "rewrite_query", return_value=rewrite) as rewrite_mock,
            patch.object(search_router, "embed_texts", side_effect=embed_side_effect) as embed_mock,
            patch.object(search_router, "rerank_action_candidates", return_value=verdicts) as rerank_mock,
        ):
            response = self.request()
        return response, mock_search_client, rewrite_mock, embed_mock, rerank_mock

    def test_scoring_sequence_is_promoted_above_a_higher_scored_mention(self):
        hits = [
            _hit("mention", score=10.0, rerank_score=3.0,
                 summary="Post-match interview discussing today's goal"),
            _hit("goal", score=5.0, rerank_score=1.5,
                 summary="Player strikes the ball into the net, goal confirmed"),
        ]
        rewrite = {"entities": [], "actions": ["goal", "scores"], "expanded_query": "goals being scored", "must_entities": []}
        verdicts = {
            "mention": {"match_type": "mention_only", "entity_match": None, "evidence": "", "reason": "discusses the goal"},
            "goal": {"match_type": "scoring_sequence", "entity_match": None, "evidence": "strikes the ball", "reason": "shows the goal"},
        }
        response, _, _, embed_mock, rerank_mock = self.run_search(
            hits, rewrite, embed_side_effect=lambda texts: [[1.0]] * len(texts), verdicts=verdicts,
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([it["id"] for it in items], ["goal", "mention"])
        self.assertEqual(items[0]["match_type"], "scoring_sequence")
        self.assertEqual(items[1]["match_type"], "mention_only")
        # Action rerank only runs in hybrid mode and is given the raw search query + rewrite.
        rerank_mock.assert_called_once()
        self.assertEqual(rerank_mock.call_args.args[0], "goals being scored")
        # No entities named -> embed_texts is called with ONLY the content text (entity_vector skipped).
        embed_mock.assert_called_once_with(["goals being scored"])

    def test_action_rerank_failure_falls_back_to_prior_ranking_unchanged(self):
        hits = [
            _hit("first", score=10.0, rerank_score=3.0, summary="Player strikes the ball into the net"),
            _hit("second", score=5.0, rerank_score=1.5, summary="Post-match interview"),
        ]
        rewrite = {"entities": [], "actions": [], "expanded_query": "goals being scored", "must_entities": []}
        response, _, _, _, rerank_mock = self.run_search(
            hits, rewrite, embed_side_effect=lambda texts: [[1.0]] * len(texts), verdicts={},
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([it["id"] for it in items], ["first", "second"])
        self.assertIsNone(items[0]["match_type"])
        self.assertIsNone(items[1]["match_type"])
        rerank_mock.assert_called_once()

    def test_disabling_the_feature_flag_skips_the_reranker_entirely(self):
        hits = [
            _hit("first", score=10.0, rerank_score=3.0, summary="Player strikes the ball into the net"),
            _hit("second", score=5.0, rerank_score=1.5, summary="Post-match interview"),
        ]
        rewrite = {"entities": [], "actions": [], "expanded_query": "goals being scored", "must_entities": []}
        with patch.object(settings, "search_action_rerank_enabled", False):
            response, _, _, _, rerank_mock = self.run_search(
                hits, rewrite, embed_side_effect=lambda texts: [[1.0]] * len(texts), verdicts={},
            )
        self.assertEqual(response.status_code, 200)
        rerank_mock.assert_not_called()
        items = response.json()["items"]
        self.assertEqual([it["id"] for it in items], ["first", "second"])

    def test_candidate_slice_is_bounded_by_max_candidates_setting(self):
        hits = [_hit(f"item-{i}", score=float(10 - i), rerank_score=float(3 - i * 0.1)) for i in range(5)]
        rewrite = {"entities": [], "actions": [], "expanded_query": "goals being scored", "must_entities": []}
        with patch.object(settings, "search_action_rerank_max_candidates", 2):
            response, _, _, _, rerank_mock = self.run_search(
                hits, rewrite, embed_side_effect=lambda texts: [[1.0]] * len(texts), verdicts={},
            )
        self.assertEqual(response.status_code, 200)
        sent_candidates = rerank_mock.call_args.args[2]
        self.assertEqual([c["id"] for c in sent_candidates], ["item-0", "item-1"])

    def test_entity_mismatch_demotes_below_a_lower_scored_entity_match(self):
        hits = [
            _hit("wrong-player", score=3.0, rerank_score=3.0, summary="A different player scores a goal"),
            _hit("rivera", score=1.0, rerank_score=1.0, summary="Sam Rivera scores a goal"),
        ]
        rewrite = {
            "entities": ["Sam Rivera"], "actions": ["goal"],
            "expanded_query": "Sam Rivera scoring a goal", "must_entities": ["Sam Rivera"],
        }
        verdicts = {
            "wrong-player": {"match_type": "scoring_sequence", "entity_match": False, "evidence": "", "reason": ""},
            "rivera": {"match_type": "scoring_sequence", "entity_match": True, "evidence": "", "reason": ""},
        }
        response, _, _, embed_mock, _ = self.run_search(
            hits, rewrite, embed_side_effect=lambda texts: [[1.0], [2.0]], verdicts=verdicts,
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([it["id"] for it in items], ["rivera", "wrong-player"])
        # Entities named -> embed_texts is called with BOTH content text and entity text.
        embed_mock.assert_called_once_with(["Sam Rivera scoring a goal", "Sam Rivera"])

    def test_separate_vectors_are_sent_to_azure_search_for_content_and_entities(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        rewrite = {
            "entities": ["Sam Rivera"], "actions": ["goal"],
            "expanded_query": "Sam Rivera scoring a goal", "must_entities": [],
        }
        _, mock_search_client, _, _, _ = self.run_search(
            hits, rewrite, embed_side_effect=lambda texts: [[1.0], [2.0]], verdicts={},
        )
        sent_vector_queries = mock_search_client.search.call_args.kwargs["vector_queries"]
        self.assertEqual(len(sent_vector_queries), 2)
        self.assertEqual(sent_vector_queries[0].fields, "content_vector")
        self.assertEqual(sent_vector_queries[0].vector, [1.0])
        self.assertEqual(sent_vector_queries[1].fields, "entity_vector")
        self.assertEqual(sent_vector_queries[1].vector, [2.0])

    def test_no_entities_sends_only_the_content_vector_query(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        rewrite = {"entities": [], "actions": ["goal"], "expanded_query": "goals being scored", "must_entities": []}
        _, mock_search_client, _, _, _ = self.run_search(
            hits, rewrite, embed_side_effect=lambda texts: [[1.0]], verdicts={},
        )
        sent_vector_queries = mock_search_client.search.call_args.kwargs["vector_queries"]
        self.assertEqual(len(sent_vector_queries), 1)
        self.assertEqual(sent_vector_queries[0].fields, "content_vector")


class SearchVisualRerankIntegrationTests(unittest.TestCase):
    """End-to-end wiring of the two-stage (text, then frames) rerank inside search().

    `_gather_confirmation_frames` and `rerank_action_candidates` are covered directly by their own
    test classes; these tests verify search() sends the RIGHT candidates to the frame stage, applies
    the frame verdicts correctly and degrades gracefully when any piece fails.
    """

    def setUp(self):
        app = FastAPI()
        app.include_router(search_router.router, prefix="/api/search")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        for name, value in (
            ("search_action_rerank_enabled", True),
            ("search_action_rerank_max_candidates", 40),
            ("search_visual_rerank_enabled", True),
            ("search_visual_rerank_max_candidates", 2),
        ):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)

    def request(self, **overrides):
        return self.client.post("/api/search", json={
            "query": "goals being scored", "top": 5,
            "clip_duration_seconds": 15, "mode": "hybrid", **overrides,
        })

    @staticmethod
    def verdict(match_type, reason="r"):
        return {"match_type": match_type, "entity_match": None, "evidence": "", "reason": reason}

    def run_search(self, hits, text_verdicts, visual_verdicts=None, frames_return=None, **request_overrides):
        """Stage 1 (no `frames` kwarg) returns `text_verdicts`; stage 2 (with frames) `visual_verdicts`."""
        rewrite = {"entities": [], "actions": ["goal"], "expanded_query": "goals being scored", "must_entities": []}
        mock_search_client = Mock()
        mock_search_client.search.return_value = _FakeSearchResults(hits)

        def fake_rerank(query, rewrite_, candidates, frames=None):
            return (visual_verdicts or {}) if frames else text_verdicts

        if frames_return is None:
            frames_return = lambda items, scene_ends: {it.id: [b"frame"] for it in items}  # noqa: E731
        with (
            patch.object(search_router, "search_client", return_value=mock_search_client),
            patch.object(search_router, "rewrite_query", return_value=rewrite),
            patch.object(search_router, "embed_texts", side_effect=lambda texts: [[1.0]] * len(texts)),
            patch.object(search_router, "_gather_confirmation_frames", side_effect=frames_return) as gather_mock,
            patch.object(search_router, "rerank_action_candidates", side_effect=fake_rerank) as rerank_mock,
        ):
            response = self.request(**request_overrides)
        return response, gather_mock, rerank_mock

    def test_only_text_positive_candidates_are_sent_to_the_frame_stage_best_first_and_bounded(self):
        hits = [
            _hit("mention", score=9.0, rerank_score=3.9),
            _hit("score-low", score=2.0, rerank_score=1.2),
            _hit("score-high", score=3.0, rerank_score=3.0),
            _hit("celebration", score=4.0, rerank_score=2.0),
        ]
        text = {
            "mention": self.verdict("mention_only"),
            "score-low": self.verdict("scoring_sequence"),
            "score-high": self.verdict("scoring_sequence"),
            "celebration": self.verdict("celebration_only"),
        }
        response, gather_mock, _ = self.run_search(hits, text)
        self.assertEqual(response.status_code, 200)
        gather_mock.assert_called_once()
        # visual max is 2: the two best text-positives, never the (higher scored) mention.
        self.assertEqual([it.id for it in gather_mock.call_args.args[0]], ["score-high", "score-low"])

    def test_frames_are_not_gathered_when_no_candidate_is_text_positive(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0), _hit("b", score=1.0, rerank_score=1.0)]
        text = {"a": self.verdict("mention_only"), "b": self.verdict("attempt_only")}
        response, gather_mock, rerank_mock = self.run_search(hits, text)
        self.assertEqual(response.status_code, 200)
        gather_mock.assert_not_called()
        self.assertEqual(rerank_mock.call_count, 1)  # text stage only

    def test_a_text_claimed_goal_that_the_frames_show_to_be_an_attempt_is_demoted_below_a_confirmed_goal(self):
        # The reported failure: an attempt ranked first because its text claimed a goal.
        hits = [
            _hit("attempt", score=9.0, rerank_score=3.5, summary="Scene depicting a goal being scored"),
            _hit("goal", score=1.0, rerank_score=1.2, summary="Header into the empty net"),
            _hit("mention", score=5.0, rerank_score=3.0),
        ]
        text = {
            "attempt": self.verdict("scoring_sequence"),
            "goal": self.verdict("scoring_sequence"),
            "mention": self.verdict("mention_only"),
        }
        visual = {
            "attempt": self.verdict("attempt_only", "shot saved by the keeper"),
            "goal": self.verdict("scoring_sequence", "ball in the net"),
        }
        response, gather_mock, _ = self.run_search(hits, text, visual)
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([it["id"] for it in items], ["goal", "attempt", "mention"])
        self.assertEqual(items[0]["match_type"], "scoring_sequence")
        self.assertTrue(items[0]["visual_checked"])
        self.assertEqual(items[1]["match_type"], "attempt_only")
        self.assertEqual(items[1]["match_reason"], "shot saved by the keeper")
        self.assertTrue(items[1]["visual_checked"])
        self.assertFalse(items[2]["visual_checked"])

    def test_an_inconclusive_visual_verdict_keeps_the_text_verdict_and_does_not_mark_it_checked(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        response, _, _ = self.run_search(
            hits, {"a": self.verdict("scoring_sequence")}, {"a": self.verdict("uncertain")},
        )
        item = response.json()["items"][0]
        self.assertEqual(item["match_type"], "scoring_sequence")
        self.assertFalse(item["visual_checked"])

    def test_candidates_the_judge_skipped_rank_above_ones_judged_mention_only(self):
        # Regression: 5-7 of 20 verdicts were dropped in production and the dropped candidates
        # (including the real goal windows) fell below every mention_only result.
        hits = [
            _hit("mention", score=9.0, rerank_score=3.9),
            _hit("skipped", score=1.0, rerank_score=1.1),
        ]
        response, gather_mock, _ = self.run_search(hits, {"mention": self.verdict("mention_only")})
        self.assertEqual([it["id"] for it in response.json()["items"]], ["skipped", "mention"])
        self.assertIsNone(response.json()["items"][0]["match_type"])
        # Unknown is not a negative: the skipped candidate (and only it) gets its frames checked.
        self.assertEqual([it.id for it in gather_mock.call_args.args[0]], ["skipped"])

    def test_an_undecided_candidate_is_frame_checked_and_a_goal_in_its_frames_is_promoted(self):
        # Garbled commentary leaves a real goal "uncertain" from text alone; the spare frame slot
        # lets its frames settle it (the real 69:35 penalty window had an unusable transcript).
        hits = [
            _hit("garbled", score=1.0, rerank_score=1.0, summary="Players on the pitch"),
            _hit("mention", score=9.0, rerank_score=3.9),
        ]
        text = {"garbled": self.verdict("uncertain"), "mention": self.verdict("mention_only")}
        visual = {"garbled": self.verdict("scoring_sequence", "ball in the net")}
        response, gather_mock, _ = self.run_search(hits, text, visual)
        items = response.json()["items"]
        self.assertEqual([it["id"] for it in items], ["garbled", "mention"])
        self.assertEqual(items[0]["match_type"], "scoring_sequence")
        self.assertTrue(items[0]["visual_checked"])
        self.assertEqual([it.id for it in gather_mock.call_args.args[0]], ["garbled"])

    def test_results_beyond_the_judged_pool_do_not_leapfrog_judged_candidates(self):
        # Pool of 2 judged: "skipped" (no verdict) and "mention" (judged). "tail" was never submitted
        # and is semantically weaker, so it must not be pulled above the judged mention just
        # because it carries no verdict.
        hits = [
            _hit("mention", score=9.0, rerank_score=3.0),
            _hit("skipped", score=1.0, rerank_score=1.2),
            _hit("tail", score=5.0, rerank_score=1.1),
        ]
        with patch.object(settings, "search_action_rerank_max_candidates", 2):
            response, _, rerank_mock = self.run_search(hits, {"mention": self.verdict("mention_only")})
        self.assertEqual([it["id"] for it in response.json()["items"]], ["skipped", "mention", "tail"])
        # The first judge call is the text stage over the pool (the second one, for frames, only has "skipped").
        self.assertEqual([c["id"] for c in rerank_mock.call_args_list[0].args[2]], ["mention", "skipped"])

    def test_when_no_frames_could_be_extracted_the_text_verdicts_stand(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        response, gather_mock, rerank_mock = self.run_search(
            hits, {"a": self.verdict("scoring_sequence")}, frames_return=lambda items, scene_ends: {},
        )
        self.assertEqual(response.status_code, 200)
        gather_mock.assert_called_once()
        self.assertEqual(rerank_mock.call_count, 1)  # no stage 2 without frames
        item = response.json()["items"][0]
        self.assertEqual(item["match_type"], "scoring_sequence")
        self.assertFalse(item["visual_checked"])

    def test_only_candidates_that_actually_got_frames_are_sent_to_the_vision_judge(self):
        hits = [_hit("a", score=2.0, rerank_score=2.0), _hit("b", score=1.0, rerank_score=1.0)]
        text = {"a": self.verdict("scoring_sequence"), "b": self.verdict("scoring_sequence")}
        response, _, rerank_mock = self.run_search(
            hits, text, {"a": self.verdict("scoring_sequence")}, frames_return=lambda items, ends: {"a": [b"f"]},
        )
        self.assertEqual(response.status_code, 200)
        visual_call = rerank_mock.call_args_list[-1]
        self.assertEqual([c["id"] for c in visual_call.args[2]], ["a"])
        self.assertEqual(visual_call.kwargs["frames"], {"a": [b"f"]})

    def test_scene_end_by_id_reflects_the_natural_shot_end_not_the_padded_clip_window(self):
        # Hit's natural end_seconds=5.0, but clip_duration_seconds=15 in
        # self.request() pads the item's playback end_seconds to 15.0 —
        # frame gathering must still be told the natural 5.0.
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        response, gather_mock, _ = self.run_search(hits, {"a": self.verdict("scoring_sequence")})
        self.assertEqual(response.status_code, 200)
        gathered_items, scene_end_by_id = gather_mock.call_args.args
        self.assertEqual(gathered_items[0].end_seconds, 15.0)  # padded playback window
        self.assertEqual(scene_end_by_id["a"], 5.0)  # natural shot end used for frame sampling

    def test_frame_gathering_exception_fails_open_and_text_verdicts_stand(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        response, _, _ = self.run_search(
            hits, {"a": self.verdict("scoring_sequence")}, frames_return=RuntimeError("boom"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["match_type"], "scoring_sequence")

    def test_disabling_visual_rerank_skips_frame_gathering_entirely(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        with patch.object(settings, "search_visual_rerank_enabled", False):
            response, gather_mock, rerank_mock = self.run_search(hits, {"a": self.verdict("scoring_sequence")})
        self.assertEqual(response.status_code, 200)
        gather_mock.assert_not_called()
        self.assertEqual(rerank_mock.call_count, 1)

    def test_request_level_override_disables_visual_rerank_even_though_settings_enable_it(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        response, gather_mock, _ = self.run_search(
            hits, {"a": self.verdict("scoring_sequence")}, visual_rerank_enabled=False,
        )
        self.assertEqual(response.status_code, 200)
        gather_mock.assert_not_called()

    def test_request_level_override_widens_the_candidate_count_beyond_the_settings_default(self):
        hits = [_hit(f"item-{i}", score=float(10 - i), rerank_score=float(3 - i * 0.1)) for i in range(5)]
        text = {f"item-{i}": self.verdict("scoring_sequence") for i in range(5)}
        response, gather_mock, _ = self.run_search(hits, text, visual_rerank_max_candidates=4)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(gather_mock.call_args.args[0]), 4)  # settings default is 2

    def test_visual_stage_disabled_when_the_action_rerank_itself_is_disabled(self):
        hits = [_hit("a", score=1.0, rerank_score=1.0)]
        with patch.object(settings, "search_action_rerank_enabled", False):
            response, gather_mock, rerank_mock = self.run_search(hits, {"a": self.verdict("scoring_sequence")})
        self.assertEqual(response.status_code, 200)
        gather_mock.assert_not_called()
        rerank_mock.assert_not_called()


class ChatDeploymentParameterTests(unittest.TestCase):
    """The `chat` deployment takes classic parameters, or reasoning parameters when an effort is configured."""

    def create_kwargs(self, effort):
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = _fake_chat_response(json.dumps({"summary": "s", "expanded_query": "q"}))
        with (
            patch.object(settings, "azure_openai_chat_reasoning_effort", effort),
            patch.object(openai_helpers, "openai_client", return_value=fake_client),
        ):
            openai_helpers.rewrite_query("goals")
            openai_helpers.summarize_scene("transcript", [], [])
            openai_helpers.enrich_scene("transcript", [], [], "")
        return [call.kwargs for call in fake_client.chat.completions.create.call_args_list]

    def test_classic_models_get_temperature_and_max_tokens(self):
        calls = self.create_kwargs("")
        self.assertEqual(len(calls), 3)
        for kwargs in calls:
            self.assertIn("temperature", kwargs)
            self.assertIn("max_tokens", kwargs)
            self.assertNotIn("reasoning_effort", kwargs)
            self.assertNotIn("max_completion_tokens", kwargs)

    def test_reasoning_models_get_max_completion_tokens_with_headroom_and_no_temperature(self):
        calls = self.create_kwargs(" Low ")
        self.assertEqual(len(calls), 3)
        for kwargs in calls:
            self.assertEqual(kwargs["reasoning_effort"], "low")
            self.assertNotIn("temperature", kwargs)
            self.assertNotIn("max_tokens", kwargs)
        # The answer budget (250 for the query rewrite) is raised by the headroom hidden reasoning needs.
        self.assertEqual(calls[0]["max_completion_tokens"], 250 + openai_helpers._CHAT_REASONING_HEADROOM)


if __name__ == "__main__":
    unittest.main()
