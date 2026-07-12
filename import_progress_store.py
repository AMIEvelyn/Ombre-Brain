"""Batch-import progress storage. See docs/batch-import-design.md §4.

Tracks which buckets have already been swept by a "line" (so the sweep
doesn't pick them as a new starting point again), the lines themselves
(candidate cards -- never auto-written to CardStore), and pending
associations that couldn't be confidently judged yet.

Self-contained like cards_store.py: stdlib sqlite3 only, doesn't touch
bucket files, doesn't depend on the rest of the project.

"Swept" only blocks a bucket from being picked as a *new* starting point.
It does not block the bucket from being cited as evidence by some other,
later line -- see docs/batch-import-design.md §4.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone


def _gen_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:12]


class ImportProgressStore:
    def __init__(self, config: dict | None = None, *, db_path: str = ""):
        config = config or {}
        if not db_path:
            cfg = config.get("batch_import", {}) if isinstance(config.get("batch_import"), dict) else {}
            state_dir = config.get("state_dir") or os.path.join(
                os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
                "state",
            )
            db_path = str(cfg.get("progress_db_path") or os.path.join(state_dir, "import_progress.sqlite"))
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bucket_sweep (
                bucket_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'swept',
                swept_by_line_id TEXT NOT NULL DEFAULT '',
                swept_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS lines (
                id TEXT PRIMARY KEY,
                seed_bucket_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                bucket_ids TEXT NOT NULL DEFAULT '[]',
                candidate_card TEXT NOT NULL DEFAULT '{}',
                reasoning TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_associations (
                id TEXT PRIMARY KEY,
                bucket_ids TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # Sweep status
    # ------------------------------------------------------------------
    def is_swept(self, bucket_id: str) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM bucket_sweep WHERE bucket_id = ?", (bucket_id,)
        ).fetchone()
        conn.close()
        return row is not None

    def mark_swept(self, bucket_ids: list[str], line_id: str) -> None:
        now = self._now_iso()
        conn = self._connect()
        for bid in bucket_ids:
            conn.execute(
                """
                INSERT INTO bucket_sweep (bucket_id, status, swept_by_line_id, swept_at)
                VALUES (?, 'swept', ?, ?)
                ON CONFLICT(bucket_id) DO UPDATE SET
                    swept_by_line_id = excluded.swept_by_line_id,
                    swept_at = excluded.swept_at
                """,
                (bid, line_id, now),
            )
        conn.commit()
        conn.close()

    def next_unswept_bucket_id(self, ordered_bucket_ids: list[str]) -> str | None:
        """`ordered_bucket_ids` is whatever order the caller wants to sweep in
        (e.g. chronological). This store only tracks status, not ordering."""
        conn = self._connect()
        swept_ids = {
            r["bucket_id"] for r in conn.execute("SELECT bucket_id FROM bucket_sweep")
        }
        conn.close()
        for bid in ordered_bucket_ids:
            if bid not in swept_ids:
                return bid
        return None

    # ------------------------------------------------------------------
    # Lines (candidate cards)
    # ------------------------------------------------------------------
    def save_line(
        self,
        *,
        seed_bucket_id: str,
        status: str,
        bucket_ids: list[str],
        candidate_card: dict,
        reasoning: str,
        confidence: float,
    ) -> str:
        line_id = _gen_id("L")
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO lines
                (id, seed_bucket_id, status, bucket_ids, candidate_card, reasoning, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                line_id, seed_bucket_id, status,
                json.dumps(bucket_ids, ensure_ascii=False),
                json.dumps(candidate_card, ensure_ascii=False),
                reasoning, float(confidence), self._now_iso(),
            ),
        )
        conn.commit()
        conn.close()
        return line_id

    def list_lines(self, status: str | None = None) -> list[dict]:
        conn = self._connect()
        if status:
            rows = conn.execute(
                "SELECT * FROM lines WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM lines ORDER BY created_at").fetchall()
        conn.close()
        out = []
        for row in rows:
            d = dict(row)
            d["bucket_ids"] = json.loads(d["bucket_ids"] or "[]")
            d["candidate_card"] = json.loads(d["candidate_card"] or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # Pending associations ("拿不准", waiting for more evidence)
    # ------------------------------------------------------------------
    def save_pending_association(self, bucket_ids: list[str], reason: str) -> str:
        pending_id = _gen_id("P")
        conn = self._connect()
        conn.execute(
            "INSERT INTO pending_associations (id, bucket_ids, reason, created_at) VALUES (?, ?, ?, ?)",
            (pending_id, json.dumps(bucket_ids, ensure_ascii=False), reason, self._now_iso()),
        )
        conn.commit()
        conn.close()
        return pending_id

    def list_pending_associations(self) -> list[dict]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM pending_associations ORDER BY created_at").fetchall()
        conn.close()
        out = []
        for row in rows:
            d = dict(row)
            d["bucket_ids"] = json.loads(d["bucket_ids"] or "[]")
            out.append(d)
        return out
