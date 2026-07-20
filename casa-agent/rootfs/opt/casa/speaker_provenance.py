from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import asdict
from typing import Iterable, Literal

from canonical_bytes import canonical_json_bytes
from personality_types import (
    AuthenticatedUser,
    SpeakerProvenance,
    TrustedOrigin,
)

RESERVED_SOURCE_NAMESPACE = "casa-source-"
RESERVED_SOURCE_PREFIX = "casa-source-v1."
_ROLE_RE = re.compile(r"^(resident|specialist|executor):[a-z0-9][a-z0-9-]*$")
_PERSONA_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/[a-z0-9][a-z0-9-]*$"
)
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MAX_CANONICAL_PROVENANCE_BYTES = 2048
MAX_ENCODED_PROVENANCE_TAG_BYTES = 2746
_FIELD_LIMITS = {
    "role_id": (128, 128),
    "persona_id": (192, 192),
    "persona_version": (64, 64),
    "display_name": (80, 320),
    "user_peer": (256, 512),
    "user_id": (256, 512),
}


class UserProvenance:
    """Trusted factory namespace; not a fifth runtime identity object."""

    @staticmethod
    def from_origin(
        *,
        surface: Literal["telegram", "voice", "invoke", "webhook"],
        server_origin: TrustedOrigin,
        authenticated_user: AuthenticatedUser | None,
        user_peer: str,
    ) -> SpeakerProvenance:
        if server_origin.route != surface:
            raise ValueError("trusted origin route does not match surface")
        if surface == "voice" and authenticated_user is None:
            if user_peer != "voice_speaker":
                raise ValueError("anonymous voice must use voice_speaker")
            value = SpeakerProvenance(
                speaker_kind="user", user_peer="voice_speaker",
                user_id=None, display_name=None,
            )
            validate_speaker_provenance(value)
            return value
        trusted_user = (
            authenticated_user
            if authenticated_user is not None and server_origin.is_authenticated
            else None
        )
        value = SpeakerProvenance(
            speaker_kind="user",
            display_name=(
                unicodedata.normalize("NFC", trusted_user.configured_display_name)
                if trusted_user and trusted_user.configured_display_name else None
            ),
            user_peer=unicodedata.normalize("NFC", user_peer),
            user_id=(
                unicodedata.normalize("NFC", trusted_user.stable_id)
                if trusted_user else None
            ),
        )
        validate_speaker_provenance(value)
        return value


def _validate_string_bound(name: str, value: str | None) -> None:
    if value is None:
        return
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise ValueError(f"{name} must already be NFC")
    scalar_limit, byte_limit = _FIELD_LIMITS[name]
    if len(value) > scalar_limit or len(value.encode("utf-8")) > byte_limit:
        raise ValueError(f"{name} exceeds provenance length limit")


def validate_speaker_provenance(value: SpeakerProvenance) -> None:
    for field_name in ("speaker_kind", "role_id", "persona_id", "persona_version",
                       "display_name", "binding_digest", "user_peer", "user_id"):
        field_value = getattr(value, field_name)
        if field_value is not None and not isinstance(field_value, str):
            raise ValueError(f"{field_name} must be a string or null")
    kind = value.speaker_kind
    if kind in {"resident", "specialist"}:
        if not value.role_id or not value.role_id.startswith(kind + ":"):
            raise ValueError("agent role_id must match speaker_kind")
        if not value.persona_id or not _PERSONA_RE.fullmatch(value.persona_id):
            raise ValueError("resident/specialist persona_id is required")
        if not value.persona_version or not _SEMVER_RE.fullmatch(value.persona_version):
            raise ValueError("resident/specialist persona_version is required")
        if not value.binding_digest or not _CHECKSUM_RE.fullmatch(value.binding_digest):
            raise ValueError("resident/specialist binding_digest is required")
        if value.user_peer is not None or value.user_id is not None:
            raise ValueError("agent provenance cannot contain user identity")
    elif kind == "executor":
        if not value.role_id or not value.role_id.startswith("executor:"):
            raise ValueError("executor role_id is required")
        if any((value.persona_id, value.persona_version,
                value.binding_digest, value.user_peer, value.user_id)):
            raise ValueError("executor persona and user fields must be null")
    elif kind == "user":
        if not value.user_peer:
            raise ValueError("user_peer is required for user provenance")
        if any((value.role_id, value.persona_id,
                value.persona_version, value.binding_digest)):
            raise ValueError("user agent fields must be null")
    elif kind == "system":
        if any((value.role_id, value.persona_id, value.persona_version,
                value.binding_digest, value.user_peer, value.user_id)):
            raise ValueError("system identity fields must be null")
    else:
        raise ValueError("invalid speaker_kind")
    if value.role_id is not None and not _ROLE_RE.fullmatch(value.role_id):
        raise ValueError("invalid role_id")
    for field_name in _FIELD_LIMITS:
        _validate_string_bound(field_name, getattr(value, field_name))


def provenance_mapping(value: SpeakerProvenance) -> dict[str, object]:
    validate_speaker_provenance(value)
    return asdict(value)


def provenance_from_mapping(raw: object) -> SpeakerProvenance:
    if not isinstance(raw, dict):
        raise ValueError("provenance must be an object")
    expected = {
        "speaker_kind", "role_id", "persona_id", "persona_version",
        "display_name", "binding_digest", "user_peer", "user_id",
    }
    if set(raw) != expected:
        raise ValueError("provenance fields must match the v1 schema exactly")
    value = SpeakerProvenance(**raw)
    validate_speaker_provenance(value)
    return value


def encode_provenance_tag(value: SpeakerProvenance) -> str:
    wire = canonical_json_bytes(provenance_mapping(value))
    if len(wire) > MAX_CANONICAL_PROVENANCE_BYTES:
        raise ValueError("canonical provenance payload exceeds 2048 bytes")
    payload = base64.urlsafe_b64encode(wire).decode("ascii").rstrip("=")
    tag = RESERVED_SOURCE_PREFIX + payload
    if len(tag.encode("ascii")) > MAX_ENCODED_PROVENANCE_TAG_BYTES:
        raise ValueError("encoded provenance tag exceeds 2746 bytes")
    return tag


def decode_provenance_tag(tag: str) -> SpeakerProvenance:
    if not isinstance(tag, str) or not tag.isascii():
        raise ValueError("provenance tag must be ASCII")
    if len(tag.encode("ascii")) > MAX_ENCODED_PROVENANCE_TAG_BYTES:
        raise ValueError("encoded provenance tag exceeds 2746 bytes")
    if not tag.startswith(RESERVED_SOURCE_PREFIX):
        raise ValueError("unsupported provenance tag")
    payload = tag[len(RESERVED_SOURCE_PREFIX):]
    if not payload or "=" in payload:
        raise ValueError("provenance tag must use unpadded base64url")
    if (len(payload) * 3) // 4 > MAX_CANONICAL_PROVENANCE_BYTES:
        raise ValueError("encoded payload would exceed 2048 bytes")
    try:
        wire = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        if len(wire) > MAX_CANONICAL_PROVENANCE_BYTES:
            raise ValueError("canonical provenance payload exceeds 2048 bytes")
        raw = json.loads(wire)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid provenance payload") from exc
    if wire != canonical_json_bytes(raw):
        raise ValueError("provenance payload is not canonical RFC 8785 JSON")
    return provenance_from_mapping(raw)


def decode_provenance_from_tags(
    tags: Iterable[str],
) -> tuple[SpeakerProvenance | None, str]:
    reserved = [
        tag for tag in tags
        if isinstance(tag, str) and tag.startswith(RESERVED_SOURCE_NAMESPACE)
    ]
    if not reserved:
        return None, "missing"
    if len(reserved) != 1:
        return None, "multiple"
    try:
        return decode_provenance_tag(reserved[0]), "ok"
    except ValueError:
        return None, "malformed"
