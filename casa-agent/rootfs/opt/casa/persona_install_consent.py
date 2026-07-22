"""Structural operator-consent gate for the bare-persona install pipeline —
sibling of specialist_install_consent.py, same ChallengeCoordinator pattern
(Round-2, finding #3)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from persona_install import PersonaInstallAckStore, persona_install_consent_identity

logger = logging.getLogger(__name__)

INSTALL_CONSENT_TTL_S = 600


@dataclass(frozen=True)
class PersonaInstallConsentKey:
    """Same shape as specialist_install_consent.SpecialistInstallConsentKey —
    a plain frozen dataclass, matching trigger_consent.TriggerConsentKey's
    real pattern (no shared base class exists in the codebase)."""
    persona_id: str
    identity: str


def render_persona_install_consent_message(inspection: Any) -> str:
    return (
        "\U0001F510 Persona install consent\n\n"
        f"Install '{inspection.persona_id}@{inspection.version}' "
        f"({inspection.display_name})?\n"
        f"Checksum: {inspection.checksum}\n\n"
        "Approve to install; Deny to discard the staged fetch."
    )


def prompt_persona_install_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int, inspection: Any,
    acks: "PersonaInstallAckStore", reconcile_cb: "Callable[[], Awaitable[None]] | None" = None,
) -> Any:
    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    key = PersonaInstallConsentKey(persona_id=inspection.persona_id, identity=identity)
    text = render_persona_install_consent_message(inspection)

    def _on_commit_sync(idx: int, meta: dict) -> None:
        if idx == 0:
            acks.record(identity=identity, persona_id=inspection.persona_id,
                        version=inspection.version, checksum=inspection.checksum)
            meta["acked"] = True

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"⌛ Expired — persona install consent for {inspection.persona_id!r} was not "
                    "answered; nothing was installed")
                return
            if outcome.get("option_index") == 0:
                if not req.meta.get("acked"):
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "internal error recording persona install consent — re-run the install to "
                        "be prompted again")
                    return
                await channel.edit_dm_message(
                    chat_id, message_id, f"✅ Approved — installing {inspection.persona_id!r}")
                if reconcile_cb is not None:
                    try:
                        await reconcile_cb()
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "post-consent persona install commit failed (persona_id=%s)",
                            inspection.persona_id)
            else:
                await channel.edit_dm_message(
                    chat_id, message_id, f"❌ Denied — {inspection.persona_id!r} was not installed")

        return _finish

    return coordinator.register_challenge(
        key, chat_id=chat_id, operator_id=operator_id, channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="persona_install_consent",
        meta_extra={"persona_id": inspection.persona_id, "persona_version": inspection.version},
        timeout_s=INSTALL_CONSENT_TTL_S,
    )
