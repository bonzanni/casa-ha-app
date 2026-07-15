"""v0.83.0 (A4 · F-BTN) — distinguishing short button labels + agent-supplied
``short`` labels.

Two halves:

  1. PURE-FUNCTION tests of the elision ladder (``short_option_labels`` +
     ``_short_option_label``) pinning the spec invariants (i)-(iv): every label
     ≤ 30 incl. the number prefix; labels pairwise-distinct when the full
     options are; the LIVE regression pair keeps "Single"/"aliases"/"Separate";
     Sol's shared-token triple yields pairwise-distinct labels. Plus cap edges,
     the degenerate 40-char single token, and whitespace-heavy options.

  2. HANDLER-LEVEL dict-options tests (REAL ``VerdictBroker`` + REAL
     ``EngagementRegistry`` + REAL ``_make_channel_handlers``, injected clocks)
     asserting the body carries FULL labels verbatim, the buttons carry
     ``n · <short>``, a tap resolves to the FULL label in the response AND the
     settle ✅ line, and invalid dict shapes refuse ``invalid_args``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.telegram import (
    _ASK_BUTTON_LABEL_CAP,
    _short_option_label,
    short_option_labels,
)


# ===========================================================================
# Pure-function: the elision ladder
# ===========================================================================


def _summary(label: str) -> str:
    """Strip the ``n · `` number prefix, returning the summary text."""
    return label.split(" · ", 1)[1] if " · " in label else label


class TestInvariants:
    def test_i_every_label_within_cap_including_prefix(self) -> None:
        options = [
            "Single account with aliases",
            "Separate Google accounts",
            "Configure the enterprise SSO integration flow now",
            "one two three four five six seven eight nine ten",
        ]
        for lab in short_option_labels(options):
            assert len(lab) <= _ASK_BUTTON_LABEL_CAP

    def test_ii_pairwise_distinct_when_full_options_distinct(self) -> None:
        options = [
            "Deploy to production immediately now",
            "Deploy to staging immediately now",
            "Deploy to development immediately now",
        ]
        labels = short_option_labels(options)
        assert len(set(labels)) == len(labels)

    def test_iii_live_pair_keeps_single_aliases_separate(self) -> None:
        # The exact live F-BTN regression, verbatim.
        options = ["Single account with aliases", "Separate Google accounts"]
        labels = short_option_labels(options)
        assert len(set(labels)) == 2
        # Option 1's label surfaces BOTH the head "Single" and the tail
        # differentiator "aliases"; option 2 surfaces "Separate".
        assert "Single" in labels[0]
        assert "aliases" in labels[0]
        assert "Separate" in labels[1]

    def test_iv_sol_shared_token_triple_pairwise_distinct(self) -> None:
        # Sol's shared-token triple, verbatim — every token of option 1 is
        # shared with SOME sibling, so naive elision could collapse 1 and 3.
        options = [
            "Production deployment for European infrastructure",
            "Production deployment for American infrastructure",
            "Production monitoring for European infrastructure",
        ]
        labels = short_option_labels(options)
        # All three FULL button labels are pairwise-distinct.
        assert len(set(labels)) == 3
        # And so are the summaries (a differentiating token is surfaced, since
        # one fits) — the operator never sees two identical captions.
        summaries = [_summary(lab).casefold() for lab in labels]
        assert len(set(summaries)) == 3
        for lab in labels:
            assert len(lab) <= _ASK_BUTTON_LABEL_CAP


class TestCapEdges:
    def test_exact_30_char_boundary_fits_verbatim(self) -> None:
        # "1 · " (4) + 26-char summary = exactly 30 → verbatim, no elision.
        opt = "abcd efgh ijkl mnop qrst u"  # 26 chars
        assert len(opt) == 26
        label = _short_option_label(1, opt)
        assert label == "1 · " + opt
        assert len(label) == _ASK_BUTTON_LABEL_CAP

    def test_one_char_over_cap_elides(self) -> None:
        # 27-char summary → "1 · " + 27 = 31 > 30 → must elide below the cap.
        opt = "abcd efgh ijkl mnop qrst uv"  # 27 chars
        assert len(opt) == 27
        label = _short_option_label(1, opt)
        assert len(label) <= _ASK_BUTTON_LABEL_CAP

    def test_degenerate_40_char_single_token_hard_slice(self) -> None:
        opt = "x" * 40
        assert " " not in opt
        label = _short_option_label(1, opt)
        assert len(label) <= _ASK_BUTTON_LABEL_CAP
        assert label.startswith("1 · ")

    def test_whitespace_heavy_options(self) -> None:
        # Collapsed whitespace: split() drops the runs, so the summary is clean.
        options = ["  Single   account   with   aliases  ",
                   "Separate    Google   accounts"]
        labels = short_option_labels(options)
        for lab in labels:
            assert len(lab) <= _ASK_BUTTON_LABEL_CAP
            assert "  " not in _summary(lab)  # no doubled spaces
        assert len(set(labels)) == 2

    def test_whitespace_only_option_degenerate(self) -> None:
        label = _short_option_label(3, "     ")
        assert len(label) <= _ASK_BUTTON_LABEL_CAP
        assert label.startswith("3 · ") or label == "3 · "


class TestNoRegressionShortOptions:
    def test_short_fitting_options_render_verbatim(self) -> None:
        # Options that fully fit are byte-identical to a plain "n · <opt>" —
        # no ellipsis, no truncation (parity with the pre-A4 short path).
        options = ["Personal Gmail", "Work Outlook", "Yahoo Mail"]
        assert short_option_labels(options) == [
            "1 · Personal Gmail", "2 · Work Outlook", "3 · Yahoo Mail"]

    def test_single_option_fallback_matches_set_helper(self) -> None:
        assert _short_option_label(1, "Personal Gmail") == "1 · Personal Gmail"
        assert short_option_labels(["Personal Gmail"]) == ["1 · Personal Gmail"]


# ===========================================================================
# Handler-level: dict options end-to-end
# ===========================================================================


class _FakeChannel:
    """Records keyboard posts (incl. the ``shorts`` kwarg) and settle edits."""

    def __init__(self) -> None:
        self.keyboards: list[dict] = []
        self.edits: list[dict] = []
        self._next = 8000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options, shorts=None,
    ) -> int:
        self.keyboards.append(
            {"question": question, "options": list(options),
             "shorts": list(shorts) if shorts is not None else None})
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


@pytest.fixture
def fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
async def wired(tmp_path, fresh_broker, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    # driver=None ⇒ eager fallback (no relay), the simplest deterministic post.
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", None)
    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    ch = _FakeChannel()
    handlers = _make_channel_handlers(
        telegram_channel=ch, engagement_registry=reg)
    return {
        "reg": reg, "rec": rec, "ch": ch, "broker": fresh_broker,
        "ask": handlers["/internal/channel/ask"],
    }


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text)


async def _drive_answer(wired, payload, option_index):
    """Post an ask, tap ``option_index``, return the tool response dict."""
    task = asyncio.ensure_future(wired["ask"](_FakeRequest(payload)))
    await asyncio.sleep(0.02)
    assert wired["broker"].deliver(
        namespace="engagement_ask", scope=wired["rec"].id,
        request_id=payload["request_id"], option_index=option_index,
        actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    await wired["broker"].drain_hooks()
    return _body(resp)


class TestDictOptionsEndToEnd:
    async def test_body_full_buttons_short_tap_resolves_full(self, wired):
        options = [
            {"label": "Single account with aliases", "short": "Aliased"},
            {"label": "Separate Google accounts", "short": "Separate"},
        ]
        payload = {
            "engagement_id": wired["rec"].id, "request_id": "d1",
            "question": "Which account?", "options": options, "timeout_s": 60,
        }
        body = await _drive_answer(wired, payload, option_index=0)

        # Tap resolves to the FULL label (not the short) in the response.
        assert body["ok"] is True
        assert body["outcome"] == "answered"
        assert body["option"] == "Single account with aliases"
        assert body["option_index"] == 0

        kb = wired["ch"].keyboards[-1]
        # The MESSAGE body carries the FULL labels VERBATIM + numbered.
        assert "1. Single account with aliases" in kb["question"]
        assert "2. Separate Google accounts" in kb["question"]
        # The keyboard is driven by full labels + a parallel shorts list.
        assert kb["options"] == [
            "Single account with aliases", "Separate Google accounts"]
        assert kb["shorts"] == ["Aliased", "Separate"]

        # Settle ✅ line shows the FULL chosen label, not the short.
        settle = wired["ch"].edits[-1]["text"]
        assert "✅ Single account with aliases" in settle
        assert wired["ch"].edits[-1]["clear_keyboard"] is True

    async def test_mixed_str_and_dict_allowed(self, wired):
        options = [
            "Personal Gmail",
            {"label": "Configure the enterprise SSO integration",
             "short": "SSO"},
        ]
        payload = {
            "engagement_id": wired["rec"].id, "request_id": "d2",
            "question": "Which?", "options": options, "timeout_s": 60,
        }
        body = await _drive_answer(wired, payload, option_index=1)
        assert body["option"] == "Configure the enterprise SSO integration"
        kb = wired["ch"].keyboards[-1]
        assert kb["options"] == [
            "Personal Gmail", "Configure the enterprise SSO integration"]
        # str option → None short (heuristic); dict option → its short.
        assert kb["shorts"] == [None, "SSO"]

    async def test_str_only_ask_omits_shorts_kwarg(self, wired):
        # No agent short anywhere → the keyboard is posted WITHOUT ``shorts``
        # (backward-compatible call shape); the fake records None.
        payload = {
            "engagement_id": wired["rec"].id, "request_id": "d3",
            "question": "Which?", "options": ["A", "B"], "timeout_s": 60,
        }
        await _drive_answer(wired, payload, option_index=0)
        assert wired["ch"].keyboards[-1]["shorts"] is None


class TestInvalidDictShapes:
    """Invalid option shapes refuse ``invalid_args`` at validation (no post)."""

    def _invalid(self, options):
        from channels.channel_handlers import _validate_ask_args
        return _validate_ask_args(
            {"question": "q?", "options": options, "timeout_s": 60}) is None

    def test_missing_label_key(self):
        assert self._invalid([{"short": "x"}, "B"])

    def test_missing_short_key(self):
        assert self._invalid([{"label": "Full label"}, "B"])

    def test_short_over_25_chars(self):
        assert self._invalid(
            [{"label": "Full", "short": "x" * 26}, "B"])

    def test_short_whitespace_only(self):
        assert self._invalid([{"label": "Full", "short": "   "}, "B"])

    def test_label_whitespace_only(self):
        assert self._invalid([{"label": "   ", "short": "ok"}, "B"])

    def test_label_over_48_chars(self):
        assert self._invalid(
            [{"label": "x" * 49, "short": "ok"}, "B"])

    def test_short_non_string(self):
        assert self._invalid([{"label": "Full", "short": 7}, "B"])

    def test_duplicate_shorts_refused(self):
        assert self._invalid([
            {"label": "First full label", "short": "dup"},
            {"label": "Second full label", "short": "dup"},
        ])

    def test_duplicate_full_labels_refused(self):
        assert self._invalid([
            {"label": "Same", "short": "a"},
            {"label": "Same", "short": "b"},
        ])

    def test_option_wrong_type_refused(self):
        assert self._invalid([123, "B"])

    def test_valid_dict_short_at_25_accepted(self):
        from channels.channel_handlers import _validate_ask_args
        out = _validate_ask_args({
            "question": "q?", "timeout_s": 60,
            "options": [
                {"label": "First full label", "short": "y" * 25},
                {"label": "Second full label", "short": "z"},
            ]})
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["First full label", "Second full label"]
        assert shorts == ["y" * 25, "z"]

    def test_valid_str_only_shorts_all_none(self):
        from channels.channel_handlers import _validate_ask_args
        out = _validate_ask_args({
            "question": "q?", "options": ["A", "B"], "timeout_s": 60})
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["A", "B"]
        assert shorts == [None, None]
