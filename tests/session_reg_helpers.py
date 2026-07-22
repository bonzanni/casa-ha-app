"""Task 9: shared stub provenances for tests that call
``SessionRegistry.register`` but do not themselves exercise provenance content.

``register`` gained REQUIRED keyword-only ``binding_digest``/
``speaker_provenance``/``user_provenance`` params (an unset provenance is the
bug the personality plan closes — there is no back-compat default). Tests whose
subject is elsewhere (sweeper, reaper, reset hooks, save orchestration) pass
these honest stand-ins: an ``assistant`` resident identity and an anonymous
telegram user."""
from __future__ import annotations

from personality_types import SpeakerProvenance

# A valid resident executing-identity (mirrors the assistant resident used by
# most fixtures). Provenance validation requires persona_id/version/digest for
# a resident, so this is a fully-formed stand-in.
STUB_BINDING_DIGEST = "sha256:" + "a" * 64
STUB_SPEAKER_PROV = SpeakerProvenance(
    speaker_kind="resident",
    role_id="resident:assistant",
    persona_id="casa/tester",
    persona_version="0.1.0",
    display_name="Tester",
    binding_digest=STUB_BINDING_DIGEST,
)
# A valid anonymous user identity.
STUB_USER_PROV = SpeakerProvenance(speaker_kind="user", user_peer="tester")

# --- Per-role resident identity (for _make_agent-driven resume tests) --------
# A single fixed binding digest every synthetic resident config shares, so a
# seed entry written under a role's canonical id resumes against that config.
RESIDENT_DIGEST = "sha256:" + "b" * 64


def resident_role_id(slot: str) -> str:
    return f"resident:{slot}"


def resident_prov(slot: str) -> SpeakerProvenance:
    """A valid resident executing-identity for a role slot (e.g. ``butler``)."""
    return SpeakerProvenance(
        speaker_kind="resident",
        role_id=f"resident:{slot}",
        persona_id=f"casa/{slot}",
        persona_version="0.1.0",
        display_name=slot.capitalize(),
        binding_digest=RESIDENT_DIGEST,
    )
