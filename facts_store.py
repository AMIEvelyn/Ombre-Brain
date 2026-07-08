from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

PREDICATE_MODES = {"exclusive_current", "multi_current", "historical_event"}
DEFAULT_PREDICATE_MODE = "multi_current"  # unclassified predicates default to the safest mode
EVIDENCE_TYPES = {"bucket", "raw_event", "manual", "tool"}
FACT_BUCKET_RELATION_TYPES = {"evidence", "origin", "related"}
DEFAULT_FACT_BUCKET_RELATION_TYPE = "evidence"
# No hard cap at the DB layer -- a fact can link arbitrarily many buckets
# (batch import may need to attach dozens). These are advisory soft limits
# surfaced to the caller as a warning, never enforced as a block: evidence
# should stay tight (too many and you can't tell what actually backs the
# fact), origin is almost always a single bucket, related can grow long
# and is expected to be paginated/collapsed by the UI instead of capped.
FACT_BUCKET_RELATION_SOFT_LIMITS = {"evidence": 5, "origin": 3, "related": 20}
_FACT_BUCKET_RELATION_PRIORITY = {"origin": 0, "evidence": 1, "related": 2}


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
                created_at TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                attachments TEXT NOT NULL DEFAULT '[]',
                tags TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        # Backfill columns for databases created before title/content/attachments/tags
        # existed -- SQLite has no "ADD COLUMN IF NOT EXISTS", so probe and ignore
        # the "duplicate column" error on databases that already have them.
        for column, ddl in (
            ("title", "ALTER TABLE facts ADD COLUMN title TEXT NOT NULL DEFAULT ''"),
            ("content", "ALTER TABLE facts ADD COLUMN content TEXT NOT NULL DEFAULT ''"),
            ("attachments", "ALTER TABLE facts ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"),
            ("tags", "ALTER TABLE facts ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"),
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fact_bucket_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id INTEGER NOT NULL,
                bucket_id TEXT NOT NULL,
                moment_id TEXT NOT NULL DEFAULT '',
                relation_type TEXT NOT NULL DEFAULT 'evidence',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(fact_id, bucket_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_bucket_links_fact ON fact_bucket_links(fact_id)")
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
        title: str = "",
        content: str = "",
        attachments: list[dict] | None = None,
        tags: list[str] | None = None,
    ) -> int:
        subject_key = str(subject_key or "").strip()
        predicate_key = str(predicate_key or "").strip()
        if not subject_key or not predicate_key:
            raise ValueError("subject_key and predicate_key are required")
        mode = predicate_mode if predicate_mode in PREDICATE_MODES else self.get_predicate_mode(predicate_key)
        evidence_type = evidence_type if evidence_type in EVIDENCE_TYPES else "bucket"
        state_key = make_state_key(subject_key, predicate_key)
        attachments_json = json.dumps(attachments or [], ensure_ascii=False)
        tags_json = json.dumps(tags or [], ensure_ascii=False)

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
                valid_at, invalid_at, confidence, evidence_type, evidence_id, evidence_quote, created_at,
                title, content, attachments, tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject_key, predicate_key, str(object_text or ""), state_key, mode,
                valid_at, invalid_at, max(0.0, min(1.0, float(confidence))),
                evidence_type, str(evidence_id or ""), str(evidence_quote or ""), self._now_iso(),
                str(title or ""), str(content or ""), attachments_json, tags_json,
            ),
        )
        fact_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return fact_id

    def update_fact(
        self,
        fact_id: int,
        *,
        object_text: str | None = None,
        title: str | None = None,
        content: str | None = None,
        attachments: list[dict] | None = None,
        tags: list[str] | None = None,
    ) -> bool:
        """Edit a fact in place -- does not touch valid_at/invalid_at and does
        not create a new timeline point. Only fields explicitly passed (not
        None) are changed."""
        updates: dict[str, Any] = {}
        if object_text is not None:
            updates["object_text"] = str(object_text)
        if title is not None:
            updates["title"] = str(title)
        if content is not None:
            updates["content"] = str(content)
        if attachments is not None:
            updates["attachments"] = json.dumps(attachments, ensure_ascii=False)
        if tags is not None:
            updates["tags"] = json.dumps(tags, ensure_ascii=False)
        if not updates:
            return False
        conn = self._connect()
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        cursor = conn.execute(
            f"UPDATE facts SET {set_clause} WHERE id = ?",
            (*updates.values(), int(fact_id)),
        )
        conn.commit()
        updated = cursor.rowcount > 0
        conn.close()
        return updated

    def invalidate_fact(self, fact_id: int, invalid_at: str | None = None) -> bool:
        """Manually mark a fact as no longer true, with no replacement value
        (unlike add_fact's automatic supersede-on-new-value for exclusive_current).
        No-op if the fact is already invalidated."""
        conn = self._connect()
        cursor = conn.execute(
            "UPDATE facts SET invalid_at = ? WHERE id = ? AND invalid_at IS NULL",
            (invalid_at or self._now_iso(), int(fact_id)),
        )
        conn.commit()
        updated = cursor.rowcount > 0
        conn.close()
        return updated

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["attachments"] = json.loads(item.get("attachments") or "[]")
        except (TypeError, ValueError):
            item["attachments"] = []
        try:
            item["tags"] = json.loads(item.get("tags") or "[]")
        except (TypeError, ValueError):
            item["tags"] = []
        return item

    def get_fact(self, fact_id: int) -> dict | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (int(fact_id),)).fetchone()
        conn.close()
        return self._row_to_fact(row) if row else None

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
        return [self._row_to_fact(row) for row in rows]

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
        return [self._row_to_fact(row) for row in rows]

    def get_fact_history(self, subject_key: str, predicate_key: str) -> list[dict]:
        state_key = make_state_key(subject_key, predicate_key)
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM facts WHERE state_key = ? ORDER BY valid_at ASC",
            (state_key,),
        ).fetchall()
        conn.close()
        return [self._row_to_fact(row) for row in rows]

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

    # ------------------------------------------------------------------
    # fact_bucket_links -- many-to-many links between a fact/card and the
    # memory buckets that back it (evidence / origin / related). Separate
    # from facts.evidence_id, which stays as the single auto-attributed
    # source recorded by add_fact()/profile_fact(); this table is for the
    # facts-card UI's manually curated, possibly-multiple bucket links.
    # ------------------------------------------------------------------
    def count_bucket_links(self, fact_id: int, relation_type: str = "") -> int:
        conn = self._connect()
        if relation_type:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM fact_bucket_links WHERE fact_id = ? AND relation_type = ?",
                (int(fact_id), relation_type),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM fact_bucket_links WHERE fact_id = ?",
                (int(fact_id),),
            ).fetchone()
        conn.close()
        return int(row["n"]) if row else 0

    def count_bucket_links_by_relation(self, fact_id: int) -> dict[str, int]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT relation_type, COUNT(*) AS n FROM fact_bucket_links WHERE fact_id = ? GROUP BY relation_type",
            (int(fact_id),),
        ).fetchall()
        conn.close()
        return {row["relation_type"]: int(row["n"]) for row in rows}

    def get_bucket_links(
        self,
        fact_id: int,
        *,
        relation_type: str = "",
        limit: int = 0,
        offset: int = 0,
    ) -> list[dict]:
        """relation_type filters to one type; limit=0 means no limit. Results
        are sorted origin -> evidence -> related, newest-first within each
        type, so callers that just want "the top few" can slice the front
        without re-sorting (matches the UI's "show 3 most important" default)."""
        clauses = ["fact_id = ?"]
        params: list[Any] = [int(fact_id)]
        if relation_type:
            clauses.append("relation_type = ?")
            params.append(relation_type)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM fact_bucket_links WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        ).fetchall()
        conn.close()
        items = [dict(row) for row in rows]
        items.sort(key=lambda item: _FACT_BUCKET_RELATION_PRIORITY.get(item["relation_type"], 9))
        if limit:
            items = items[offset : offset + limit]
        elif offset:
            items = items[offset:]
        return items

    def add_bucket_link(
        self,
        fact_id: int,
        bucket_id: str,
        *,
        moment_id: str = "",
        relation_type: str = DEFAULT_FACT_BUCKET_RELATION_TYPE,
        note: str = "",
    ) -> tuple[int, str | None]:
        """Link a memory bucket to a fact/card. Idempotent -- linking the same
        (fact_id, bucket_id) pair twice returns the existing link's id instead
        of erroring or duplicating.

        No hard cap: the DB layer allows arbitrarily many links per fact (batch
        import may need dozens under relation_type="related"). Returns
        (link_id, soft_limit_warning) -- warning is a human-readable string
        when this relation_type's count now exceeds its recommended soft
        limit (FACT_BUCKET_RELATION_SOFT_LIMITS), or None otherwise. The
        caller decides what to do with the warning (e.g. surface it in the
        API response); the link is created either way."""
        fact_id = int(fact_id)
        bucket_id = str(bucket_id or "").strip()
        if not bucket_id:
            raise ValueError("bucket_id is required")
        relation_type = relation_type if relation_type in FACT_BUCKET_RELATION_TYPES else DEFAULT_FACT_BUCKET_RELATION_TYPE

        conn = self._connect()
        existing = conn.execute(
            "SELECT id FROM fact_bucket_links WHERE fact_id = ? AND bucket_id = ?",
            (fact_id, bucket_id),
        ).fetchone()
        if existing:
            conn.close()
            return int(existing["id"]), None

        cursor = conn.execute(
            """
            INSERT INTO fact_bucket_links (fact_id, bucket_id, moment_id, relation_type, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fact_id, bucket_id, str(moment_id or ""), relation_type, str(note or ""), self._now_iso()),
        )
        link_id = cursor.lastrowid
        new_count = conn.execute(
            "SELECT COUNT(*) AS n FROM fact_bucket_links WHERE fact_id = ? AND relation_type = ?",
            (fact_id, relation_type),
        ).fetchone()["n"]
        conn.commit()
        conn.close()

        soft_limit = FACT_BUCKET_RELATION_SOFT_LIMITS.get(relation_type)
        warning = None
        if soft_limit and new_count > soft_limit:
            warning = f"这张卡的 {relation_type} 已有 {new_count} 个，超过建议的 {soft_limit} 个软上限，建议精简或改归类到 related"
        return link_id, warning

    def remove_bucket_link(self, link_id: int) -> bool:
        conn = self._connect()
        cursor = conn.execute("DELETE FROM fact_bucket_links WHERE id = ?", (int(link_id),))
        conn.commit()
        removed = cursor.rowcount > 0
        conn.close()
        return removed
