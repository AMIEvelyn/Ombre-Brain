"""Plan-only: suggest event dates for buckets with no "YYYY-MM-DD 标题" name
prefix, by matching their content against original ChatGPT/Claude-style
export JSON files (the ones with created_at / messages[].create_time).

A single conversation thread can span weeks or months, so its overall
created_at is not a reliable date for every bucket chunked out of it.
Each conversation is first split into per-local-day segments using each
message's own create_time, and buckets are matched against those day
segments rather than the whole conversation.

Pure local keyword-overlap matching (jieba if available, else regex
tokenization) -- no LLM/API calls, no cost. This script is read-only: it
never touches bucket files. It writes a review table so a human confirms
matches before anything is applied.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio

try:
    import jieba
except Exception:  # pragma: no cover
    jieba = None

from bucket_manager import BucketManager
from utils import load_config, strip_wikilinks

NAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+(.*))?$")
STOPWORDS = {
    "我", "你", "她", "他", "它", "我们", "你们", "他们", "的", "了", "是", "在", "就",
    "都", "而", "及", "与", "这", "那", "也", "还", "又", "但", "不", "没", "很", "啊",
    "吧", "呢", "吗", "哦", "嗯", "一个", "一些", "这个", "那个", "什么", "因为", "所以",
    "但是", "不是", "没有", "可以", "已经", "现在", "觉得", "感觉", "记得", "记忆",
    "自己", "今天", "时候", "这样", "那样", "一起", "之间", "关系", "事情",
}


def has_name_date(name: str) -> bool:
    return bool(NAME_DATE_RE.match(str(name or "").strip()))


def epoch_to_local_date(value, tz_offset_hours: float) -> str | None:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=tz_offset_hours)
    return dt.strftime("%Y-%m-%d")


def tokenize(text: str) -> set[str]:
    text = strip_wikilinks(text or "")
    text = re.sub(r"(?im)^###\s*.*$", " ", text)
    if jieba:
        words = jieba.cut(text)
    else:
        words = re.findall(r"[A-Za-z0-9_一-鿿]{2,}", text)
    result = set()
    for word in words:
        word = str(word).strip().lower()
        if len(word) < 2 or word in STOPWORDS:
            continue
        if not re.search(r"[a-z0-9一-鿿]", word):
            continue
        result.add(word)
    return result


def load_conversations(exports_dir: str) -> list[dict]:
    conversations = []
    for root, _, files in os.walk(exports_dir):
        for fname in files:
            if not fname.lower().endswith(".json"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                conversations.append(
                    {
                        "source_file": fname,
                        "uuid": item.get("uuid", ""),
                        "name": item.get("name", ""),
                        "created_at": item.get("created_at"),
                        "messages": item.get("messages") or [],
                        "fullTextSearch": item.get("fullTextSearch") or "",
                    }
                )
    return conversations


def build_day_segments(conversations: list[dict], tz_offset_hours: float) -> list[dict]:
    """Split each conversation into per-local-day segments using each
    message's own create_time -- a single conversation thread can span
    weeks/months, so its overall created_at is not a reliable date for
    every bucket chunked out of it. Falls back to the whole-conversation
    created_at only if no per-message timestamps are available at all.
    """
    segments = []
    for conv in conversations:
        by_date: dict[str, list[str]] = {}
        for msg in conv.get("messages", []):
            if not isinstance(msg, dict):
                continue
            date = epoch_to_local_date(msg.get("create_time"), tz_offset_hours)
            if not date:
                continue
            by_date.setdefault(date, []).append(str(msg.get("content", "")))

        if by_date:
            for date, texts in by_date.items():
                segments.append(
                    {
                        "source_file": conv["source_file"],
                        "conv_name": conv.get("name", ""),
                        "date": date,
                        "text": " ".join(texts),
                    }
                )
            continue

        # Fallback: no per-message timestamps at all, use whole-conversation created_at.
        date = epoch_to_local_date(conv.get("created_at"), tz_offset_hours)
        if date:
            text = conv.get("fullTextSearch") or " ".join(
                str(m.get("content", "")) for m in conv.get("messages", []) if isinstance(m, dict)
            )
            segments.append(
                {
                    "source_file": conv["source_file"],
                    "conv_name": conv.get("name", ""),
                    "date": date,
                    "text": text,
                }
            )
    return segments


def score(bucket_words: set[str], conv_words: set[str]) -> tuple[float, int]:
    if not bucket_words or not conv_words:
        return 0.0, 0
    overlap = len(bucket_words & conv_words)
    union = max(1, len(bucket_words | conv_words))
    return overlap / union, overlap


def markdown_cell(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buckets-dir", default="", help="Override buckets_dir from config.")
    parser.add_argument("--exports-dir", required=True, help="Directory containing original export .json files.")
    parser.add_argument("--tz-offset", type=float, default=8.0, help="Hours from UTC for date conversion (default +8, China).")
    parser.add_argument("--min-overlap", type=int, default=3, help="Minimum keyword overlap to consider a candidate.")
    parser.add_argument("--top", type=int, default=3, help="How many candidates to keep per bucket.")
    parser.add_argument("--report", default="", help="Write full candidate list as JSON to this path.")
    parser.add_argument("--review-markdown", default="", help="Write a human-review table to this path.")
    args = parser.parse_args()

    if not jieba:
        print("WARNING: jieba not installed, falling back to regex tokenization (lower match quality).")

    config = load_config()
    if args.buckets_dir:
        config["buckets_dir"] = os.path.abspath(args.buckets_dir)

    mgr = BucketManager(config)
    buckets = await mgr.list_all(include_archive=True)
    no_prefix = [b for b in buckets if not has_name_date((b.get("metadata") or {}).get("name", ""))]

    conversations = load_conversations(args.exports_dir)
    segments = build_day_segments(conversations, args.tz_offset)
    conv_indexed = [{**seg, "words": tokenize(seg["text"])} for seg in segments]

    plans = []
    for bucket in no_prefix:
        meta = bucket.get("metadata", {}) or {}
        content = str(bucket.get("content", ""))
        bucket_words = tokenize(meta.get("name", "") + " " + content)
        scored = []
        for conv in conv_indexed:
            ratio, overlap = score(bucket_words, conv["words"])
            if overlap < args.min_overlap:
                continue
            scored.append((ratio, overlap, conv))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        plans.append(
            {
                "bucket_id": bucket.get("id"),
                "name": meta.get("name", ""),
                "preview": strip_wikilinks(content).replace("\n", " ").strip()[:160],
                "candidates": [
                    {
                        "date": conv["date"],
                        "conv_name": conv["conv_name"],
                        "source_file": conv["source_file"],
                        "overlap": overlap,
                        "score": round(ratio, 4),
                    }
                    for ratio, overlap, conv in scored[: max(1, args.top)]
                ],
            }
        )

    matched = sum(1 for p in plans if p["candidates"])
    print(f"buckets_dir: {config['buckets_dir']}")
    print(f"exports_dir: {os.path.abspath(args.exports_dir)}")
    print(f"no-date-prefix buckets: {len(no_prefix)}")
    print(f"per-day conversation segments indexed from exports: {len(conv_indexed)}")
    print(f"buckets with at least one candidate match: {matched}")
    print(f"buckets with no candidate at all: {len(no_prefix) - matched}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(plans, f, ensure_ascii=False, indent=2)
        print(f"Full report written to {args.report}")

    if args.review_markdown:
        lines = [
            "# Missing-date Recovery Review",
            "",
            "只读审阅表。best_date/best_conv 是本脚本猜的候选，不是自动写入的结果。",
            "",
            "| bucket_id | name | preview | best_date | best_conv | overlap | score |",
            "| --- | --- | --- | --- | --- | ---: | ---: |",
        ]
        for plan in plans:
            best = plan["candidates"][0] if plan["candidates"] else {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(plan["bucket_id"]),
                        markdown_cell(plan["name"]),
                        markdown_cell(plan["preview"]),
                        markdown_cell(best.get("date", "")),
                        markdown_cell(best.get("conv_name", "")),
                        markdown_cell(best.get("overlap", "")),
                        markdown_cell(best.get("score", "")),
                    ]
                )
                + " |"
            )
        with open(args.review_markdown, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Review table written to {args.review_markdown}")


if __name__ == "__main__":
    asyncio.run(main())
