"""Tests for authz_grants.py — canonical argument JSON + the single-use,
TTL-bound GrantStore (A:§3.3, §3.6), plus the casa_core.py hourly sweep
wiring.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

import pytest

from authz_grants import (
    DEFAULT_GRANT_TTL_S,
    GRANTS,
    GrantKey,
    GrantStore,
    canonical_args_hash,
    canonical_args_json,
)


# ---------------------------------------------------------------------------
# canonical_args_json (A:§3.6)
# ---------------------------------------------------------------------------


class TestCanonicalArgsJson:
    def test_keys_are_sorted(self):
        assert canonical_args_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_nested_dict_keys_are_sorted(self):
        assert (
            canonical_args_json({"b": 1, "a": {"z": 1, "y": 2}})
            == '{"a":{"y":2,"z":1},"b":1}'
        )

    def test_separators_are_compact_no_spaces(self):
        out = canonical_args_json({"a": 1, "b": 2})
        assert " " not in out

    def test_unicode_is_not_escaped(self):
        out = canonical_args_json({"name": "héllo wörld", "emoji": "\U0001F389"})
        assert out == '{"emoji":"\U0001F389","name":"héllo wörld"}'
        assert "\\u" not in out

    def test_list_order_is_preserved_not_sorted(self):
        assert canonical_args_json({"a": [3, 1, 2]}) == '{"a":[3,1,2]}'

    def test_list_of_dicts_each_dict_sorted(self):
        out = canonical_args_json({"a": [{"b": 1, "a": 2}]})
        assert out == '{"a":[{"a":2,"b":1}]}'

    def test_exact_string_shared_prefix_inputs_differ(self):
        a = canonical_args_json({"invoice_id": "INV-1"})
        b = canonical_args_json({"invoice_id": "INV-10"})
        assert a != b
        assert a == '{"invoice_id":"INV-1"}'
        assert b == '{"invoice_id":"INV-10"}'

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_raises_valueerror_on_non_finite_float(self, bad):
        with pytest.raises(ValueError):
            canonical_args_json({"amount": bad})

    def test_raises_valueerror_on_unserializable_value(self):
        with pytest.raises(ValueError):
            canonical_args_json({"tags": {1, 2, 3}})

    def test_unserializable_error_message_is_clear(self):
        with pytest.raises(ValueError, match="not JSON-serializable"):
            canonical_args_json({"tags": {1, 2, 3}})


# ---------------------------------------------------------------------------
# canonical_args_hash
# ---------------------------------------------------------------------------


class TestCanonicalArgsHash:
    def test_is_sha256_hexdigest(self):
        h = canonical_args_hash({"a": 1})
        assert len(h) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", h)

    def test_stable_for_same_input(self):
        assert canonical_args_hash({"a": 1, "b": 2}) == canonical_args_hash(
            {"b": 2, "a": 1}
        )

    def test_distinct_for_shared_prefix_inputs(self):
        h1 = canonical_args_hash({"invoice_id": "INV-1"})
        h2 = canonical_args_hash({"invoice_id": "INV-10"})
        assert h1 != h2

    def test_raises_valueerror_on_non_finite_float(self):
        with pytest.raises(ValueError):
            canonical_args_hash({"amount": float("nan")})


# ---------------------------------------------------------------------------
# GrantKey
# ---------------------------------------------------------------------------


def _key(**overrides) -> GrantKey:
    base = dict(
        operator_id=1,
        chat_id=100,
        enforcement_role="finance",
        artifact_id="artifact-abc",
        tool_name="invoice_reset",
        args_hash="deadbeef",
    )
    base.update(overrides)
    return GrantKey(**base)


class TestGrantKey:
    def test_frozen_and_hashable(self):
        k1 = _key()
        k2 = _key()
        assert k1 == k2
        assert hash(k1) == hash(k2)
        assert {k1: "x"}[k2] == "x"

    def test_distinct_field_differs(self):
        assert _key() != _key(tool_name="other_tool")


# ---------------------------------------------------------------------------
# GrantStore
# ---------------------------------------------------------------------------


class _FakeClock:
    """Injectable monotonic-like clock for TTL tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestGrantStoreMintConsume:
    def test_consume_without_mint_is_false(self):
        store = GrantStore(_now=_FakeClock())
        assert store.consume(_key()) is False

    def test_mint_then_consume_is_true_then_false(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key)
        assert store.consume(key) is True
        assert store.consume(key) is False

    def test_default_ttl_matches_module_constant(self):
        assert DEFAULT_GRANT_TTL_S == 300.0

    def test_mint_expires_after_ttl_via_injected_clock(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key, ttl_s=10.0)
        clock.advance(10.0)  # exactly at expiry -> expired
        assert store.consume(key) is False

    def test_mint_not_yet_expired_still_consumable(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key, ttl_s=10.0)
        clock.advance(9.999)
        assert store.consume(key) is True

    def test_mint_uses_default_ttl_when_unspecified(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key)
        clock.advance(DEFAULT_GRANT_TTL_S - 1)
        assert store.consume(key) is True

    def test_mint_replaces_existing_grant_resets_used(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key)
        assert store.consume(key) is True
        assert store.consume(key) is False
        store.mint(key)  # replace -> fresh, unused grant
        assert store.consume(key) is True

    def test_mint_replaces_expired_grant_with_fresh_one(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key, ttl_s=5.0)
        clock.advance(10.0)
        assert store.consume(key) is False  # expired
        store.mint(key, ttl_s=5.0)
        assert store.consume(key) is True


class TestGrantStorePurges:
    def test_purge_chat_removes_only_matching_returns_count(self):
        store = GrantStore(_now=_FakeClock())
        k1 = _key(chat_id=100)
        k2 = _key(chat_id=100, tool_name="other_tool")
        k3 = _key(chat_id=200)
        for k in (k1, k2, k3):
            store.mint(k)
        assert store.purge_chat(100) == 2
        assert store.consume(k1) is False
        assert store.consume(k2) is False
        assert store.consume(k3) is True

    def test_purge_role_removes_only_matching_returns_count(self):
        store = GrantStore(_now=_FakeClock())
        k1 = _key(enforcement_role="finance")
        k2 = _key(enforcement_role="finance", tool_name="other_tool")
        k3 = _key(enforcement_role="ops")
        for k in (k1, k2, k3):
            store.mint(k)
        assert store.purge_role("finance") == 2
        assert store.consume(k1) is False
        assert store.consume(k2) is False
        assert store.consume(k3) is True

    def test_purge_artifact_removes_only_matching_returns_count(self):
        store = GrantStore(_now=_FakeClock())
        k1 = _key(artifact_id="artifact-abc")
        k2 = _key(artifact_id="artifact-abc", tool_name="other_tool")
        k3 = _key(artifact_id="artifact-xyz")
        for k in (k1, k2, k3):
            store.mint(k)
        assert store.purge_artifact("artifact-abc") == 2
        assert store.consume(k1) is False
        assert store.consume(k2) is False
        assert store.consume(k3) is True

    def test_purge_on_empty_store_returns_zero(self):
        store = GrantStore(_now=_FakeClock())
        assert store.purge_chat(1) == 0
        assert store.purge_role("x") == 0
        assert store.purge_artifact("y") == 0


class TestGrantStoreSweep:
    def test_sweep_drops_only_expired(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        expired_key = _key(tool_name="expired_tool")
        live_key = _key(tool_name="live_tool")
        store.mint(expired_key, ttl_s=5.0)
        store.mint(live_key, ttl_s=100.0)
        clock.advance(10.0)  # expired_key is now past TTL, live_key is not
        removed = store.sweep()
        assert removed == 1
        assert store.consume(expired_key) is False
        assert store.consume(live_key) is True

    def test_sweep_returns_zero_when_nothing_expired(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        store.mint(_key(), ttl_s=100.0)
        assert store.sweep() == 0

    def test_sweep_does_not_remove_consumed_but_unexpired_grant(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key, ttl_s=100.0)
        assert store.consume(key) is True
        assert store.sweep() == 0


class TestGrantStoreRealThreadConcurrency:
    def test_exactly_one_thread_consumes(self):
        """r1-B6: N real threads behind a barrier all consume the SAME
        grant concurrently — the store's lock must serialize compare-and-
        mark so exactly one thread sees True."""
        store = GrantStore()  # real clock: default time.monotonic
        key = _key()
        store.mint(key, ttl_s=60.0)

        n = 32
        barrier = threading.Barrier(n)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            r = store.consume(key)
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == n
        assert results.count(True) == 1
        assert results.count(False) == n - 1


def test_grants_singleton_is_a_grant_store():
    assert isinstance(GRANTS, GrantStore)


# ---------------------------------------------------------------------------
# casa_core.py wiring: the hourly _authz_grant_sweep job
# ---------------------------------------------------------------------------


def test_authz_grant_sweep_calls_grants_sweep(monkeypatch):
    import casa_core

    calls: list[str] = []

    class _FakeStore:
        def sweep(self) -> int:
            calls.append("swept")
            return 2

    monkeypatch.setattr(casa_core, "GRANTS", _FakeStore())
    asyncio.run(casa_core._authz_grant_sweep())
    assert calls == ["swept"]


def test_authz_grant_sweep_survives_store_exception(monkeypatch):
    """A sweep failure must not kill the shared scheduler job (same
    contract as every other sweep in this file)."""
    import casa_core

    class _BrokenStore:
        def sweep(self) -> int:
            raise RuntimeError("boom")

    monkeypatch.setattr(casa_core, "GRANTS", _BrokenStore())
    asyncio.run(casa_core._authz_grant_sweep())  # must not raise


def test_authz_grant_sweep_is_a_coroutine_function():
    import casa_core

    assert asyncio.iscoroutinefunction(casa_core._authz_grant_sweep)


_CASA_CORE_SRC = (
    Path(__file__).resolve().parent.parent
    / "casa-agent"
    / "rootfs"
    / "opt"
    / "casa"
    / "casa_core.py"
)


def test_authz_grant_sweep_registered_hourly_beside_engagement_sweep():
    """Static wiring guard, mirroring test_scheduled_sweeper_jobs.py: the
    new job must be registered as an hourly interval job, using the
    coroutine function directly (never a sync lambda wrapper — the
    v0.13.0 regression this file's sibling test guards against)."""
    text = _CASA_CORE_SRC.read_text(encoding="utf-8")

    m = re.search(
        r"scheduler\.add_job\(\s*_authz_grant_sweep,(?P<kwargs>.*?)\)",
        text,
        re.S,
    )
    assert m, (
        "casa_core.py must register _authz_grant_sweep with "
        "scheduler.add_job(_authz_grant_sweep, ...)"
    )
    kwargs = m.group("kwargs")
    assert re.search(r"trigger\s*=\s*\"interval\"", kwargs), (
        "_authz_grant_sweep must be an interval job"
    )
    assert re.search(r"hours\s*=\s*1\b", kwargs), (
        "_authz_grant_sweep must run hourly (hours=1)"
    )
    assert re.search(r'id\s*=\s*"authz_grant_sweep"', kwargs)

    # Registered "beside" _engagement_daily_sweep: both add_job calls
    # exist and _authz_grant_sweep is not defined as a sync lambda.
    assert "_engagement_daily_sweep" in text
    assert not re.search(r"lambda:\s*asyncio\.create_task\(\s*_authz_grant_sweep", text)
