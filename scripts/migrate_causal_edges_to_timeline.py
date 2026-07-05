"""Copy existing causal/temporal edges from memory_edges.jsonl into
facts.sqlite's timeline_edges table.

Only the relation types with clear causal/temporal meaning are migrated
(causes, triggers, precedes) -- the more associative/semantic relation
types (relates_to, emotional_echo, supports, ...) stay in OB's normal
diffusion graph; they belong to the "heart", not the "skeleton".

Pure local copy, no AI calls, no data loss (memory_edges.jsonl is left
untouched -- this only adds rows to timeline_edges). Safe to re-run:
skips edges already migrated (same source/target/relation_type).
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from facts_store import FactStore
from memory_edges import MemoryEdgeStore
from utils import load_config

CAUSAL_RELATION_TYPES = {"causes", "triggers", "precedes"}


def already_migrated(store: FactStore, from_bucket_id: str, to_bucket_id: str, relation_type: str) -> bool:
    conn = store._connect()
    row = conn.execute(
        """
        SELECT 1 FROM timeline_edges
        WHERE from_bucket_id = ? AND to_bucket_id = ? AND relation_type = ?
        LIMIT 1
        """,
        (from_bucket_id, to_bucket_id, relation_type),
    ).fetchone()
    conn.close()
    return row is not None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buckets-dir", default="", help="Override buckets_dir from config (used to locate state_dir).")
    parser.add_argument("--apply", action="store_true", help="Actually write to timeline_edges. Default is dry-run.")
    args = parser.parse_args()

    config = load_config()
    if args.buckets_dir:
        config["buckets_dir"] = os.path.abspath(args.buckets_dir)

    edge_store = MemoryEdgeStore(config)
    all_edges = edge_store.list_edges()
    causal_edges = [e for e in all_edges if e.get("relation_type") in CAUSAL_RELATION_TYPES]

    print(f"total memory_edges: {len(all_edges)}")
    print(f"causal/temporal edges ({', '.join(sorted(CAUSAL_RELATION_TYPES))}): {len(causal_edges)}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write these into timeline_edges.")
        return

    fact_store = FactStore(config)
    inserted, skipped = 0, 0
    for edge in causal_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        relation_type = str(edge.get("relation_type") or "")
        if not source or not target or already_migrated(fact_store, source, target, relation_type):
            skipped += 1
            continue
        fact_store.add_timeline_edge(
            relation_type,
            from_bucket_id=source,
            to_bucket_id=target,
            confidence=float(edge.get("confidence", 0.5)),
        )
        inserted += 1

    print(f"\nApplied: {inserted} edge(s) inserted, {skipped} skipped (already migrated or missing id).")


if __name__ == "__main__":
    main()
