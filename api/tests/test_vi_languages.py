import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import ProcessJob
from app.pipelines import video_indexer
from app.routers import indexing

SOURCE_LANGUAGES = [
    {"languageCode": "en-US", "name": "English United States", "isSourceLanguage": True},
    {"languageCode": "da-DK", "name": "Danish (Denmark)", "isSourceLanguage": True},
    {"languageCode": "af-ZA", "name": "Afrikaans", "isSourceLanguage": False},
]
PROCESS_REQUEST = {
    "video_id": "test-video",
    "filename": "match.mp4",
    "blob_url": "https://teststorage.blob.core.windows.net/videos/match.mp4",
}


class VideoIndexerLanguageTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("azure_storage_account", "teststorage"), ("azure_storage_videos_container", "videos")):
            setting = patch.object(indexing.settings, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        video_indexer.get_source_languages.cache_clear()
        self.addCleanup(video_indexer.get_source_languages.cache_clear)
        app = FastAPI()
        app.include_router(indexing.router, prefix="/api/indexing")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.catalog_response = httpx.Response(
            200, json=SOURCE_LANGUAGES,
            request=httpx.Request("GET", "https://example.test/SupportedLanguages"),
        )

    def test_catalog_filters_translation_only_languages_and_caches(self):
        with patch.object(video_indexer.httpx, "get", return_value=self.catalog_response) as fetch:
            response = self.client.get("/api/indexing/languages")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), [
                {"code": "da-DK", "name": "Danish (Denmark)"},
                {"code": "en-US", "name": "English United States"},
            ])
            self.assertEqual(self.client.get("/api/indexing/languages").json(), response.json())
            fetch.assert_called_once_with(
                f"{video_indexer.VI_API_BASE}/{video_indexer.settings.vi_location}/SupportedLanguages",
                timeout=10,
            )

    def test_explicit_source_language_is_canonicalized_and_forwarded(self):
        def run_target(**kwargs):
            kwargs["target"](Mock(), Mock(), Mock())
            return ProcessJob(video_id="test-video", status="submitted")

        with (
            patch.object(video_indexer.httpx, "get", return_value=self.catalog_response),
            patch.object(indexing, "ensure_indexes"),
            patch.object(indexing.jobs, "run_async", side_effect=run_target),
            patch.object(indexing, "process_video") as process,
        ):
            response = self.client.post(
                "/api/indexing/process", json={**PROCESS_REQUEST, "source_language": " DA-dk "},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(process.call_args.kwargs["source_language"], "da-DK")

    def test_omitted_blank_and_auto_values_do_not_require_catalog(self):
        for extra in ({}, {"source_language": None}, {"source_language": ""},
                      {"source_language": " "}, {"source_language": "auto"},
                      {"source_language": " AUTO "}):
            with (
                self.subTest(extra=extra),
                patch.object(indexing, "get_source_languages") as catalog,
                patch.object(indexing, "ensure_indexes"),
                patch.object(indexing.jobs, "run_async", return_value=ProcessJob(
                    video_id="test-video", status="submitted",
                )) as run,
                patch.object(indexing, "process_video") as process,
            ):
                response = self.client.post("/api/indexing/process", json={**PROCESS_REQUEST, **extra})
                self.assertEqual(response.status_code, 200)
                run.call_args.kwargs["target"](Mock(), Mock(), Mock())
                self.assertIsNone(process.call_args.kwargs["source_language"])
                catalog.assert_not_called()

    def test_invalid_and_translation_only_codes_cannot_start_jobs(self):
        for language in ("af-ZA", "invalid", "da-DK&indexingPreset=Basic", "x" * 33):
            with (
                self.subTest(language=language),
                patch.object(video_indexer.httpx, "get", return_value=self.catalog_response),
                patch.object(indexing, "ensure_indexes") as indexes,
                patch.object(indexing.jobs, "run_async") as run,
            ):
                response = self.client.post(
                    "/api/indexing/process", json={**PROCESS_REQUEST, "source_language": language},
                )
                self.assertEqual(response.status_code, 422)
                indexes.assert_not_called()
                run.assert_not_called()

    def test_catalog_failure_returns_retryable_error_without_starting_job(self):
        with (
            patch.object(video_indexer.httpx, "get", side_effect=httpx.ConnectError("offline")),
            patch.object(indexing, "ensure_indexes") as indexes,
            patch.object(indexing.jobs, "run_async") as run,
        ):
            self.assertEqual(self.client.get("/api/indexing/languages").status_code, 503)
            response = self.client.post(
                "/api/indexing/process", json={**PROCESS_REQUEST, "source_language": "da-DK"},
            )
            self.assertEqual(response.status_code, 503)
            indexes.assert_not_called()
            run.assert_not_called()

    def test_empty_catalog_is_not_cached_as_success(self):
        empty = httpx.Response(200, json=[], request=self.catalog_response.request)
        with patch.object(video_indexer.httpx, "get", side_effect=[empty, self.catalog_response]):
            self.assertEqual(self.client.get("/api/indexing/languages").status_code, 503)
            self.assertEqual(self.client.get("/api/indexing/languages").status_code, 200)

    def test_upload_preserves_auto_and_explicit_locale(self):
        for language, expected in ((None, "auto"), ("", "auto"), ("  ", "auto"),
                                   ("auto", "auto"), ("da-DK", "da-DK"), ("en-GB", "en-GB")):
            response = httpx.Response(
                200, json={"id": "vi-test"}, request=httpx.Request("POST", "https://example.test"),
            )
            with (
                self.subTest(language=language),
                patch.object(video_indexer, "get_vi_access_token", return_value="test-token"),
                patch.object(video_indexer, "_vi_request", return_value=response) as request,
            ):
                result = video_indexer.upload_video(
                    PROCESS_REQUEST["blob_url"], "match.mp4", "test-video", source_language=language,
                )
                parameters = parse_qs(urlparse(request.call_args.args[1]).query)
                self.assertEqual(result, "vi-test")
                self.assertEqual(parameters["language"], [expected])
                self.assertEqual(parameters["indexingPreset"], ["Advanced"])
                self.assertEqual(parameters["useManagedIdentityToDownloadVideo"], ["true"])

    def test_pipeline_passes_locale_to_upload(self):
        with (
            patch.object(video_indexer, "make_sas_url", return_value=PROCESS_REQUEST["blob_url"]),
            patch.object(video_indexer, "extract_thumbnail", return_value=None),
            patch.object(video_indexer, "record_pipeline_run"),
            patch.object(video_indexer, "upload_video", side_effect=RuntimeError("stop test")) as upload,
        ):
            with self.assertRaisesRegex(RuntimeError, "stop test"):
                video_indexer.process_video(**PROCESS_REQUEST, source_language="da-DK")
            self.assertEqual(upload.call_args.kwargs["source_language"], "da-DK")


class VideoIndexerWidgetTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(indexing.router, prefix="/api/indexing")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.raw = {"videos": [{"id": "vi-test", "durationInSeconds": 60,
                               "insights": {"keywords": [{"id": 1, "text": "goal", "instances": []}]}}]}

    def test_player_access_is_reader_scoped_to_stored_video_and_not_cached(self):
        with (patch.object(indexing, "read_video_json", side_effect=[None, self.raw]),
              patch.object(indexing, "get_vi_access_token", return_value="test-reader-token") as token):
            response = self.client.get("/api/indexing/widgets/test-video")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        token.assert_called_once_with("Reader", "Video", video_id="vi-test")

    def test_arm_video_token_request_includes_video_id(self):
        response = httpx.Response(200, json={"accessToken": "test-reader-token"},
                                  request=httpx.Request("POST", "https://example.test"))
        with (patch.object(video_indexer, "_arm_headers", return_value={}),
              patch.object(video_indexer.httpx, "post", return_value=response) as request):
            video_indexer.get_vi_access_token("Reader", "Video", video_id="vi-test")
        self.assertEqual(request.call_args.kwargs["json"],
                         {"permissionType": "Reader", "scope": "Video", "videoId": "vi-test"})
        with self.assertRaises(ValueError):
            video_indexer.get_vi_access_token("Reader", "Video")

    def test_events_exposes_native_evidence_but_not_replay_audit_candidates(self):
        manifest = {"events": [], "status": "completed", "candidates": [{"presentation": "replay"}]}
        with patch.object(indexing, "read_video_json", side_effect=[manifest, self.raw]):
            response = self.client.get("/api/indexing/events/test-video")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("candidates", response.json())
        self.assertEqual(response.json()["raw_insights_data"][0]["rawInsightsKey"], "keywords")

    def test_legacy_metadata_requires_enrichment_and_missing_metadata_is_not_empty_success(self):
        with patch.object(indexing, "read_video_json", side_effect=[None, self.raw]):
            self.assertEqual(self.client.get("/api/indexing/events/test-video").json()["status"], "needs_enrichment")
        with patch.object(indexing, "read_video_json", return_value=None):
            self.assertEqual(self.client.get("/api/indexing/widgets/test-video").status_code, 404)
        with patch.object(indexing, "read_video_json", side_effect=RuntimeError("private provider detail")):
            response = self.client.get("/api/indexing/events/test-video")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private provider detail", response.text)

    def test_enrichment_requires_a_supported_profile_and_does_not_repeat_vi_indexing(self):
        with (patch.object(indexing, "read_video_json", side_effect=[None, self.raw]),
              patch.object(indexing.jobs, "get_job", return_value=None),
              patch.object(indexing.jobs, "run_async", return_value=ProcessJob(
                  video_id="test-video", status="submitted")) as run,
              patch.object(indexing, "enrich_video_events") as enrich):
            response = self.client.post("/api/indexing/events/test-video/enrich", json={"content_type": "soccer_match"})
            self.assertEqual(response.status_code, 200)
            run.call_args.kwargs["target"](Mock(), Mock(), Mock())
            self.assertEqual(enrich.call_args.args[:2], ("test-video", "soccer_match"))
        response = self.client.post("/api/indexing/events/test-video/enrich", json={"content_type": "tennis_match"})
        self.assertEqual(response.status_code, 422)

    def test_profile_plan_skips_complete_videos_and_protects_running_work(self):
        client = Mock()
        client.search.return_value = [{"video_id": "done"}, {"video_id": "todo"}, {"video_id": "todo"}, {"video_id": "busy"}]
        def current(video_id):
            return ProcessJob(video_id=video_id, status="processing") if video_id == "busy" else None
        def manifest(video_id, filename):
            return {"content_type": "soccer_match", "status": "completed"} if video_id == "done" else None
        with (patch.object(indexing, "search_client", return_value=client),
              patch.object(indexing.jobs, "get_job", side_effect=current),
              patch.object(indexing, "read_video_json", side_effect=manifest)):
            response = self.client.get("/api/indexing/profiles/soccer_match/analysis")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queued_video_ids"], ["todo"])
        self.assertEqual(response.json()["skipped_video_ids"], ["done"])
        self.assertEqual(response.json()["active_video_ids"], ["busy"])
        self.assertNotIn("top", client.search.call_args.kwargs)

    def test_profile_submission_reserves_each_video_and_tracks_one_batch(self):
        plan = {"content_type": "soccer_match", "queued_video_ids": ["one", "two"], "skipped_video_ids": [], "active_video_ids": []}
        with (patch.object(indexing, "_profile_plan", return_value=plan),
              patch.object(indexing.jobs, "get_job", return_value=None),
              patch.object(indexing.jobs, "update") as update,
              patch.object(indexing.jobs, "run_async", return_value=ProcessJob(
                  video_id="profile-soccer_match", status="submitted")) as run):
            response = self.client.post("/api/indexing/profiles/soccer_match/enrich")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["video_id"], "profile-soccer_match")
        self.assertEqual([call.args[0] for call in update.call_args_list], ["one", "two"])
        self.assertTrue(run.call_args.kwargs["pass_progress_callback"])

    def test_profile_plan_skips_finished_targeted_pass_but_retries_failures(self):
        client = Mock()
        client.search.return_value = [{"video_id": "targeted"}, {"video_id": "retry"}]
        def manifest(video_id, filename):
            return {"content_type": "soccer_match", "status": "partial", "analysis_mode": "query_targeted",
                    "analysis_version": 1, "analysis_complete": video_id == "targeted"}
        with (patch.object(indexing, "search_client", return_value=client),
              patch.object(indexing.jobs, "get_job", return_value=None),
              patch.object(indexing, "read_video_json", side_effect=manifest)):
            response = self.client.get("/api/indexing/profiles/soccer_match/analysis")
        self.assertEqual(response.json()["skipped_video_ids"], ["targeted"])
        self.assertEqual(response.json()["queued_video_ids"], ["retry"])
        self.assertEqual(response.json()["scene_budget"], indexing.settings.event_analysis_max_scenes)

    def test_profile_analysis_continues_after_one_video_fails(self):
        with (patch.object(indexing, "enrich_video_events", side_effect=[RuntimeError("failed"), {"event_status": "completed"}]) as enrich,
              patch.object(indexing.jobs, "init_stages"), patch.object(indexing.jobs, "update") as update,
              patch.object(indexing.jobs, "set_stage")):
            with self.assertRaises(RuntimeError):
                indexing._enrich_profile(["one", "two"], "soccer_match", Mock(), Mock(), Mock())
        self.assertEqual([call.args[0] for call in enrich.call_args_list], ["one", "two"])
        self.assertTrue(any(call.args[0] == "two" and call.kwargs.get("status") == "indexed" for call in update.call_args_list))

    def test_profile_batch_cannot_be_started_twice(self):
        with (patch.object(indexing.jobs, "get_job", return_value=ProcessJob(
                  video_id="profile-soccer_match", status="processing")),
              patch.object(indexing.jobs, "run_async") as run):
            response = self.client.post("/api/indexing/profiles/soccer_match/enrich")
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()

    def test_running_job_cannot_start_a_second_enrichment(self):
        with (patch.object(indexing, "read_video_json", side_effect=[None, self.raw]),
              patch.object(indexing.jobs, "get_job", return_value=ProcessJob(
                  video_id="test-video", status="processing")),
              patch.object(indexing.jobs, "run_async") as run):
            response = self.client.post("/api/indexing/events/test-video/enrich", json={"content_type": "soccer_match"})
            self.assertEqual(response.status_code, 409)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
