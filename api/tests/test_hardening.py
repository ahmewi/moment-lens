"""Security-relevant behaviour of the public API surface (the API has no sign-in of its own)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import storage
from app.config import settings
from app.main import mount_ui
from app.models import ProcessJob
from app.routers import indexing, media, upload, videos

ACCOUNT_URL = "https://teststorage.blob.core.windows.net"


def _settings_patches(testcase):
    for name, value in (
        ("azure_storage_account", "teststorage"),
        ("azure_storage_videos_container", "videos"),
        ("azure_storage_thumbnails_container", "thumbnails"),
    ):
        setting = patch.object(settings, name, value)
        setting.start()
        testcase.addCleanup(setting.stop)


class StaticUiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "secret.txt").write_text("outside the UI folder")
        static = root / "static"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<html>index</html>")
        (static / "logo.svg").write_text("<svg/>")
        app = FastAPI()
        self.assertTrue(mount_ui(app, static))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_shipped_files_and_unknown_routes_are_served(self):
        self.assertEqual(self.client.get("/logo.svg").text, "<svg/>")
        self.assertEqual(self.client.get("/library").text, "<html>index</html>")

    def test_paths_cannot_escape_the_static_folder(self):
        for path in ("/..%2fsecret.txt", "/%2e%2e%2fsecret.txt", "/..%252fsecret.txt", "//etc/passwd", "/%2fetc%2fpasswd"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, "<html>index</html>")

    def test_without_a_build_there_is_nothing_to_mount(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertFalse(mount_ui(FastAPI(), Path(empty)))


class FakeBlobs:
    """Stand-in for BlobServiceClient that records which blobs were opened."""

    def __init__(self, content_type="video/mp4", size=10):
        self.opened = []
        self.deleted = []
        self.content_type = content_type
        self.size = size

    def get_container_client(self, container):
        return SimpleNamespace(get_blob_client=lambda name: self._blob(container, name))

    def _blob(self, container, name):
        self.opened.append((container, name))
        blob = MagicMock()
        blob.container_name, blob.blob_name = container, name
        blob.url = f"{ACCOUNT_URL}/{container}/{name}"
        blob.get_blob_properties.return_value = SimpleNamespace(
            size=self.size, content_settings=SimpleNamespace(content_type=self.content_type),
        )
        blob.download_blob.return_value.chunks.return_value = iter([b"x" * self.size])
        blob.delete_blob.side_effect = lambda **_: self.deleted.append((container, name))
        return blob


class MediaProxyTests(unittest.TestCase):
    def setUp(self):
        _settings_patches(self)
        self.blobs = FakeBlobs()
        service = patch.object(storage, "blob_service", return_value=self.blobs)
        service.start()
        self.addCleanup(service.stop)
        app = FastAPI()
        app.include_router(media.router, prefix="/api/media")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def get(self, url):
        return self.client.get("/api/media/blob", params={"url": url})

    def test_only_videos_and_thumbnails_are_served(self):
        for url in (
            f"{ACCOUNT_URL}/insights/v1/insights.json",
            f"{ACCOUNT_URL}/$web/index.html",
            "https://other.blob.core.windows.net/videos/v1/a.mp4",
            "file:///etc/passwd",
            f"{ACCOUNT_URL}/videos/../insights/x.json",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.get(url).status_code, 400)
        self.assertEqual(self.get(f"{ACCOUNT_URL}/videos/v1/a.mp4").status_code, 200)
        self.assertEqual(self.get(f"{ACCOUNT_URL}/thumbnails/v1.jpg").status_code, 200)

    def test_uploaded_files_can_never_run_in_the_browser_as_a_page(self):
        self.blobs.content_type = "text/html"
        response = self.get(f"{ACCOUNT_URL}/videos/v1/page.html")
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertEqual(response.headers["content-disposition"], "attachment")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", response.headers["content-security-policy"])

    def test_media_type_falls_back_to_the_extension(self):
        self.blobs.content_type = "application/octet-stream"
        response = self.get(f"{ACCOUNT_URL}/videos/v1/clip.mp4")
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertNotIn("content-disposition", response.headers)

    def test_errors_do_not_expose_provider_details(self):
        with patch.object(storage, "blob_service", side_effect=RuntimeError("secret-account-detail")):
            response = self.get(f"{ACCOUNT_URL}/videos/v1/a.mp4")
        self.assertNotIn("secret-account-detail", response.text)


class ProcessRequestValidationTests(unittest.TestCase):
    def setUp(self):
        _settings_patches(self)
        service = patch.object(storage, "blob_service", return_value=FakeBlobs())
        service.start()
        self.addCleanup(service.stop)
        app = FastAPI()
        app.include_router(indexing.router, prefix="/api/indexing")
        app.include_router(videos.router, prefix="/api/videos")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def process(self, blob_url, video_id="video-1"):
        job = ProcessJob(video_id="video-1", status="submitted")
        with patch.object(indexing, "ensure_indexes"), patch.object(indexing.jobs, "run_async", return_value=job) as run:
            response = self.client.post("/api/indexing/process", json={
                "video_id": video_id, "filename": "match.mp4", "blob_url": blob_url,
            })
        return response, run

    def test_only_videos_of_the_configured_container_can_be_processed(self):
        for url in (
            f"{ACCOUNT_URL}/insights/v1/insights.json",
            "https://other.blob.core.windows.net/videos/v1/a.mp4",
            "http://teststorage.blob.core.windows.net/videos/v1/a.mp4",
            f"{ACCOUNT_URL}/videos/v1/a.mp4?sv=2025-01-05&sig=abc",
            "file:///etc/passwd",
            "http://169.254.169.254/metadata/identity/oauth2/token",
        ):
            with self.subTest(url=url):
                response, run = self.process(url)
                self.assertEqual(response.status_code, 400)
                run.assert_not_called()

    def test_ids_are_restricted_to_a_safe_alphabet(self):
        _, run = self.process(f"{ACCOUNT_URL}/videos/v1/a.mp4")
        run.assert_called_once()
        for video_id in ("a'b", "a b", "../x", "x" * 200, ""):
            with self.subTest(video_id=video_id):
                response, run = self.process(f"{ACCOUNT_URL}/videos/v1/a.mp4", video_id=video_id)
                self.assertEqual(response.status_code, 422)
                run.assert_not_called()
        for path in ("/api/videos/a'%20or%201%20eq%201/scenes", "/api/indexing/status/a%27b", "/api/indexing/events/a%27b"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 422)
        self.assertEqual(self.client.delete("/api/videos/a%27b").status_code, 422)


class UploadSessionTests(unittest.TestCase):
    def setUp(self):
        _settings_patches(self)
        app = FastAPI()
        app.include_router(upload.router, prefix="/api/upload")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def commit(self, **fields):
        body = {"blob_name": "abc/match.mp4", "filename": "match.mp4", "size_bytes": 10, "total_blocks": 1, **fields}
        return self.client.post("/api/upload/session/abc/commit", json=body)

    def test_a_commit_never_replaces_an_existing_blob(self):
        blob = MagicMock(url=f"{ACCOUNT_URL}/videos/abc/match.mp4")
        service = Mock()
        service.get_container_client.return_value.get_blob_client.return_value = blob
        with patch.object(storage, "blob_service", return_value=service):
            self.assertEqual(self.commit().status_code, 200)
        self.assertIs(blob.commit_block_list.call_args.kwargs["match_condition"], MatchConditions.IfMissing)

        with patch.object(upload, "commit_upload_blocks", side_effect=ResourceExistsError("exists")):
            self.assertEqual(self.commit().status_code, 409)

    def test_only_video_content_types_are_stored(self):
        for sent, stored in (("video/x-matroska", "video/x-matroska"), ("text/html", "video/mp4"), (None, "video/mp4")):
            with self.subTest(sent=sent), patch.object(upload, "commit_upload_blocks", return_value="u") as commit:
                self.assertEqual(self.commit(content_type=sent).status_code, 200)
                self.assertEqual(commit.call_args.kwargs["content_type"], stored)

    def test_blob_names_must_belong_to_the_session(self):
        for name in ("other/match.mp4", "abc/sub/match.mp4", "abc/../match.mp4", "abc/match.html"):
            with self.subTest(name=name):
                self.assertGreaterEqual(self.commit(blob_name=name).status_code, 400)

    def test_direct_upload_and_sas_endpoints_are_gone(self):
        self.assertEqual(self.client.post("/api/upload/sas", json={"filename": "a.mp4"}).status_code, 404)
        self.assertEqual(self.client.post("/api/upload", files={"file": ("a.mp4", b"x")}).status_code, 404)


class BlobHelperTests(unittest.TestCase):
    def setUp(self):
        _settings_patches(self)
        self.blobs = FakeBlobs()
        service = patch.object(storage, "blob_service", return_value=self.blobs)
        service.start()
        self.addCleanup(service.stop)

    def test_urls_are_only_signed_for_this_account(self):
        with self.assertRaises(ValueError):
            storage.make_sas_url("https://other.blob.core.windows.net/videos/a.mp4")
        with self.assertRaises(ValueError):
            storage.make_sas_url("file:///etc/passwd")

    def test_only_served_containers_can_be_deleted(self):
        self.assertFalse(storage.delete_blob_by_url(f"{ACCOUNT_URL}/insights/v1/insights.json"))
        self.assertFalse(storage.delete_blob_by_url("https://other.blob.core.windows.net/videos/v1/a.mp4"))
        self.assertEqual(self.blobs.deleted, [])
        self.assertTrue(storage.delete_blob_by_url(f"{ACCOUNT_URL}/videos/v1/a.mp4?sv=x"))
        self.assertTrue(storage.delete_blob_by_url(f"{ACCOUNT_URL}/thumbnails/v1.jpg"))
        self.assertEqual(self.blobs.deleted, [("videos", "v1/a.mp4"), ("thumbnails", "v1.jpg")])


if __name__ == "__main__":
    unittest.main()
