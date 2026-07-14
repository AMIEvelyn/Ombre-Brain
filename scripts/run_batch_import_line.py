#!/usr/bin/env python3
"""Manual test-run for the batch-import pull-a-line pipeline
(see docs/batch-import-design.md). Run once on a single seed bucket,
print the candidate report as JSON. Does not write anything to CardStore
-- purely a report for human review.

Usage:
    docker exec ombre-brain python3 /app/scripts/run_batch_import_line.py <seed_bucket_id>
    docker exec ombre-brain python3 /app/scripts/run_batch_import_line.py --query "书稿" --top 5
    docker exec ombre-brain python3 /app/scripts/run_batch_import_line.py <seed_bucket_id> \
        --sweep-tags "文学创作,小说创作,慁"

--query prints search hits (id + name + date) so you can pick a seed_bucket_id
without already knowing one.

--sweep-tags turns on the tag-sweep channel (see docs/batch-import-design.md
§2.1) for exactly the tags you list, comma-separated. Only pass tags you've
personally confirmed are specific to this one project/story -- a tag that
also recurs across other, unrelated topics will sweep those in too (this
is exactly what happened on the first real test run: broad recurring
concept tags on the seed bucket pulled in ~300 unrelated buckets about a
completely different theme).

Without --sweep-tags, the seed's own tags are checked against
resources/batch_import_known_projects.json (docs §13) -- if the seed
matches a registered project (一澜/林湛 vetted its tags once, ahead of
time), that project's tag list is applied automatically, no typing
needed. If it doesn't match anything registered, no tag sweep runs at
all -- similarity search only.
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bucket_manager import BucketManager
from cards_store import CardStore
from embedding_engine import EmbeddingEngine
from batch_import_engine import BatchImportEngine
from import_progress_store import ImportProgressStore
from utils import load_config


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("seed_bucket_id", nargs="?", help="Bucket id to start the line from")
    parser.add_argument("--query", help="Search for candidate seed buckets instead of running a line")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--sweep-tags", default="",
        help="Comma-separated tags to sweep on, e.g. '文学创作,小说创作,慁'. Only tags you've "
        "confirmed are specific to this one project -- see the module docstring.",
    )
    args = parser.parse_args()
    sweep_tags = [t.strip() for t in args.sweep_tags.split(",") if t.strip()] or None

    config = load_config()
    bucket_mgr = BucketManager(config)
    embedding_engine = EmbeddingEngine(config)
    card_store = CardStore(config)
    progress_store = ImportProgressStore(config)
    engine = BatchImportEngine(config, bucket_mgr, embedding_engine, card_store)

    if args.query:
        matches = await bucket_mgr.search_with_semantic(args.query, embedding_engine, limit=args.top)
        for b in matches:
            meta = b.get("metadata", {})
            print(f"{b['id']}\t{meta.get('created', '')}\t{meta.get('name', '')}")
        return

    if not args.seed_bucket_id:
        parser.error("need a seed_bucket_id, or pass --query to find one first")

    result = await engine.run_single_line(args.seed_bucket_id, progress_store, sweep_tags)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
