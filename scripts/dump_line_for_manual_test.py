#!/usr/bin/env python3
"""Dump one pulled-and-judged batch-import line as raw JSON, for manual
testing outside the API (paste into a chat app, no cost).

Reuses the existing pull_line()/judge_line() machinery from
BatchImportEngine as-is (see docs/batch-import-design.md §2/§3) -- same
tag-sweep + similarity-search + LLM line-judgment that run_single_line()
uses. The only thing this script skips is everything *after* the line is
assembled: no milestone extraction, no candidate-card generation, no
progress-store writes (nothing gets marked "swept", nothing is saved as
a pending association). It's a read-only detour that stops right where
the lossy compression stages would normally begin, so the full,
untouched bucket records can be inspected or handed to a model by hand
instead.

Usage:
    docker exec ombre-brain python3 /app/scripts/dump_line_for_manual_test.py <seed_bucket_id>
    docker exec ombre-brain python3 /app/scripts/dump_line_for_manual_test.py <seed_bucket_id> \
        --sweep-tags "文学创作,小说创作,慁" --out /app/state/line_book.json

Without --sweep-tags, the seed's own tags are checked against the known-
projects registry (docs §13) the same way run_single_line() does.

Output is one JSON file: seed id, the judged specific_question, candidate
counts, and a "buckets" array (one object per bucket in the line, sorted
by real event date) with id/date/title/tags/domain/importance/content --
full content, not the 400-char excerpt judge_line/extract_milestones use
internally.
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
from batch_import_engine import BatchImportEngine, _bucket_date
from utils import load_config


def _bucket_record(bucket: dict) -> dict:
    meta = bucket.get("metadata", {}) or {}
    return {
        "id": bucket.get("id"),
        "date": _bucket_date(bucket),
        "title": meta.get("name", ""),
        "tags": meta.get("tags", []),
        "domain": meta.get("domain", []),
        "importance": meta.get("importance", 5),
        "content": str(bucket.get("content", "") or ""),
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed_bucket_id", help="Bucket id to start the line from")
    parser.add_argument(
        "--sweep-tags", default="",
        help="Comma-separated tags to sweep on, e.g. '文学创作,小说创作,慁'. "
        "Leave empty to auto-resolve from the known-projects registry.",
    )
    parser.add_argument("--out", default="", help="Output path; defaults to line_<seed_id>.json")
    args = parser.parse_args()
    explicit_sweep_tags = [t.strip() for t in args.sweep_tags.split(",") if t.strip()] or None

    config = load_config()
    bucket_mgr = BucketManager(config)
    embedding_engine = EmbeddingEngine(config)
    card_store = CardStore(config)
    engine = BatchImportEngine(config, bucket_mgr, embedding_engine, card_store)

    seed_bucket = await bucket_mgr.get(args.seed_bucket_id)
    if not seed_bucket:
        print(f"bucket not found: {args.seed_bucket_id}", file=sys.stderr)
        sys.exit(1)

    sweep_tags = engine.resolve_sweep_tags(seed_bucket, explicit_sweep_tags)
    pulled = await engine.pull_line(seed_bucket, sweep_tags)
    similarity_candidates = pulled["similarity"]
    tag_matched_candidates = pulled["tag_matched"]

    # Same assembly as _run_single_line_inner(), minus any writes: tag-matched
    # buckets are trusted outright, similarity candidates go through the same
    # LLM judgment run_single_line() would use.
    judgment = await engine.judge_line(seed_bucket, similarity_candidates)
    in_line_ids = {
        v["bucket_id"] for v in judgment["bucket_verdicts"] if v.get("verdict") == "in_line"
    }
    uncertain_ids = {
        v["bucket_id"] for v in judgment["bucket_verdicts"] if v.get("verdict") == "uncertain"
    }
    tag_matched_ids = {b["id"] for b in tag_matched_candidates}

    by_id = {b["id"]: b for b in similarity_candidates}
    by_id.update({b["id"]: b for b in tag_matched_candidates})
    by_id[seed_bucket["id"]] = seed_bucket

    line_bucket_ids = sorted((in_line_ids | tag_matched_ids | {seed_bucket["id"]}) & set(by_id))
    line_buckets = [by_id[bid] for bid in line_bucket_ids]
    line_buckets.sort(key=_bucket_date)

    dump = {
        "seed_bucket_id": seed_bucket["id"],
        "specific_question": judgment.get("specific_question", ""),
        "sweep_tags_used": sweep_tags or [],
        "counts": {
            "similarity_candidates": len(similarity_candidates),
            "tag_matched_candidates": len(tag_matched_candidates),
            "uncertain_count": len(uncertain_ids),
            "line_bucket_count": len(line_buckets),
        },
        "buckets": [_bucket_record(b) for b in line_buckets],
    }

    out_path = args.out or f"line_{seed_bucket['id']}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)
    print(
        f"wrote {len(line_buckets)} buckets ({dump['counts']}) to {out_path}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    asyncio.run(main())
