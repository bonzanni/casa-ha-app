"""Validated specialist result envelopes and voice disclosure policy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

from channel_trust import channel_trust
from sensitivity import clearance_for_channel


VOICE_JOB_OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status", "spoken_summary", "answer", "clarification",
            "citations", "assumptions", "provenance", "sensitivity",
            "delivery_ttl_s",
        ],
        "properties": {
            "status": {"enum": [
                "answered", "needs_clarification", "tentative", "not_found",
                "dependency_unavailable", "deadline_exceeded", "cancelled", "failed",
            ]},
            "spoken_summary": {"type": "string", "maxLength": 1200},
            "answer": {"type": "string"},
            "clarification": {"type": "string", "maxLength": 600},
            "citations": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "provenance": {"type": "object"},
            "sensitivity": {"enum": ["public", "household", "private"]},
            "delivery_ttl_s": {"type": "integer", "minimum": 30, "maximum": 3600},
        },
    },
}

_STATUSES = frozenset(VOICE_JOB_OUTPUT_FORMAT["schema"]["properties"]["status"]["enum"])
_SENSITIVITIES = frozenset(
    VOICE_JOB_OUTPUT_FORMAT["schema"]["properties"]["sensitivity"]["enum"]
)
_FIELDS = frozenset(VOICE_JOB_OUTPUT_FORMAT["schema"]["required"])


class VoiceJobResultError(ValueError):
    """A specialist result did not satisfy the voice-job contract."""


@dataclass(frozen=True)
class VoiceJobResult:
    """Immutable, validated result returned by a voice specialist job."""

    status: str
    spoken_summary: str
    answer: str
    clarification: str
    citations: tuple[str, ...]
    assumptions: tuple[str, ...]
    provenance: Mapping[str, Any]
    sensitivity: Literal["public", "household", "private"]
    delivery_ttl_s: int

    @property
    def awaiting_input(self) -> bool:
        return self.status == "needs_clarification"


def _string_field(payload: dict[str, Any], name: str, max_length: int | None = None) -> str:
    value = payload[name]
    if not isinstance(value, str):
        raise VoiceJobResultError(f"{name} must be a string")
    if max_length is not None and len(value) > max_length:
        raise VoiceJobResultError(f"{name} exceeds {max_length} characters")
    return value


def _string_list_field(payload: dict[str, Any], name: str) -> tuple[str, ...]:
    value = payload[name]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise VoiceJobResultError(f"{name} must be an array of strings")
    return tuple(value)


def _freeze_json(value: Any) -> Any:
    """Return an immutable snapshot of a JSON-shaped provenance value."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise VoiceJobResultError("provenance must contain only JSON values")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise VoiceJobResultError("provenance must contain only JSON values")


def parse_voice_job_result(structured_output: Any) -> VoiceJobResult:
    """Validate one SDK structured result, raising on every contract breach.

    Errors name fields only. They never interpolate the rejected payload, so a
    private result cannot escape through exception or log rendering.
    """
    if not isinstance(structured_output, dict):
        raise VoiceJobResultError("structured_output must be an object")

    present = frozenset(structured_output)
    if any(not isinstance(key, str) for key in present):
        raise VoiceJobResultError("unexpected fields are not allowed")
    missing = sorted(_FIELDS - present)
    if missing:
        raise VoiceJobResultError(f"missing required fields: {', '.join(missing)}")
    extra = present - _FIELDS
    if extra:
        raise VoiceJobResultError("unexpected fields are not allowed")

    status = structured_output["status"]
    if not isinstance(status, str) or status not in _STATUSES:
        raise VoiceJobResultError("status is invalid")
    spoken_summary = _string_field(structured_output, "spoken_summary", 1200)
    answer = _string_field(structured_output, "answer")
    clarification = _string_field(structured_output, "clarification", 600)
    citations = _string_list_field(structured_output, "citations")
    assumptions = _string_list_field(structured_output, "assumptions")

    provenance = structured_output["provenance"]
    if not isinstance(provenance, dict):
        raise VoiceJobResultError("provenance must be an object")

    sensitivity = structured_output["sensitivity"]
    if not isinstance(sensitivity, str) or sensitivity not in _SENSITIVITIES:
        raise VoiceJobResultError("sensitivity is invalid")

    delivery_ttl_s = structured_output["delivery_ttl_s"]
    if (isinstance(delivery_ttl_s, bool)
            or not isinstance(delivery_ttl_s, int)
            or not 30 <= delivery_ttl_s <= 3600):
        raise VoiceJobResultError("delivery_ttl_s must be an integer from 30 to 3600")

    if status == "answered" and not spoken_summary.strip():
        raise VoiceJobResultError("answered result requires spoken_summary")
    if status == "needs_clarification":
        if not spoken_summary.strip():
            raise VoiceJobResultError(
                "needs_clarification result requires spoken_summary"
            )
        question = clarification.strip()
        if not question or not question.endswith("?") or question.count("?") != 1:
            raise VoiceJobResultError(
                "needs_clarification result requires exactly one clarification question"
            )

    return VoiceJobResult(
        status=status,
        spoken_summary=spoken_summary,
        answer=answer,
        clarification=clarification,
        citations=citations,
        assumptions=assumptions,
        provenance=_freeze_json(provenance),
        sensitivity=sensitivity,
        delivery_ttl_s=delivery_ttl_s,
    )


def spoken_text_for(
    result: VoiceJobResult,
    *,
    prompted: bool,
    identity_clearance: Literal["household", "private"],
) -> str:
    """Resolve disclosure before any result text reaches the voice wire."""
    if result.sensitivity != "private" or identity_clearance == "private":
        return result.spoken_summary
    if prompted:
        return "Your result is ready; I can't read private details on this voice route."
    return "Your result is ready; ask me for the details."


def voice_identity_clearance(
    origin: Mapping[str, Any] | None,
) -> Literal["household", "private"]:
    """Resolve voice identity without trusting user/model speaker claims.

    ``_authenticated_speaker`` is deliberately server-owned: current voice
    ingress never copies arbitrary message context into the turn origin. A
    future speaker-authentication layer may bind the strict boolean marker,
    but it grants private disclosure only when that channel has independently
    been assigned private sensitivity clearance. Current voice therefore
    remains household-only.
    """
    if not isinstance(origin, Mapping):
        return "household"
    channel = origin.get("channel")
    if not isinstance(channel, str) or channel_trust(channel) == "public":
        return "household"
    if (origin.get("_authenticated_speaker") is True
            and clearance_for_channel(channel) == "private"):
        return "private"
    return "household"


__all__ = [
    "VOICE_JOB_OUTPUT_FORMAT",
    "VoiceJobResult",
    "VoiceJobResultError",
    "parse_voice_job_result",
    "spoken_text_for",
    "voice_identity_clearance",
]
