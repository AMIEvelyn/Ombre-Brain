#!/usr/bin/env python3
"""Manual test-run for the batch-import pull-a-line pipeline
(see docs/batch-import-design.md). Run once on a single seed bucket,
print the candidate report as JSON. Does not write anything to CardStore
-- purely a report for human review.

Usage:
    docker exec ombre-brain python3 /app/scripts/run_batch_import_line.py <seed_bucket_id>
    docker exec ombre-brain python3 /app/scripts/run_batch_import_line.py --query "书稿" --top 5

--query prints search hits (id + name + date) so you can pick a seed_bucket_id
without already knowing one.
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
    args = parser.parse_args()

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

    result = await engine.run_single_line(args.seed_bucket_id, progress_store)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
