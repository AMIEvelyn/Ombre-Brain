"""Facts-model v2 HTTP API (step 2) -- a thin layer over cards_store.CardStore.

Kept in its own module on purpose (see docs/facts-model-v2-collection-redesign.md
"入口分层"): all card/folder endpoints live here, and server.py wires them in
with a single call, so the big server.py file barely changes and this surface
stays independently testable.

Wire-in (server.py), next to where fact_store is built:

    import cards_api
    from cards_store import CardStore
    card_store = CardStore(config)
    cards_api.register_card_routes(mcp, card_store, _require_dashboard_auth)

register_card_routes registers every route on the existing FastMCP `mcp`,
reusing the existing dashboard-auth gate. Nothing here reaches into server.py
globals, so it cannot perturb existing behavior.
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

from cards_store import AUTHOR_YI_LAN, AUTHOR_DISPLAY_NAMES, SegmentOwnershipError

# 2026-07-16: raised from the frontend's old combined-5 cap to a per-type
# cap of 10 (Yi Lan wants up to 10 photos AND up to 10 files on one card).
# Enforced here (not just dashboard.html) so any future caller has to go
# through the same limit.
FACTS_CARD_MAX_ATTACHMENTS_PER_TYPE = 10


def _validate_attachment_count(attachments) -> str | None:
    """None if within limits, else an error string. Counts image vs.
    non-image separately -- the two types are capped independently."""
    if not isinstance(attachments, list):
        return None
    photos = sum(1 for a in attachments if isinstance(a, dict) and a.get("type") == "image")
    files = sum(1 for a in attachments if isinstance(a, dict) and a.get("type") != "image")
    if photos > FACTS_CARD_MAX_ATTACHMENTS_PER_TYPE:
        return f"图片附件最多 {FACTS_CARD_MAX_ATTACHMENTS_PER_TYPE} 个，现在有 {photos} 个。"
    if files > FACTS_CARD_MAX_ATTACHMENTS_PER_TYPE:
        return f"文件附件最多 {FACTS_CARD_MAX_ATTACHMENTS_PER_TYPE} 个，现在有 {files} 个。"
    return None


def _ownership_conflict_response(e: SegmentOwnershipError) -> JSONResponse:
    """2026-07-17: structured 409 for rule C (Yi Lan's decision) -- the
    frontend needs the real author + a text preview to build an actual
    confirmation dialog, not a generic 500 with just an exception message.
    `message` is a ready-to-use fallback string for any caller that doesn't
    build its own dialog; wording matches what Yi Lan asked for after the
    original "确定要整段覆盖吗" phrasing read as more destructive than most
    of these actually are (often just a wording tweak, not a real overwrite)."""
    name = AUTHOR_DISPLAY_NAMES.get(e.author, e.author)
    return JSONResponse(
        {
            "error": "ownership_conflict",
            "segment_id": e.segment_id,
            "author": e.author,
            "text_preview": e.text_preview,
            "message": f"你修改了{name}的内容，需要 force=true 才能保存。",
        },
        status_code=409,
    )


def register_card_routes(mcp, store, require_auth, bucket_summary=None) -> None:
    """Register the /api/cards-skeleton/* routes.

    mcp          -- the FastMCP instance (provides .custom_route)
    store        -- a cards_store.CardStore
    require_auth -- callable(request) -> error-response-or-None (dashboard auth)
    """

    def _guard(request):
        return require_auth(request)

    async def _body(request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _truthy(value: Any) -> bool:
        return str(value).lower() in ("1", "true", "yes", "on")

    # ---- cards -------------------------------------------------------
    async def create_card(request):
        err = _guard(request)
        if err:
            return err
        body = await _body(request)
        title = str(body.get("title") or "").strip()
        content = str(body.get("content") or "")
        content_segments = None
        if "content_text" in body:
            content_segments = store.parse_content_text(str(body.get("content_text") or ""))
            content = ""  # content_segments takes over; avoid double-wrapping
        if not title and not content and not content_segments:
            return JSONResponse({"error": "title or content required"}, status_code=400)
        count_err = _validate_attachment_count(body.get("attachments") or [])
        if count_err:
            return JSONResponse({"error": count_err}, status_code=400)
        try:
            card_id = store.create_card(
                title=title,
                content=content,
                content_segments=content_segments,
                tags=body.get("tags") or [],
                attachments=body.get("attachments") or [],
                valid_at=(str(body.get("valid_at")).strip() or None) if body.get("valid_at") else None,
                note=str(body.get("note") or ""),
                folder_ids=[str(f) for f in (body.get("folder_ids") or [])],
                author=AUTHOR_YI_LAN,
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"status": "created", "card": store.get_card(card_id)})

    async def get_card(request):
        err = _guard(request)
        if err:
            return err
        card = store.get_card(str(request.path_params["card_id"]))
        if not card:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"card": card})

    async def edit_card(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        if not store.get_card(card_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await _body(request)
        # only fields explicitly present are changed (edit in place, no new timepoint)
        kwargs = {}
        for key in ("title", "content", "tags", "attachments"):
            if key in body:
                kwargs[key] = body[key]
        if "content_text" in body:
            # content editor modal (2026-07-17, second pass): the Dashboard
            # sends back the whole edited "作者：..." text, parsed here into
            # segments -- overrides a plain `content` if both were somehow sent.
            kwargs.pop("content", None)
            kwargs["content_segments"] = store.parse_content_text(str(body.get("content_text") or ""))
        if not kwargs:
            return JSONResponse({"error": "nothing to edit"}, status_code=400)
        if "attachments" in kwargs:
            count_err = _validate_attachment_count(kwargs["attachments"])
            if count_err:
                return JSONResponse({"error": count_err}, status_code=400)
        # 2026-07-17: whole-content replace can lose a segment someone else
        # wrote, so it's rule-C gated -- force=true in the body is required
        # to go through with overwriting/losing a segment Lin Zhan wrote.
        force = bool(body.get("force"))
        try:
            store.edit_current(card_id, author=AUTHOR_YI_LAN, force=force, **kwargs)
        except SegmentOwnershipError as e:
            return _ownership_conflict_response(e)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"status": "edited", "card": store.get_card(card_id)})

    async def add_revision(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        if not store.get_card(card_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await _body(request)
        kwargs = {}
        for key in ("title", "content", "tags", "attachments"):
            if key in body:
                kwargs[key] = body[key]
        if "content_text" in body:
            kwargs.pop("content", None)
            kwargs["content_segments"] = store.parse_content_text(str(body.get("content_text") or ""))
        if body.get("valid_at"):
            kwargs["valid_at"] = str(body["valid_at"]).strip()
        kwargs["note"] = str(body.get("note") or "")
        if "attachments" in kwargs:
            count_err = _validate_attachment_count(kwargs["attachments"])
            if count_err:
                return JSONResponse({"error": count_err}, status_code=400)
        # 2026-07-17, second pass: New Revision used to replace content freely
        # (the old revision stays in history either way); Yi Lan's updated
        # call is that it should be rule-C gated the same as Edit now that
        # the content editor shows exactly whose text is where.
        force = bool(body.get("force"))
        try:
            rev_id = store.add_revision(card_id, author=AUTHOR_YI_LAN, force=force, **kwargs)
        except SegmentOwnershipError as e:
            return _ownership_conflict_response(e)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"status": "revised", "revision_id": rev_id, "card": store.get_card(card_id)})

    async def list_revisions(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        if not store.get_card(card_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"revisions": store.list_revisions(card_id)})

    async def delete_card(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        if not store.delete_card(card_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"status": "deleted", "card_id": card_id})

    # ---- folders -----------------------------------------------------
    async def create_folder(request):
        err = _guard(request)
        if err:
            return err
        body = await _body(request)
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        parent_id = str(body.get("parent_id") or "").strip()
        if parent_id and not store.get_folder(parent_id):
            return JSONResponse({"error": "parent folder not found"}, status_code=400)
        folder_id = store.create_folder(name, parent_id=parent_id, is_favorite=bool(body.get("is_favorite")))
        return JSONResponse({"status": "created", "folder": store.get_folder(folder_id)})

    async def list_folders(request):
        err = _guard(request)
        if err:
            return err
        parent = request.query_params.get("parent_id")
        folders = store.list_folders(parent_id=parent if parent is not None else None)
        return JSONResponse({"folders": folders})

    async def update_folder(request):
        err = _guard(request)
        if err:
            return err
        folder_id = str(request.path_params["folder_id"])
        if not store.get_folder(folder_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await _body(request)
        kwargs = {}
        if "name" in body:
            kwargs["name"] = str(body["name"])
        if "is_favorite" in body:
            kwargs["is_favorite"] = bool(body["is_favorite"])
        if not kwargs:
            return JSONResponse({"error": "nothing to update"}, status_code=400)
        store.update_folder(folder_id, **kwargs)
        return JSONResponse({"status": "updated", "folder": store.get_folder(folder_id)})

    async def delete_folder(request):
        err = _guard(request)
        if err:
            return err
        folder_id = str(request.path_params["folder_id"])
        if not store.delete_folder(folder_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"status": "deleted", "folder_id": folder_id})

    async def folder_cards(request):
        err = _guard(request)
        if err:
            return err
        folder_id = str(request.path_params["folder_id"])
        if not store.get_folder(folder_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        recursive = _truthy(request.query_params.get("recursive"))
        return JSONResponse({"cards": store.list_cards_in_folder(folder_id, recursive=recursive)})

    async def folder_timeline(request):
        err = _guard(request)
        if err:
            return err
        folder_id = str(request.path_params["folder_id"])
        if not store.get_folder(folder_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        recursive = request.query_params.get("recursive")
        recursive = True if recursive is None else _truthy(recursive)  # default recursive
        return JSONResponse({"timeline": store.folder_timeline(folder_id, recursive=recursive)})

    # ---- membership (playlist model) --------------------------------
    async def card_folders(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        if not store.get_card(card_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({
            "folders": store.get_card_folders(card_id),
            "favorites": store.get_card_favorite_folders(card_id),
        })

    async def add_card_folder(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        if not store.get_card(card_id):
            return JSONResponse({"error": "card not found"}, status_code=404)
        body = await _body(request)
        folder_id = str(body.get("folder_id") or "").strip()
        if not folder_id or not store.get_folder(folder_id):
            return JSONResponse({"error": "folder not found"}, status_code=400)
        added = store.add_card_to_folder(card_id, folder_id)
        return JSONResponse({"status": "linked" if added else "already_linked", "folder_id": folder_id})

    async def remove_card_folder(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        folder_id = str(request.path_params["folder_id"])
        removed = store.remove_card_from_folder(card_id, folder_id)
        if not removed:
            return JSONResponse({"error": "link not found"}, status_code=404)
        return JSONResponse({"status": "unlinked", "card_id": card_id, "folder_id": folder_id})

    # ---- card <-> memory bucket links -------------------------------
    async def card_buckets(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        if not store.get_card(card_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        links = store.get_bucket_links(card_id)
        if bucket_summary is not None:
            for link in links:
                try:
                    link["bucket"] = await bucket_summary(link["bucket_id"])
                except Exception:
                    link["bucket"] = None
        return JSONResponse({"buckets": links})

    async def add_card_bucket(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        if not store.get_card(card_id):
            return JSONResponse({"error": "card not found"}, status_code=404)
        body = await _body(request)
        bucket_id = str(body.get("bucket_id") or "").strip()
        if not bucket_id:
            return JSONResponse({"error": "bucket_id required"}, status_code=400)
        try:
            added = store.add_bucket_link(card_id, bucket_id, relation_type=str(body.get("relation_type") or "evidence"), note=str(body.get("note") or ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"status": "linked" if added else "already_linked", "bucket_id": bucket_id})

    async def remove_card_bucket(request):
        err = _guard(request)
        if err:
            return err
        card_id = str(request.path_params["card_id"])
        bucket_id = str(request.path_params["bucket_id"])
        removed = store.remove_bucket_link(card_id, bucket_id)
        if not removed:
            return JSONResponse({"error": "link not found"}, status_code=404)
        return JSONResponse({"status": "unlinked", "card_id": card_id, "bucket_id": bucket_id})

    # ---- register everything ----------------------------------------
    routes = [
        ("/api/cards-skeleton/cards", ["POST"], create_card),
        ("/api/cards-skeleton/cards/{card_id}", ["GET"], get_card),
        ("/api/cards-skeleton/cards/{card_id}", ["PATCH"], edit_card),
        ("/api/cards-skeleton/cards/{card_id}", ["DELETE"], delete_card),
        ("/api/cards-skeleton/cards/{card_id}/revisions", ["POST"], add_revision),
        ("/api/cards-skeleton/cards/{card_id}/revisions", ["GET"], list_revisions),
        ("/api/cards-skeleton/cards/{card_id}/folders", ["GET"], card_folders),
        ("/api/cards-skeleton/cards/{card_id}/folders", ["POST"], add_card_folder),
        ("/api/cards-skeleton/cards/{card_id}/folders/{folder_id}", ["DELETE"], remove_card_folder),
        ("/api/cards-skeleton/folders", ["POST"], create_folder),
        ("/api/cards-skeleton/folders", ["GET"], list_folders),
        ("/api/cards-skeleton/folders/{folder_id}", ["PATCH"], update_folder),
        ("/api/cards-skeleton/folders/{folder_id}", ["DELETE"], delete_folder),
        ("/api/cards-skeleton/folders/{folder_id}/cards", ["GET"], folder_cards),
        ("/api/cards-skeleton/folders/{folder_id}/timeline", ["GET"], folder_timeline),
        ("/api/cards-skeleton/cards/{card_id}/buckets", ["GET"], card_buckets),
        ("/api/cards-skeleton/cards/{card_id}/buckets", ["POST"], add_card_bucket),
        ("/api/cards-skeleton/cards/{card_id}/buckets/{bucket_id}", ["DELETE"], remove_card_bucket),
    ]
    for path, methods, handler in routes:
        mcp.custom_route(path, methods=methods)(handler)
