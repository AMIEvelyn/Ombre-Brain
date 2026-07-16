"""Facts-model v2 MCP tools (step 3) -- what Lin Zhan actually calls.

Read-only tools over cards_store.CardStore, wired in from server.py via
register_card_tools(mcp, store). Kept out of server.py so the surface stays
small and testable (see docs/facts-model-v2-collection-redesign.md §4, §10).

  card_lookup     -- find cards by keyword; leads with the CURRENT state
                     (latest revision), folds history behind a hint. This is
                     how "取消失效" reads cleanly for Lin Zhan: newest = current.
  folder_timeline -- pull a whole folder's cards onto one chronological line
                     (a narrative ribbon), Yi Lan's headline use-case.
  card_history    -- the other half of card_lookup's "有 N 条历史" hint: reads
                     back the full timeline of one specific card (see
                     docs/facts-model-v2-collection-redesign.md §14 item 2).
"""

from __future__ import annotations


def _fmt_card(store, card: dict) -> str:
    cur = card.get("current") or {}
    title = str(cur.get("title") or "").strip() or "(无标题)"
    content = str(cur.get("content") or "").strip()
    history = card.get("history") or []
    tags = [str(t) for t in (cur.get("tags") or [])]

    lines = [f"【{title}】"]
    lines.append(f"  现在：{content}" if content else "  现在：（无内容）")
    if tags:
        lines.append("  " + " ".join(f"#{t}" for t in tags))
    if len(history) > 1:
        lines.append(f"  （有 {len(history)} 条历史，要看变化过程再说）")
    favorites = card.get("folders") or []
    fav = [f for f in favorites if f.get("is_favorite")]
    if fav:
        lines.append("  ⭐ 收藏在：" + "、".join(store.folder_path(f["id"]) for f in fav))
    other = [f for f in favorites if not f.get("is_favorite")]
    if other:
        lines.append("  所在文件夹：" + "、".join(store.folder_path(f["id"]) for f in other))
    return "\n".join(lines)


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
        return None, f"没找到叫「{card}」的资料卡。"
    if len(candidates) == 1:
        return candidates[0], ""
    titles = "、".join(str((c.get("current") or {}).get("title") or c["id"]) for c in candidates)
    return None, f"有多张卡匹配「{card}」：{titles}。请说得更具体一点。"


def _resolve_folder(store, folder: str):
    """Return (folder_id, error_message). Accepts an exact folder id or a name
    (disambiguated by path if several match)."""
    folder = str(folder or "").strip()
    if not folder:
        return "", "请给一个文件夹名或文件夹 id。"
    if store.get_folder(folder):
        return folder, ""
    matches = store.find_folders_by_name(folder)
    if not matches:
        return "", f"没找到叫「{folder}」的文件夹。"
    if len(matches) == 1:
        return matches[0]["id"], ""
    paths = "、".join(f"{store.folder_path(m['id'])}" for m in matches)
    return "", f"有多个文件夹匹配「{folder}」：{paths}。请说得更具体一点（可以用完整路径里的名字）。"


def register_card_tools(mcp, store) -> None:

    @mcp.tool()
    async def card_lookup(query: str = "", folder: str = "") -> str:
        """只读查询骨架层资料卡（collection/歌单式的新模型，cards.sqlite）。
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
            return f"查询资料卡失败: {e}"
        if not cards:
            if query:
                return f"没搜到匹配「{query}」的资料卡。"
            return "这个范围里还没有资料卡。（新模型刚建好，数据还很少，正常）"
        scope = f"（在 {store.folder_path(folder_id)} 里）" if folder_id else ""
        header = f"=== 资料卡{scope} ==="
        return "\n".join([header] + [_fmt_card(store, c) for c in cards])

    @mcp.tool()
    async def folder_timeline(folder: str = "", recursive: bool = True) -> str:
        """把某个文件夹里的所有资料卡按时间铺成一条完整时间线（叙事丝带）。
        比如"讲讲我们旅行的经过"就传 folder="旅行"，会把 我们/旅行 里所有卡按日期排出来。
        folder：文件夹名或 id。recursive=True（默认）会把子文件夹里的卡也一起收进来，
        recursive=False 只排这个文件夹里直接归的卡。
        每张卡在时间线上是一个点（落在它当前时间点的日期），要看某张卡自己的变化史，
        再用 card_lookup 或读那张卡。"""
        folder_id, err = _resolve_folder(store, folder)
        if err:
            return err
        try:
            entries = store.folder_timeline(folder_id, recursive=bool(recursive))
        except Exception as e:
            return f"拉取时间线失败: {e}"
        if not entries:
            return f"「{store.folder_path(folder_id)}」这个文件夹里还没有资料卡。"
        lines = [f"=== 【{store.folder_path(folder_id)}】时间线 ==="]
        for e in entries:
            date = str(e.get("date") or "未知日期")
            title = str(e.get("title") or "(无标题)")
            content = str(e.get("content") or "").strip()
            n = int(e.get("revision_count") or 1)
            hist = f"（{n} 条历史）" if n > 1 else ""
            body = f"：{content}" if content else ""
            lines.append(f"- {date} 【{title}】{hist}{body}")
        return "\n".join(lines)

    @mcp.tool()
    async def card_history(card: str = "") -> str:
        """读一张资料卡的完整时间线（全部历史时间点，不只是 card_lookup 顶出来的
        最新状态）。card_lookup 对更早的时间点只给一句"有 N 条历史"的提示，这个
        工具才是真正把那 N 条内容逐条读出来的入口，想知道某件事是怎么一步步变
        成现在这样时用这个。
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
        return "\n".join(lines)
