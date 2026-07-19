"""Server-side origin-marker stamping at ingress (Release A, Layer 3)."""
from __future__ import annotations

import casa_core


def test_invoke_stamps_origin_route_invoke():
    msg = casa_core.build_invoke_message("assistant", "hi", {"context": {}})
    assert msg.context["_origin_route"] == "invoke"


def test_invoke_route_is_unspoofable():
    # A caller passing _origin_route in the external context is stripped by
    # sanitization; the server stamps "invoke" regardless.
    msg = casa_core.build_invoke_message(
        "assistant", "hi",
        {"context": {"_origin_route": "webhook_trigger", "_origin_clearance": "private"}},
    )
    assert msg.context["_origin_route"] == "invoke"
    assert "_origin_clearance" not in msg.context  # invoke carries no trigger clearance


def test_invoke_preserves_caller_cid_and_chatid():
    msg = casa_core.build_invoke_message(
        "assistant", "hi", {"context": {"cid": "abc123", "chat_id": "pinned"}})
    assert msg.context["cid"] == "abc123"
    assert msg.context["chat_id"] == "pinned"
    assert msg.context["_origin_route"] == "invoke"
