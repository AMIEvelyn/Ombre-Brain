"""Read-only: list every predicate_key that appears in existing profile_fact
buckets, with example values, so a human can classify each one as
exclusive_current / multi_current / historical_event (with notes) before
migrating into facts_store.py.

Per review feedback: this script does NOT guess a mode for every predicate.
It only suggests a mode when the predicate name matches an unambiguous
keyword pattern; everything else is explicitly marked 需要人工判断 rather
than forced into a category. Writes nothing.
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
from utils import load_config, strip_wikilinks

# Only suggest a mode when the predicate name unambiguously matches --
# everything else is left for human judgement, per review feedback.
EXCLUSIVE_HINTS = re.compile(r"(height|weight|lives_in|location|address|current_|status|school|job|occupation)", re.IGNORECASE)
MULTI_HINTS = re.compile(r"(preference|like|kink|style|hobby|favorite|taste)", re.IGNORECASE)
HISTORICAL_HINTS = re.compile(r"(commitment|promise|event|anniversary|milestone|ring|marry|propose)", re.IGNORECASE)


def suggest_mode(predicate_key: str) -> str:
    if EXCLUSIVE_HINTS.search(predicate_key):
        return "建议 exclusive_current（名字像会变化的当前状态，需人工确认）"
    if MULTI_HINTS.search(predicate_key):
        return "建议 multi_current（名字像偏好/爱好，需人工确认）"
    if HISTORICAL_HINTS.search(predicate_key):
        return "建议 historical_event（名字像一次性事件，需人工确认）"
    return "无法判断，需人工看内容决定"


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buckets-dir", default="", help="Override buckets_dir from config.")
    parser.add_argument("--examples", type=int, default=3, help="How many example values to show per predicate.")
    parser.add_argument("--review-markdown", default="", help="Write a fill-in-the-blanks review table here.")
    parser.add_argument("--report", default="", help="Write the full raw inventory as JSON here.")
    args = parser.parse_args()

    config = load_config()
    if args.buckets_dir:
        config["buckets_dir"] = os.path.abspath(args.buckets_dir)

    mgr = BucketManager(config)
    buckets = await mgr.list_all(include_archive=True)

    grouped: dict[str, list[dict]] = {}
    for bucket in buckets:
        meta = bucket.get("metadata", {}) or {}
        tags = {str(t) for t in (meta.get("tags") or [])}
        if "profile_fact" not in tags:
            continue
        predicate_key = str(meta.get("predicate") or "").strip()
        if not predicate_key:
            predicate_key = "(未设置 predicate)"
        grouped.setdefault(predicate_key, []).append(
            {
                "bucket_id": bucket.get("id"),
                "subject": meta.get("subject", ""),
                "object": meta.get("object", ""),
                "date": meta.get("date", ""),
                "preview": strip_wikilinks(str(bucket.get("content", ""))).replace("\n", " ").strip()[:120],
            }
        )

    print(f"buckets_dir: {config['buckets_dir']}")
    print(f"distinct predicate_key values found: {len(grouped)}")
    print(f"total profile_fact buckets: {sum(len(v) for v in grouped.values())}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(grouped, f, ensure_ascii=False, indent=2)
        print(f"Full inventory written to {args.report}")

    if args.review_markdown:
        lines = [
            "# Profile Fact Predicate 分类表（待人工填写）",
            "",
            "mode 只能填三选一：exclusive_current / multi_current / historical_event",
            "notes 请简单写一句为什么这么分类，尤其是被标「无法判断」的项",
            "",
            "| predicate_key | count | examples (subject / object / date) | 建议 | mode（待填） | notes（待填） |",
            "| --- | ---: | --- | --- | --- | --- |",
        ]
        for predicate_key in sorted(grouped.keys()):
            items = grouped[predicate_key]
            examples = "; ".join(
                f"{item['subject']} / {item['object']} / {item['date'] or '无日期'}"
                for item in items[: max(1, args.examples)]
            )

            def cell(v):
                return str(v).replace("|", "\\|").replace("\n", " ")

            lines.append(
                "| "
                + " | ".join(
                    [
                        cell(predicate_key),
                        cell(len(items)),
                        cell(examples),
                        cell(suggest_mode(predicate_key)),
                        "",
                        "",
                    ]
                )
                + " |"
            )
        with open(args.review_markdown, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Review table (fill in mode/notes columns) written to {args.review_markdown}")


if __name__ == "__main__":
    asyncio.run(main())
