"""Resolve missing bucket dates using creation-order neighbor bounds,
cross-checked against the keyword-match candidates from
plan_missing_date_recovery.py.

Why this works: import_memory.py creates buckets strictly sequentially
(no concurrency), and buckets were imported in chronological content
order. So a bucket with no date must have a real event date that falls
between the dates of its nearest already-dated neighbors in creation
order -- that's a hard structural bound, independent of any text
matching.

Classification per dateless bucket:
  - neighbor_exact: both neighbors already resolved to the *same* date
    -> use it directly, keyword guess is irrelevant.
  - keyword_confirmed_by_bound: neighbor bound exists and the keyword
    match's top candidate falls inside it -> use the keyword date.
  - conflict: keyword match's top candidate falls OUTSIDE the neighbor
    bound -> do not resolve automatically, flag for manual review.
  - bound_only: a neighbor bound exists (lower != upper) but there is no
    keyword candidate (or it conflicts) -> not auto-resolved, range shown
    for manual judgement.
  - unresolved: no usable bound and no keyword candidate (rare, usually
    only possible at the very start/end of the whole dataset).

Read-only by default (--apply required to write). Only "neighbor_exact"
and "keyword_confirmed_by_bound" are ever written automatically.
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


def has_name_date(name: str) -> bool:
    return bool(NAME_DATE_RE.match(str(name or "").strip()))


def valid_date(value) -> str | None:
    text = str(value or "").strip()[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def parse_created(value) -> str:
    # ISO-ish created timestamps sort correctly as plain strings; fall back
    # to empty string (sorts first) if missing/unparseable.
    return str(value or "")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buckets-dir", default="", help="Override buckets_dir from config.")
    parser.add_argument("--candidates-report", required=True, help="The --report JSON from plan_missing_date_recovery.py.")
    parser.add_argument("--apply", action="store_true", help="Actually write resolved dates. Default is dry-run.")
    parser.add_argument("--backup-dir", default="", help="Copy each modified bucket .md file here before writing.")
    parser.add_argument("--report", default="", help="Write the full classification as JSON to this path.")
    parser.add_argument("--review-markdown", default="", help="Write conflict/bound_only/unresolved rows as a review table.")
    args = parser.parse_args()

    config = load_config()
    if args.buckets_dir:
        config["buckets_dir"] = os.path.abspath(args.buckets_dir)

    mgr = BucketManager(config)
    buckets = await mgr.list_all(include_archive=True)

    with open(args.candidates_report, "r", encoding="utf-8") as f:
        candidate_plans = json.load(f)
    top_candidate = {}
    for plan in candidate_plans:
        candidates = plan.get("candidates") or []
        if candidates:
            top_candidate[plan["bucket_id"]] = candidates[0]

    # Sort in true creation order (import_memory.py writes sequentially).
    ordered = sorted(buckets, key=lambda b: parse_created((b.get("metadata") or {}).get("created")))

    resolved_date_at = {}
    dateless_ids = set()
    for bucket in ordered:
        meta = bucket.get("metadata", {}) or {}
        bucket_id = bucket.get("id")
        name = meta.get("name", "")
        existing = valid_date(meta.get("date"))
        if existing and has_name_date(name):
            resolved_date_at[bucket_id] = existing
        elif not has_name_date(name):
            dateless_ids.add(bucket_id)
        elif existing:
            resolved_date_at[bucket_id] = existing

    n = len(ordered)
    results = {"neighbor_exact": [], "keyword_confirmed_by_bound": [], "conflict": [], "bound_only": [], "unresolved": []}

    for index, bucket in enumerate(ordered):
        bucket_id = bucket.get("id")
        if bucket_id not in dateless_ids:
            continue
        meta = bucket.get("metadata", {}) or {}

        lower = None
        for j in range(index - 1, -1, -1):
            candidate_id = ordered[j].get("id")
            if candidate_id in resolved_date_at:
                lower = resolved_date_at[candidate_id]
                break
        upper = None
        for j in range(index + 1, n):
            candidate_id = ordered[j].get("id")
            if candidate_id in resolved_date_at:
                upper = resolved_date_at[candidate_id]
                break

        row = {
            "bucket_id": bucket_id,
            "name": meta.get("name", ""),
            "lower_bound": lower,
            "upper_bound": upper,
            "keyword_candidate": (top_candidate.get(bucket_id) or {}).get("date"),
            "keyword_overlap": (top_candidate.get(bucket_id) or {}).get("overlap"),
            "keyword_conv_name": (top_candidate.get(bucket_id) or {}).get("conv_name"),
        }

        if lower and upper and lower == upper:
            row["resolved_date"] = lower
            results["neighbor_exact"].append(row)
            continue

        kw_date = row["keyword_candidate"]
        in_bound = kw_date and (lower is None or kw_date >= lower) and (upper is None or kw_date <= upper)

        if kw_date and in_bound:
            row["resolved_date"] = kw_date
            results["keyword_confirmed_by_bound"].append(row)
        elif kw_date and not in_bound and (lower or upper):
            results["conflict"].append(row)
        elif lower or upper:
            results["bound_only"].append(row)
        else:
            results["unresolved"].append(row)

    print(f"buckets_dir: {config['buckets_dir']}")
    print(f"total dateless buckets: {len(dateless_ids)}")
    for key in ("neighbor_exact", "keyword_confirmed_by_bound", "conflict", "bound_only", "unresolved"):
        print(f"  {key}: {len(results[key])}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\nFull report written to {args.report}")

    if args.review_markdown:
        lines = [
            "# Missing-date Neighbor Cross-check Review",
            "",
            "只列出没有自动解决的行（conflict / bound_only / unresolved），需要人工看一眼。",
            "",
            "| status | bucket_id | name | lower_bound | upper_bound | keyword_candidate | overlap | keyword_conv |",
            "| --- | --- | --- | --- | --- | --- | ---: | --- |",
        ]
        for status in ("conflict", "bound_only", "unresolved"):
            for row in results[status]:
                def cell(v):
                    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            cell(status), cell(row["bucket_id"]), cell(row["name"]),
                            cell(row["lower_bound"]), cell(row["upper_bound"]),
                            cell(row["keyword_candidate"]), cell(row["keyword_overlap"]), cell(row["keyword_conv_name"]),
                        ]
                    )
                    + " |"
                )
        with open(args.review_markdown, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Review table (needs-attention rows only) written to {args.review_markdown}")

    to_apply = results["neighbor_exact"] + results["keyword_confirmed_by_bound"]
    if not args.apply:
        print(f"\nDry-run only. Re-run with --apply to write {len(to_apply)} resolved date(s).")
        print(f"({len(results['conflict']) + len(results['bound_only']) + len(results['unresolved'])} bucket(s) left for manual review/later.)")
        return

    if args.backup_dir:
        os.makedirs(args.backup_dir, exist_ok=True)

    updated = 0
    failed = []
    for row in to_apply:
        bucket_id = row["bucket_id"]
        if args.backup_dir:
            src_path = mgr._find_bucket_file(bucket_id)
            if src_path:
                shutil.copy2(src_path, os.path.join(args.backup_dir, os.path.basename(src_path)))
        ok = await mgr.update(bucket_id, date=row["resolved_date"])
        if ok:
            updated += 1
        else:
            failed.append(bucket_id)

    print(f"\nApplied: {updated} bucket(s) updated.")
    if failed:
        print(f"Failed to update {len(failed)} bucket(s): {failed}")


if __name__ == "__main__":
    asyncio.run(main())
