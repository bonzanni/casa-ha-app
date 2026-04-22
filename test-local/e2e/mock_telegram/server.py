"""Minimal Telegram Bot API mock for engagement e2e.

Implements just enough of the Bot API for TelegramChannel's engagement path:
- createForumTopic → assigns incrementing message_thread_id, stores state
- editForumTopic → updates icon/name/closed flag
- closeForumTopic → marks topic closed
- sendMessage → stores message under thread_id
- setMyCommands → stores per-scope commands
- getMyCommands → returns stored commands
- getChatMember → returns can_manage_topics=True (default)
- getMe → returns a stub bot user

State is in-memory; server restart clears it. POST /_inspect returns a JSON
dump of state so the e2e script can assert on it.
"""

from __future__ import annotations

import asyncio
import json
import os
from aiohttp import web

STATE: dict = {
    "supergroup_id": int(os.environ.get("MOCK_TG_SUPERGROUP_ID", "-1001")),
    "bot_id": 4242,
    "next_thread_id": 1001,
    "topics": {},
    "messages_by_thread": {},
    "main_feed_messages": [],
    "commands_by_scope": {},
    "can_manage_topics": True,
}


async def handle_method(request: web.Request) -> web.Response:
    method = request.match_info["method"]
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        try:
            form = await request.post()
            payload = dict(form)
        except Exception:
            payload = {}

    if method == "getMe":
        return web.json_response({"ok": True, "result": {"id": STATE["bot_id"],
                                                         "is_bot": True,
                                                         "first_name": "CasaBot"}})

    if method == "getChatMember":
        chat_id = int(payload.get("chat_id", 0))
        return web.json_response({"ok": True, "result": {
            "user": {"id": STATE["bot_id"]},
            "status": "administrator",
            "can_manage_topics": STATE["can_manage_topics"],
        }})

    if method == "createForumTopic":
        tid = STATE["next_thread_id"]
        STATE["next_thread_id"] += 1
        STATE["topics"][tid] = {
            "name": payload.get("name", ""),
            "icon_custom_emoji_id": payload.get("icon_custom_emoji_id"),
            "closed": False,
        }
        return web.json_response({"ok": True, "result": {"message_thread_id": tid,
                                                         "name": payload.get("name", ""),
                                                         "icon_color": 0}})

    if method == "editForumTopic":
        tid = int(payload.get("message_thread_id", 0))
        topic = STATE["topics"].get(tid)
        if topic is None:
            return web.json_response({"ok": False, "description": "topic not found"}, status=400)
        if "name" in payload:
            topic["name"] = payload["name"]
        if "icon_custom_emoji_id" in payload:
            topic["icon_custom_emoji_id"] = payload["icon_custom_emoji_id"]
        return web.json_response({"ok": True, "result": True})

    if method == "closeForumTopic":
        tid = int(payload.get("message_thread_id", 0))
        topic = STATE["topics"].get(tid)
        if topic is not None:
            topic["closed"] = True
        return web.json_response({"ok": True, "result": True})

    if method == "sendMessage":
        tid = payload.get("message_thread_id")
        text = payload.get("text", "")
        if tid:
            STATE["messages_by_thread"].setdefault(int(tid), []).append(text)
        else:
            STATE["main_feed_messages"].append(text)
        return web.json_response({"ok": True, "result": {"message_id": 1, "text": text}})

    if method == "setMyCommands":
        scope = json.dumps(payload.get("scope") or {"type": "default"})
        STATE["commands_by_scope"][scope] = payload.get("commands", [])
        return web.json_response({"ok": True, "result": True})

    if method == "getMyCommands":
        scope = json.dumps(payload.get("scope") or {"type": "default"})
        return web.json_response({"ok": True, "result": STATE["commands_by_scope"].get(scope, [])})

    # Swallow all other methods (deleteForumTopic, getUpdates, etc.)
    return web.json_response({"ok": True, "result": True})


async def handle_inspect(request: web.Request) -> web.Response:
    return web.json_response(STATE)


async def handle_reset(request: web.Request) -> web.Response:
    STATE["topics"].clear()
    STATE["messages_by_thread"].clear()
    STATE["main_feed_messages"].clear()
    STATE["commands_by_scope"].clear()
    STATE["next_thread_id"] = 1001
    STATE["can_manage_topics"] = True
    return web.json_response({"ok": True})


def app_factory() -> web.Application:
    app = web.Application()
    # Match both /bot<token>/<method> and /<method>
    app.router.add_post(r"/bot{token}/{method}", handle_method)
    app.router.add_post(r"/{method}", handle_method)
    app.router.add_get("/_inspect", handle_inspect)
    app.router.add_post("/_reset", handle_reset)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_TG_PORT", "8081"))
    web.run_app(app_factory(), host="0.0.0.0", port=port)
