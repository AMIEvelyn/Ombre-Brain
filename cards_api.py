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


def register_card_routes(mcp, store, require_auth) -> None:
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
        if not title and not content:
            return JSONResponse({"error": "title or content required"}, status_code=400)
        try:
            card_id = store.create_card(
                title=title,
                content=content,
                tags=body.get("tags") or [],
                attachments=body.get("attachments") or [],
                valid_at=(str(body.get("valid_at")).strip() or None) if body.get("valid_at") else None,
                note=str(body.get("note") or ""),
                folder_ids=[str(f) for f in (body.get("folder_ids") or [])],
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
        if not kwargs:
            return JSONResponse({"error": "nothing to edit"}, status_code=400)
        try:
            store.edit_current(card_id, **kwargs)
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
        if body.get("valid_at"):
            kwargs["valid_at"] = str(body["valid_at"]).strip()
        kwargs["note"] = str(body.get("note") or "")
        try:
            rev_id = store.add_revision(card_id, **kwargs)
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
    ]
    for path, methods, handler in routes:
        mcp.custom_route(path, methods=methods)(handler)
