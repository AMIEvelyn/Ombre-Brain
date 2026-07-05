"""Load Lin Zhan's hand-written predicate_registry seed list into facts.sqlite.

This is a starting seed, not a final/complete set (per Lin Zhan's own note:
future migration/inventory scans will surface predicates found in real
bucket data that aren't in this list yet -- those get listed for human
confirmation, never auto-classified).

Dry-run by default; --apply to actually write.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from facts_store import PREDICATE_MODES, FactStore
from utils import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed-file",
        default=str(ROOT / "resources" / "predicate_registry_seed.json"),
        help="Path to the seed JSON (list of {predicate_key, subject_key, mode, notes}).",
    )
    parser.add_argument("--state-dir", default="", help="Override state_dir from config.")
    parser.add_argument("--apply", action="store_true", help="Actually write to facts.sqlite. Default is dry-run.")
    args = parser.parse_args()

    with open(args.seed_file, "r", encoding="utf-8") as f:
        items = json.load(f)

    valid = [item for item in items if item.get("mode") in PREDICATE_MODES]
    invalid = [item for item in items if item.get("mode") not in PREDICATE_MODES]

    # De-duplicate by predicate_key (mode/notes are per-predicate, not per-subject;
    # subject_key only matters at the individual fact level, not in the registry).
    by_key: dict[str, dict] = {}
    for item in valid:
        by_key[item["predicate_key"]] = item

    print(f"seed file: {args.seed_file}")
    print(f"total entries: {len(items)}")
    print(f"valid (recognized mode): {len(valid)} -> {len(by_key)} distinct predicate_key")
    if invalid:
        print(f"skipped (not a real predicate / invalid mode, needs human decision): "
              f"{[item['predicate_key'] for item in invalid]}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write these into predicate_registry.")
        return

    config = load_config()
    if args.state_dir:
        config["state_dir"] = os.path.abspath(args.state_dir)
    store = FactStore(config)
    for predicate_key, item in by_key.items():
        store.upsert_predicate(predicate_key, mode=item["mode"], notes=item.get("notes", ""))

    print(f"\nApplied: {len(by_key)} predicate(s) written to {store.db_path}")


if __name__ == "__main__":
    main()
