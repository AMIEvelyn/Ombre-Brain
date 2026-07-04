"""Backfill bucket metadata `date` (event date) from a "YYYY-MM-DD 标题" name prefix.

Buckets created via bulk import never got the formal `date` field filled in,
even when the bucket name already carries the real event date as a prefix
(e.g. "2026-07-11 一起去逛街"). Gateway's Date Recall matching/sorting prefers
`date` when present but silently falls back to created/updated_at/last_active
(i.e. import time) when it's empty — which misdates historical bulk-imported
memories. This script promotes the name-prefix date into the real `date`
field so Date Recall and any future timeline feature read the correct day.

Default is dry-run: it only prints/writes a report. Pass --apply to write.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio

from bucket_manager import BucketManager
from utils import load_config

NAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+(.*))?$")


def extract_name_date(name: str) -> str | None:
    match = NAME_DATE_RE.match(str(name or "").strip())
    if not match:
        return None
    date_text = match.group(1)
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return None
    return date_text


def classify_buckets(buckets: list[dict]) -> dict:
    to_backfill = []
    already_set = []
    conflicts = []
    no_prefix = []

    for bucket in buckets:
        meta = bucket.get("metadata", {}) or {}
        name = meta.get("name", "")
        existing_date = str(meta.get("date") or "").strip()
        name_date = extract_name_date(name)

        if not name_date:
            no_prefix.append({"id": bucket.get("id"), "name": name})
            continue

        if not existing_date:
            to_backfill.append({"id": bucket.get("id"), "name": name, "date": name_date})
        elif existing_date == name_date:
            already_set.append({"id": bucket.get("id"), "name": name, "date": existing_date})
        else:
            conflicts.append(
                {
                    "id": bucket.get("id"),
                    "name": name,
                    "existing_date": existing_date,
                    "name_date": name_date,
                }
            )

    return {
        "to_backfill": to_backfill,
        "already_set": already_set,
        "conflicts": conflicts,
        "no_prefix": no_prefix,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buckets-dir", default="", help="Override buckets_dir from config.")
    parser.add_argument("--apply", action="store_true", help="Actually write the date field. Default is dry-run.")
    parser.add_argument("--backup-dir", default="", help="Copy each modified bucket .md file here before writing.")
    parser.add_argument("--report", default="", help="Write the full classification report as JSON to this path.")
    args = parser.parse_args()

    config = load_config()
    if args.buckets_dir:
        config["buckets_dir"] = os.path.abspath(args.buckets_dir)

    mgr = BucketManager(config)
    buckets = await mgr.list_all(include_archive=True)
    report = classify_buckets(buckets)

    print(f"buckets_dir: {config['buckets_dir']}")
    print(f"total buckets: {len(buckets)}")
    print(f"  already correct (name date == metadata date): {len(report['already_set'])}")
    print(f"  to backfill (name has date, metadata.date empty): {len(report['to_backfill'])}")
    print(f"  conflicts (name date != metadata date, skipped): {len(report['conflicts'])}")
    print(f"  no date prefix in name: {len(report['no_prefix'])}")

    if report["conflicts"]:
        print("\nCONFLICTS (not touched, review manually):")
        for item in report["conflicts"]:
            print(f"  {item['id']}: name={item['name']!r} metadata.date={item['existing_date']!r}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nFull report written to {args.report}")

    if not args.apply:
        print(f"\nDry-run only. Re-run with --apply to write {len(report['to_backfill'])} date field(s).")
        return

    if args.backup_dir:
        os.makedirs(args.backup_dir, exist_ok=True)

    updated = 0
    failed = []
    for item in report["to_backfill"]:
        bucket_id = item["id"]
        if args.backup_dir:
            src_path = mgr._find_bucket_file(bucket_id)
            if src_path:
                shutil.copy2(src_path, os.path.join(args.backup_dir, os.path.basename(src_path)))
        ok = await mgr.update(bucket_id, date=item["date"])
        if ok:
            updated += 1
        else:
            failed.append(bucket_id)

    print(f"\nApplied: {updated} bucket(s) updated.")
    if failed:
        print(f"Failed to update {len(failed)} bucket(s): {failed}")


if __name__ == "__main__":
    asyncio.run(main())
