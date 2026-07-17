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


def _gen_segment_id() -> str:
    return "seg" + uuid.uuid4().hex[:12]


# Co-authorship (2026-07-17): both Yi Lan (Dashboard) and Lin Zhan (MCP write
# tools, once built) can write into a card's content. AUTHOR_* are the only
# two valid values -- anything else is a bug in the caller, not a real third
# author, so writes validate against this set rather than accepting any string.
AUTHOR_YI_LAN = "yi_lan"
AUTHOR_LIN_ZHAN = "lin_zhan"
VALID_AUTHORS = {AUTHOR_YI_LAN, AUTHOR_LIN_ZHAN}
AUTHOR_DISPLAY_NAMES = {AUTHOR_YI_LAN: "一澜", AUTHOR_LIN_ZHAN: "林湛"}
# Inverse of AUTHOR_DISPLAY_NAMES, for parse_content_text (2026-07-17, second
# pass): recognizes the exact "一澜："/"林湛：" prefix _render_content already
# generates, so a save-then-reload of hand-edited text round-trips cleanly.
_AUTHOR_PREFIX_TO_CODE = {f"{name}：": code for code, name in AUTHOR_DISPLAY_NAMES.items()}


class SegmentOwnershipError(Exception):
    """Raised by edit_content_segment/delete_content_segment when the caller
    is trying to change or remove a segment someone else wrote, without
    force=True. Carries enough detail for the caller (Dashboard API / MCP
    tool) to show a real confirmation prompt instead of a generic error."""
    def __init__(self, segment_id: str, author: str, text_preview: str):
        self.segment_id = segment_id
        self.author = author
        self.text_preview = text_preview
        super().__init__(
            f"segment {segment_id} was written by {author!r}, not the caller -- "
            f"pass force=True to override (preview: {text_preview[:40]!r})"
        )


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
                created_at TEXT NOT NULL,
                merged_into TEXT NOT NULL DEFAULT '',
                tags_override TEXT NOT NULL DEFAULT ''
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

            CREATE TABLE IF NOT EXISTS card_buckets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT NOT NULL,
                bucket_id TEXT NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'evidence',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(card_id, bucket_id)
            );
            CREATE INDEX IF NOT EXISTS idx_card_buckets_card ON card_buckets(card_id);
            """
        )
        conn.commit()
        self._migrate_authorship_columns(conn)
        self._migrate_merge_columns(conn)
        # 2026-07-17 (Yi Lan + Lin Zhan): relation_type on card_buckets
        # (evidence/origin/related) turned out never to be used by either of
        # them in practice -- everything is 'evidence' -- so it's retired as
        # a real distinction. Cheap to normalize any stray value on every
        # startup; a no-op once nothing but 'evidence' exists.
        conn.execute("UPDATE card_buckets SET relation_type = 'evidence' WHERE relation_type != 'evidence'")
        conn.commit()
        conn.close()

    def _migrate_authorship_columns(self, conn: sqlite3.Connection) -> None:
        """Migration-safe ALTER TABLE, same pattern as facts_store.py's --
        adds title_author/content_segments to an existing card_revisions
        table without touching any existing row's data. Safe to run on
        every startup: checks column existence first, so it's a no-op once
        already applied.

        title_author defaults to 'yi_lan' for every pre-existing row via the
        column DEFAULT itself (Yi Lan's explicit request: everything written
        before this feature existed was in fact written by her, so it should
        say so, not show up unlabeled).

        content_segments can't get its per-row backfill from a column
        DEFAULT alone (DEFAULT '[]' would make every old card's content
        look like it has *no* author, not 'written by Yi Lan') -- so after
        adding the column, a second pass wraps each pre-existing row's plain
        `content` string into a single yi_lan-authored segment. Only rows
        still at the untouched default ('[]' with non-empty content) get
        backfilled, so this is idempotent and never overwrites a real
        segment list written by the new code."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(card_revisions)")}
        if "title_author" not in existing:
            conn.execute(
                f"ALTER TABLE card_revisions ADD COLUMN title_author TEXT NOT NULL DEFAULT '{AUTHOR_YI_LAN}'"
            )
        if "content_segments" not in existing:
            conn.execute("ALTER TABLE card_revisions ADD COLUMN content_segments TEXT NOT NULL DEFAULT '[]'")
        conn.commit()

        rows = conn.execute(
            "SELECT id, content FROM card_revisions WHERE content_segments = '[]' AND content != ''"
        ).fetchall()
        for row in rows:
            segments = [{"id": _gen_segment_id(), "author": AUTHOR_YI_LAN, "text": row["content"]}]
            conn.execute(
                "UPDATE card_revisions SET content_segments = ? WHERE id = ?",
                (json.dumps(segments, ensure_ascii=False), row["id"]),
            )
        if rows:
            conn.commit()

        # 2026-07-17 (second pass, Yi Lan's call): _render_content now labels
        # every segment with a real author, not just cards with 2+ segments
        # -- so a single-author card written before this change looks the
        # same as one written after it. Rows migrated under the OLD rule (or
        # written by the old code before this pass existed) still have the
        # un-labeled flat `content` from back then; resync it from
        # content_segments on every startup. Cheap (small table) and
        # idempotent: only rewrites rows where the derived text actually
        # differs from what's stored.
        for row in conn.execute("SELECT id, content, content_segments FROM card_revisions"):
            try:
                segments = json.loads(row["content_segments"] or "[]")
            except (TypeError, ValueError):
                continue
            rendered = self._render_content(segments)
            if rendered != row["content"]:
                conn.execute("UPDATE card_revisions SET content = ? WHERE id = ?", (rendered, row["id"]))
        conn.commit()

    def _migrate_merge_columns(self, conn: sqlite3.Connection) -> None:
        """Migration-safe ALTER TABLE for the merge/dedup tool (2026-07-17,
        docs/facts-model-v2-collection-redesign.md §14 item 4):

        merged_into: a permanent redirect pointer, set on the discarded card
        when two cards get merged (merge_cards). Distinct from the eventual
        card recycle bin (§14 item 7, for real deletions with a restore/purge
        window) -- a merge tombstone never gets purged, it's not "pending
        deletion", the discarded id's data all still lives under the kept
        id forever, this is just so an old id never dead-ends.

        tags_override: holds the union of both cards' current tags right
        after a merge, without rewriting either original revision's own
        `tags` column (Lin Zhan's call: the timeline's real timepoints stay
        byte-for-byte untouched; only the card-level "what tags show right
        now" reads through this instead). Cleared back to '' the next time
        anyone explicitly sets tags (edit_current/add_revision with a real
        tags= value) -- from then on the ordinary latest-revision tags take
        over again, same as any card that was never merged.

        Both default to '' for every pre-existing row via the column
        DEFAULT itself -- '' means "not merged" / "no override", a no-op."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(cards)")}
        if "merged_into" not in existing:
            conn.execute("ALTER TABLE cards ADD COLUMN merged_into TEXT NOT NULL DEFAULT ''")
        if "tags_override" not in existing:
            conn.execute("ALTER TABLE cards ADD COLUMN tags_override TEXT NOT NULL DEFAULT ''")
        conn.commit()

    def _resolve_id(self, card_id: str) -> str:
        """Follow a card's merged_into tombstone pointer to the canonical
        living card id, if it was ever merged away. The overwhelming common
        case (never merged) resolves to itself with one cheap SELECT. The
        hop-count bound is defensive only -- merge_cards() flattens every
        existing tombstone that pointed at the card being discarded, so a
        chain longer than one hop should never actually occur."""
        card_id = str(card_id or "")
        if not card_id:
            return card_id
        conn = self._connect()
        seen: set[str] = set()
        current = card_id
        for _ in range(10):
            if current in seen:
                break
            seen.add(current)
            row = conn.execute("SELECT merged_into FROM cards WHERE id = ?", (current,)).fetchone()
            if not row or not row["merged_into"]:
                break
            current = row["merged_into"]
        conn.close()
        return current

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value or [], ensure_ascii=False)

    @staticmethod
    def _check_author(author: str) -> str:
        author = str(author or "")
        if author not in VALID_AUTHORS:
            raise ValueError(f"author must be one of {sorted(VALID_AUTHORS)}, got {author!r}")
        return author

    @staticmethod
    def _render_content(segments: list[dict]) -> str:
        """Flatten authored segments into the plain-text `content` column that
        every existing reader (search, card_lookup, the old Dashboard render
        path) already knows how to use, so nothing else in the codebase has
        to learn about segments to keep working.

        2026-07-17 (Yi Lan, second pass): labeling isn't about segment count
        anymore -- every segment with a real author (yi_lan/lin_zhan) gets
        its "作者：文本" block, even when it's the only segment on the card,
        so Lin Zhan can tell at a glance who wrote something even when only
        one of them did. A segment with no recognized author (author=None --
        see parse_content_text, for text typed without tagging it) renders
        as plain text with no label, on purpose: forgetting to tag something
        just leaves it looking like ordinary prose. Blocks are blank-line
        separated, the same convention Yi Lan was already typing by hand,
        now generated instead of relying on anyone remembering to type it."""
        parts = []
        for s in segments:
            name = AUTHOR_DISPLAY_NAMES.get(s.get("author"))
            text = str(s.get("text", ""))
            parts.append(f"{name}：{text}" if name else text)
        return "\n\n".join(parts)

    @staticmethod
    def _first_lost_foreign_segment(old_segments: list[dict], new_segments: list[dict], author: str) -> dict | None:
        """Rule C for whole-content replaces (content editor modal, New
        Revision): find the first segment in `old_segments` that was written
        by someone other than `author` whose exact text is no longer present
        among `new_segments`' segments for that same author. Segments with no
        real author (untagged text) protect nothing -- there's no one to ask
        permission from. Returns None if every foreign segment survived
        (whether untouched, reordered, or sitting alongside brand-new text),
        meaning the replace can proceed without a confirm prompt."""
        remaining: dict[str, list[str]] = {}
        for s in new_segments:
            remaining.setdefault(s.get("author"), []).append(s.get("text"))
        for s in old_segments:
            seg_author = s.get("author")
            if seg_author == author or seg_author not in VALID_AUTHORS:
                continue
            bucket = remaining.get(seg_author, [])
            text = s.get("text")
            if text in bucket:
                bucket.remove(text)
            else:
                return s
        return None

    @staticmethod
    def parse_content_text(text: str) -> list[dict]:
        """Inverse of _render_content: turns free-form edited text (as shown
        and typed in the Dashboard's content editor -- the same "作者：..."
        labels _render_content produces) back into structured segments.

        A paragraph (blank-line-separated block) that starts with a
        recognized "一澜：" or "林湛：" prefix becomes a segment credited to
        that author, prefix stripped. Any other paragraph becomes an
        untagged segment (author=None) that renders as plain text -- Yi
        Lan's rule: if you forget to tag something, it just looks like
        ordinary text, it doesn't silently get attributed to whoever's
        editing. Blank paragraphs are dropped, so a save-then-reload
        round-trips cleanly through _render_content."""
        blocks = [b for b in str(text or "").split("\n\n") if b.strip()]
        segments = []
        for block in blocks:
            author = None
            body = block
            for prefix, code in _AUTHOR_PREFIX_TO_CODE.items():
                if block.startswith(prefix):
                    author = code
                    body = block[len(prefix):]
                    break
            segments.append({"id": _gen_segment_id(), "author": author, "text": body})
        return segments

    @staticmethod
    def _revision_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("tags", "attachments", "content_segments"):
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
        content_segments: list[dict] | None = None,
        tags: list[str] | None = None,
        attachments: list[dict] | None = None,
        valid_at: str | None = None,
        note: str = "",
        folder_ids: list[str] | None = None,
        author: str = AUTHOR_YI_LAN,
    ) -> str:
        """Create a card and its first revision. Optionally file it into folders
        right away. Returns the new card id.

        author: who's creating this card -- AUTHOR_YI_LAN (default, matches
        every existing caller unchanged) or AUTHOR_LIN_ZHAN once his write
        tools call this. Tags the title and wraps `content` (if any) into a
        single segment credited to `author` -- the whole card starts out
        single-authored, which is the overwhelmingly common case.

        content_segments: full control (e.g. from parse_content_text, for
        the Dashboard's free-text content editor), overrides `content`.
        No ownership check here -- it's a brand new card, nothing to lose."""
        author = self._check_author(author)
        card_id = _gen_id("F")
        now = self._now_iso()
        if content_segments is not None:
            segments = content_segments
        else:
            segments = [{"id": _gen_segment_id(), "author": author, "text": str(content)}] if content else []
        conn = self._connect()
        conn.execute("INSERT INTO cards (id, created_at) VALUES (?, ?)", (card_id, now))
        conn.execute(
            """
            INSERT INTO card_revisions
                (card_id, title, content, tags, attachments, valid_at, created_at, note,
                 title_author, content_segments)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id, str(title or ""), self._render_content(segments),
                self._dump(tags), self._dump(attachments),
                str(valid_at or now), now, str(note or ""),
                author, json.dumps(segments, ensure_ascii=False),
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
        content_segments: list[dict] | None = None,
        tags: list[str] | None = None,
        attachments: list[dict] | None = None,
        valid_at: str | None = None,
        note: str = "",
        author: str = AUTHOR_YI_LAN,
        force: bool = False,
    ) -> int:
        """New Revision: append a new timepoint to a card's timeline. Fields left
        as None are carried over from the current (latest) revision, so a bare
        add_revision snapshots the card forward; typically content is supplied.
        The new revision becomes the current state (valid_at defaults to now).

        author: who's writing this new revision. Only matters for fields
        actually being changed here -- title_author only updates if `title`
        is given AND actually differs from the current title text (a title
        passed unchanged never steals credit from whoever titled it before);
        content authorship only changes if `content`/`content_segments` is
        given.

        content vs content_segments -- two ways to say the same thing:
        - content_segments: full control, pass the exact segment list this
          revision should have (e.g. "keep Yi Lan's segment, add mine").
        - content: convenience for the common single-author case -- wraps
          the whole string into one segment credited to `author`, replacing
          whatever segments existed before. Fine when one person is writing
          the whole new revision; if you want to preserve someone else's
          existing segment alongside a new one, use content_segments (see
          add_content_segment for the even simpler "just add mine" case
          that doesn't require a new timepoint at all).
        Passing neither carries the current revision's segments forward
        unchanged, same as every other field here.

        Rule C (2026-07-17, second pass -- Yi Lan's call): New Revision is
        no longer a free pass just because the old revision stays visible in
        history. If replacing `content`/`content_segments` here would lose
        the exact text of a segment someone else wrote (not present anywhere
        in the new segment list), raises SegmentOwnershipError unless
        force=True -- same rule, same exception shape, as edit_current's
        whole-content replace."""
        card_id = self._resolve_id(card_id)
        author = self._check_author(author)
        current = self.get_current_revision(card_id)
        if current is None:
            raise ValueError(f"card not found: {card_id}")
        now = self._now_iso()
        title_author = current.get("title_author", AUTHOR_YI_LAN)
        if title is None:
            title = current["title"]
        else:
            new_title = str(title)
            if new_title != current.get("title", ""):
                title_author = author
            title = new_title
        if content_segments is not None:
            segments = content_segments
        elif content is not None:
            segments = [{"id": _gen_segment_id(), "author": author, "text": str(content)}] if content else []
        else:
            segments = current.get("content_segments") or []
        if content is not None or content_segments is not None:
            lost = self._first_lost_foreign_segment(current.get("content_segments") or [], segments, author)
            if lost and not force:
                raise SegmentOwnershipError(str(lost.get("id", "")), str(lost.get("author", "")), str(lost.get("text", "")))
        tags_val = current["tags"] if tags is None else tags
        attachments_val = current["attachments"] if attachments is None else attachments
        conn = self._connect()
        cursor = conn.execute(
            """
            INSERT INTO card_revisions
                (card_id, title, content, tags, attachments, valid_at, created_at, note,
                 title_author, content_segments)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id, title, self._render_content(segments),
                self._dump(tags_val), self._dump(attachments_val),
                str(valid_at or now), now, str(note or ""),
                title_author, json.dumps(segments, ensure_ascii=False),
            ),
        )
        rev_id = cursor.lastrowid
        if tags is not None:
            conn.execute("UPDATE cards SET tags_override = '' WHERE id = ?", (card_id,))
        conn.commit()
        conn.close()
        return rev_id

    def edit_current(
        self,
        card_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        content_segments: list[dict] | None = None,
        tags: list[str] | None = None,
        attachments: list[dict] | None = None,
        author: str = AUTHOR_YI_LAN,
        force: bool = False,
    ) -> bool:
        """Edit the current (latest) revision in place -- no new timepoint. Title
        can be changed freely by either author (tags title_author to whoever
        just changed it, but only if the title text actually differs from
        what's there now -- an unchanged title passed along for the ride
        never steals credit); identity is the card id, so renaming never
        splits the timeline (that was the old title-as-identity bug).

        content vs content_segments -- same two ways to say it as
        add_revision: `content` is the convenience whole-string replace
        (wraps into one segment credited to `author`); content_segments is
        full control (e.g. from parse_content_text, for the Dashboard's
        free-text content editor -- lets several segments, including ones
        written by the other author, survive the same replace call).

        Rule C (2026-07-17, second pass): freely *adding* content never
        needs permission, but *losing* text the other author wrote does.
        Checks whether the new segment list still contains every existing
        foreign segment's exact text; if not and force isn't True, raises
        SegmentOwnershipError instead of silently overwriting someone
        else's words. Use add_content_segment to add your own text without
        touching what's already there (never needs force)."""
        card_id = self._resolve_id(card_id)
        author = self._check_author(author)
        current = self.get_current_revision(card_id)
        if current is None:
            return False
        updates: dict[str, Any] = {}
        if title is not None:
            new_title = str(title)
            if new_title != current.get("title", ""):
                updates["title"] = new_title
                updates["title_author"] = author
        if content is not None or content_segments is not None:
            existing_segments = current.get("content_segments") or []
            if content_segments is not None:
                segments = content_segments
            else:
                segments = [{"id": _gen_segment_id(), "author": author, "text": content}] if content else []
            lost = self._first_lost_foreign_segment(existing_segments, segments, author)
            if lost and not force:
                raise SegmentOwnershipError(str(lost.get("id", "")), str(lost.get("author", "")), str(lost.get("text", "")))
            updates["content"] = self._render_content(segments)
            updates["content_segments"] = json.dumps(segments, ensure_ascii=False)
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
        if tags is not None:
            conn.execute("UPDATE cards SET tags_override = '' WHERE id = ?", (card_id,))
        conn.commit()
        conn.close()
        return True

    def set_title(self, card_id: str, title: str, *, author: str = AUTHOR_YI_LAN) -> bool:
        """Change just the current revision's title, in place, tagged to
        whoever changed it -- no ownership check (Yi Lan's call: titles are
        short and either of you retitling a card doesn't need a confirm
        prompt the way rewriting someone else's paragraph does)."""
        return self.edit_current(card_id, title=title, author=author)

    def add_content_segment(self, card_id: str, *, author: str, text: str) -> str:
        """Append a new segment to the current revision's content, in place --
        no new timepoint, and never needs force: adding your own words next
        to what's already there doesn't touch anyone else's. This is the
        normal way either of you adds to a card's content day-to-day; the
        whole-string replace in edit_current(content=...) is for the rarer
        case of actually starting over. Returns the new segment's id."""
        author = self._check_author(author)
        current = self.get_current_revision(card_id)
        if current is None:
            raise ValueError(f"card not found: {card_id}")
        segments = list(current.get("content_segments") or [])
        segment_id = _gen_segment_id()
        segments.append({"id": segment_id, "author": author, "text": str(text)})
        conn = self._connect()
        conn.execute(
            "UPDATE card_revisions SET content = ?, content_segments = ? WHERE id = ?",
            (self._render_content(segments), json.dumps(segments, ensure_ascii=False), int(current["id"])),
        )
        conn.commit()
        conn.close()
        return segment_id

    def edit_content_segment(
        self, card_id: str, segment_id: str, *, text: str, author: str, force: bool = False,
    ) -> bool:
        """Change one existing segment's text in place. Rule C: if that
        segment wasn't written by `author` and force isn't True, raises
        SegmentOwnershipError instead of silently rewriting someone else's
        words -- the caller (Dashboard API / MCP tool) is expected to turn
        that into a real confirmation prompt, then retry with force=True."""
        author = self._check_author(author)
        current = self.get_current_revision(card_id)
        if current is None:
            return False
        segments = list(current.get("content_segments") or [])
        idx = next((i for i, s in enumerate(segments) if s.get("id") == segment_id), None)
        if idx is None:
            return False
        existing = segments[idx]
        if existing.get("author") != author and not force:
            raise SegmentOwnershipError(segment_id, str(existing.get("author", "")), str(existing.get("text", "")))
        segments[idx] = {"id": segment_id, "author": existing.get("author"), "text": str(text)}
        conn = self._connect()
        conn.execute(
            "UPDATE card_revisions SET content = ?, content_segments = ? WHERE id = ?",
            (self._render_content(segments), json.dumps(segments, ensure_ascii=False), int(current["id"])),
        )
        conn.commit()
        conn.close()
        return True

    def delete_content_segment(self, card_id: str, segment_id: str, *, author: str, force: bool = False) -> bool:
        """Remove one segment from the current revision. Same rule C check as
        edit_content_segment: deleting someone else's segment needs force=True."""
        author = self._check_author(author)
        current = self.get_current_revision(card_id)
        if current is None:
            return False
        segments = list(current.get("content_segments") or [])
        idx = next((i for i, s in enumerate(segments) if s.get("id") == segment_id), None)
        if idx is None:
            return False
        existing = segments[idx]
        if existing.get("author") != author and not force:
            raise SegmentOwnershipError(segment_id, str(existing.get("author", "")), str(existing.get("text", "")))
        del segments[idx]
        conn = self._connect()
        conn.execute(
            "UPDATE card_revisions SET content = ?, content_segments = ? WHERE id = ?",
            (self._render_content(segments), json.dumps(segments, ensure_ascii=False), int(current["id"])),
        )
        conn.commit()
        conn.close()
        return True

    def get_current_revision(self, card_id: str) -> dict | None:
        card_id = self._resolve_id(card_id)
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
        card_id = self._resolve_id(card_id)
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM card_revisions WHERE card_id = ? ORDER BY valid_at DESC, id DESC",
            (str(card_id),),
        ).fetchall()
        conn.close()
        return [self._revision_row(row) for row in rows]

    def get_card(self, card_id: str) -> dict | None:
        """Card with its current state, full history, and folder memberships.
        A merged-away id transparently resolves to the card it was merged
        into (see merge_cards) -- an old id never just dead-ends."""
        card_id = self._resolve_id(card_id)
        conn = self._connect()
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (str(card_id),)).fetchone()
        conn.close()
        if not card:
            return None
        history = self.list_revisions(card_id)
        current = history[0] if history else None
        if current and card["tags_override"]:
            try:
                override_tags = json.loads(card["tags_override"])
            except (TypeError, ValueError):
                override_tags = None
            if override_tags is not None:
                current = dict(current)
                current["tags"] = override_tags
        return {
            "id": card["id"],
            "created_at": card["created_at"],
            "current": current,
            "history": history,
            "folders": self.get_card_folders(card_id),
            "buckets": self.get_bucket_links(card_id),
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
        conn.execute("DELETE FROM card_buckets WHERE card_id = ?", (str(card_id),))
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
        card_id = self._resolve_id(card_id)
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
        card_id = self._resolve_id(card_id)
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
        card_id = self._resolve_id(card_id)
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

    # ==================================================================
    # card <-> memory-bucket links (evidence)
    # ==================================================================
    def add_bucket_link(self, card_id: str, bucket_id: str, *, note: str = "") -> bool:
        """Link a memory bucket to a card as evidence. Idempotent: returns
        False if that bucket was already linked, True if newly linked.

        2026-07-17 (Yi Lan + Lin Zhan): dropped the relation_type param
        (evidence/origin/related) -- neither of them ever actually used
        anything but the default 'evidence' in practice, so it was retired
        as a real distinction rather than kept as an unused decision surface.
        Every link is 'evidence' now; merging two cards' bucket links is a
        plain union by bucket_id, no relation_type to reconcile."""
        card_id = self._resolve_id(card_id)
        bucket_id = str(bucket_id or "").strip()
        if not bucket_id:
            raise ValueError("bucket_id is required")
        conn = self._connect()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO card_buckets (card_id, bucket_id, relation_type, note, created_at) VALUES (?, ?, 'evidence', ?, ?)",
            (str(card_id), bucket_id, str(note or ""), self._now_iso()),
        )
        conn.commit()
        added = cursor.rowcount > 0
        conn.close()
        return added

    def remove_bucket_link(self, card_id: str, bucket_id: str) -> bool:
        card_id = self._resolve_id(card_id)
        conn = self._connect()
        cursor = conn.execute(
            "DELETE FROM card_buckets WHERE card_id = ? AND bucket_id = ?",
            (str(card_id), str(bucket_id)),
        )
        conn.commit()
        removed = cursor.rowcount > 0
        conn.close()
        return removed

    def get_bucket_links(self, card_id: str) -> list[dict]:
        card_id = self._resolve_id(card_id)
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM card_buckets WHERE card_id = ? ORDER BY created_at",
            (str(card_id),),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    # ==================================================================
    # merge / dedup (docs/facts-model-v2-collection-redesign.md §14 item 4)
    #
    # Two cards that turned out to describe the same real thing get folded
    # into one: their timelines interleave by date (revisions are re-parented
    # to the kept card's id, never rewritten -- every original timepoint's
    # title/content/attachments stay exactly as recorded), folders and bucket
    # links union, current tags union (see tags_override on the cards table).
    # No auto-detection, no guessing which content "wins" -- Yi Lan and Lin
    # Zhan pick the two cards and which id survives; this just does the
    # bookkeeping precisely and leaves nothing orphaned or duplicated.
    # ==================================================================
    def merge_preview(self, card_a_id: str, card_b_id: str) -> dict:
        """Non-destructive: what merging these two cards would produce,
        without touching any data. Call this before merge_cards."""
        a_id = self._resolve_id(card_a_id)
        b_id = self._resolve_id(card_b_id)
        if a_id == b_id:
            raise ValueError("这两张卡已经是同一张卡了（可能已经被合并过）。")
        card_a = self.get_card(a_id)
        card_b = self.get_card(b_id)
        if not card_a or not card_b:
            raise ValueError("卡片不存在。")
        tags_a = [str(t) for t in ((card_a.get("current") or {}).get("tags") or [])]
        tags_b = [str(t) for t in ((card_b.get("current") or {}).get("tags") or [])]
        tags_after = list(dict.fromkeys(tags_a + tags_b))
        folder_ids_after = list(dict.fromkeys(
            [f["id"] for f in (card_a.get("folders") or [])] + [f["id"] for f in (card_b.get("folders") or [])]
        ))
        bucket_ids_after = list(dict.fromkeys(
            [b["bucket_id"] for b in (card_a.get("buckets") or [])]
            + [b["bucket_id"] for b in (card_b.get("buckets") or [])]
        ))
        attachment_count_after = sum(
            len(rev.get("attachments") or [])
            for rev in (card_a.get("history") or []) + (card_b.get("history") or [])
        )
        return {
            "card_a": {
                "id": a_id,
                "title": (card_a.get("current") or {}).get("title", ""),
                "revision_count": len(card_a.get("history") or []),
            },
            "card_b": {
                "id": b_id,
                "title": (card_b.get("current") or {}).get("title", ""),
                "revision_count": len(card_b.get("history") or []),
            },
            "revision_count_after": len(card_a.get("history") or []) + len(card_b.get("history") or []),
            "tags_after": tags_after,
            "folder_paths_after": [self.folder_path(fid) for fid in folder_ids_after],
            "bucket_count_after": len(bucket_ids_after),
            "attachment_count_after": attachment_count_after,
        }

    def merge_cards(self, keep_id: str, discard_id: str) -> dict:
        """Merge discard_id into keep_id. Interleaving is just re-parenting:
        every one of discard_id's revisions gets its card_id column changed
        to keep_id, so the two timelines sort together by valid_at with no
        content rewritten. Folders and bucket links union (dedup by
        folder_id / bucket_id, already enforced by their UNIQUE constraints).
        Current tags union into keep_id's tags_override (see that column's
        docstring in _migrate_merge_columns) -- neither card's own latest
        revision row is touched.

        discard_id becomes a permanent tombstone: its `cards` row survives
        with merged_into=keep_id (not deleted), and every id-taking method
        in this class resolves through that pointer transparently, forever.
        This is a redirect, not a pending deletion -- unrelated to the
        eventual card recycle bin (§14 item 7), which will be about
        undoing real card_delete calls, not merges.

        Any tombstone that already pointed at discard_id gets flattened to
        point directly at keep_id first, so redirects never chain."""
        keep_id = self._resolve_id(keep_id)
        discard_id = self._resolve_id(discard_id)
        if keep_id == discard_id:
            raise ValueError("这两张卡已经是同一张卡了（可能已经被合并过）。")
        conn = self._connect()
        keep_row = conn.execute("SELECT id FROM cards WHERE id = ?", (keep_id,)).fetchone()
        discard_row = conn.execute("SELECT id FROM cards WHERE id = ?", (discard_id,)).fetchone()
        if not keep_row or not discard_row:
            conn.close()
            raise ValueError("卡片不存在。")

        keep_current = conn.execute(
            "SELECT tags FROM card_revisions WHERE card_id = ? ORDER BY valid_at DESC, id DESC LIMIT 1",
            (keep_id,),
        ).fetchone()
        discard_current = conn.execute(
            "SELECT tags FROM card_revisions WHERE card_id = ? ORDER BY valid_at DESC, id DESC LIMIT 1",
            (discard_id,),
        ).fetchone()
        keep_tags = json.loads(keep_current["tags"]) if keep_current and keep_current["tags"] else []
        discard_tags = json.loads(discard_current["tags"]) if discard_current and discard_current["tags"] else []
        tags_after = list(dict.fromkeys([str(t) for t in keep_tags] + [str(t) for t in discard_tags]))

        revisions_merged = conn.execute(
            "SELECT COUNT(*) AS c FROM card_revisions WHERE card_id = ?", (discard_id,)
        ).fetchone()["c"]
        folders_merged = conn.execute(
            "SELECT COUNT(*) AS c FROM card_folders WHERE card_id = ?", (discard_id,)
        ).fetchone()["c"]
        buckets_merged = conn.execute(
            "SELECT COUNT(*) AS c FROM card_buckets WHERE card_id = ?", (discard_id,)
        ).fetchone()["c"]

        # Flatten pre-existing tombstones so nothing ever chains through
        # more than one redirect hop.
        conn.execute("UPDATE cards SET merged_into = ? WHERE merged_into = ?", (keep_id, discard_id))

        conn.execute("UPDATE card_revisions SET card_id = ? WHERE card_id = ?", (keep_id, discard_id))

        now = self._now_iso()
        for row in conn.execute("SELECT folder_id FROM card_folders WHERE card_id = ?", (discard_id,)).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO card_folders (card_id, folder_id, created_at) VALUES (?, ?, ?)",
                (keep_id, row["folder_id"], now),
            )
        conn.execute("DELETE FROM card_folders WHERE card_id = ?", (discard_id,))

        for row in conn.execute("SELECT bucket_id, note FROM card_buckets WHERE card_id = ?", (discard_id,)).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO card_buckets (card_id, bucket_id, relation_type, note, created_at) "
                "VALUES (?, ?, 'evidence', ?, ?)",
                (keep_id, row["bucket_id"], row["note"], now),
            )
        conn.execute("DELETE FROM card_buckets WHERE card_id = ?", (discard_id,))

        conn.execute(
            "UPDATE cards SET tags_override = ? WHERE id = ?",
            (json.dumps(tags_after, ensure_ascii=False), keep_id),
        )
        conn.execute("UPDATE cards SET merged_into = ? WHERE id = ?", (keep_id, discard_id))

        conn.commit()
        conn.close()
        return {
            "kept_id": keep_id,
            "discarded_id": discard_id,
            "revisions_merged": revisions_merged,
            "folders_merged": folders_merged,
            "buckets_merged": buckets_merged,
            "tags_after": tags_after,
        }

    # ==================================================================
    # lookup helpers (back the MCP tools in cards_mcp.py)
    # ==================================================================
    def all_cards(self) -> list[dict]:
        """Every living card -- excludes merge tombstones (merged_into != ''),
        so a card merged away doesn't show up twice (once under its own id,
        once again via its canonical id resolving back to the same card)."""
        conn = self._connect()
        rows = conn.execute("SELECT id FROM cards WHERE merged_into = '' ORDER BY created_at").fetchall()
        conn.close()
        return [c for c in (self.get_card(r["id"]) for r in rows) if c]

    def search_cards(self, query: str, *, folder_id: str = "", recursive: bool = True) -> list[dict]:
        """Find cards whose id or current revision matches query (case-
        insensitive substring over id/title/content/tags -- 2026-07-17,
        Yi Lan's request: pasting a card id, e.g. to find "the other card"
        for merge, should work here too, not just an exact get_card hit).
        Optionally scope to a folder subtree. Empty query returns everything
        in scope. Fuzzy matching can be layered on later; substring is
        deterministic and dependency-free for now."""
        cards = self.list_cards_in_folder(folder_id, recursive=recursive) if folder_id else self.all_cards()
        q = str(query or "").strip().lower()
        if not q:
            return cards
        out = []
        for card in cards:
            cur = card.get("current") or {}
            hay = " ".join([
                str(card.get("id", "")),
                str(cur.get("title", "")),
                str(cur.get("content", "")),
                " ".join(str(t) for t in (cur.get("tags") or [])),
            ]).lower()
            if q in hay:
                out.append(card)
        return out

    def find_folders_by_name(self, name: str) -> list[dict]:
        """Folders whose name contains `name` (case-insensitive). Names are not
        unique across subjects, so callers should disambiguate via folder_path."""
        n = str(name or "").strip().lower()
        if not n:
            return []
        return [f for f in self.list_folders() if n in str(f.get("name", "")).lower()]

    def folder_path(self, folder_id: str) -> str:
        """Readable path from the top subject down, e.g. '我们 / 旅行 / 香港'."""
        by_id = {f["id"]: f for f in self.list_folders()}
        parts: list[str] = []
        fid = str(folder_id)
        seen: set[str] = set()
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            parts.append(str(by_id[fid].get("name", "")))
            fid = str(by_id[fid].get("parent_id") or "")
        return " / ".join(reversed(parts))
