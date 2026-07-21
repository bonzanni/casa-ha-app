from __future__ import annotations

import hashlib
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


def deep_freeze(value: object) -> object:
    """Recursively freeze authored content: dict/Mapping -> MappingProxyType
    of deep-frozen values, list/tuple -> tuple of deep-frozen values,
    scalars unchanged. Used by role_artifact.py and persona_pack.py so
    that wrapping only the TOP-level mapping in MappingProxyType (which
    leaves nested dicts/lists mutable) can't silently let a caller mutate
    loaded, checksummed content in place."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    return value
