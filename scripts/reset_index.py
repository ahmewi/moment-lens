"""Reset the Azure AI Search index so videos can be indexed again from scratch.

    python scripts/reset_index.py            # delete the index; the API recreates it (current schema) on its next start
    python scripts/reset_index.py --empty    # keep the index and its schema, delete every document
    python scripts/reset_index.py --yes      # do not ask for confirmation

Settings come from api/.env or the environment (see api/.env.example) and the call is made with your `az login` identity,
which needs the "Search Service Contributor" role (delete index) or "Search Index Data Contributor" (delete documents)
on the search service. The videos in Blob Storage are not touched.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from app.azure_clients import search_client, search_index_client  # noqa: E402
from app.config import settings  # noqa: E402

BATCH = 1000


def empty_index(name: str) -> int:
    client = search_client(name)
    deleted = 0
    while True:
        ids = [doc["id"] for doc in client.search(search_text="*", select=["id"], top=BATCH)]
        if not ids:
            return deleted
        client.delete_documents(documents=[{"id": doc_id} for doc_id in ids])
        deleted += len(ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--empty", action="store_true", help="delete the documents but keep the index")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    if not settings.azure_search_endpoint:
        print("AZURE_SEARCH_ENDPOINT is not set (see api/.env.example).", file=sys.stderr)
        return 2
    name = settings.search_index
    action = "delete every document in" if args.empty else "delete"
    if not args.yes and input(f"About to {action} index '{name}' on {settings.azure_search_endpoint}. Type 'yes' to continue: ") != "yes":
        print("Cancelled.")
        return 1

    if args.empty:
        print(f"Deleted {empty_index(name)} document(s) from '{name}'.")
    else:
        search_index_client().delete_index(name)
        print(f"Deleted index '{name}'. Restart the web app (or index a video) to recreate it with the current schema.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
