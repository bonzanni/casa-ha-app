"""Channel-owned wording for deferred specialist answers (#233).

Two rules, and they pull in opposite directions:

* the phrasing should VARY, so Casa does not sound like a recording;
* the COMMITMENT each phrase makes is fixed per modality and chosen by the
  channel — never by the model.

The second rule is not stylistic. Promising to speak an answer aloud on a
device that can only show a notification is the exact failure this module
exists to prevent, and a model asked to "acknowledge naturally" will make that
promise. So the caller passes the modality that was actually selected, and the
wording is derived from it.

Selection is by explicit seed rather than random draw, so a turn's wording is
reproducible in tests and from the logs.
"""

from __future__ import annotations

# Spoken here, on the device that asked.
_ACK_AUDIO = (
    "I'll ask {s} — this can take up to a minute, and I'll answer here.",
    "Let me put that to {s}. It may take up to a minute; I'll read it out when it lands.",
    "Asking {s} now — give me up to a minute and I'll tell you here.",
    "I'll check with {s}. That can take a minute, then I'll say it here.",
)

# Sent to the device that asked — it cannot speak, so do not imply it will.
_ACK_TEXT = (
    "I'll ask {s} — this can take up to a minute, and I'll send the answer to your device.",
    "Let me put that to {s}. It may take up to a minute; I'll send it over.",
    "Asking {s} now — I'll send you the answer in up to a minute.",
    "I'll check with {s} — up to a minute — then send the answer to your device.",
)

# The answer lands long after the question, so attribute it.
_ANNOUNCE = (
    "{s} says: {a}",
    "Here's {s}: {a}",
    "{s} came back on that: {a}",
    "From {s}: {a}",
)

_FALLBACK_SPECIALIST = "the specialist"


def _pick(options: tuple[str, ...], seed: int) -> str:
    try:
        index = abs(int(seed))
    except (TypeError, ValueError):
        index = 0
    return options[index % len(options)]


def seed_for(job_id: str | None) -> int:
    """A stable seed from a job id, so wording is reproducible per job."""
    if not isinstance(job_id, str) or not job_id:
        return 0
    digits = "".join(ch for ch in job_id if ch in "0123456789abcdefABCDEF")
    if not digits:
        return 0
    try:
        return int(digits[:8], 16)
    except ValueError:
        return 0


def acknowledgement(specialist: str | None, modality: str | None,
                    seed: int) -> str:
    """The spoken promise. Its commitment always matches ``modality``.

    Anything other than ``audio`` is treated as a non-speaking endpoint: it is
    safer to promise a notification and then also speak, than to promise speech
    on a device that cannot produce it.
    """
    options = _ACK_AUDIO if modality == "audio" else _ACK_TEXT
    return _pick(options, seed).format(
        s=(specialist or _FALLBACK_SPECIALIST))


def announcement(specialist: str | None, spoken: str, seed: int) -> str:
    """Attribute the specialist's own answer when it finally arrives."""
    return _pick(_ANNOUNCE, seed).format(
        s=(specialist or _FALLBACK_SPECIALIST), a=spoken)
