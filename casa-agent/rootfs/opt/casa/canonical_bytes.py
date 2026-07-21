from __future__ import annotations

import hashlib
import math
import unicodedata
from types import MappingProxyType
from typing import Mapping

import rfc8785


def canonical_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    return "\n".join(lines).rstrip("\n") + "\n"


def canonical_json_bytes(value: object) -> bytes:
    return rfc8785.dumps(value)


def checksum_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def checksum_json(value: object) -> str:
    return checksum_bytes(canonical_json_bytes(value))


_DEEP_FREEZE_MAX_DEPTH = 64


def deep_freeze(
    value: object,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
    max_depth: int = _DEEP_FREEZE_MAX_DEPTH,
) -> object:
    """Recursively freeze authored content: dict/Mapping -> MappingProxyType
    of deep-frozen values, list/tuple -> tuple of deep-frozen values,
    scalars unchanged. Used by role_artifact.py and persona_pack.py so
    that wrapping only the TOP-level mapping in MappingProxyType (which
    leaves nested dicts/lists mutable) can't silently let a caller mutate
    loaded, checksummed content in place.

    Hardened independent of any caller's own guards (foundation review
    r2, R1): a visited-set-by-id cycle guard and a depth bound make this
    primitive raise ValueError on a cyclic or pathologically deep input
    rather than crash with an uncaught RecursionError. deep_freeze may
    assume its input is JSON-native (dict/list/tuple/scalars only) —
    callers that parse untrusted data (role_artifact.py, persona_pack.py)
    are expected to call assert_json_safe first; deep_freeze does not
    itself special-case set/bytes/other non-JSON types."""
    if _depth > max_depth:
        raise ValueError("value nesting exceeds limit")
    if isinstance(value, Mapping):
        if _seen is None:
            _seen = set()
        if id(value) in _seen:
            raise ValueError("cyclic structure cannot be frozen")
        _seen.add(id(value))
        frozen_mapping = MappingProxyType({
            key: deep_freeze(item, _depth=_depth + 1, _seen=_seen, max_depth=max_depth)
            for key, item in value.items()
        })
        _seen.discard(id(value))
        return frozen_mapping
    if isinstance(value, (list, tuple)):
        if _seen is None:
            _seen = set()
        if id(value) in _seen:
            raise ValueError("cyclic structure cannot be frozen")
        _seen.add(id(value))
        frozen_sequence = tuple(
            deep_freeze(item, _depth=_depth + 1, _seen=_seen, max_depth=max_depth)
            for item in value
        )
        _seen.discard(id(value))
        return frozen_sequence
    return value


# G3 (foundation review r4, P1): the exact integer bound rfc8785's own
# canonical_json_bytes enforces (rfc8785._impl._INT_MIN/_INT_MAX) — the
# IEEE-754/JCS safe-integer range. Mirrored here (not imported) since
# rfc8785 does not export these as public constants; kept in lockstep with
# canonical_json_bytes's own IntegerDomainError check below.
_CANONICAL_INT_MIN = -(2**53) + 1
_CANONICAL_INT_MAX = 2**53 - 1

_JSON_SCALARS = (str, bool, int, float, type(None))


def _require_utf8_encodable(text: str, *, what: str = "string") -> None:
    # H1 (foundation review r5): shared by both the dict-key check and the
    # str-leaf check below, so a lone surrogate is rejected identically
    # whether it appears as a mapping KEY or a string VALUE — DRY, and
    # closes the gap where only values were checked.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{what} not UTF-8 encodable") from exc


def assert_json_safe(value, *, _depth=0, _seen=None, max_depth=64):
    """Recursively assert *value* is a finite tree of JSON-native types only.
    Rejects non-JSON types (set, bytes, datetime, ...), cycles, and excessive
    depth. Raises ValueError otherwise. bool is intentionally allowed (it is a
    JSON scalar) even though it is an int subclass.

    role_artifact.py and persona_pack.py both parse role.yaml/persona.yaml
    with yaml.safe_load, which is NOT the same type universe as json.loads:
    YAML tags/aliases can still yield `set` (!!set), `bytes` (!!binary),
    `datetime` (!!timestamp), or a cyclic structure (a self-referential
    anchor). Calling this immediately after yaml.safe_load and before any
    other recursive walk (the marker scan, deep_freeze) guarantees those
    walks only ever see a finite JSON-only tree — they can neither crash on
    a cycle nor be bypassed by a marker hidden inside a non-dict/list/str
    container."""
    if _depth > max_depth:
        raise ValueError("parsed data nesting exceeds limit")
    if _seen is None:
        _seen = set()
    if isinstance(value, dict):
        if id(value) in _seen:
            raise ValueError("cyclic structure in parsed data")
        _seen.add(id(value))
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("non-string key in parsed data")
            # H1 (foundation review r5): a dict key that IS a str still
            # needs the same UTF-8-encodability check applied to string
            # values below — a lone-surrogate key (e.g. from a YAML
            # "\Uxxxxxxxx" escape landing in surrogate range) previously
            # passed this gate unrejected, then made canonical_json_bytes
            # raise UnicodeEncodeError far downstream.
            _require_utf8_encodable(k, what="dict key")
            assert_json_safe(v, _depth=_depth + 1, _seen=_seen, max_depth=max_depth)
        _seen.discard(id(value))
        return
    if isinstance(value, list):
        if id(value) in _seen:
            raise ValueError("cyclic structure in parsed data")
        _seen.add(id(value))
        for v in value:
            assert_json_safe(v, _depth=_depth + 1, _seen=_seen, max_depth=max_depth)
        _seen.discard(id(value))
        return
    # NB: `bool` must be checked via isinstance in _JSON_SCALARS; do NOT use
    # type(value) is ... . tuple is NOT expected from yaml.safe_load; reject it
    # too (only dict/list/scalars are JSON-native from a fresh parse).
    if not isinstance(value, _JSON_SCALARS):
        raise ValueError(f"non-JSON-native type in parsed data: {type(value).__name__}")
    # F-D (foundation review r3, P1): NaN/Infinity are not valid JSON
    # numbers at all, yet every `float` was accepted here regardless —
    # `yaml.safe_load` happily parses `.nan`/`.inf`/`-.inf`, and the
    # project's own canonical_json_bytes (RFC 8785) later raises
    # FloatDomainError for them. Reject non-finite floats at the same
    # gate as every other non-JSON-native value, before that later,
    # harder-to-attribute failure. `bool` is an `int` subclass, never a
    # `float`, so this check cannot misfire on booleans.
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite float in parsed data")
    # G3 (foundation review r4, P1): assert_json_safe's contract is
    # "everything passing this can be RFC-8785-canonicalized without
    # error." An `int` outside the JCS safe-integer range (mirrors
    # canonical_json_bytes's own IntegerDomainError bound exactly, see
    # _CANONICAL_INT_MIN/_CANONICAL_INT_MAX above) passed this gate
    # unrejected yet made canonical_json_bytes raise far downstream.
    # `bool` is an `int` subclass but is never in numeric domain trouble
    # (True/False), so it must be excluded from this check.
    if isinstance(value, int) and not isinstance(value, bool):
        if value < _CANONICAL_INT_MIN or value > _CANONICAL_INT_MAX:
            raise ValueError("integer outside canonical-safe range")
    # G3 (foundation review r4, P1): a `str` containing a lone surrogate
    # (e.g. from a YAML "\Uxxxxxxxx" escape landing in surrogate range) is
    # not UTF-8 encodable, so canonical_json_bytes's UTF-8 serialization
    # raises far downstream. Reject it at the same gate as every other
    # non-canonical-safe value.
    if isinstance(value, str):
        _require_utf8_encodable(value)
