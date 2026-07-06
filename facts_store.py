from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

PREDICATE_MODES = {"exclusive_current", "multi_current", "historical_event"}
DEFAULT_PREDICATE_MODE = "multi_current"  # unclassified predicates default to the safest mode
EVIDENCE_TYPES = {"bucket", "raw_event", "manual", "tool"}


def make_state_key(subject_key: str, predicate_key: str) -> str:
    return f"{str(subject_key or '').strip()}:{str(predicate_key or '').strip()}"


class FactStore:
    """Facts / timeline layer -- independent of the decaying memory-bucket pool.

    Stores stable facts (with soft-invalidation per predicate_mode) and
    causal/temporal edges. Never participates in decay or emotional
    scoring; queries here are meant to be precise, not associative.
    """

    def __init__(self, config: dict):
        config = config or {}
        facts_cfg = config.get("facts", {}) if isinstance(config.get("facts", {}), dict) else {}
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.db_path = str(facts_cfg.get("db_path") or os.path.join(state_dir, "facts.sqlite"))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS predicate_registry (
                predicate_key TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'multi_current',
                display_name TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_key TEXT NOT NULL,
                predicate_key TEXT NOT NULL,
                object_text TEXT NOT NULL,
                state_key TEXT NOT NULL,
                predicate_mode TEXT NOT NULL DEFAULT 'multi_current',
                valid_at TEXT,
                invalid_at TEXT,
                confidence REAL NOT NULL DEFAULT 0.9,
                evidence_type TEXT NOT NULL DEFAULT 'bucket',
                evidence_id TEXT NOT NULL DEFAULT '',
                evidence_quote TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_state ON facts(state_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_valid ON facts(valid_at, invalid_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject_key, predicate_key)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timeline_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_fact_id INTEGER,
                from_bucket_id TEXT NOT NULL DEFAULT '',
                to_fact_id INTEGER,
                to_bucket_id TEXT NOT NULL DEFAULT '',
                relation_type TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_edges_from ON timeline_edges(from_bucket_id, from_fact_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_edges_to ON timeline_edges(to_bucket_id, to_fact_id)")
        conn.commit()
        conn.close()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # predicate_registry
    # ------------------------------------------------------------------
    def upsert_predicate(
        self,
        predicate_key: str,
        mode: str = DEFAULT_PREDICATE_MODE,
        display_name: str = "",
        notes: str = "",
    ) -> None:
        predicate_key = str(predicate_key or "").strip()
        if not predicate_key:
            return
        mode = mode if mode in PREDICATE_MODES else DEFAULT_PREDICATE_MODE
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO predicate_registry (predicate_key, mode, display_name, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(predicate_key) DO UPDATE SET
                mode = excluded.mode,
                display_name = excluded.display_name,
                notes = excluded.notes
            """,
            (predicate_key, mode, display_name, notes),
        )
        conn.commit()
        conn.close()

    def get_predicate_mode(self, predicate_key: str) -> str:
        conn = self._connect()
        row = conn.execute(
            "SELECT mode FROM predicate_registry WHERE predicate_key = ?",
            (str(predicate_key or "").strip(),),
        ).fetchone()
        conn.close()
        return row["mode"] if row else DEFAULT_PREDICATE_MODE

    def list_predicates(self) -> list[dict]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM predicate_registry ORDER BY predicate_key").fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_predicate(self, predicate_key: str) -> dict | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM predicate_registry WHERE predicate_key = ?",
            (str(predicate_key or "").strip(),),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # facts
    # ------------------------------------------------------------------
    def add_fact(
        self,
        subject_key: str,
        predicate_key: str,
        object_text: str,
        *,
        valid_at: str | None = None,
        invalid_at: str | None = None,
        confidence: float = 0.9,
        evidence_type: str = "bucket",
        evidence_id: str = "",
        evidence_quote: str = "",
        predicate_mode: str | None = None,
    ) -> int:
        subject_key = str(subject_key or "").strip()
        predicate_key = str(predicate_key or "").strip()
        if not subject_key or not predicate_key:
            raise ValueError("subject_key and predicate_key are required")
        mode = predicate_mode if predicate_mode in PREDICATE_MODES else self.get_predicate_mode(predicate_key)
        evidence_type = evidence_type if evidence_type in EVIDENCE_TYPES else "bucket"
        state_key = make_state_key(subject_key, predicate_key)

        conn = self._connect()
        if mode == "exclusive_current" and invalid_at is None:
            # New current fact supersedes any prior still-open fact under the same state_key.
            conn.execute(
                "UPDATE facts SET invalid_at = ? WHERE state_key = ? AND invalid_at IS NULL",
                (valid_at or self._now_iso(), state_key),
            )
        cursor = conn.execute(
            """
            INSERT INTO facts (
                subject_key, predicate_key, object_text, state_key, predicate_mode,
                valid_at, invalid_at, confidence, evidence_type, evidence_id, evidence_quote, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject_key, predicate_key, str(object_text or ""), state_key, mode,
                valid_at, invalid_at, max(0.0, min(1.0, float(confidence))),
                evidence_type, str(evidence_id or ""), str(evidence_quote or ""), self._now_iso(),
            ),
        )
        fact_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return fact_id

    def get_current_facts(self, subject_key: str = "", predicate_key: str = "") -> list[dict]:
        clauses = ["invalid_at IS NULL"]
        params: list[Any] = []
        if subject_key:
            clauses.append("subject_key = ?")
            params.append(subject_key)
        if predicate_key:
            clauses.append("predicate_key = ?")
            params.append(predicate_key)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM facts WHERE {' AND '.join(clauses)} ORDER BY valid_at DESC",
            params,
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_facts_at(self, at_date: str, subject_key: str = "") -> list[dict]:
        clauses = ["(valid_at IS NULL OR valid_at <= ?)", "(invalid_at IS NULL OR invalid_at > ?)"]
        params: list[Any] = [at_date, at_date]
        if subject_key:
            clauses.append("subject_key = ?")
            params.append(subject_key)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM facts WHERE {' AND '.join(clauses)} ORDER BY valid_at DESC",
            params,
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_fact_history(self, subject_key: str, predicate_key: str) -> list[dict]:
        state_key = make_state_key(subject_key, predicate_key)
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM facts WHERE state_key = ? ORDER BY valid_at ASC",
            (state_key,),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def delete_fact(self, fact_id: int) -> bool:
        conn = self._connect()
        cursor = conn.execute("DELETE FROM facts WHERE id = ?", (int(fact_id),))
        conn.commit()
        deleted = cursor.rowcount > 0
        conn.close()
        return deleted

    # ------------------------------------------------------------------
    # timeline_edges
    # ------------------------------------------------------------------
    def add_timeline_edge(
        self,
        relation_type: str,
        *,
        from_fact_id: int | None = None,
        from_bucket_id: str = "",
        to_fact_id: int | None = None,
        to_bucket_id: str = "",
        confidence: float = 0.5,
    ) -> int:
        conn = self._connect()
        cursor = conn.execute(
            """
            INSERT INTO timeline_edges (
                from_fact_id, from_bucket_id, to_fact_id, to_bucket_id, relation_type, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                from_fact_id, str(from_bucket_id or ""), to_fact_id, str(to_bucket_id or ""),
                str(relation_type or "").strip(), max(0.0, min(1.0, float(confidence))), self._now_iso(),
            ),
        )
        edge_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return edge_id
