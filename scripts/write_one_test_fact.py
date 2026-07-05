"""Manually write exactly one fact into facts.sqlite, for the closed-loop
verification test (write -> queryable via fact_lookup MCP tool).

This bypasses profile_fact entirely -- it's meant as a plumbing test, not
a real data-entry path.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from facts_store import PREDICATE_MODES, FactStore
from utils import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subject_key", help="e.g. yi_lan / lin_zhan / relationship")
    parser.add_argument("predicate_key", help="e.g. height, food_preference, ring")
    parser.add_argument("object_text", help="the value, e.g. '165cm'")
    parser.add_argument("--valid-at", default="", help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--state-dir", default="", help="Override state_dir from config.")
    args = parser.parse_args()

    config = load_config()
    if args.state_dir:
        config["state_dir"] = os.path.abspath(args.state_dir)

    store = FactStore(config)
    mode = store.get_predicate_mode(args.predicate_key)
    fact_id = store.add_fact(
        args.subject_key,
        args.predicate_key,
        args.object_text,
        valid_at=args.valid_at or None,
        evidence_type="manual",
        evidence_id="closed_loop_test",
        evidence_quote="手动写入的闭环测试事实",
    )
    print(f"written: fact_id={fact_id} mode={mode} (predicate_registry 里{'已登记' if mode != 'multi_current' or args.predicate_key in [p['predicate_key'] for p in store.list_predicates()] else '未登记，按默认 multi_current 处理'})")
    print(f"db: {store.db_path}")


if __name__ == "__main__":
    main()
