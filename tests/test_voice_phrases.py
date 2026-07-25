"""Channel-owned wording: varied phrasing, fixed commitments (#233).

The operator asked for phrasing that is not robotic. The constraint that
cannot bend is that the PROMISE match what the endpoint can actually do — Casa
promised a spoken answer to a phone with no speaker, and the ruling expired
unheard. So the wording varies; the commitment is derived from the selected
modality, never authored by the model.
"""

from __future__ import annotations

import pytest

from voice_phrases import acknowledgement, announcement, seed_for

pytestmark = [pytest.mark.unit]

_SEEDS = range(24)


class TestAcknowledgementMatchesModality:
    def test_audio_promises_to_answer_here(self):
        for seed in _SEEDS:
            text = acknowledgement("Judge", "audio", seed)
            assert "Judge" in text
            low = text.lower()
            assert "here" in low or "read it out" in low, text
            # Must never imply a notification on a speaking endpoint.
            assert "send" not in low, text

    def test_text_promises_to_send_not_to_speak(self):
        for seed in _SEEDS:
            text = acknowledgement("Judge", "text", seed)
            assert "Judge" in text
            low = text.lower()
            assert "send" in low, text
            # The live bug in one assertion: never promise speech to a device
            # that cannot speak.
            assert "answer here" not in low, text
            assert "read it out" not in low, text

    def test_every_acknowledgement_sets_a_wait_expectation(self):
        for modality in ("audio", "text"):
            for seed in _SEEDS:
                text = acknowledgement("Judge", modality, seed)
                assert "minute" in text.lower(), text

    def test_unknown_modality_is_treated_as_non_speaking(self):
        """Fail safe: promising a notification and also speaking is
        recoverable; promising speech that never comes is not."""
        text = acknowledgement("Judge", "smoke-signal", 3)
        assert "send" in text.lower()

    def test_missing_specialist_name_still_reads(self):
        assert "the specialist" in acknowledgement(None, "audio", 1)


class TestVariation:
    def test_wording_varies(self):
        variants = {acknowledgement("Judge", "audio", s) for s in _SEEDS}
        assert len(variants) > 1, "phrasing should vary, not be one fixed string"

    def test_same_seed_is_stable(self):
        assert (acknowledgement("Judge", "audio", 7)
                == acknowledgement("Judge", "audio", 7))

    def test_seed_is_derived_stably_from_the_job_id(self):
        job_id = "748e6643-f1a2-4f6f-bdb4-cf944f5c29c0"
        assert seed_for(job_id) == seed_for(job_id)
        assert isinstance(seed_for(job_id), int)

    def test_seed_tolerates_junk(self):
        for bad in (None, "", "zzzz", 12345):
            assert seed_for(bad) == 0 or isinstance(seed_for(bad), int)


class TestAnnouncement:
    def test_answer_is_attributed_and_varies(self):
        outs = {announcement("Judge", "Counterspell is an Instant.", s)
                for s in _SEEDS}
        assert len(outs) > 1
        for text in outs:
            assert "Judge" in text
            assert "Counterspell is an Instant." in text

    def test_specialist_text_is_never_altered(self):
        spoken = "It resolves; the ability still triggers."
        for seed in _SEEDS:
            assert spoken in announcement("Judge", spoken, seed)
