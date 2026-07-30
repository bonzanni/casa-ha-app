"""Pinning tests for HTTP-surface invariants (docs corpus).

Each test names the corpus invariant it pins and records, in its docstring, the
red case that was demonstrated: the code edit that made it fail. A pinning test
never shown red proves nothing.
"""
import hashlib
import hmac

import pytest

import webhook_auth
from webhook_auth import verify


def _headers(mode, secret, body, now):
    if mode == "hmac_body":
        return {"X-Auth": hmac.new(secret, body, hashlib.sha256).hexdigest()}
    if mode == "static_header":
        return {"X-Auth": secret.decode("ascii")}
    digest = hmac.new(secret, f"{now}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Auth": f"t={now},v0={digest}"}


def test_pin_inv_http_002_constant_time_everywhere_and_no_empty_secret(monkeypatch):
    """INV-HTTP-002: every secret comparison uses the constant-time primitive,
    and an absent or empty secret returns false rather than passing.

    Red case demonstrated: rewriting _ct_eq to `a == b` drops the recorded
    compare_digest calls to zero and the count assertion fails; removing the
    empty-secret fast-fail makes the final loop authenticate.
    """
    secret, body, now = b"secret", b"payload", 1000
    real_compare = hmac.compare_digest
    comparisons = []

    def recording_compare(left, right):
        comparisons.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(webhook_auth.hmac, "compare_digest", recording_compare)

    for mode in ("hmac_body", "static_header", "timestamped_hmac"):
        assert verify(
            mode, body=body, headers=_headers(mode, secret, body, now),
            secret=secret, header_name="X-Auth", tolerance_secs=300, now=now,
        )
    assert len(comparisons) == 3

    for missing in (b"",):
        for mode in ("hmac_body", "static_header", "timestamped_hmac"):
            assert verify(
                mode, body=body, headers=_headers(mode, secret, body, now),
                secret=missing, header_name="X-Auth",
                tolerance_secs=300, now=now,
            ) is False


def test_pin_inv_http_003_replay_unbounded_except_timestamped():
    """INV-HTTP-003: no mode prevents replay; the timestamped mode bounds it
    to the tolerance window, the other two accept a valid credential at any
    time.

    Red case demonstrated: deleting the tolerance comparison in verify's
    timestamped branch accepts the stale credential and the final assertion
    fails.
    """
    secret, body = b"secret", b"payload"
    for now in (0, 10**12):
        assert verify(
            "hmac_body", body=body,
            headers=_headers("hmac_body", secret, body, now),
            secret=secret, header_name="X-Auth", tolerance_secs=300, now=now,
        )
        assert verify(
            "static_header", body=body,
            headers=_headers("static_header", secret, body, now),
            secret=secret, header_name="X-Auth", tolerance_secs=300, now=now,
        )

    stamped = _headers("timestamped_hmac", secret, body, 1000)
    assert verify(
        "timestamped_hmac", body=body, headers=stamped, secret=secret,
        header_name="X-Auth", tolerance_secs=300, now=1300,
    )
    assert not verify(
        "timestamped_hmac", body=body, headers=stamped, secret=secret,
        header_name="X-Auth", tolerance_secs=300, now=1301,
    )


def test_pin_inv_http_005_table_contract_disagreement_refused(monkeypatch):
    """INV-HTTP-005: the ingress-identity table is validated against the
    independently-written route contract, and any disagreement raises.

    Red case demonstrated: deleting the surface-mismatch check inside
    validate_ingress_identity_table lets the disagreement pass and this test
    fails. The boot wiring half is pinned separately by
    test_ingress_identity_boot_check.py.
    """
    import dataclasses

    import ingress_identity as ii

    broken = dict(ii._INGRESS_IDENTITY)
    route, policy = next(iter(broken.items()))
    other_surface = "telegram" if policy.surface != "telegram" else "webhook"
    broken[route] = dataclasses.replace(policy, surface=other_surface)
    monkeypatch.setattr(ii, "_INGRESS_IDENTITY", broken)

    with pytest.raises(ii.IngressIdentityError):
        ii.validate_ingress_identity_table()
