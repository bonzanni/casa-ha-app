"""Whole-branch review ROUND 2 — F5: ExplanationStore.record/get/prune now run
on worker threads concurrently (agent.py offloads the per-turn write via
asyncio.to_thread). The store must be thread-safe: an instance-level lock
serializes file ops and each write stages through a per-write-unique temp name,
so concurrent writes for the same cid never collide and prune never trips over a
half-published file."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import explanation_store
from explanation_store import ExplanationRecord, ExplanationStore


def _record(cid: str) -> ExplanationRecord:
    return ExplanationRecord(
        correlation_id=cid,
        role_id="concierge",
        kind="resident",
        resolved_model="claude-sonnet",
        persona_ref="gary@1",
        role_checksum="rc-deadbeef",
        binding_digest="bd-cafef00d",
        dependency_digests=("dep-1",),
        effective_config_digest="ecd-1234",
        lifecycle_state="active",
        projection="text",
        static_prompt_digest="spd-5678",
        static_prompt_estimated_tokens=512,
        memory_tiers=("recent",),
        memory_attributions=("attr-1",),
        tool_calls=("send_message",),
        denials=(),
        system_prompt="prose",
        memory_text="mem",
    )


def test_concurrent_records_same_and_distinct_cids_never_raise_and_stay_readable(
    tmp_path: Path,
) -> None:
    store = ExplanationStore(tmp_path / "explanations")
    # 4 distinct cids x 2 concurrent writers each = 8 tasks; the same-cid pairs
    # exercise the per-write-unique temp name (a shared temp path would collide).
    cids = [f"cid-{i}" for i in range(4)]
    tasks = [cid for cid in cids for _ in range(2)]

    errors: list[BaseException] = []

    def _do(cid: str) -> None:
        try:
            store.record(_record(cid))
        except BaseException as exc:  # noqa: BLE001 — surface any thread error to the assert
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_do, tasks))

    assert not errors, errors
    # Every distinct cid resolves to a readable, well-formed record (last writer
    # won cleanly — never a torn/partial file).
    for cid in cids:
        got = store.get(cid)
        assert got["correlation_id"] == cid
    # No orphaned temp files left behind by the atomic os.replace publish.
    assert not list((tmp_path / "explanations").glob("*.tmp"))


def test_concurrent_records_respect_the_max_records_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A small cap keeps this fast (each record() prunes, which globs+sorts every
    # file) while still exercising the concurrent-write-vs-prune path that a
    # shared temp name would corrupt.
    monkeypatch.setattr(explanation_store, "EXPLANATION_MAX_RECORDS", 50)
    clock = {"t": 1000.0}

    def _now() -> float:
        # Monotonically advancing clock so prune's newest-first ordering is
        # deterministic even though writes race across threads.
        clock["t"] += 1.0
        return clock["t"]

    store = ExplanationStore(tmp_path / "explanations", now=_now)
    n = 90

    def _do(i: int) -> None:
        store.record(_record(f"cid-{i:06d}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_do, range(n)))

    files = list((tmp_path / "explanations").glob("*.json"))
    assert len(files) <= 50
    assert not list((tmp_path / "explanations").glob("*.tmp"))
