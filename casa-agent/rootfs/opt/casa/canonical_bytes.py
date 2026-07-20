from __future__ import annotations

import hashlib
import unicodedata

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
