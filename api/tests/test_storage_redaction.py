import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app import storage

SAS_URL = "https://acct.blob.core.windows.net/videos/v/a.mp4?sp=r&sv=2026-10-06&sig=TOPSECRETSIGNATURE%3D"


class RedactSasTests(unittest.TestCase):
    def test_full_urls_lose_their_query_string(self):
        out = storage._redact_sas(f"Error opening {SAS_URL}: 404")
        self.assertNotIn("TOPSECRET", out)
        self.assertIn("https://acct.blob.core.windows.net/videos/v/a.mp4?<redacted>", out)

    def test_a_truncated_tail_that_starts_mid_query_is_still_redacted(self):
        # stderr[-300:] often begins in the middle of the URL, so there is no scheme to match.
        out = storage._redact_sas(b"sp=r&sv=2026-10-06&sig=TOPSECRETSIGNATURE%3D.\nServer returned 404 Not Found\n")
        self.assertNotIn("TOPSECRET", out)
        self.assertIn("Server returned 404 Not Found", out)

    def test_text_without_a_sas_is_unchanged(self):
        self.assertEqual(storage._redact_sas("plain error"), "plain error")


class FrameGrabLoggingTests(unittest.TestCase):
    def test_a_failed_ffmpeg_run_does_not_log_the_signed_url(self):
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=f"x {SAS_URL} 404".encode())
        with (
            patch.object(storage.shutil, "which", return_value="ffmpeg"),
            patch.object(storage.subprocess, "run", return_value=failed),
            self.assertLogs("app.storage", level="WARNING") as logs,
        ):
            self.assertIsNone(storage.extract_frame_bytes(SAS_URL, 5.0))
        self.assertNotIn("TOPSECRET", "\n".join(logs.output))

    def test_a_timeout_does_not_log_the_command_line_that_embeds_the_signed_url(self):
        timeout = subprocess.TimeoutExpired(cmd=["ffmpeg", "-i", SAS_URL], timeout=12)
        with (
            patch.object(storage.shutil, "which", return_value="ffmpeg"),
            patch.object(storage.subprocess, "run", side_effect=timeout),
            self.assertLogs("app.storage", level="WARNING") as logs,
        ):
            self.assertIsNone(storage.extract_frame_bytes(SAS_URL, 5.0))
        self.assertNotIn("TOPSECRET", "\n".join(logs.output))

    def test_missing_ffmpeg_returns_none_without_running_anything(self):
        with patch.object(storage.shutil, "which", return_value=None), patch.object(storage.subprocess, "run") as run:
            self.assertIsNone(storage.extract_frame_bytes(SAS_URL, 5.0))
        run.assert_not_called()


class ExtractWindowFramesTests(unittest.TestCase):
    @staticmethod
    def _fake_ffmpeg(frames, returncode=0, stderr=b""):
        """A subprocess.run stand-in that writes `frames` (bytes) to the output pattern ffmpeg was given."""
        recorded = {}

        def run(cmd, **kwargs):
            recorded["cmd"] = cmd
            out_dir = Path(cmd[-1]).parent
            for i, data in enumerate(frames, 1):
                (out_dir / f"f_{i:02d}.jpg").write_bytes(data)
            recorded["out_dir"] = out_dir
            return subprocess.CompletedProcess(cmd, returncode, b"", stderr)

        return run, recorded

    def test_one_process_returns_frames_in_chronological_order_and_cleans_up(self):
        run, recorded = self._fake_ffmpeg([b"first", b"second", b"third"])
        with patch.object(storage.shutil, "which", return_value="ffmpeg"), patch.object(storage.subprocess, "run", side_effect=run) as spy:
            frames = storage.extract_window_frames(SAS_URL, 100.0, 130.0, n=4, max_width=640)
        self.assertEqual(frames, [b"first", b"second", b"third"])
        spy.assert_called_once()
        self.assertFalse(recorded["out_dir"].exists(), "temp directory must be removed")

    def test_command_samples_keyframes_only_inside_the_window_with_the_requested_width(self):
        run, recorded = self._fake_ffmpeg([b"x"])
        with patch.object(storage.shutil, "which", return_value="ffmpeg"), patch.object(storage.subprocess, "run", side_effect=run):
            storage.extract_window_frames(SAS_URL, 100.0, 130.0, n=4, max_width=640)
        cmd = recorded["cmd"]
        self.assertEqual(cmd[cmd.index("-skip_frame") + 1], "nokey")
        self.assertEqual(cmd[cmd.index("-i") + 1], SAS_URL)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "4")
        # Starts half a step into the window so the first sample is interior, never before `start`.
        self.assertGreaterEqual(float(cmd[cmd.index("-ss") + 1]), 100.0)
        self.assertLessEqual(float(cmd[cmd.index("-ss") + 1]) + float(cmd[cmd.index("-t") + 1]), 130.0 + 0.01)
        self.assertIn("scale=640:-2", cmd[cmd.index("-vf") + 1])

    def test_degenerate_windows_or_missing_ffmpeg_return_nothing_without_running(self):
        with patch.object(storage.shutil, "which", return_value="ffmpeg"), patch.object(storage.subprocess, "run") as run:
            self.assertEqual(storage.extract_window_frames(SAS_URL, 10.0, 10.0), [])
            self.assertEqual(storage.extract_window_frames(SAS_URL, 10.0, 5.0), [])
            self.assertEqual(storage.extract_window_frames(SAS_URL, 0.0, 30.0, n=0), [])
        with patch.object(storage.shutil, "which", return_value=None), patch.object(storage.subprocess, "run") as run:
            self.assertEqual(storage.extract_window_frames(SAS_URL, 0.0, 30.0), [])
        run.assert_not_called()

    def test_ffmpeg_failure_returns_empty_and_does_not_log_the_signed_url(self):
        run, _ = self._fake_ffmpeg([], returncode=1, stderr=f"x {SAS_URL} 404".encode())
        with (
            patch.object(storage.shutil, "which", return_value="ffmpeg"),
            patch.object(storage.subprocess, "run", side_effect=run),
            self.assertLogs("app.storage", level="WARNING") as logs,
        ):
            self.assertEqual(storage.extract_window_frames(SAS_URL, 0.0, 30.0), [])
        self.assertNotIn("TOPSECRET", "\n".join(logs.output))

    def test_timeout_returns_empty_and_does_not_log_the_command_line(self):
        timeout = subprocess.TimeoutExpired(cmd=["ffmpeg", "-i", SAS_URL], timeout=40)
        with (
            patch.object(storage.shutil, "which", return_value="ffmpeg"),
            patch.object(storage.subprocess, "run", side_effect=timeout),
            self.assertLogs("app.storage", level="WARNING") as logs,
        ):
            self.assertEqual(storage.extract_window_frames(SAS_URL, 0.0, 30.0), [])
        self.assertNotIn("TOPSECRET", "\n".join(logs.output))

    def test_frames_written_before_a_nonzero_exit_are_still_returned(self):
        # ffmpeg can exit non-zero on a trailing read error after writing usable frames.
        run, _ = self._fake_ffmpeg([b"usable"], returncode=1, stderr=b"read error near the end")
        with patch.object(storage.shutil, "which", return_value="ffmpeg"), patch.object(storage.subprocess, "run", side_effect=run):
            self.assertEqual(storage.extract_window_frames(SAS_URL, 0.0, 30.0), [b"usable"])

    def test_niceness_prefixes_the_command_only_when_requested_and_available(self):
        for niceness, nice_available, expected in (
            (10, True, ["nice", "-n", "10", "ffmpeg"]), (10, False, ["ffmpeg"]), (0, True, ["ffmpeg"]),
        ):
            with self.subTest(niceness=niceness, nice_available=nice_available):
                run, recorded = self._fake_ffmpeg([b"x"])
                which = lambda name: name if name == "ffmpeg" or nice_available else None  # noqa: E731
                with patch.object(storage.shutil, "which", side_effect=which), patch.object(storage.subprocess, "run", side_effect=run):
                    storage.extract_window_frames(SAS_URL, 0.0, 30.0, niceness=niceness)
                self.assertEqual(recorded["cmd"][:len(expected)], expected)


if __name__ == "__main__":
    unittest.main()
