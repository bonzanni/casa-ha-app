"""#233/#224: the concierge hand-off wire CONTRACT, round-tripped.

This is the test whose absence let the feature die. Casa emits a hand-off
frame; the companion HA integration replies with a `handoff_received` receipt
built from that frame; Casa binds the receipt to the job. Both repos had
thorough unit tests — of their OWN frame shapes — and both were green while
the shapes disagreed:

    Casa required   {"type","protocol","job_id","handoff_id"}
    the client sent {"type","handoff_id"}

Casa's coordinator `return`ed silently, the hand-off stayed PENDING, and the
specialist's finished answer expired without ever being spoken. Nothing was
logged on either side.

So these tests do NOT assert Casa's shape in isolation. They build the receipt
the way a client can — using ONLY fields present in the frame Casa actually
emitted — and require Casa to accept it. A field Casa starts demanding but
never sends will fail here.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from channels.voice.channel import VoiceHandoff, VoiceHandoffCoordinator

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_ROUTE_ID = "route-1"
_JOB_ID = "job-1"


def _job():
    job = MagicMock()
    job.id = _JOB_ID
    job.handoff_id = "handoff-1"
    job.specialist_display_name = "Judge"
    job.origin_route_id = _ROUTE_ID
    return job


def _registry(job):
    registry = MagicMock()
    registry.get = MagicMock(return_value=job)
    registry.acknowledge_handoff = AsyncMock()
    return registry


def _route():
    route = MagicMock()
    route.route_id = _ROUTE_ID
    route.send_json = AsyncMock()
    return route


def _client_receipt(frame: dict) -> dict:
    """Build the receipt the way the HA integration does.

    Mirrors `custom_components/casa/api.py`: echo the protocol, the handoff id
    and the job id back from the offer. It may use ONLY what the offer carried
    — that is the whole point of the contract.
    """
    return {
        "type": "handoff_received",
        "protocol": frame["protocol"],
        "handoff_id": frame["handoff_id"],
        "job_id": frame["job_id"],
    }


class TestHandoffRoundTrip:
    async def test_the_client_can_build_an_accepted_receipt_from_the_frame(self):
        """THE regression test: offer -> client receipt -> acknowledged."""
        job = _job()
        registry = _registry(job)
        coordinator = VoiceHandoffCoordinator(registry)

        frame = VoiceHandoff.from_job("utterance-1", job).frame()
        # Everything the client needs must be IN the frame.
        receipt = _client_receipt(frame)

        await coordinator.handle(_route(), receipt)

        registry.acknowledge_handoff.assert_awaited_once_with(
            _JOB_ID, "handoff-1")

    async def test_the_frame_carries_every_field_the_receipt_needs(self):
        frame = VoiceHandoff.from_job("utterance-1", _job()).frame()
        for field in ("type", "protocol", "handoff_id", "job_id", "text"):
            assert field in frame, (
                f"{field!r} missing from the hand-off frame — the client "
                f"cannot echo what it never received"
            )
        assert frame["job_id"] == _JOB_ID

    async def test_the_spoken_offer_sets_a_wait_expectation(self):
        """#233: the caller waits ~40s; 'I will ask X.' left them guessing."""
        text = VoiceHandoff.from_job("utterance-1", _job()).text
        assert "Judge" in text
        assert "minute" in text, f"no wait expectation in: {text!r}"


class TestReceiptRejectionsAreLoud:
    """Every refusal must say WHY. Silence here is what hid the bug."""

    async def test_missing_protocol_is_refused_and_logged(self, caplog):
        job = _job()
        registry = _registry(job)
        coordinator = VoiceHandoffCoordinator(registry)

        # Exactly what the shipped client used to send.
        with caplog.at_level(logging.WARNING, logger="channels.voice.channel"):
            await coordinator.handle(_route(), {
                "type": "handoff_received", "handoff_id": "handoff-1",
            })

        registry.acknowledge_handoff.assert_not_awaited()
        assert any("protocol_mismatch" in r.getMessage()
                   for r in caplog.records), (
            "a refused receipt MUST be logged — this exact silence cost the "
            "feature three debugging sessions"
        )

    async def test_missing_job_id_is_refused_and_logged(self, caplog):
        registry = _registry(_job())
        coordinator = VoiceHandoffCoordinator(registry)

        with caplog.at_level(logging.WARNING, logger="channels.voice.channel"):
            await coordinator.handle(_route(), {
                "type": "handoff_received", "protocol": 2,
                "handoff_id": "handoff-1",
            })

        registry.acknowledge_handoff.assert_not_awaited()
        assert any("missing_identifier" in r.getMessage()
                   for r in caplog.records)

    async def test_route_mismatch_is_refused_and_logged(self, caplog):
        job = _job()
        job.origin_route_id = "some-other-route"
        registry = _registry(job)
        coordinator = VoiceHandoffCoordinator(registry)
        frame = VoiceHandoff.from_job("utterance-1", job).frame()

        with caplog.at_level(logging.WARNING, logger="channels.voice.channel"):
            await coordinator.handle(_route(), _client_receipt(frame))

        registry.acknowledge_handoff.assert_not_awaited()
        assert any("route_mismatch" in r.getMessage() for r in caplog.records)

    async def test_a_foreign_frame_type_stays_silent(self, caplog):
        """The WS multiplexes other traffic — only OUR type is our business."""
        registry = _registry(_job())
        coordinator = VoiceHandoffCoordinator(registry)

        with caplog.at_level(logging.WARNING, logger="channels.voice.channel"):
            await coordinator.handle(_route(), {"type": "block", "text": "hi"})

        assert not caplog.records
        registry.acknowledge_handoff.assert_not_awaited()
