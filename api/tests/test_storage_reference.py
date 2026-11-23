import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError
from azure.storage.blob import BlobServiceClient
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import storage
from app.config import settings
from app.routers import option1, upload, videos


class StorageReferenceTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("azure_storage_account", "teststorage"),
            ("azure_storage_videos_container", "videos"),
            ("max_upload_mb", 100),
        ):
            setting = patch.object(settings, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.service = BlobServiceClient("https://teststorage.blob.core.windows.net")
        self.addCleanup(self.service.close)
        self.properties = Mock(return_value=SimpleNamespace(size=1024))
        self.clients = []

        def get_client(container, blob_name):
            client = self.service.get_blob_client(container, blob_name)
            client.get_blob_properties = self.properties
            client.download_blob = Mock(side_effect=AssertionError("Must not download source"))
            client.upload_blob = Mock(side_effect=AssertionError("Must not upload source"))
            self.clients.append(client)
            return client

        self.list_blobs = Mock()
        self.backend = Mock()
        self.backend.get_container_client.side_effect = lambda name: SimpleNamespace(
            get_blob_client=lambda blob_name: get_client(name, blob_name),
            list_blobs=self.list_blobs,
        )
        service = patch.object(storage, "blob_service", return_value=self.backend)
        service.start()
        self.addCleanup(service.stop)
        app = FastAPI()
        app.include_router(upload.router, prefix="/api/upload")
        app.include_router(option1.router, prefix="/api/option1")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = "https://teststorage.blob.core.windows.net/videos/existing/match.mp4"

    def reference(self, url=None):
        return self.client.post("/api/upload/reference", json={"blob_url": url or self.url})

    def test_reference_reads_metadata_without_uploading_or_downloading(self):
        response = self.reference()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["blob_url"], self.url)
        self.assertEqual(response.json()["filename"], "match.mp4")
        self.assertEqual(response.json()["size_bytes"], 1024)
        self.assertTrue(response.json()["video_id"].startswith("ref-"))
        self.properties.assert_called_once_with(timeout=30)
        self.clients[0].download_blob.assert_not_called()
        self.clients[0].upload_blob.assert_not_called()
        self.assertNotEqual(self.reference().json()["video_id"], response.json()["video_id"])

    def test_reference_can_start_vi_without_upload_stage(self):
        reference = self.reference().json()
        for option, router in (("option1", option1),):
            with (
                self.subTest(option=option),
                patch.object(router, "ensure_indexes"),
                patch.object(router.jobs, "run_async", return_value={
                    "video_id": reference["video_id"], "option": option, "status": "submitted",
                }) as run,
                patch.object(router, "process_video") as process,
            ):
                response = self.client.post(f"/api/{option}/process", json=reference)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(run.call_args.kwargs["initial_stages"]["upload"], "skipped")
                run.call_args.kwargs["target"](Mock(), Mock(), Mock())
                self.assertEqual(process.call_args.args[2], self.url)

    def test_signed_url_is_canonicalized_without_retaining_credentials(self):
        response = self.reference(self.url + "?sv=2025-01-05&sig=test-secret&se=expired")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["blob_url"], self.url)
        self.assertNotIn("test-secret", response.text)

    def test_encoded_names_are_decoded_once_by_the_storage_client(self):
        url = "https://teststorage.blob.core.windows.net/videos/folder/Match%20%231%20%2B%20100%25.mp4"
        response = self.reference(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["filename"], "Match #1 + 100%.mp4")
        self.assertEqual(self.clients[0].blob_name, "folder/Match #1 + 100%.mp4")
        self.assertEqual(storage._split_blob_url(response.json()["blob_url"])[1], self.clients[0].blob_name)

    def test_untrusted_hosts_and_non_https_urls_are_rejected_without_io(self):
        for url in (
            "http://teststorage.blob.core.windows.net/videos/match.mp4",
            "https://other.blob.core.windows.net/videos/match.mp4",
            "https://teststorage.blob.core.windows.net.attacker.test/videos/match.mp4",
            "https://user@teststorage.blob.core.windows.net/videos/match.mp4",
            "https://teststorage.blob.core.windows.net:444/videos/match.mp4",
            "https://127.0.0.1/videos/match.mp4",
            "file:///videos/match.mp4",
            "match.mp4",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.reference(url).status_code, 400)
        self.backend.get_container_client.assert_not_called()
        self.properties.assert_not_called()

    def test_other_containers_and_ambiguous_paths_are_rejected(self):
        for url in (
            self.url.replace("/videos/", "/insights/"),
            self.url + "#fragment",
            self.url + "?snapshot=old",
            self.url + "?versionid=old",
            self.url.replace("existing", "%2e%2e"),
            self.url.replace("existing", "bad%5Cpath"),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.reference(url).status_code, 400)
        self.properties.assert_not_called()

    def test_unsupported_files_are_rejected_before_storage_access(self):
        self.assertEqual(self.reference(self.url.replace(".mp4", ".json")).status_code, 415)
        self.properties.assert_not_called()

    def test_empty_or_oversize_blob_is_rejected(self):
        for size, status in ((0, 400), (101 * 1024 * 1024, 413)):
            with self.subTest(size=size):
                self.properties.return_value = SimpleNamespace(size=size)
                self.assertEqual(self.reference().status_code, status)

    def test_storage_failures_have_safe_error_responses(self):
        forbidden = HttpResponseError("private storage details")
        forbidden.status_code = 403
        for error, status in (
            (ResourceNotFoundError("private storage details"), 404),
            (forbidden, 403),
            (ServiceRequestError("private storage details"), 503),
        ):
            with self.subTest(status=status):
                self.properties.side_effect = error
                response = self.reference()
                self.assertEqual(response.status_code, status)
                self.assertNotIn("private storage details", response.text)

    def test_unconfigured_storage_returns_service_error(self):
        with patch.object(settings, "azure_storage_account", ""):
            self.assertEqual(self.reference().status_code, 503)
        self.backend.get_container_client.assert_not_called()


    def test_browsing_is_paged_and_excludes_unsupported_files(self):
        pages = MagicMock()
        pages.__next__.return_value = iter([
            SimpleNamespace(name="existing/Match One.MP4", size=1024),
            SimpleNamespace(name="existing/insights.json", size=1024),
            SimpleNamespace(name="empty.mp4", size=0),
            SimpleNamespace(name="large.mp4", size=101 * 1024 * 1024),
        ])
        pages.continuation_token = "next-page"
        self.list_blobs.return_value.by_page.return_value = pages
        with patch.object(upload, "blob_service", return_value=self.backend):
            response = self.client.get("/api/upload/stored", params={"continuation_token": "previous-page"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["account"], "teststorage")
        self.assertEqual(body["container"], "videos")
        self.assertEqual(body["continuation_token"], "next-page")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["blob_name"], "existing/Match One.MP4")
        self.assertIn("Match%20One.MP4", body["items"][0]["blob_url"])
        self.list_blobs.assert_called_once_with(results_per_page=100, timeout=30)
        self.list_blobs.return_value.by_page.assert_called_once_with(continuation_token="previous-page")
        self.properties.assert_not_called()

    def test_empty_storage_page_returns_an_empty_list(self):
        pages = MagicMock()
        pages.__next__.side_effect = StopIteration
        pages.continuation_token = None
        self.list_blobs.return_value.by_page.return_value = pages
        with patch.object(upload, "blob_service", return_value=self.backend):
            response = self.client.get("/api/upload/stored")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        self.assertIsNone(response.json()["continuation_token"])

    def test_storage_list_failure_does_not_expose_provider_details(self):
        self.list_blobs.side_effect = ServiceRequestError("private provider detail")
        with patch.object(upload, "blob_service", return_value=self.backend):
            response = self.client.get("/api/upload/stored")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private provider detail", response.text)

    def test_sas_signs_decoded_name_and_does_not_append_to_old_sas(self):
        url = "https://teststorage.blob.core.windows.net/videos/folder/Match%20%231%20%2B%20100%25.mp4"
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        with (
            patch.object(storage, "_get_user_delegation_key", return_value=(Mock(), expiry)),
            patch.object(storage, "generate_blob_sas", return_value="sig=new-test-signature") as sign,
        ):
            result = storage.make_sas_url(url + "?sig=old-signature")
        self.assertEqual(sign.call_args.kwargs["blob_name"], "folder/Match #1 + 100%.mp4")
        self.assertEqual(result.count("?"), 1)
        self.assertNotIn("old-signature", result)
        self.assertEqual(storage._split_blob_url(result)[1], "folder/Match #1 + 100%.mp4")


class StorageReferenceCleanupTests(unittest.TestCase):
    def test_library_queries_only_vi_and_defaults_scenes_to_vi(self):
        client = Mock()
        client.search.return_value = []
        with patch.object(videos, "search_client", return_value=client) as factory:
            self.assertEqual(videos.list_videos()["items"], [])
            factory.assert_called_once_with(settings.search_index_option1)
            factory.reset_mock()
            self.assertEqual(videos.list_scenes("example")["option"], "option1")
            factory.assert_called_once_with(settings.search_index_option1)

    def test_public_api_does_not_expose_content_understanding(self):
        from app.main import app

        paths = app.openapi()["paths"]
        self.assertIn("/api/option1/process", paths)
        self.assertFalse(any(path.startswith("/api/option2") for path in paths))
        with TestClient(app) as client:
            self.assertNotIn("option2", client.get("/api/health").json()["indexes"])

    def test_search_request_rejects_cu(self):
        from pydantic import ValidationError

        from app.models import SearchRequest

        self.assertEqual(SearchRequest(query="goal").option, "option1")
        with self.assertRaises(ValidationError):
            SearchRequest(query="goal", option="option2")

    def test_cleanup_keeps_referenced_and_shared_sources(self):
        source = "https://teststorage.blob.core.windows.net/videos/original/match.mp4"
        for video_id, shared, lookup_fails, deleted in (
            ("ref-example", False, False, False),
            ("original", True, False, False),
            ("original", False, True, False),
            ("original", False, False, True),
        ):
            client = Mock()

            def search(**kwargs):
                if " ne " in kwargs["filter"]:
                    if lookup_fails:
                        raise RuntimeError("search unavailable")
                    return [{"blob_url": source}] if shared else []
                return [{"id": "scene", "blob_url": source}]

            client.search.side_effect = search
            with (
                self.subTest(video_id=video_id, shared=shared, lookup_fails=lookup_fails),
                patch.object(videos, "search_client", return_value=client),
                patch.object(videos, "delete_blob_by_url", return_value=True) as delete,
                patch.object(videos, "delete_blobs_with_prefix", return_value=0),
                patch.object(videos.jobs, "clear_video"),
            ):
                result = videos.delete_video(video_id)
                self.assertEqual(result["blobs_deleted"], int(deleted))
                if deleted:
                    delete.assert_called_once_with(source)
                else:
                    delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
