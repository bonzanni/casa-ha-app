"""MEDIA_POLICIES magic predicates — per-kind acceptance + totality (v0.73.0)."""
from __future__ import annotations

import pytest

from media_policies import MEDIA_POLICIES, MediaPolicy

pytestmark = pytest.mark.unit

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
ID3_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00rest"
# Valid MPEG-1 Layer III frame: FF FB 90 64 (sync, layer III, bitrate ok, 44.1k)
LAYER3_MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 60
ADTS_AAC = b"\xff\xf1\x50\x80\x00\x1f\xfc"          # FF F1 — reserved layer (00)
LAYER2_MP3 = b"\xff\xfd\x90\x64" + b"\x00" * 60      # FF FD — Layer II
OGG_OPUS = b"OggS\x00\x02" + b"\x00" * 22 + b"\x01OpusHead\x01\x02"
OGG_VORBIS = b"OggS\x00\x02" + b"\x00" * 22 + b"\x01vorbis\x00\x00"


def test_table_shape():
    assert list(MEDIA_POLICIES) == ["document", "photo", "audio", "voice"]
    for k, p in MEDIA_POLICIES.items():
        assert isinstance(p, MediaPolicy)
        assert p.ptb_method == {"document": "send_document", "photo": "send_photo",
                                "audio": "send_audio", "voice": "send_voice"}[k]
        assert p.size_cap >= 10 * 1024 * 1024
        assert all(e == e.lower() and e.startswith(".") for e in p.extensions)


@pytest.mark.parametrize("head", [b"", b"%", b"\xff", b"\xff\xd8", b"O", b"ID"])
def test_predicates_are_total_on_short_input(head):
    # No predicate may raise on empty/short input; all must return a bool.
    for p in MEDIA_POLICIES.values():
        assert p.accepts(head) in (True, False)


def test_document_accepts_pdf_only():
    assert MEDIA_POLICIES["document"].accepts(PDF) is True
    assert MEDIA_POLICIES["document"].accepts(PNG) is False
    assert MEDIA_POLICIES["document"].accepts(b"%PD") is False


def test_photo_accepts_jpeg_and_png():
    ph = MEDIA_POLICIES["photo"]
    assert ph.accepts(JPEG) is True
    assert ph.accepts(PNG) is True
    assert ph.accepts(PDF) is False
    assert ph.accepts(b"\xff\xd8") is False  # too short for the 3-byte JPEG SOI


def test_audio_accepts_id3_and_layer3_rejects_aac_and_layer2():
    au = MEDIA_POLICIES["audio"]
    assert au.accepts(ID3_MP3) is True
    assert au.accepts(LAYER3_MP3) is True                        # FF FB (MPEG1 L3)
    assert au.accepts(b"\xff\xfa\x90\x64" + b"\x00" * 60) is True  # FF FA (MPEG1 L3)
    assert au.accepts(b"\xff\xf3\x40\x00" + b"\x00" * 60) is True  # FF F3 (MPEG2 L3)
    assert au.accepts(ADTS_AAC) is False      # FF F1 — reserved layer, must reject
    assert au.accepts(b"\xff\xf9\x50\x80") is False  # FF F9 — AAC ADTS variant
    assert au.accepts(LAYER2_MP3) is False    # Layer II — must reject
    assert au.accepts(b"\xff\xfb\x00\x64") is False  # bitrate index 0 (free) — reject
    assert au.accepts(b"\xff\xfb\xf0\x64") is False  # bitrate index 15 (bad) — reject
    assert au.accepts(b"\xff\xfb\x9c\x64") is False  # sample-rate index 3 (reserved) — reject
    assert au.accepts(b"\xff\xfb") is False    # sync only, indexes past -> must not raise


def test_voice_accepts_opus_rejects_vorbis():
    vo = MEDIA_POLICIES["voice"]
    assert vo.accepts(OGG_OPUS) is True
    assert vo.accepts(OGG_VORBIS) is False
    assert vo.accepts(b"OggS") is False       # OggS but no OpusHead


def test_exact_caps_and_extensions():
    assert MEDIA_POLICIES["document"].size_cap == 20 * 1024 * 1024
    assert MEDIA_POLICIES["photo"].size_cap == 10 * 1024 * 1024
    assert MEDIA_POLICIES["audio"].size_cap == 20 * 1024 * 1024
    assert MEDIA_POLICIES["voice"].size_cap == 20 * 1024 * 1024
    assert MEDIA_POLICIES["document"].extensions == frozenset({".pdf"})
    assert MEDIA_POLICIES["photo"].extensions == frozenset({".jpg", ".jpeg", ".png"})
    assert MEDIA_POLICIES["audio"].extensions == frozenset({".mp3"})
    assert MEDIA_POLICIES["voice"].extensions == frozenset({".ogg", ".oga"})
