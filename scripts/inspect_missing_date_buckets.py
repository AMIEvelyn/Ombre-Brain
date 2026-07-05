"""Read-only diagnostic for buckets with no "YYYY-MM-DD 标题" name prefix.

Checks whether these buckets still carry a usable original timestamp from
import (import_timestamp_start / import_timestamp_end in metadata), which
could let us bulk-recover event dates instead of manual entry. Writes
nothing; only prints/reports counts and a few example values.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio

from bucket_manager import BucketManager
from utils import load_config

NAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+(.*))?$")


def has_name_date(name: str) -> bool:
    return bool(NAME_DATE_RE.match(str(name or "").strip()))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buckets-dir", default="", help="Override buckets_dir from config.")
    parser.add_argument("--examples", type=int, default=5, help="How many example rows to print per group.")
    parser.add_argument("--report", default="", help="Write full details as JSON to this path.")
    args = parser.parse_args()

    config = load_config()
    if args.buckets_dir:
        config["buckets_dir"] = os.path.abspath(args.buckets_dir)

    mgr = BucketManager(config)
    buckets = await mgr.list_all(include_archive=True)

    no_prefix = [b for b in buckets if not has_name_date((b.get("metadata") or {}).get("name", ""))]

    with_start_ts = []
    with_end_ts = []
    with_created_only = []

    for bucket in no_prefix:
        meta = bucket.get("metadata", {}) or {}
        row = {
            "id": bucket.get("id"),
            "name": meta.get("name", ""),
            "import_timestamp_start": meta.get("import_timestamp_start"),
            "import_timestamp_end": meta.get("import_timestamp_end"),
            "created": meta.get("created"),
            "source": meta.get("source"),
        }
        if meta.get("import_timestamp_start"):
            with_start_ts.append(row)
        elif meta.get("import_timestamp_end"):
            with_end_ts.append(row)
        else:
            with_created_only.append(row)

    print(f"buckets_dir: {config['buckets_dir']}")
    print(f"no-date-prefix buckets total: {len(no_prefix)}")
    print(f"  have import_timestamp_start: {len(with_start_ts)}")
    print(f"  have import_timestamp_end only: {len(with_end_ts)}")
    print(f"  have neither (only created/import time): {len(with_created_only)}")

    def show(label, rows):
        if not rows:
            return
        print(f"\n{label} (showing up to {args.examples}):")
        for row in rows[: args.examples]:
            print(f"  id={row['id']} name={row['name']!r} start={row['import_timestamp_start']!r} "
                  f"end={row['import_timestamp_end']!r} created={row['created']!r} source={row['source']!r}")

    show("Examples with import_timestamp_start", with_start_ts)
    show("Examples with import_timestamp_end only", with_end_ts)
    show("Examples with neither (fallback would be created/import time)", with_created_only)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "with_start_ts": with_start_ts,
                    "with_end_ts": with_end_ts,
                    "with_created_only": with_created_only,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\nFull report written to {args.report}")


if __name__ == "__main__":
    asyncio.run(main())
