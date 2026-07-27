"""Canonical per-kind media policy table (v0.73.0, spec §3.1).

The ONE type-specific surface for the ``send_media`` capability: the JSON-schema
``kind`` enum, argument validation, the magic gate, the extension allowlist, the
size cap, and the PTB send-method dispatch ALL derive from ``MEDIA_POLICIES``.

Dependency-neutral by design: this module imports only the stdlib so BOTH
``tools.py`` and ``channels/telegram.py`` can import it without a cycle
(``tools`` already imports ``channels``; putting the table in ``tools`` would
make a ``channels.telegram`` import of it cycle).

Every ``accepts(head)`` predicate is TOTAL — it returns ``False`` (never raises)
on empty/short input. Slicing/``startswith`` are inherently safe; every index
access is length-guarded.
"""
from __future__ import annotations

from typing import Callable, NamedTuple

_MB = 1024 * 1024


class MediaPolicy(NamedTuple):
    ptb_method: str                     # Bot.<method> to dispatch to
    accepts: Callable[[bytes], bool]    # TOTAL magic predicate over the head bytes
    extensions: frozenset[str]          # lower-cased, dot-prefixed allowlist
    size_cap: int                       # bytes


def _accepts_pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF-")


def _accepts_photo(head: bytes) -> bool:
    # JPEG SOI (FF D8 FF) or the 8-byte PNG signature.
    return head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n"


def _accepts_mp3(head: bytes) -> bool:
    # ID3v2 tag, or a VALIDATED MPEG Layer-III frame header. The bare 11-bit
    # sync mask (FF Ex) is too broad — it also matches ADTS AAC (FF F1) and
    # MPEG Layer I/II, and would index past short input. Require, in order:
    if head[:3] == b"ID3":
        return True
    if len(head) < 4:
        return False
    if head[0] != 0xFF:
        return False
    b1, b2 = head[1], head[2]
    if (b1 & 0xE0) != 0xE0:        # 11-bit frame sync
        return False
    if (b1 & 0x18) == 0x08:        # MPEG version ID: reserved -> reject
        return False
    if (b1 & 0x06) != 0x02:        # Layer III only (rejects AAC's reserved 00 + Layer I/II)
        return False
    if (b2 & 0xF0) in (0x00, 0xF0):  # bitrate index: free/bad -> reject
        return False
    if (b2 & 0x0C) == 0x0C:        # sample-rate index: reserved -> reject
        return False
    return True


def _accepts_ogg_opus(head: bytes) -> bool:
    # Ogg container whose first page carries the Opus ID header (RFC 7845: the
    # OpusHead magic sits in the first packet, alone on the first Ogg page).
    return head[:4] == b"OggS" and b"OpusHead" in head[:64]


MEDIA_POLICIES: dict[str, MediaPolicy] = {
    "document": MediaPolicy("send_document", _accepts_pdf,
                            frozenset({".pdf"}), 20 * _MB),
    "photo": MediaPolicy("send_photo", _accepts_photo,
                         frozenset({".jpg", ".jpeg", ".png"}), 10 * _MB),
    "audio": MediaPolicy("send_audio", _accepts_mp3,
                         frozenset({".mp3"}), 20 * _MB),
    "voice": MediaPolicy("send_voice", _accepts_ogg_opus,
                         frozenset({".ogg", ".oga"}), 20 * _MB),
}
