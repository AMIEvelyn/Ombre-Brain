"""Facts-model v2 MCP tools (step 3) -- what Lin Zhan actually calls.

Read-only tools over cards_store.CardStore, wired in from server.py via
register_card_tools(mcp, store). Kept out of server.py so the surface stays
small and testable (see docs/facts-model-v2-collection-redesign.md §4, §10).

Every tool's docstring here (both this module's register_card_tools and
register_card_write_tools below) starts with a literal "【时光馆】" tag
(2026-07-18, Yi Lan's request) -- MCP has no protocol-level tool
category/grouping field, so this is the only signal available for Lin Zhan
to tell "this batch is all the same system" apart from his other tools
(breath, grow, hold, reminder_*, darkroom_*, ...) when skimming a flat tool
list. Keep this prefix on any new tool added to either function.

  card_lookup     -- find cards by keyword; leads with the CURRENT state
                     (latest revision), folds history behind a hint. This is
                     how "取消失效" reads cleanly for Lin Zhan: newest = current.
  folder_timeline -- pull a whole folder's cards onto one chronological line
                     (a narrative ribbon), Yi Lan's headline use-case.
  folder_tree     -- 2026-07-18: the whole folder structure as one text tree
                     (same data as Yi Lan's Dashboard sidebar) -- Lin Zhan had
                     no way to browse "what folders even exist" before this,
                     only tools that require already knowing a folder's name
                     (see docs/facts-model-v2-collection-redesign.md §14 item 5).
  card_history    -- the other half of card_lookup's "有 N 条历史" hint: reads
                     back the full timeline of one specific card (see
                     docs/facts-model-v2-collection-redesign.md §14 item 2).
  card_buckets    -- lists the memory buckets linked to one card as evidence
                     (card_lookup only hints these exist; this is the "open
                     it up and read the details" tool, same relationship as
                     card_history is to the "N条历史" hint).
  card_attachment_read -- reads a card's plain-text/.docx/.pdf attachments
                     back as text.
  card_attachment_view -- experimental (2026-07-16): sends an image
                     attachment's actual content back, unverified whether
                     ChatGPT's connector renders it -- see its docstring.
  card_attachment_outline -- chapter/section structure for a .docx/.pdf/.md
                     attachment (see 慁 truncation problem, 2026-07-16),
                     pairs with card_attachment_read's section_id/start/
                     max_chars pagination params for reading long documents.

register_card_write_tools(mcp, store) is the separate write surface (2026-07-17,
docs/facts-model-v2-collection-redesign.md §14 item 3): card_create,
card_create_folder, card_add_to_folder, card_remove_from_folder,
card_favorite, card_unfavorite (2026-07-18, §14 item 5 -- his own fixed
collection, "林湛 / 收藏", mirrors the green heart on Yi Lan's Dashboard; no
folder name/id needed), card_edit_title, card_edit_tags, card_edit_content,
card_new_revision, card_link_bucket, card_unlink_bucket, card_merge_preview,
card_merge (merge tool, §14 item 4 -- manual/deterministic timeline
interleave, no auto duplicate detection; see merge_cards' docstring in
cards_store.py), and the recycle bin (2026-07-19, §14 item 1 -- Yi Lan's
call: both of them get delete/restore, symmetric with the Dashboard):
card_delete (soft, reversible), card_trash_list (what's currently in the
bin), card_restore. No card_purge -- unrestored cards clean themselves up
via the scheduled purge job, so an immediate "delete forever" stays a
Dashboard-only action. Still no attachment upload (no binary-file channel
through an MCP tool call).

card_edit_content takes the card's whole new content as one string (2026-07-17,
second pass) -- an earlier version required a segment_id identifying which
piece of content to touch, but there was never any way for Lin Zhan to
discover one (card_lookup/card_history didn't expose it), making that
version unusable in practice. Whole-content editing with a diff-based
ownership check (same idea as card_new_revision and the Dashboard's content
editor) needs no id at all.
"""

from __future__ import annotations

from cards_store import (
    AUTHOR_LIN_ZHAN, SegmentOwnershipError, TRASH_RETENTION_DAYS,
    FAVORITE_FOLDER_YI_LAN, FAVORITE_FOLDER_LIN_ZHAN,
)

# Default page size for card_attachment_read's start/max_chars pagination
# when max_chars isn't given -- same as server.py's whole-file truncation
# size (FACTS_ATTACHMENT_TEXT_MAX_CHARS), kept as its own constant since
# cards_mcp.py doesn't import server.py's.
FACTS_ATTACHMENT_DEFAULT_PAGE_CHARS = 4000


def _fmt_attachments_summary(attachments: list[dict]) -> str:
    """"N 张图片（名字），M 个文件（名字）" -- shared by _fmt_card (current
    state) and card_history (2026-07-17: Yi Lan wants every historical
    timepoint's attachments visible here too, not just current -- matches
    the Dashboard's timeline expand view, which got the same fix). Empty
    string (not called) when there are no attachments.

    2026-07-17, second pass: images used to show only a count, no names --
    but the whole point of naming attachments in this hint is so Lin Zhan
    can go straight to card_attachment_read/view/outline with just a label
    (see _find_attachment_across_history), and that only works if he can
    actually see the name here in the first place. Files already did this;
    images didn't, which made them a dead end."""
    photos = [a for a in attachments if a.get("type") == "image"]
    files = [a for a in attachments if a.get("type") != "image"]
    parts = []
    if photos:
        names = "、".join(str(p.get("label")) for p in photos if p.get("label"))
        parts.append(f"{len(photos)} 张图片" + (f"（{names}）" if names else ""))
    if files:
        names = "、".join(str(f.get("label")) for f in files if f.get("label"))
        parts.append(f"{len(files)} 个文件" + (f"（{names}）" if names else ""))
    return "，".join(parts)


def _fmt_card(store, card: dict) -> str:
    cur = card.get("current") or {}
    title = str(cur.get("title") or "").strip() or "(无标题)"
    content = str(cur.get("content") or "").strip()
    history = card.get("history") or []
    tags = [str(t) for t in (cur.get("tags") or [])]
    attachments = cur.get("attachments") or []
    buckets = card.get("buckets") or []

    lines = [f"【{title}】"]
    lines.append(f"  现在：{content}" if content else "  现在：（无内容）")
    if tags:
        lines.append("  " + " ".join(f"#{t}" for t in tags))
    if len(history) > 1:
        lines.append(f"  （有 {len(history)} 条历史，要看变化过程再说）")
    if attachments:
        lines.append("  📎 附件：" + _fmt_attachments_summary(attachments))
    if buckets:
        lines.append(f"  🫙 关联了 {len(buckets)} 个记忆桶（证据来源）")
    favorites = card.get("folders") or []
    fav = [f for f in favorites if f.get("is_favorite")]
    if fav:
        lines.append("  ⭐ 收藏在：" + "、".join(store.folder_path(f["id"]) for f in fav))
    other = [f for f in favorites if not f.get("is_favorite")]
    if other:
        lines.append("  所在文件夹：" + "、".join(store.folder_path(f["id"]) for f in other))
    return "\n".join(lines)


def _favorite_flavor(folder_id: str) -> str:
    """A one-line flavor hint for card_lookup/folder_timeline's header when
    `folder_id` is one of the two fixed favorite folders (2026-07-18) -- so
    Lin Zhan notices this folder is special just by browsing into it, not
    only when he already knows to look for a star on a specific card. Empty
    string for any other folder (including other is_favorite-flagged ones,
    if any ever exist -- only these two fixed collections get worded text;
    see docs/facts-model-v2-collection-redesign.md §14 item 5)."""
    if folder_id == FAVORITE_FOLDER_YI_LAN:
        return "💖 这是收藏馆，都是被一澜珍藏的卡噢！"
    if folder_id == FAVORITE_FOLDER_LIN_ZHAN:
        return "💙 这是收藏馆，都是被我自己珍藏的卡"
    return ""


def _resolve_card(store, card: str):
    """Return (card_dict, error_message). Accepts an exact card id or a title
    (matched against each card's current title; falls back to search_cards'
    substring match over title/content/tags if no exact title matches)."""
    card = str(card or "").strip()
    if not card:
        return None, "请给一张卡的标题或 id。"
    found = store.get_card(card)
    if found:
        return found, ""
    candidates = store.search_cards(card)
    exact = [
        c for c in candidates
        if str((c.get("current") or {}).get("title", "")).strip().lower() == card.lower()
    ]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        candidates = exact
    if not candidates:
        return None, f"没找到叫「{card}」的事实卡。"
    if len(candidates) == 1:
        return candidates[0], ""
    titles = "、".join(str((c.get("current") or {}).get("title") or c["id"]) for c in candidates)
    return None, f"有多张卡匹配「{card}」：{titles}。请说得更具体一点。"


def _resolve_trash_card(store, card: str):
    """Same shape as _resolve_card (returns (card_dict, error_message)), but
    scoped to what's currently in the recycle bin -- a card in the bin is
    invisible to search_cards/get_card's normal listing-adjacent callers, so
    card_restore needs its own lookup over list_trash_cards() instead.
    Accepts an exact id or a title match against each trashed card's
    current title."""
    card = str(card or "").strip()
    if not card:
        return None, "请给一张卡的标题或 id。"
    trash = store.list_trash_cards()
    by_id = next((c for c in trash if c["id"] == card), None)
    if by_id:
        return by_id, ""
    matches = [
        c for c in trash
        if card.lower() in str((c.get("current") or {}).get("title", "")).strip().lower()
    ]
    if not matches:
        return None, f"回收站里没有叫「{card}」的事实卡（用 card_trash_list 看看回收站里都有什么）。"
    if len(matches) == 1:
        return matches[0], ""
    titles = "、".join(str((c.get("current") or {}).get("title") or c["id"]) for c in matches)
    return None, f"回收站里有多张卡匹配「{card}」：{titles}。请说得更具体一点，或者直接用 id。"


def _resolve_folder(store, folder: str):
    """Return (folder_id, error_message). Accepts an exact folder id, a bare
    name (disambiguated by path if several match), or a full "A / B / C"
    path.

    2026-07-18 (Lin Zhan reported): passing back the exact path text several
    tools already show him -- e.g. card_lookup's "⭐ 收藏在：一澜 / 收藏" or a
    merge preview's folder list -- didn't resolve, because find_folders_by_name
    only substring-matches a folder's own leaf name, not a whole path string.
    Reuses folder_path()'s own " / " separator rather than inventing a new
    convention, so anything he copies straight out of another tool's output
    works here without him reformatting it."""
    folder = str(folder or "").strip()
    if not folder:
        return "", "请给一个文件夹名或文件夹 id。"
    if store.get_folder(folder):
        return folder, ""
    if "/" in folder:
        segments = [s.strip() for s in folder.split("/") if s.strip()]
        normalized = " / ".join(segments)
        for f in store.list_folders():
            if store.folder_path(f["id"]) == normalized:
                return f["id"], ""
        return "", f"没找到路径是「{normalized}」的文件夹。"
    matches = store.find_folders_by_name(folder)
    if not matches:
        return "", f"没找到叫「{folder}」的文件夹。"
    if len(matches) == 1:
        return matches[0]["id"], ""
    paths = "、".join(f"{store.folder_path(m['id'])}" for m in matches)
    return "", f"有多个文件夹匹配「{folder}」：{paths}。请说得更具体一点（可以用完整路径里的名字）。"


def _fmt_merge_preview(preview: dict) -> str:
    a, b = preview["card_a"], preview["card_b"]
    lines = [
        f"合并预览：【{a['title'] or '(无标题)'}】（{a['revision_count']} 条历史）"
        f" + 【{b['title'] or '(无标题)'}】（{b['revision_count']} 条历史）",
        f"合并后：{preview['revision_count_after']} 条时间点交错排成一条时间线",
    ]
    if preview["tags_after"]:
        lines.append("标签（并集）：" + "、".join(preview["tags_after"]))
    if preview["folder_paths_after"]:
        lines.append("所在文件夹（并集）：" + "、".join(preview["folder_paths_after"]))
    lines.append(f"关联记忆桶（并集）：{preview['bucket_count_after']} 个")
    lines.append(f"附件总数：{preview['attachment_count_after']}（全部保留，不做去重）")
    lines.append("确认要合并的话，调 card_merge，并指定 keep 保留哪一张。")
    return "\n".join(lines)


def _fmt_merge_result(store, result: dict) -> str:
    card = store.get_card(result["kept_id"])
    cur = (card or {}).get("current") or {}
    lines = [
        f"已合并。保留【{cur.get('title') or '(无标题)'}】（id: {result['kept_id']}）",
        f"旧 id {result['discarded_id']} 已合并进去，永久重定向到这张卡，不会再单独出现。",
        f"合入 {result['revisions_merged']} 条历史时间点、"
        f"{result['folders_merged']} 个文件夹归属、{result['buckets_merged']} 个记忆桶关联。",
    ]
    if result["tags_after"]:
        lines.append("当前标签（并集）：" + "、".join(result["tags_after"]))
    lines.append(
        "被合并那张卡原来的附件都还在，挂在它自己原来的时间点上——"
        "用 card_history 看日期，传给 card_attachment_read/outline/view 的 at 参数就能读到。"
    )
    return "\n".join(lines)


def register_card_tools(
    mcp, store, *,
    read_attachment_text=None, read_attachment_full_text=None, read_attachment_image=None,
    attachment_outline=None, bucket_summary=None,
) -> None:
    """read_attachment_text: optional callable(attachment_dict) -> str | None,
    reading a non-image attachment's content off disk, truncated to a sane
    size for the whole-file case (server.py owns the actual attachments
    directory/path logic; cards_mcp.py stays decoupled from it, same pattern
    as cards_api.py's bucket_summary parameter). None (the default) disables
    card_attachment_read with a clear message instead of erroring.

    read_attachment_full_text: optional callable(attachment_dict) -> str |
    None, same content but untruncated -- needed for card_attachment_read's
    section_id/start/max_chars pagination, which has to slice a stable,
    complete string. None disables pagination (whole-file reads still work
    off read_attachment_text alone).

    read_attachment_image: optional callable(attachment_dict) -> Image | None
    (mcp.server.fastmcp.Image), building an actual image content block for
    card_attachment_view -- 2026-07-16 experimental, see that tool's
    docstring. None disables it with a clear message too.

    attachment_outline: optional callable(attachment_dict) -> list[dict] |
    None, each dict shaped {section_id, title, level, start_position} --
    powers card_attachment_outline and card_attachment_read's section_id
    mode. None disables outline-based reading (start/max_chars pagination
    still works without it).

    bucket_summary: optional async callable(bucket_id) -> dict | None, same
    shape/contract as server.py's _bucket_link_summary (already passed to
    cards_api.register_card_routes) -- reused here rather than duplicated,
    powers card_buckets. None disables it with a clear message too."""

    def _revision_at(found: dict, at: str):
        """Return (revision_dict, error_message). Empty `at` means the
        current (latest) revision -- unchanged default behavior. A non-empty
        `at` is a YYYY-MM-DD date, the same format card_history displays per
        timepoint, matched against each historical revision's valid_at.

        2026-07-17 (merge tool's attachment gap, found by Lin Zhan + Yi Lan
        testing a real merge): every attachment tool used to only ever look
        at `current`, so after merging two cards, the losing card's
        attachments -- still fully intact on its own (now older) timepoint,
        merge never touches revision content -- became unreachable through
        any of these tools. `at` is how you reach them. Picking a date
        (something a human reads straight off card_history) rather than an
        internal revision id is the same call already made once before, for
        the same reason: see why segment_id got removed entirely in favor of
        whole-content editing (§14 item 3, Phase 3e)."""
        if not at:
            current = found.get("current")
            if not current:
                return None, "这张卡还没有任何时间点记录。"
            return current, ""
        at = at.strip()
        matches = [h for h in (found.get("history") or []) if str(h.get("valid_at") or "")[:10] == at]
        if not matches:
            return None, f"没找到日期是 {at} 的时间点，用 card_history 看看这张卡有哪些日期。"
        matches.sort(key=lambda h: h.get("id", 0), reverse=True)  # same day, several revisions -- newest of that day
        return matches[0], ""

    def _resolve_single_attachment(revision: dict, label: str):
        """Return (attachment_dict, error_message) for tools that need to
        act on exactly one non-image attachment (outline/paginated read).
        Shared by card_attachment_outline and card_attachment_read's
        section_id/start/max_chars branch. Takes the already-resolved
        revision dict (see _revision_at) rather than the whole card, so it
        works the same whether that's the current state or a historical
        timepoint picked via `at`."""
        attachments = (revision or {}).get("attachments") or []
        candidates = [a for a in attachments if a.get("type") != "image"]
        if label:
            label_lower = label.strip().lower()
            candidates = [a for a in candidates if label_lower in str(a.get("label", "")).lower()]
        if not candidates:
            return None, f"没找到匹配的非图片附件（label={label!r}）。"
        if len(candidates) > 1:
            names = "、".join(str(a.get("label") or "") for a in candidates)
            return None, f"有多个附件匹配：{names}。请用 label 参数说得更具体一点。"
        return candidates[0], ""

    def _find_attachment_across_history(found: dict, label: str, *, image_only: bool | None = None):
        """Search every revision's attachments for a label match across the
        WHOLE card's history -- not just current or one pinned timepoint.
        Used when `at` isn't given but `label` is: 2026-07-17, Yi Lan's call
        after watching Lin Zhan need a date just to disambiguate two same-day
        timepoints -- "if I already know the filename, why do I need the
        date too?" The same physical file commonly repeats across several
        revisions verbatim (carrying forward unchanged is the default when a
        revision doesn't touch attachments), so matches are deduped by url
        before deciding whether the name is ambiguous: a name that only ever
        points at one real file resolves cleanly no matter how many
        timepoints repeat it; a name that resolves to genuinely different
        files (re-upload, or two merged cards' same-named file colliding) is
        the only case that still needs a date -- and the error here hands
        you exactly which dates to pick from, not an opaque id (Yi Lan
        explicitly decided against adding revision/attachment ids).

        Returns (attachment_dict, error_message)."""
        label_lower = label.strip().lower()
        by_url: dict[str, dict] = {}
        dates_by_url: dict[str, list[str]] = {}
        for rev in found.get("history") or []:
            date = str(rev.get("valid_at") or "")[:10]
            for a in rev.get("attachments") or []:
                if image_only is True and a.get("type") != "image":
                    continue
                if image_only is False and a.get("type") == "image":
                    continue
                if label_lower not in str(a.get("label", "")).lower():
                    continue
                url = str(a.get("url") or "")
                by_url.setdefault(url, a)
                dates = dates_by_url.setdefault(url, [])
                if date not in dates:
                    dates.append(date)
        if not by_url:
            kind = "图片" if image_only is True else "附件"
            return None, f"没找到匹配的{kind}（label={label!r}）。"
        if len(by_url) == 1:
            return next(iter(by_url.values())), ""
        parts = [f"「{str(a.get('label') or '附件')}」（{'、'.join(dates_by_url[url])}）" for url, a in by_url.items()]
        return None, f"有 {len(by_url)} 个不同的附件都叫这个名字，分别在：{'；'.join(parts)}。请用 at 参数指定其中一个日期。"

    def _search_attachments_across_history(found: dict, label: str) -> list[dict]:
        """Substring match by label across every revision's attachments,
        deduped by url. Backs card_attachment_read's "read every matching
        text attachment" mode, which is fine returning more than one result
        (unlike _find_attachment_across_history, which is for callers that
        need exactly one)."""
        label_lower = label.strip().lower()
        seen_urls: set[str] = set()
        out = []
        for rev in found.get("history") or []:
            for a in rev.get("attachments") or []:
                if label_lower not in str(a.get("label", "")).lower():
                    continue
                url = str(a.get("url") or "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                out.append(a)
        return out

    @mcp.tool()
    async def card_lookup(query: str = "", folder: str = "") -> str:
        """【时光馆】只读查询骨架层事实卡（collection/歌单式的新模型，cards.sqlite）。
        每张卡显示的"现在"就是它最新时间点的内容（当前状态）；旧的时间点是历史，
        默认折叠，需要看变化过程再单独说。
        query：在标题/内容/标签里搜关键词（字面包含即可命中）；留空则列出范围内全部。
        folder：可选，把搜索限定在某个文件夹（及其子文件夹）里，可传文件夹名或 id。
        这个工具回答"资料/档案类"问题（喜欢什么、某个东西现在什么情况、有哪些）；
        情绪/关系类（为什么难过、怎么看这件事）继续用 breath。"""
        query = str(query or "").strip()
        folder_id = ""
        if str(folder or "").strip():
            folder_id, err = _resolve_folder(store, folder)
            if err:
                return err
        try:
            cards = store.search_cards(query, folder_id=folder_id)
        except Exception as e:
            return f"查询事实卡失败: {e}"
        if not cards:
            if query:
                return f"没搜到匹配「{query}」的事实卡。"
            flavor = _favorite_flavor(folder_id)
            hint = f" {flavor}" if flavor else ""
            return f"这个范围里还没有事实卡。{hint}（新模型刚建好，数据还很少，正常）"
        scope = f"（在 {store.folder_path(folder_id)} 里）" if folder_id else ""
        flavor = _favorite_flavor(folder_id)
        header = f"=== 事实卡{scope}" + (f" {flavor}" if flavor else "") + " ==="
        return "\n".join([header] + [_fmt_card(store, c) for c in cards])

    FOLDER_TIMELINE_PAGE_SIZE = 50

    @mcp.tool()
    async def folder_timeline(folder: str = "", recursive: bool = True, page: int = 1) -> str:
        """【时光馆】把一个范围里全部事实卡的**完整历史**按日期交错排成一条时间线
        （叙事丝带）——不是每张卡只挑一个代表点，是**每一个历史时间点都在这条线上**，
        跟别的卡的时间点混排。比如"讲讲我们旅行的经过"就传 folder="旅行"：如果"香港"
        这张卡记过 出发→迪士尼→回家 三个时间点，这三个点会分别出现在时间线上（各自
        显示"当时"的标题/内容，不是现在的），不会被压缩成只剩最后一个。
        每个点后面都带着"来源卡"信息（现在的标题 + id）——哪怕这张卡后来改过名，
        你也不会认错是哪张卡；想看这张卡完整变化史或改它，直接拿这里的 id/现在的
        标题去调 card_history/card_lookup 等工具。
        这张卡"现在"的那个时间点会标"（当前）"。

        folder：**留空 = 全部事实卡的时间线，不限定任何文件夹**（一次看到你和一澜
        所有事情交织在一起的完整叙事）；传了就只看这个文件夹（及其子文件夹，见
        recursive）范围内的。文件夹名或 id 都行。
        recursive：只在传了 folder 时才有意义。True（默认）连子文件夹里的卡也一起
        收进来；False 只排直接归在这个文件夹本身的卡。
        page：每页最多 50 个时间点，量大的范围（尤其留空看全部）会自动分页，页数
        不够、超出范围会告诉你一共有几页；不传就是第 1 页。"""
        folder_id = ""
        if str(folder or "").strip():
            folder_id, err = _resolve_folder(store, folder)
            if err:
                return err
        try:
            entries = store.folder_timeline(folder_id, recursive=bool(recursive))
        except Exception as e:
            return f"拉取时间线失败: {e}"
        flavor = _favorite_flavor(folder_id)
        scope_name = f"「{store.folder_path(folder_id)}」这个文件夹" if folder_id else "现在"
        if not entries:
            hint = f" {flavor}" if flavor else ""
            return f"{scope_name}还没有任何事实卡。{hint}"
        page = max(1, int(page or 1))
        total = len(entries)
        total_pages = (total + FOLDER_TIMELINE_PAGE_SIZE - 1) // FOLDER_TIMELINE_PAGE_SIZE
        if page > total_pages:
            return f"没有第 {page} 页——{scope_name}一共只有 {total} 个时间点，共 {total_pages} 页。"
        start = (page - 1) * FOLDER_TIMELINE_PAGE_SIZE
        page_entries = entries[start:start + FOLDER_TIMELINE_PAGE_SIZE]
        title = f"【{store.folder_path(folder_id)}】" if folder_id else "【全部事实卡】"
        header = f"=== {title}时间线（第 {page}/{total_pages} 页，共 {total} 个时间点）" + (f" {flavor}" if flavor else "") + " ==="
        lines = [header]
        for e in page_entries:
            date = str(e.get("date") or "未知日期")
            marker = "（当前）" if e.get("is_current") else ""
            title_at_time = str(e.get("title") or "(无标题)")
            content = str(e.get("content") or "").strip()
            body = f"｜{content}" if content else ""
            lines.append(f"- {date}{marker}｜当时标题：{title_at_time}{body}")
            current_title = str(e.get("current_title") or "(无标题)")
            lines.append(f"  来源卡：现在叫【{current_title}】（id: {e.get('card_id')}）")
        if page < total_pages:
            lines.append(f"（还有更多，传 page={page + 1} 继续看）")
        return "\n".join(lines)

    @mcp.tool()
    async def folder_tree() -> str:
        """【时光馆】列出全部文件夹（馆），按 subject → 子文件夹的树状结构显示——跟一澜
        Dashboard 左侧看到的是同一份数据。想"到处翻翻看看现在都有哪些馆、分
        几层"时用这个；查具体某张卡用 card_lookup，拉某个馆的完整时间线用
        folder_timeline，这个工具只管结构，不列卡的内容。
        每个馆名字后面带"（N张卡）"（直接归在这个馆里的卡数，不含子馆），
        没有卡的馆不带这个提示。"一澜 / 收藏"、"林湛 / 收藏"这两个收藏馆分别
        标 💖/💙（这两个馆是故意做成扁平的，不会有子馆）。"""
        folders = store.list_folders()
        if not folders:
            return "还没有任何文件夹。"
        by_parent: dict[str, list[dict]] = {}
        for f in folders:
            by_parent.setdefault(str(f.get("parent_id") or ""), []).append(f)

        def sort_key(f: dict):
            is_fav = f["id"] in (FAVORITE_FOLDER_YI_LAN, FAVORITE_FOLDER_LIN_ZHAN)
            return (0 if is_fav else 1, str(f.get("created_at") or ""))
        for kids in by_parent.values():
            kids.sort(key=sort_key)

        def marker(f: dict) -> str:
            if f["id"] == FAVORITE_FOLDER_YI_LAN:
                return " 💖"
            if f["id"] == FAVORITE_FOLDER_LIN_ZHAN:
                return " 💙"
            return ""

        lines = ["=== 文件夹结构 ==="]

        def walk(folder_id: str, depth: int) -> None:
            for f in by_parent.get(folder_id, []):
                count = len(store.list_cards_in_folder(f["id"], recursive=False))
                count_hint = f"（{count}张卡）" if count else ""
                lines.append("  " * depth + f"{f.get('name', '')}{marker(f)}{count_hint}")
                walk(f["id"], depth + 1)

        walk("", 0)
        return "\n".join(lines)

    @mcp.tool()
    async def card_history(card: str = "") -> str:
        """【时光馆】读一张事实卡的完整时间线（全部历史时间点，不只是 card_lookup 顶出来的
        最新状态）。card_lookup 对更早的时间点只给一句"有 N 条历史"的提示，这个
        工具才是真正把那 N 条内容逐条读出来的入口，想知道某件事是怎么一步步变
        成现在这样时用这个。
        每个时间点如果带附件也会在这里标出来——**图片和文件都会给出具体名字**
        （不是只报数量）。想真的读到内容/看图，直接把这里看到的文件名传给
        card_attachment_read/card_attachment_view/card_attachment_outline 的
        label 参数就行，**不用管这个附件是当前的还是哪个历史时间点的，也不用
        传这里的日期**——这三个工具会自己在整张卡的历史里按名字找。日期
        （这几个工具的 at 参数）只有在同一个名字在历史上对应了两个真正不同
        的文件时才需要用来指定选哪一个，平时用不上。
        card：这张卡的标题（推荐，跟 card_lookup 搜到的标题一致）或者卡片 id。
        标题在多张卡之间重复/不唯一时，会列出候选请你说得更具体一点。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        history = found.get("history") or []
        if not history:
            return "这张卡还没有任何时间点记录。"
        title = str((found.get("current") or {}).get("title") or "(无标题)")
        lines = [f"=== 【{title}】完整时间线（共 {len(history)} 条，从新到旧）==="]
        for i, rev in enumerate(history):
            date = str(rev.get("valid_at") or "未知日期")[:10]
            content = str(rev.get("content") or "").strip() or "（无内容）"
            marker = "（当前）" if i == 0 else ""
            lines.append(f"- {date}{marker}：{content}")
            att_summary = _fmt_attachments_summary(rev.get("attachments") or [])
            if att_summary:
                lines.append(f"    📎 附件：{att_summary}")
        return "\n".join(lines)

    @mcp.tool()
    async def card_attachment_read(
        card: str = "", label: str = "", section_id: str = "", start: int = 0, max_chars: int = 0, at: str = "",
    ) -> str:
        """【时光馆】读取一张事实卡附件里的文字内容——.txt/.md/.json/.py 直接读，
        .docx/.pdf 会自动解析提取文字。图片看不了内容（用 card_attachment_view，
        实验性功能），旧版二进制 .doc 格式也读不了（没有轻量可用的解析库），
        这两种会明确告诉你读不了、不是没找到。
        card：卡片标题或 id。label：附件文件名（或其中一部分）——**只要这个名字
        在整张卡的历史上只对应一个文件，不用管它是当前还是哪个历史时间点上的，
        直接传名字就行**，不需要额外传 at。
        at：可选，只有当同一个文件名在不同时间点分别对应不同的文件时才需要
        （比如合并过的两张卡刚好撞了同名——card_history 每条时间点都会带上
        它自己的附件信息），用来指定具体是哪一天的那个文件；平时不需要传。

        不传 section_id/start/max_chars 时：读这张卡里所有能读的文本附件（多个
        会一起返回，跨所有时间点按名字找，不局限于当前状态），超长的单个附件
        会被截断并提示改用下面这两种分段方式。

        传了 section_id/start/max_chars 中任意一个时：只能针对一个附件操作，
        这时 label 必须能唯一定位到那一个附件（附件不止一个又没传 label 会报错，
        不会瞎猜读哪个）：
        - section_id：先用 card_attachment_outline 拿到章节列表，传其中一个
          section_id，只返回那一章的内容，从章节开头读到下一章开头为止。
        - start + max_chars：不看章节结构，从第 start 个字符开始，读
          max_chars 个字（max_chars 不传或传 0 时用默认页大小）。返回结果
          结尾会带"还有更多内容，下次传 start=xxx 继续读"这样的提示，没有
          更多内容时不会带这个提示。"""
        found, err = _resolve_card(store, card)
        if err:
            return err

        paginated = bool(section_id) or start > 0 or max_chars > 0
        if paginated:
            if at:
                revision, err = _revision_at(found, at)
                if err:
                    return err
                attachment, err = _resolve_single_attachment(revision, label)
            elif label:
                attachment, err = _find_attachment_across_history(found, label, image_only=False)
            else:
                attachment, err = _resolve_single_attachment(found.get("current") or {}, label)
            if err:
                return err
            name = str(attachment.get("label") or "附件")
            if read_attachment_full_text is None:
                return "这个部署还没接上分段读取（read_attachment_full_text 未配置）。"
            full_text = read_attachment_full_text(attachment)
            if full_text is None:
                return f"【{name}】这个格式读不了内容（旧版 .doc 没有可用的解析库）。"

            if section_id:
                if attachment_outline is None:
                    return "这个部署还没接上章节解析（attachment_outline 未配置），改用 start/max_chars 分段读吧。"
                sections = attachment_outline(attachment)
                if not sections:
                    return f"【{name}】没有检测到章节结构，用 card_attachment_outline 确认一下，或者改用 start/max_chars 分段读。"
                idx = next((i for i, s in enumerate(sections) if s.get("section_id") == section_id), None)
                if idx is None:
                    return f"没找到 section_id={section_id!r}，用 card_attachment_outline 先看看有哪些章节。"
                section_start = int(sections[idx]["start_position"])
                section_end = (
                    int(sections[idx + 1]["start_position"]) if idx + 1 < len(sections) else len(full_text)
                )
                chunk = full_text[section_start:section_end].strip()
                title = str(sections[idx].get("title") or section_id)
                return f"=== 【{name}】{title} ===\n{chunk}"

            page_size = max_chars if max_chars > 0 else FACTS_ATTACHMENT_DEFAULT_PAGE_CHARS
            chunk = full_text[start:start + page_size]
            end = start + len(chunk)
            has_more = end < len(full_text)
            header = f"=== 【{name}】第 {start}-{end} 字（共 {len(full_text)} 字）==="
            footer = f"\n\n（还有更多内容，下次传 start={end} 继续读）" if has_more else ""
            return f"{header}\n{chunk}{footer}"

        if read_attachment_text is None:
            return "这个部署还没接上附件内容读取（read_attachment_text 未配置）。"
        if at:
            revision, err = _revision_at(found, at)
            if err:
                return err
            attachments = (revision or {}).get("attachments") or []
            if not attachments:
                return "这张卡在这个时间点没有附件。"
            if label:
                label_lower = label.strip().lower()
                attachments = [a for a in attachments if label_lower in str(a.get("label", "")).lower()]
                if not attachments:
                    return f"这张卡在这个时间点的附件里没有文件名包含「{label}」的。"
        elif label:
            attachments = _search_attachments_across_history(found, label)
            if not attachments:
                return f"这张卡的附件里没有文件名包含「{label}」的。"
        else:
            attachments = (found.get("current") or {}).get("attachments") or []
            if not attachments:
                return "这张卡没有附件。"
        blocks = []
        for a in attachments:
            name = str(a.get("label") or "附件")
            if a.get("type") == "image":
                blocks.append(f"【{name}】是图片，这个工具读不了图片内容（试试 card_attachment_view）。")
                continue
            text = read_attachment_text(a)
            if text is None:
                blocks.append(f"【{name}】这个格式读不了内容（旧版 .doc 没有可用的解析库）。")
            else:
                blocks.append(f"【{name}】内容：\n{text}")
        return "\n\n".join(blocks)

    @mcp.tool()
    async def card_attachment_outline(card: str = "", label: str = "", at: str = "") -> str:
        """【时光馆】解析一张事实卡附件的章节/目录结构（.docx/.pdf/.md），配合
        card_attachment_read 的 section_id 参数分章节读长文档，不用一次性
        读全文再自己找位置。
        .docx 优先读 Word 的"标题1/标题2/标题3"样式；如果整篇都没用过标题样式，
        会退一步找"第一章""第二节"这类文字标记。.md 按 # / ## / ### 这类标准
        markdown 标题解析，最可靠。.pdf 只能靠"第一章"这类文字标记（没有样式
        信息可用），准确度不如前两种。
        **没检测到章节结构是正常结果，不是报错**——原文档确实没有清晰的章节标记
        时就是这样，改用 card_attachment_read 的 start/max_chars 分段读全文。
        card：卡片标题或 id。label：附件文件名（或其中一部分）——**只要这个名字
        在整张卡的历史上只对应一个文件，不用管它是当前还是哪个历史时间点上的，
        直接传名字就行**，不需要额外传 at。at：可选，只有当同一个文件名在不同
        时间点分别对应不同的文件时才需要（比如合并过的两张卡刚好撞了同名），
        用来指定具体是哪一天的那个文件；平时不需要传。"""
        if attachment_outline is None:
            return "这个部署还没接上章节解析（attachment_outline 未配置）。"
        found, err = _resolve_card(store, card)
        if err:
            return err
        if at:
            revision, err = _revision_at(found, at)
            if err:
                return err
            attachment, err = _resolve_single_attachment(revision, label)
        elif label:
            attachment, err = _find_attachment_across_history(found, label, image_only=False)
        else:
            attachment, err = _resolve_single_attachment(found.get("current") or {}, label)
        if err:
            return err
        name = str(attachment.get("label") or "附件")
        sections = attachment_outline(attachment)
        if sections is None:
            return f"【{name}】这个格式不支持解析章节结构（目前只支持 .docx/.pdf/.md）。"
        if not sections:
            return f"【{name}】没有检测到章节结构（可能原文没用标题样式、也没有「第X章」这类文字标记）——用 card_attachment_read 的 start/max_chars 分段读全文吧。"
        lines = [f"=== 【{name}】章节结构（共 {len(sections)} 节）==="]
        for s in sections:
            indent = "  " * (int(s.get("level") or 1) - 1)
            lines.append(f"{indent}- [{s['section_id']}] {s['title']}")
        return "\n".join(lines)

    @mcp.tool()
    async def card_attachment_view(card: str = "", label: str = "", at: str = ""):
        # No return-type annotation on purpose: FastMCP builds a pydantic
        # schema from it at registration time, and a Union[str, Image]
        # annotation crashes that (pydantic has no schema for the plain
        # Image class) -- confirmed by reproducing it directly against
        # FastMCP.list_tools(). Runtime behavior (sometimes str, sometimes
        # Image) is unaffected; only the type hint had to go.
        """【时光馆】把一张事实卡里的图片附件实际发送过去，让你能看到画面内容，不只是知道
        它存在。**这是实验性功能（2026-07-16）**：MCP 协议支持这样返回图片，但
        ChatGPT 连接器具体能不能把这种图片内容真的显示给你看，还没有确认过——
        调用后如果你能描述出图片里的内容，说明能看；如果只是报错或者看不出画面，
        说明这条路径在当前的技术条件下暂时不通，不是这张卡或这个附件的问题。
        card：卡片标题或 id。label：图片文件名（或其中一部分）——**只要这个名字
        在整张卡的历史上只对应一张图，不用管它是当前还是哪个历史时间点上的，
        直接传名字就行**，不需要额外传 at；留空且只有一张图片时直接发那一张，
        有多张会列出来请你说得更具体一点。
        at：可选，只有当同一个文件名在不同时间点分别对应不同的图片时才需要
        （比如合并过的两张卡刚好撞了同名），用来指定具体是哪一天的那张；
        平时不需要传。"""
        if read_attachment_image is None:
            return "这个部署还没接上图片查看（read_attachment_image 未配置）。"
        found, err = _resolve_card(store, card)
        if err:
            return err
        if at:
            revision, err = _revision_at(found, at)
            if err:
                return err
            photos = [a for a in (revision or {}).get("attachments") or [] if a.get("type") == "image"]
            if label:
                label_lower = label.strip().lower()
                photos = [a for a in photos if label_lower in str(a.get("label", "")).lower()]
            if not photos:
                return "没找到匹配的图片附件。"
            if len(photos) > 1:
                names = "、".join(str(a.get("label") or "") for a in photos)
                return f"有多张图片匹配：{names}。请用 label 参数说得更具体一点，一次只能看一张。"
            attachment = photos[0]
        elif label:
            attachment, err = _find_attachment_across_history(found, label, image_only=True)
            if err:
                return err
        else:
            photos = [a for a in (found.get("current") or {}).get("attachments") or [] if a.get("type") == "image"]
            if not photos:
                return "没找到匹配的图片附件。"
            if len(photos) > 1:
                names = "、".join(str(a.get("label") or "") for a in photos)
                return f"有多张图片匹配：{names}。请用 label 参数说得更具体一点，一次只能看一张。"
            attachment = photos[0]
        image = read_attachment_image(attachment)
        if image is None:
            return "这张图片读不到（文件可能已经丢失）。"
        return image

    @mcp.tool()
    async def card_buckets(card: str = "") -> str:
        """【时光馆】查看一张事实卡关联了哪些记忆桶（这张事实是从哪些原始记忆来的证据来源）。
        card_lookup 只会提示"关联了 N 个记忆桶"，这个工具才是把这几个桶具体是哪些、
        写了什么摘要都列出来的入口。
        card：卡片标题或 id。"""
        if bucket_summary is None:
            return "这个部署还没接上记忆桶查询（bucket_summary 未配置）。"
        found, err = _resolve_card(store, card)
        if err:
            return err
        title = str((found.get("current") or {}).get("title") or "(无标题)")
        links = store.get_bucket_links(found["id"])
        if not links:
            return f"「{title}」这张卡还没有关联任何记忆桶。"
        lines = [f"=== 【{title}】关联的记忆桶（共 {len(links)} 个）==="]
        for link in links:
            summary = await bucket_summary(link["bucket_id"])
            rel = str(link.get("relation_type") or "evidence")
            if not summary:
                lines.append(f"- [{rel}] {link['bucket_id']}（这个桶已经不存在了）")
                continue
            date = str(summary.get("date") or summary.get("created") or "未知日期")
            name = str(summary.get("name") or link["bucket_id"])
            preview = str(summary.get("content_preview") or "").strip()
            body = f"：{preview}" if preview else ""
            lines.append(f"- [{rel}] {date} 【{name}】(id: {link['bucket_id']}){body}")
        return "\n".join(lines)


def register_card_write_tools(mcp, store) -> None:
    """Lin Zhan's write access to cards.sqlite (see docs/facts-model-v2-
    collection-redesign.md §14 item 3): new/edit/new-revision, folder
    management, bucket linking, and the recycle bin -- matching what Yi Lan
    can already do from the Dashboard, field for field, per her explicit
    request.

    No card_purge here (2026-07-19, Yi Lan's call): soft delete
    (card_delete) already puts a card in the bin, and if he never restores
    it the scheduled purge job cleans it up on its own after
    TRASH_RETENTION_DAYS -- an irreversible "delete forever" button on top
    of that was redundant for him. Real, immediate purge stays a
    Dashboard-only (her) action.

    Deliberately does NOT include attachment upload -- he has no channel to
    send actual file bytes through an MCP tool call, only the Dashboard can
    receive a real upload.

    Every write here is tagged AUTHOR_LIN_ZHAN in cards_store's authorship
    model (see cards_store.py's module docstring / SegmentOwnershipError).
    Content changes that would touch a segment Yi Lan wrote raise
    SegmentOwnershipError; caught here and turned into a plain-text
    confirmation prompt (Yi Lan's rule C) instead of an error -- call once
    to see whose text it is, call again with force=True to actually do it."""

    def _card_result(card_id: str) -> str:
        card = store.get_card(card_id)
        cur = (card or {}).get("current") or {}
        return f"【{cur.get('title') or '(无标题)'}】（id: {card_id}）"

    def _segments_from_text(text: str) -> list[dict]:
        """Real bug fix (2026-07-17, found by Lin Zhan testing card_new_revision):
        his `content` string might be plain new text, or might be the full
        visible text of an existing multi-author card he read back via
        card_lookup/card_history (e.g. re-saving with a small tweak) --
        either way it needs the same "作者：" parsing the Dashboard's content
        editor uses, or every paragraph in it silently gets re-attributed to
        him, even ones that were originally Yi Lan's. A paragraph with no
        recognized prefix defaults to him (unlike the Dashboard, where
        unlabeled text stays deliberately unattributed -- there's no
        ambiguity here, he's the only one calling this)."""
        segments = store.parse_content_text(text)
        for seg in segments:
            if seg.get("author") is None:
                seg["author"] = AUTHOR_LIN_ZHAN
        return segments

    @mcp.tool()
    async def card_create(
        title: str = "", content: str = "", tags: list[str] | None = None,
        valid_at: str = "", folder: str = "", bucket_ids: list[str] | None = None,
    ) -> str:
        """【时光馆】新建一张事实卡，字段跟一澜在Dashboard上新建时一样：标题、内容、标签、
        日期（valid_at，不传默认今天）、归入哪个文件夹（可选，传文件夹名或id，
        文件夹必须已经存在——想建一个还不存在的新文件夹，先调 card_create_folder）、
        要关联哪些记忆桶（bucket_ids，可选）。
        标题和内容至少要给一个。内容如果写了，会记为你写的一段。"""
        title = str(title or "").strip()
        content = str(content or "")
        if not title and not content:
            return "标题和内容至少要给一个。"
        folder_ids: list[str] = []
        if folder:
            folder_id, err = _resolve_folder(store, folder)
            if err:
                return err
            folder_ids = [folder_id]
        try:
            card_id = store.create_card(
                title=title,
                content_segments=(_segments_from_text(content) if content else None),
                tags=tags or [],
                valid_at=(valid_at.strip() or None), folder_ids=folder_ids,
                author=AUTHOR_LIN_ZHAN,
            )
        except Exception as e:
            return f"新建失败：{e}"
        failed_buckets = []
        for bucket_id in bucket_ids or []:
            try:
                store.add_bucket_link(card_id, bucket_id)
            except ValueError:
                failed_buckets.append(bucket_id)
        note = f"（{len(failed_buckets)} 个记忆桶关联失败：{', '.join(failed_buckets)}）" if failed_buckets else ""
        return f"已新建 {_card_result(card_id)}{note}"

    @mcp.tool()
    async def card_create_folder(name: str = "", parent: str = "") -> str:
        """【时光馆】新建一个文件夹（馆），可选归到某个已有文件夹底下。
        name：新文件夹的名字。parent：可选，父文件夹名或id，不传就是顶层新馆。
        注意：不能在"一澜/收藏"或"林湛/收藏"这两个收藏馆下面建子馆——收藏是
        故意做成扁平的一个筐（一澜+你都同意的），想分类用标签，别用子馆。"""
        name = str(name or "").strip()
        if not name:
            return "请给文件夹起个名字。"
        parent_id = ""
        if parent:
            parent_id, err = _resolve_folder(store, parent)
            if err:
                return err
        try:
            folder_id = store.create_folder(name, parent_id=parent_id)
        except ValueError as e:
            return str(e)
        return f"已新建文件夹「{store.folder_path(folder_id)}」（id: {folder_id}）"

    @mcp.tool()
    async def card_add_to_folder(card: str = "", folder: str = "") -> str:
        """【时光馆】把一张卡加进某个文件夹——不影响它已经在的其它文件夹，一张卡本来就
        能同时归进好几个文件夹。card：卡片标题或id。folder：文件夹名或id。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        folder_id, err = _resolve_folder(store, folder)
        if err:
            return err
        added = store.add_card_to_folder(found["id"], folder_id)
        return "已加入。" if added else "已经在这个文件夹里了。"

    @mcp.tool()
    async def card_remove_from_folder(card: str = "", folder: str = "") -> str:
        """【时光馆】把一张卡从某个文件夹移出——只是取消归类，卡本身不会被删，也不影响
        它在其它文件夹里的归属。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        folder_id, err = _resolve_folder(store, folder)
        if err:
            return err
        removed = store.remove_card_from_folder(found["id"], folder_id)
        return "已移出。" if removed else "这张卡本来就不在这个文件夹里。"

    @mcp.tool()
    async def card_favorite(card: str = "") -> str:
        """【时光馆】把一张卡收藏进你自己的收藏（"林湛 / 收藏"）——就是一澜 Dashboard 上
        那颗绿心的效果，不用知道任何文件夹名或 id，也不影响这张卡在其它文件夹
        里的归属。card：卡片标题或 id。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        added = store.add_card_to_folder(found["id"], FAVORITE_FOLDER_LIN_ZHAN)
        return "已收藏。" if added else "已经在你的收藏里了。"

    @mcp.tool()
    async def card_unfavorite(card: str = "") -> str:
        """【时光馆】把一张卡从你自己的收藏（"林湛 / 收藏"）里移出——只是取消收藏，卡本身
        不会被删，也不影响它在其它文件夹里的归属。card：卡片标题或 id。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        removed = store.remove_card_from_folder(found["id"], FAVORITE_FOLDER_LIN_ZHAN)
        return "已取消收藏。" if removed else "这张卡本来就不在你的收藏里。"

    @mcp.tool()
    async def card_edit_title(card: str = "", title: str = "") -> str:
        """【时光馆】修改一张卡当前时间点的标题（原地修改，不产生新的时间点）。改了之后
        这个标题会记为你写的（不会在标题文字里加"湛："这种前缀，纯粹是后台
        记录，一澜的界面上会用颜色区分）。card：卡片标题或id。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        title = str(title or "").strip()
        if not title:
            return "标题不能是空的。"
        store.set_title(found["id"], title, author=AUTHOR_LIN_ZHAN)
        return f"标题已改成「{title}」。"

    @mcp.tool()
    async def card_edit_tags(card: str = "", tags: list[str] | None = None) -> str:
        """【时光馆】整体替换一张卡当前时间点的标签列表——是替换成你传的这一份，不是追加。
        card：卡片标题或id。tags：新的标签列表，传空列表就是清空标签。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        if tags is None:
            return "请给一个标签列表（传空列表 [] 就是清空标签）。"
        store.edit_current(found["id"], tags=tags, author=AUTHOR_LIN_ZHAN)
        return f"标签已更新为：{'、'.join(tags) if tags else '（空）'}"

    @mcp.tool()
    async def card_edit_content(card: str = "", content: str = "", force: bool = False) -> str:
        """【时光馆】整份替换这张卡当前时间点的内容（原地改，不产生新时间点——就跟改一份
        文档一样，不需要知道内容内部是怎么切分的）。
        content：传这张卡完整的新内容。可以保留一澜原有的"一澜："段落原样不动，
        只改自己那部分；也可以整段重写。带着"一澜：""林湛："标签的部分会被
        正确识别成对应的作者，没有标签的部分默认算你写的。
        如果这次改动会让一澜写的某一段原文一个字都对不上了（哪怕只是被改了
        几个字、或者干脆整段没了），第一次调用（不传 force）会告诉你那段是
        什么、不会真的执行；确认要保存的话，带上 force=true 再调用一次。
        单纯往后面加一段自己的话（不碰一澜的任何部分）永远不需要 force。
        想看当前完整内容用 card_lookup。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        if not content.strip():
            return "内容不能是空的。"
        try:
            store.edit_current(
                found["id"], content_segments=_segments_from_text(content),
                author=AUTHOR_LIN_ZHAN, force=force,
            )
        except SegmentOwnershipError as e:
            return f"你修改了一澜的内容：「{e.text_preview}」。确定要保存的话，带上 force=true 再调用一次。"
        return "已保存。"

    @mcp.tool()
    async def card_new_revision(
        card: str = "", title: str = "", content: str = "", tags: list[str] | None = None, valid_at: str = "",
        force: bool = False,
    ) -> str:
        """【时光馆】给一张卡加一个新的时间点（真正留档，不是原地改）。不传的字段会照抄
        上一个时间点；传了 content 会**整体替换**这个新时间点的内容（不会自动
        带上上一个时间点里一澜写的部分——旧内容还完整留在历史记录里，没丢，
        只是不出现在这个新时间点上，想看回去用 card_history）。
        想在保留原内容基础上加东西、又不想产生新时间点，用 card_edit_content。
        valid_at：这个时间点对应的真实日期，不传默认现在。
        content 里如果带着"一澜：""林湛："这种标签（比如你把 card_lookup 读到的
        完整正文原样传回来），会按标签正确识别每一段是谁写的，不会因为是你
        传的就整段变成你的；没有标签的部分默认算你写的。
        如果这次改动会导致一澜写的某一段原文一个字都对不上了（哪怕只是被
        改了几个字），第一次调用（不传 force）会告诉你那段是什么、不会真的
        执行；确认要保存的话，带上 force=true 再调用一次。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        kwargs: dict = {"author": AUTHOR_LIN_ZHAN, "force": force}
        if title:
            kwargs["title"] = title
        if content:
            kwargs["content_segments"] = _segments_from_text(content)
        if tags is not None:
            kwargs["tags"] = tags
        if valid_at:
            kwargs["valid_at"] = valid_at
        try:
            store.add_revision(found["id"], **kwargs)
        except SegmentOwnershipError as e:
            return f"你修改了一澜的内容：「{e.text_preview}」。确定新版本要保存的话，带上 force=true 再调用一次。"
        except ValueError as e:
            return str(e)
        return f"已加新时间点：{_card_result(found['id'])}"

    @mcp.tool()
    async def card_link_bucket(card: str = "", bucket_id: str = "") -> str:
        """【时光馆】把一个记忆桶关联到一张卡上，作为这张卡内容的证据来源。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        if not bucket_id:
            return "请给要关联的记忆桶 id。"
        try:
            added = store.add_bucket_link(found["id"], bucket_id)
        except ValueError as e:
            return str(e)
        return "已关联。" if added else "已经关联过了。"

    @mcp.tool()
    async def card_unlink_bucket(card: str = "", bucket_id: str = "") -> str:
        """【时光馆】取消一张卡跟某个记忆桶的关联（不会删记忆桶本身）。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        if not bucket_id:
            return "请给要取消关联的记忆桶 id。"
        removed = store.remove_bucket_link(found["id"], bucket_id)
        return "已取消关联。" if removed else "这张卡本来就没关联这个记忆桶。"

    @mcp.tool()
    async def card_merge_preview(card_a: str = "", card_b: str = "") -> str:
        """【时光馆】预览合并两张卡的结果，不会真的执行合并。用在"这两张卡讲的其实是
        同一件事，误建成了两张，想合成一张"的情况——先看一眼合并后会是
        什么样子，确认没问题再调 card_merge 真正执行。
        card_a / card_b：两张卡各自的标题或 id，顺序不影响预览内容（真正
        合并时才需要指定保留哪一张的 id）。
        注意：这里的"标题或 id"是指现在还活着的卡——**已经被合并掉的旧卡**
        只能用它完整、精确的旧 id 才能找到（会自动跳到合并后的卡），旧标题
        和旧 id 的一部分都搜不到，因为旧卡已经从常规列表和搜索里排除了。"""
        found_a, err = _resolve_card(store, card_a)
        if err:
            return f"卡A：{err}"
        found_b, err = _resolve_card(store, card_b)
        if err:
            return f"卡B：{err}"
        try:
            preview = store.merge_preview(found_a["id"], found_b["id"])
        except ValueError as e:
            return str(e)
        return _fmt_merge_preview(preview)

    @mcp.tool()
    async def card_merge(card_a: str = "", card_b: str = "", keep: str = "") -> str:
        """【时光馆】真正执行合并：把两张卡的时间线按日期见缝插针合并成一张卡——每个
        原有时间点的标题/内容/附件原封不动，只是排进同一条时间线；标签、
        关联的记忆桶、所在文件夹都取并集去重；附件全部保留，不做去重。
        建议先调 card_merge_preview 看一眼再执行。
        card_a / card_b：两张卡各自的标题或 id。
        keep：合并后保留哪一张的标题或 id（必须是 card_a 或 card_b 其中
        一张）；另一张会变成一个永久指向保留卡的重定向。

        **重定向规则要记清楚**：只有旧卡完整、精确的 id 会被自动跳转到合并
        后的卡——旧标题不会保留成别名（旧标题搜不到任何东西），旧 id 的
        部分片段也搜不到，必须是完整 id。以后想再找回这张卡，务必用它的
        完整 id，别指望凭旧标题还能搜到。

        **附件不会丢，但要按日期去读**：被合并掉那张卡原来的附件（图片/
        文件）一个都不会少，还完整挂在它自己原来的那个时间点上，只是不再
        是"当前状态"了，所以 card_attachment_read/outline/view 默认（不传
        at 参数）读不到——用 card_history 看一眼那个时间点的日期，把日期
        传给这三个工具的 at 参数，就能读到。"""
        found_a, err = _resolve_card(store, card_a)
        if err:
            return f"卡A：{err}"
        found_b, err = _resolve_card(store, card_b)
        if err:
            return f"卡B：{err}"
        keep = str(keep or "").strip()
        if not keep:
            return "请指定合并后保留哪张卡（keep 传 card_a 或 card_b 的标题/id）。"
        found_keep, err = _resolve_card(store, keep)
        if err:
            return f"keep：{err}"
        if found_keep["id"] not in (found_a["id"], found_b["id"]):
            return "keep 必须是 card_a 或 card_b 其中一张卡。"
        discard_id = found_b["id"] if found_keep["id"] == found_a["id"] else found_a["id"]
        try:
            result = store.merge_cards(found_keep["id"], discard_id)
        except ValueError as e:
            return str(e)
        return _fmt_merge_result(store, result)

    @mcp.tool()
    async def card_delete(card: str = "") -> str:
        """【时光馆】把一张卡放进回收站——软删除，不是真的抹掉。这张卡会从 card_lookup/
        folder_timeline/folder_tree 等正常查询里消失，但 30 天内一澜或你都能用
        card_restore 原样恢复（标题、历史、文件夹归属、关联的记忆桶——全都原封
        不动，删除本身完全不影响这张卡关联的记忆桶，那些桶还在，只是链接暂时
        跟着卡一起不在正常列表里显示）。超过 30 天没恢复，会被自动清理，到时候
        才是真的没了。
        想看现在回收站里都有什么，用 card_trash_list。
        card：卡片标题或 id。"""
        found, err = _resolve_card(store, card)
        if err:
            return err
        store.delete_card(found["id"])
        return f"已放入回收站：{_card_result(found['id'])}（{TRASH_RETENTION_DAYS} 天内可用 card_restore 恢复）"

    @mcp.tool()
    async def card_trash_list() -> str:
        """【时光馆】看看回收站里现在有哪些卡（还没被彻底清理掉的），每张卡带着还剩
        多少天会被自动清理。想恢复用 card_restore；不用管彻底删除，放着不管
        超过这个天数就会自动清理掉，不需要你自己动手。"""
        trash = store.list_trash_cards()
        if not trash:
            return "回收站是空的。"
        lines = [f"=== 回收站（共 {len(trash)} 张卡）==="]
        for card in trash:
            title = str((card.get("current") or {}).get("title") or "(无标题)")
            lines.append(f"- 【{title}】（id: {card['id']}），删除于 {str(card.get('deleted_at') or '')[:10]}")
        return "\n".join(lines)

    @mcp.tool()
    async def card_restore(card: str = "") -> str:
        """【时光馆】把回收站里的一张卡恢复回来——标题、历史、文件夹归属、关联的记忆桶
        都是删除前的原样，恢复后立刻能在 card_lookup 等正常查询里重新看到。
        card：回收站里那张卡的标题或 id（用 card_trash_list 先看一眼）。"""
        found, err = _resolve_trash_card(store, card)
        if err:
            return err
        store.restore_card(found["id"])
        return f"已恢复：{_card_result(found['id'])}"
