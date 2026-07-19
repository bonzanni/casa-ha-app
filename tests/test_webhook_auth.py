"""Per-trigger webhook auth-mode verifier matrix (Release A, Task 2)."""
from __future__ import annotations

import hashlib
import hmac

from webhook_auth import verify

SEC = b"s3cr3t-bytes"


def _hb_sig(body: bytes) -> str:
    return hmac.new(SEC, body, hashlib.sha256).hexdigest()


def test_hmac_body_valid():
    assert verify(
        "hmac_body", body=b"{}",
        headers={"X-Webhook-Signature": _hb_sig(b"{}")},
        secret=SEC, header_name="X-Webhook-Signature",
        tolerance_secs=0, now=0,
    )


def test_hmac_body_wrong_secret_fails():
    assert not verify(
        "hmac_body", body=b"{}",
        headers={"X-Webhook-Signature": "deadbeef"},
        secret=SEC, header_name="X-Webhook-Signature",
        tolerance_secs=0, now=0,
    )


def test_hmac_body_missing_header_fails():
    assert not verify(
        "hmac_body", body=b"{}", headers={},
        secret=SEC, header_name="X-Webhook-Signature",
        tolerance_secs=0, now=0,
    )


def test_static_header_valid_and_missing():
    assert verify(
        "static_header", body=b"", headers={"X-API-Key": "s3cr3t-bytes"},
        secret=SEC, header_name="X-API-Key", tolerance_secs=0, now=0,
    )
    assert not verify(
        "static_header", body=b"", headers={},
        secret=SEC, header_name="X-API-Key", tolerance_secs=0, now=0,
    )


def test_non_ascii_header_is_false_not_raise():
    assert not verify(
        "static_header", body=b"", headers={"X-API-Key": "café"},
        secret=SEC, header_name="X-API-Key", tolerance_secs=0, now=0,
    )


def test_empty_secret_fails_closed():
    assert not verify(
        "static_header", body=b"", headers={"X-API-Key": ""},
        secret=b"", header_name="X-API-Key", tolerance_secs=0, now=0,
    )


def _ts_sig(t: int, body: bytes) -> str:
    return hmac.new(SEC, f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def test_timestamped_hmac_valid_within_tolerance():
    hdr = f"t=1000,v0={_ts_sig(1000, b'{}')}"
    assert verify(
        "timestamped_hmac", body=b"{}",
        headers={"ElevenLabs-Signature": hdr},
        secret=SEC, header_name="ElevenLabs-Signature",
        tolerance_secs=300, now=1200,
    )


def test_timestamped_hmac_stale_rejected():
    hdr = f"t=1000,v0={_ts_sig(1000, b'{}')}"
    assert not verify(
        "timestamped_hmac", body=b"{}",
        headers={"ElevenLabs-Signature": hdr},
        secret=SEC, header_name="ElevenLabs-Signature",
        tolerance_secs=300, now=5000,
    )


def test_timestamped_hmac_future_rejected():
    hdr = f"t=9000,v0={_ts_sig(9000, b'{}')}"
    assert not verify(
        "timestamped_hmac", body=b"{}",
        headers={"ElevenLabs-Signature": hdr},
        secret=SEC, header_name="ElevenLabs-Signature",
        tolerance_secs=300, now=1000,
    )


def test_timestamped_hmac_malformed_rejected():
    for bad in ["", "t=,v0=", "t=abc,v0=x", "v0=x", "t=1000",
                "t=1000,v0=x,extra=1", "t=1000, v0=abcd"]:
        assert not verify(
            "timestamped_hmac", body=b"{}",
            headers={"ElevenLabs-Signature": bad},
            secret=SEC, header_name="ElevenLabs-Signature",
            tolerance_secs=300, now=1000,
        )


def test_unknown_mode_fails():
    assert not verify(
        "nonsense", body=b"", headers={},
        secret=SEC, header_name="X", tolerance_secs=0, now=0,
    )
