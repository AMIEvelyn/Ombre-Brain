"""Facts-model v2: collection/playlist-style cards + nestable folders.

See docs/facts-model-v2-collection-redesign.md. This is the ground-floor data
layer for the redesigned facts skeleton and is intentionally self-contained:
it depends on nothing in the project (stdlib sqlite3 only), touches no existing
table, and is not yet wired into server.py / gateway.py -- so it cannot affect
the running deployment. The old facts_store.py / facts.sqlite are left alone.

Model in one line: a card lives once (stable id + its own revision timeline),
and is filed into any number of nestable folders. The latest revision (by
valid_at) is the card's current state; older revisions are history. There is
no invalidation flag and no per-predicate mode -- "current vs history" is just
"newest revision vs the rest".

  cards          -- identity + birth time
  card_revisions -- the timeline; newest valid_at = current state
  folders        -- a tree; parent_id='' is a top-level folder (= a subject)
  card_folders   -- many-to-many card <-> folder membership (like a playlist)
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


def _gen_id(prefix: str) -> str:
    """Stable short id. Cards use 'F' (per Yi Lan's request), folders 'D'."""
    return prefix + uuid.uuid4().hex[:12]


class CardStore:
    def __init__(self, config: dict | None = None, *, db_path: str = ""):
        config = config or {}
        if not db_path:
            cards_cfg = config.get("cards", {}) if isinstance(config.get("cards", {}), dict) else {}
            state_dir = config.get("state_dir") or os.path.join(
                os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
                "state",
            )
            db_path = str(cards_cfg.get("db_path") or os.path.join(state_dir, "cards.sqlite"))
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS card_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                attachments TEXT NOT NULL DEFAULT '[]',
                valid_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_card_revisions_card ON card_revisions(card_id);

            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                parent_id TEXT NOT NULL DEFAULT '',
                is_favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);

            CREATE TABLE IF NOT EXISTS card_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT NOT NULL,
                folder_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(card_id, folder_id)
            );
            CREATE INDEX IF NOT EXISTS idx_card_folders_card ON card_folders(card_id);
            CREATE INDEX IF NOT EXISTS idx_card_folders_folder ON card_folders(folder_id);
            """
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value or [], ensure_ascii=False)

    @staticmethod
    def _revision_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("tags", "attachments"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except (TypeError, ValueError):
                item[key] = []
        return item

    # ==================================================================
    # cards + revisions
    # ==================================================================
    def create_card(
        self,
        *,
        title: str = "",
        content: str = "",
        tags: list[str] | None = None,
        attachments: list[dict] | None = None,
        valid_at: str | None = None,
        note: str = "",
        folder_ids: list[str] | None = None,
    ) -> str:
        """Create a card and its first revision. Optionally file it into folders
        right away. Returns the new card id."""
        card_id = _gen_id("F")
        now = self._now_iso()
        conn = self._connect()
        conn.execute("INSERT INTO cards (id, created_at) VALUES (?, ?)", (card_id, now))
        conn.execute(
            """
            INSERT INTO card_revisions
                (card_id, title, content, tags, attachments, valid_at, created_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id, str(title or ""), str(content or ""),
                self._dump(tags), self._dump(attachments),
                str(valid_at or now), now, str(note or ""),
            ),
        )
        for folder_id in folder_ids or []:
            self._link_card_folder(conn, card_id, folder_id, now)
        conn.commit()
        conn.close()
        return card_id

    def add_revision(
        self,
        card_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        attachments: list[dict] | None = None,
        valid_at: str | None = None,
        note: str = "",
    ) -> int:
        """New Revision: append a new timepoint to a card's timeline. Fields left
        as None are carried over from the current (latest) revision, so a bare
        add_revision snapshots the card forward; typically content is supplied.
        The new revision becomes the current state (valid_at defaults to now)."""
        current = self.get_current_revision(card_id)
        if current is None:
            raise ValueError(f"card not found: {card_id}")
        now = self._now_iso()
        title = current["title"] if title is None else str(title)
        content = current["content"] if content is None else str(content)
        tags_val = current["tags"] if tags is None else tags
        attachments_val = current["attachments"] if attachments is None else attachments
        conn = self._connect()
        cursor = conn.execute(
            """
            INSERT INTO card_revisions
                (card_id, title, content, tags, attachments, valid_at, created_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id, title, content, self._dump(tags_val), self._dump(attachments_val),
                str(valid_at or now), now, str(note or ""),
            ),
        )
        rev_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return rev_id

    def edit_current(
        self,
        card_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        attachments: list[dict] | None = None,
    ) -> bool:
        """Edit the current (latest) revision in place -- no new timepoint. Title
        can be changed freely: identity is the card id, so renaming never splits
        the timeline (that was the old title-as-identity bug)."""
        current = self.get_current_revision(card_id)
        if current is None:
            return False
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = str(title)
        if content is not None:
            updates["content"] = str(content)
        if tags is not None:
            updates["tags"] = self._dump(tags)
        if attachments is not None:
            updates["attachments"] = self._dump(attachments)
        if not updates:
            return False
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        conn = self._connect()
        conn.execute(
            f"UPDATE card_revisions SET {set_clause} WHERE id = ?",
            (*updates.values(), int(current["id"])),
        )
        conn.commit()
        conn.close()
        return True

    def get_current_revision(self, card_id: str) -> dict | None:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT * FROM card_revisions WHERE card_id = ?
            ORDER BY valid_at DESC, id DESC LIMIT 1
            """,
            (str(card_id),),
        ).fetchone()
        conn.close()
        return self._revision_row(row) if row else None

    def list_revisions(self, card_id: str) -> list[dict]:
        """Full timeline, newest first."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM card_revisions WHERE card_id = ? ORDER BY valid_at DESC, id DESC",
            (str(card_id),),
        ).fetchall()
        conn.close()
        return [self._revision_row(row) for row in rows]

    def get_card(self, card_id: str) -> dict | None:
        """Card with its current state, full history, and folder memberships."""
        conn = self._connect()
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (str(card_id),)).fetchone()
        conn.close()
        if not card:
            return None
        history = self.list_revisions(card_id)
        return {
            "id": card["id"],
            "created_at": card["created_at"],
            "current": history[0] if history else None,
            "history": history,
            "folders": self.get_card_folders(card_id),
        }

    def delete_card(self, card_id: str) -> bool:
        """Destroy a card entirely: its revisions and all folder links go too.
        This is the explicit destructive op -- distinct from removing the card
        from a folder (remove_card_from_folder), which only unlinks."""
        conn = self._connect()
        cursor = conn.execute("DELETE FROM cards WHERE id = ?", (str(card_id),))
        existed = cursor.rowcount > 0
        conn.execute("DELETE FROM card_revisions WHERE card_id = ?", (str(card_id),))
        conn.execute("DELETE FROM card_folders WHERE card_id = ?", (str(card_id),))
        conn.commit()
        conn.close()
        return existed

    # ==================================================================
    # folders (a tree; parent_id='' == top-level == a subject)
    # ==================================================================
    def create_folder(self, name: str, *, parent_id: str = "", is_favorite: bool = False) -> str:
        folder_id = _gen_id("D")
        conn = self._connect()
        conn.execute(
            "INSERT INTO folders (id, name, parent_id, is_favorite, created_at) VALUES (?, ?, ?, ?, ?)",
            (folder_id, str(name or ""), str(parent_id or ""), 1 if is_favorite else 0, self._now_iso()),
        )
        conn.commit()
        conn.close()
        return folder_id

    def update_folder(self, folder_id: str, *, name: str | None = None, is_favorite: bool | None = None) -> bool:
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = str(name)
        if is_favorite is not None:
            updates["is_favorite"] = 1 if is_favorite else 0
        if not updates:
            return False
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        conn = self._connect()
        cursor = conn.execute(
            f"UPDATE folders SET {set_clause} WHERE id = ?",
            (*updates.values(), str(folder_id)),
        )
        conn.commit()
        updated = cursor.rowcount > 0
        conn.close()
        return updated

    def delete_folder(self, folder_id: str) -> bool:
        """Delete a folder. Its direct child folders are re-parented up to this
        folder's parent (so a subtree is never silently lost), and its card
        memberships are unlinked (cards themselves are untouched)."""
        conn = self._connect()
        row = conn.execute("SELECT parent_id FROM folders WHERE id = ?", (str(folder_id),)).fetchone()
        if not row:
            conn.close()
            return False
        parent_id = row["parent_id"]
        conn.execute("UPDATE folders SET parent_id = ? WHERE parent_id = ?", (parent_id, str(folder_id)))
        conn.execute("DELETE FROM card_folders WHERE folder_id = ?", (str(folder_id),))
        conn.execute("DELETE FROM folders WHERE id = ?", (str(folder_id),))
        conn.commit()
        conn.close()
        return True

    def get_folder(self, folder_id: str) -> dict | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (str(folder_id),)).fetchone()
        conn.close()
        return dict(row) if row else None

    def list_folders(self, parent_id: str | None = None) -> list[dict]:
        """All folders, or just the direct children of parent_id (pass '' for
        top-level subjects). Caller assembles the tree."""
        conn = self._connect()
        if parent_id is None:
            rows = conn.execute("SELECT * FROM folders ORDER BY created_at").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM folders WHERE parent_id = ? ORDER BY created_at",
                (str(parent_id),),
            ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def descendant_folder_ids(self, folder_id: str, *, include_self: bool = True) -> list[str]:
        """folder_id plus every folder nested under it, at any depth."""
        by_parent: dict[str, list[str]] = {}
        for row in self.list_folders():
            by_parent.setdefault(row["parent_id"], []).append(row["id"])
        out: list[str] = []
        stack = [str(folder_id)]
        while stack:
            fid = stack.pop()
            if fid != str(folder_id) or include_self:
                out.append(fid)
            stack.extend(by_parent.get(fid, []))
        return out

    # ==================================================================
    # card <-> folder membership (playlist model)
    # ==================================================================
    def _link_card_folder(self, conn: sqlite3.Connection, card_id: str, folder_id: str, now: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO card_folders (card_id, folder_id, created_at) VALUES (?, ?, ?)",
            (str(card_id), str(folder_id), now),
        )

    def add_card_to_folder(self, card_id: str, folder_id: str) -> bool:
        """File a card into a folder. Idempotent -- returns False if it was
        already there (no duplicate row), True if newly linked."""
        conn = self._connect()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO card_folders (card_id, folder_id, created_at) VALUES (?, ?, ?)",
            (str(card_id), str(folder_id), self._now_iso()),
        )
        conn.commit()
        added = cursor.rowcount > 0
        conn.close()
        return added

    def remove_card_from_folder(self, card_id: str, folder_id: str) -> bool:
        """Unlink a card from one folder. The card itself stays (it may end up
        in zero folders -- an allowed 'unfiled' card). Not the same as
        delete_card."""
        conn = self._connect()
        cursor = conn.execute(
            "DELETE FROM card_folders WHERE card_id = ? AND folder_id = ?",
            (str(card_id), str(folder_id)),
        )
        conn.commit()
        removed = cursor.rowcount > 0
        conn.close()
        return removed

    def get_card_folders(self, card_id: str) -> list[dict]:
        """Every folder this card is filed into -- backs the QQ-Music-style
        'which folders am I in' manager. is_favorite flags the ones that are
        favorite collections (for the star)."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT f.*, cf.created_at AS linked_at
            FROM card_folders cf JOIN folders f ON f.id = cf.folder_id
            WHERE cf.card_id = ? ORDER BY cf.created_at
            """,
            (str(card_id),),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_card_favorite_folders(self, card_id: str) -> list[dict]:
        """Favorite folders this card is in -- drives the star badge + the
        'favorited into which collection' popover."""
        return [f for f in self.get_card_folders(card_id) if f.get("is_favorite")]

    def list_cards_in_folder(self, folder_id: str, *, recursive: bool = False) -> list[dict]:
        """Cards filed directly in a folder (or anywhere under it, recursive).
        Each card is returned with its current revision merged in."""
        folder_ids = self.descendant_folder_ids(folder_id) if recursive else [str(folder_id)]
        if not folder_ids:
            return []
        placeholders = ",".join("?" for _ in folder_ids)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT DISTINCT card_id FROM card_folders WHERE folder_id IN ({placeholders})",
            folder_ids,
        ).fetchall()
        conn.close()
        cards = []
        for row in rows:
            card = self.get_card(row["card_id"])
            if card:
                cards.append(card)
        return cards

    def folder_timeline(self, folder_id: str, *, recursive: bool = True) -> list[dict]:
        """The folder overview: every card in the folder (recursively by default)
        laid out on one timeline, one entry per card at its main date (its
        current revision's valid_at), oldest first. Open a card to see its own
        history."""
        cards = self.list_cards_in_folder(folder_id, recursive=recursive)
        entries = []
        for card in cards:
            current = card.get("current") or {}
            entries.append({
                "card_id": card["id"],
                "title": current.get("title", ""),
                "date": current.get("valid_at", ""),
                "content": current.get("content", ""),
                "revision_count": len(card.get("history", [])),
            })
        entries.sort(key=lambda e: str(e.get("date") or ""))
        return entries
