"""Smoke test for a deployed MomentLens site. It uses only the public HTTP API, so it needs no Azure credentials.

    python scripts/smoke_test.py --base-url https://<web-app>.azurewebsites.net
    python scripts/smoke_test.py --base-url https://<web-app>.azurewebsites.net --video ./clip.mp4 --query "goal"

Without --video it checks the health endpoint and the library. With --video it also runs the whole flow: upload the file
through the API's block-upload endpoints, index it with Azure AI Video Indexer, wait until the job is done and search it.
Indexing a short clip takes a few minutes and incurs normal Video Indexer / Azure OpenAI charges; add --cleanup to delete
the video and its search documents afterwards.

Requires httpx (installed with the API: `pip install -e api`).
"""
from __future__ import annotations

import argparse
import mimetypes
import sys
import time
from pathlib import Path

import httpx

POLL_SECONDS = 10


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def check(response: httpx.Response, what: str) -> dict:
    if response.status_code >= 400:
        fail(f"{what}: HTTP {response.status_code} {response.text[:300]}")
    return response.json()


def upload(client: httpx.Client, path: Path) -> dict:
    content_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    size = path.stat().st_size
    session = check(client.post("/api/upload/session", json={
        "filename": path.name, "content_type": content_type, "size_bytes": size,
    }), "start upload")
    block_size = session["block_size"]
    total_blocks = max(1, -(-size // block_size))
    with path.open("rb") as stream:
        for index in range(total_blocks):
            response = client.put(
                f"/api/upload/session/{session['video_id']}/blocks/{index}",
                params={"blob_name": session["blob_name"]},
                content=stream.read(block_size),
                headers={"Content-Type": "application/octet-stream"},
            )
            check(response, f"upload block {index + 1}/{total_blocks}")
            print(f"  uploaded block {index + 1}/{total_blocks}", end="\r")
    print()
    return check(client.post(f"/api/upload/session/{session['video_id']}/commit", json={
        "blob_name": session["blob_name"], "filename": path.name, "size_bytes": size,
        "content_type": content_type, "total_blocks": total_blocks,
    }), "commit upload")


def wait_until_indexed(client: httpx.Client, video_id: str, timeout_minutes: int) -> None:
    deadline = time.monotonic() + timeout_minutes * 60
    last = ""
    while time.monotonic() < deadline:
        job = check(client.get(f"/api/indexing/status/{video_id}"), "job status")
        line = f"{job['status']} {job['progress']}% {job.get('stage') or ''}".strip()
        if line != last:
            print(f"  {line}")
            last = line
        if job["status"] == "indexed":
            return
        if job["status"] == "failed":
            fail(f"indexing failed: {(job.get('error') or '')[:500]}")
        time.sleep(POLL_SECONDS)
    fail(f"indexing did not finish within {timeout_minutes} minutes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True, help="site URL, e.g. https://ml-app-xxxx.azurewebsites.net")
    parser.add_argument("--video", type=Path, help="local video to upload, index and search")
    parser.add_argument("--query", default="a goal being scored", help="search query for the uploaded video")
    parser.add_argument("--content-type", default="generic", help="content profile (see GET /api/indexing/profiles)")
    parser.add_argument("--language", default="auto", help="spoken language code, e.g. en-US (default: auto detect)")
    parser.add_argument("--timeout-minutes", type=int, default=30)
    parser.add_argument("--cleanup", action="store_true", help="delete the uploaded video afterwards")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=120, follow_redirects=True) as client:
        health = check(client.get("/api/health"), "health")
        print(f"health: {health['status']} (version {health['version']}, index {health['index']})")
        library = check(client.get("/api/videos"), "library")
        print(f"library: {library['count']} video(s)")
        if not args.video:
            return 0
        if not args.video.is_file():
            fail(f"{args.video} is not a file")

        print(f"uploading {args.video.name} ...")
        uploaded = upload(client, args.video)
        body = {"video_id": uploaded["video_id"], "filename": uploaded["filename"], "blob_url": uploaded["blob_url"],
                "content_type": args.content_type}
        if args.language != "auto":
            body["source_language"] = args.language
        check(client.post("/api/indexing/process", json=body), "start indexing")
        print("indexing ...")
        wait_until_indexed(client, uploaded["video_id"], args.timeout_minutes)

        print(f"searching for {args.query!r} ...")
        result = check(client.post("/api/search", json={
            "query": args.query, "top": 3, "clip_duration_seconds": 15, "mode": "hybrid",
            "filters": {"video_ids": [uploaded["video_id"]]},
        }), "search")
        for item in result["items"]:
            print(f"  {item['start_seconds']:.0f}s-{item['end_seconds']:.0f}s  {item['summary'][:100]}")
        if not result["items"]:
            fail("the search returned no moments")
        if args.cleanup:
            check(client.delete(f"/api/videos/{uploaded['video_id']}"), "cleanup")
            print("deleted the test video")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
