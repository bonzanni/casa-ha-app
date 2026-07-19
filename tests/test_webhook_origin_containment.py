"""Webhook-origin containment: unspoofable origin markers + origin clearance
(Release A, Task 7, spec A4/A0)."""
from __future__ import annotations

from provenance import RESERVED_CONTEXT_KEYS, sanitize_external_context
from sensitivity import clearance_for_origin


def test_origin_keys_are_reserved():
    assert {"_origin_route", "_origin_clearance"} <= RESERVED_CONTEXT_KEYS


def test_external_context_cannot_forge_origin():
    out = sanitize_external_context(
        {"_origin_route": "invoke", "_origin_clearance": "private", "x": 1})
    assert "_origin_route" not in out
    assert "_origin_clearance" not in out
    assert out["x"] == 1


def test_webhook_trigger_gets_declared_clearance():
    assert clearance_for_origin("webhook_trigger", "public", "webhook") == "public"
    assert clearance_for_origin("webhook_trigger", "family", "webhook") == "family"


def test_webhook_trigger_defaults_public_on_missing_or_bad_clearance():
    # default-deny: missing/malformed clearance ⇒ public (least sensitive)
    assert clearance_for_origin("webhook_trigger", None, "webhook") == "public"
    assert clearance_for_origin("webhook_trigger", "bogus", "webhook") == "public"
    assert clearance_for_origin("webhook_trigger", "private", "webhook") == "public"


def test_invoke_stays_private():
    assert clearance_for_origin("invoke", None, "webhook") == "private"


def test_unknown_origin_falls_through_to_channel():
    # No origin marker ⇒ today's channel-keyed behavior is preserved.
    assert clearance_for_origin(None, None, "telegram") == "private"
    assert clearance_for_origin(None, None, "voice") == "friends"
    assert clearance_for_origin(None, None, "unknown-future") == "public"
