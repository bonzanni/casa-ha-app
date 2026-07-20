from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


SpeakerKind = Literal["user", "resident", "specialist", "executor", "system"]
SensitivityTier = Literal["public", "friends", "family", "private"]


@dataclass(frozen=True, slots=True)
class SpeakerProvenance:
    speaker_kind: SpeakerKind
    role_id: str | None = None
    persona_id: str | None = None
    persona_version: str | None = None
    display_name: str | None = None
    binding_digest: str | None = None
    user_peer: str | None = None
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetainedTurn:
    text: str
    provenance: SpeakerProvenance


@dataclass(frozen=True, slots=True)
class RecallHit:
    text: str
    memory_type: str
    sensitivity: SensitivityTier
    application_tags: tuple[str, ...]
    provenance: SpeakerProvenance | None
    backend_id: str | None
    document_id: str | None
    chunk_id: str | None
    source_fact_ids: tuple[str, ...] | None
    metadata: Mapping[str, object] | None
    context: str | None
    score: float | None

    @staticmethod
    def freeze_metadata(value: dict[str, object] | None):
        return None if value is None else MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class TrustedOrigin:
    """Server-created route/authentication result; never decoded from payload."""
    route: Literal["telegram", "voice", "invoke", "webhook"]
    is_authenticated: bool
    clearance: SensitivityTier


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Identity asserted by the authenticated transport, not by message text."""
    stable_id: str
    configured_display_name: str | None
