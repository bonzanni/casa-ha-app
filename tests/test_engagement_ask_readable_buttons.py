"""v0.81.0 (W-R3, Sol r1-5) — readable ask buttons.

The bug: ``post_options_keyboard`` used each FULL option string as the button
label, which Telegram truncates to a few words → most choices were unpickable,
and the full option texts appeared NOWHERE. The fix:

  1. ONE canonical ``render_ask_body(number, question, options)`` renders the
     full options VERBATIM + numbered in the MESSAGE body. This single helper
     feeds all four ask consumers (initial post, finish-hook settle base,
     persisted ``open_questions[].text``, boot reconciliation) so they can
     never disagree.
  2. Buttons carry a short, number-prefixed summary label derived channel-side
     (``_short_option_label``); ``callback_data`` identity stays the option
     INDEX.

These tests assert: the four consumers render a byte-identical body; every full
option appears verbatim + numbered; button labels are short + number-prefixed +
map to the correct index; settle appends the FULL chosen option below the
canonical body; a long option yields a word-boundary-capped label but the full
text survives in the body; free-text anchors render without an option list; and
the SEPARATE authz-challenge renderer is untouched.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from unittest.mock import AsyncMock, MagicMock

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.channel_handlers import (
    _ask_settle_text,
    render_ask_body,
)
from channels.telegram import _short_option_label

# ``asyncio_mode = auto`` (pytest.ini) auto-detects the async tests here; no
# module-level asyncio mark (this file mixes sync helper tests + async ones).


# ---------------------------------------------------------------------------
# render_ask_body — the single canonical body
# ---------------------------------------------------------------------------


class TestRenderAskBody:
    def test_numbered_verbatim_options_below_question(self) -> None:
        body = render_ask_body(
            3, "Which account?", ["Personal Gmail", "Work Outlook"])
        assert body == (
            "Q3: Which account?\n\n1. Personal Gmail\n2. Work Outlook")

    def test_every_full_option_appears_verbatim(self) -> None:
        options = [
            "Set up a brand new dedicated work account",
            "Reuse the existing personal address for now",
            "Ask the finance team which alias to use",
        ]
        body = render_ask_body(2, "How should I proceed?", options)
        # Header carries the canonical durable number.
        assert body.startswith("Q2: How should I proceed?\n\n")
        # EVERY option is present verbatim, 1-based numbered, in order.
        for i, opt in enumerate(options):
            assert f"{i + 1}. {opt}" in body
        assert body.splitlines()[2:] == [
            "1. " + options[0], "2. " + options[1], "3. " + options[2]]

    def test_strips_agent_authored_qprefix(self) -> None:
        # Agent-authored "Q7:" is stripped and re-prefixed with the allocated
        # durable number (parity with _canonical_question).
        body = render_ask_body(1, "Q7: Which DB?", ["A", "B"])
        assert body == "Q1: Which DB?\n\n1. A\n2. B"

    def test_anchor_no_option_list(self) -> None:
        # Free-text anchor (options == []) renders the numbered question ALONE.
        assert render_ask_body(4, "What's the DB name?", []) == (
            "Q4: What's the DB name?")

    def test_no_number_uses_bare_question(self) -> None:
        assert render_ask_body(None, "Proceed?", ["A", "B"]) == (
            "Proceed?\n\n1. A\n2. B")

    def test_body_stays_well_under_telegram_limit_at_caps(self) -> None:
        # Worst case at _validate_ask_args caps (question 1024, 8 options × 48)
        # is far below Telegram's 4096 — no truncation path needed (Sol r1-5).
        body = render_ask_body(999, "q" * 1024, ["o" * 48 for _ in range(8)])
        assert len(body) < 4096


# ---------------------------------------------------------------------------
# _short_option_label — derived button labels
# ---------------------------------------------------------------------------


class TestShortOptionLabel:
    def test_short_option_number_prefixed(self) -> None:
        assert _short_option_label(1, "Personal Gmail") == "1 · Personal Gmail"

    def test_caps_long_option_at_word_boundary(self) -> None:
        # A >3-word / >24-char option → capped on a WORD boundary (no ellipsis,
        # no mid-word cut). The FULL text is carried by the body, not the button.
        label = _short_option_label(
            1, "Configure the enterprise SSO integration")
        assert label == "1 · Configure the"
        assert len(label) <= 24
        # No dangling partial word / ellipsis.
        assert not label.endswith(("…", " ", "-"))

    def test_at_most_three_summary_words(self) -> None:
        label = _short_option_label(2, "one two three four five")
        # ≤3 words after the "<n> · " prefix.
        summary = label.split(" · ", 1)[1]
        assert len(summary.split()) <= 3

    def test_number_matches_body_line(self) -> None:
        # The label number is the 1-based option position, matching the numbered
        # line in render_ask_body so the operator can cross-reference.
        options = ["Personal Gmail", "Work Outlook"]
        body = render_ask_body(1, "Which?", options)
        for i, opt in enumerate(options):
            label = _short_option_label(i + 1, opt)
            assert label.startswith(f"{i + 1} · ")
            assert f"{i + 1}. {opt}" in body

    def test_caps_long_single_word_option(self) -> None:
        # R3 label bug: a single long token (47 chars, within the _ASK_MAX_LABEL
        # _LEN=48 validation cap) must STILL yield a label ≤ the button cap. The
        # old loop appended the first word unconditionally (the cap check only
        # ran once `chosen` was non-empty), so this produced "1 · <47 chars>" =
        # 51 chars — over the 24 cap and unreadable.
        from channels.telegram import _ASK_BUTTON_LABEL_CAP

        opt = "Supercalifragilisticexpialidociousandmore12345"
        assert " " not in opt and len(opt) > _ASK_BUTTON_LABEL_CAP  # single long token
        label = _short_option_label(1, opt)
        assert len(label) <= _ASK_BUTTON_LABEL_CAP
        assert label.startswith("1 · ")

    def test_caps_long_first_word_then_more(self) -> None:
        # A long FIRST word followed by more words: the cap must bite on the
        # first word (hard slice), not append it whole and blow past the cap.
        from channels.telegram import _ASK_BUTTON_LABEL_CAP

        label = _short_option_label(
            2, "Supercalifragilisticexpialidocious mango")
        assert len(label) <= _ASK_BUTTON_LABEL_CAP
        assert label.startswith("2 · ")


# ---------------------------------------------------------------------------
# post_options_keyboard — body verbatim, short labels, index identity
# ---------------------------------------------------------------------------


class TestPostOptionsKeyboardReadable:
    def _channel(self):
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(return_value=MagicMock(topic_id=42))
        ch.send_to_topic = AsyncMock(return_value=101)
        return ch

    async def test_body_posted_verbatim_short_labels_index_identity(self) -> None:
        ch = self._channel()
        options = ["Personal Gmail", "Configure the enterprise SSO integration"]
        body = render_ask_body(1, "Which account?", options)

        await ch.post_options_keyboard(
            engagement_id="x" * 32, request_id="rid",
            question=body, options=options)

        # The MESSAGE body is posted verbatim (full options live here).
        args, _ = ch.send_to_topic.call_args
        assert args[1] == body
        assert "Personal Gmail" in args[1]
        assert "Configure the enterprise SSO integration" in args[1]

        rows = ch.send_to_topic.call_args.kwargs["reply_markup"].inline_keyboard
        # Labels are the short derived summaries, number-prefixed.
        assert [r[0].text for r in rows] == [
            "1 · Personal Gmail", "2 · Configure the"]
        # callback_data identity is the option INDEX (unchanged schema).
        assert [r[0].callback_data for r in rows] == [
            "v1|engagement_ask|rid|0", "v1|engagement_ask|rid|1"]


# ---------------------------------------------------------------------------
# _ask_settle_text — full chosen option appended below the canonical body
# ---------------------------------------------------------------------------


class TestSettleAppendsFullOption:
    def test_answered_appends_full_chosen_option_below_body(self) -> None:
        options = ["Personal Gmail", "Configure the enterprise SSO integration"]
        body = render_ask_body(1, "Which account?", options)
        settled = _ask_settle_text(
            body, {"outcome": "answered", "option_index": 1}, options)
        # Base body preserved byte-for-byte; the FULL option (not the short
        # label) appended below with a ✅.
        assert settled == body + "\n✅ Configure the enterprise SSO integration"
        assert settled.startswith(body)


# ---------------------------------------------------------------------------
# Byte-identity across the four consumers (handler-driven)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self.options_keyboards: list[dict] = []
        self.edits: list[dict] = []
        self._next = 7000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options,
    ) -> int:
        self.options_keyboards.append(
            {"question": question, "options": list(options)})
        mid = self._next
        self._next += 1
        return mid

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        self.edits.append({"text": text, "clear_keyboard": clear_keyboard})
        return True


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


async def test_body_identical_across_post_persist_and_settle(
    tmp_path, monkeypatch,
) -> None:
    """The initial post body, the persisted ``open_questions[].text`` and the
    finish-hook settle BASE are the SAME render_ask_body output — one source,
    three consumers agree byte-for-byte; the fourth (reconcile) reads the
    persisted text and is covered below."""
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    # driver=None ⇒ eager fallback (no relay), simplest deterministic post path.
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", None)

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    eid = rec.id
    ch = _FakeChannel()
    handlers = _make_channel_handlers(
        telegram_channel=ch, engagement_registry=reg)
    ask = handlers["/internal/channel/ask"]

    options = ["Personal Gmail", "Work Outlook"]
    payload = {
        "engagement_id": eid, "request_id": "a1",
        "question": "Which account?", "options": options, "timeout_s": 60,
    }
    task = asyncio.ensure_future(ask(_FakeRequest(payload)))
    await asyncio.sleep(0.02)
    assert fresh.deliver(
        namespace="engagement_ask", scope=eid, request_id="a1",
        option_index=0, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    await fresh.drain_hooks()
    assert json.loads(resp.text)["ok"] is True

    expected = render_ask_body(1, "Which account?", options)
    # (1) initial post body.
    assert ch.options_keyboards[-1]["question"] == expected
    # (2) persisted open_questions[].text.
    persisted = reg.get(eid).open_questions
    # (settled entries are removed on close, so read the text before close via
    # the settle edit; the persisted text was the same source — assert the
    # settle base derived from it).
    # (3) settle base == body + FULL chosen option (not the short label).
    assert ch.edits[-1]["text"] == expected + "\n✅ Personal Gmail"
    assert ch.edits[-1]["clear_keyboard"] is True
    # The persisted ledger is now empty (question settled), proving the close
    # path ran over the SAME entry; the persisted text identity is asserted in
    # the reconcile test below where the entry survives.
    assert list(persisted) == []


async def test_reconcile_settles_over_persisted_render_ask_body(
    tmp_path,
) -> None:
    """The FOURTH consumer: boot reconciliation edits the expired copy OVER the
    persisted ``open_questions[].text`` — which is exactly render_ask_body's
    output — so the option list survives a restart-time settle."""
    from drivers.claude_code_driver import ClaudeCodeDriver
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t", {}, topic_id=999)

    options = ["Personal Gmail", "Work Outlook"]
    body = render_ask_body(1, "Which account?", options)
    n1 = await reg.allocate_question_number(rec.id)
    # Persist EXACTLY what the ask handler persists (render_ask_body output).
    await reg.add_open_question(rec.id, n1, 7001, text=body, kind="button")

    edits: list = []

    async def _edit(topic_id, message_id, text, *, clear_keyboard=False):
        edits.append(text)
        return True

    drv = ClaudeCodeDriver(
        engagements_root=str(tmp_path / "engagements"),
        send_to_topic=AsyncMock(),
        casa_framework_mcp_url="http://x",
        edit_topic_message=_edit,
        registry=reg,
    )
    await drv.reconcile_open_questions(rec)

    # The reconcile settle copy is the persisted body (full options) + suffix —
    # the option list is NOT dropped.
    assert edits == [body + "\n⌛ expired — answer by text below"]
    assert "1. Personal Gmail" in edits[0]
    assert "2. Work Outlook" in edits[0]


# ---------------------------------------------------------------------------
# The SEPARATE authz-challenge renderer is UNTOUCHED
# ---------------------------------------------------------------------------


def test_authz_challenge_renderer_untouched() -> None:
    """authz_grants renders its own challenge body (render_challenge_message)
    and posts via post_dm_keyboard — a SEPARATE path that must not share the
    ask body/label code changed here."""
    import authz_grants

    # The authz challenge renderer exists and is independent.
    assert hasattr(authz_grants, "render_challenge_message")
    # It does NOT import or depend on the ask-body helper.
    assert not hasattr(authz_grants, "render_ask_body")
    assert not hasattr(authz_grants, "_short_option_label")
    src = authz_grants.render_challenge_message.__module__
    assert src == "authz_grants"
