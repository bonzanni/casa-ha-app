"""Durable specialist voice-job state machine tests."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from job_registry import (
    DeliveryState,
    ExecutionState,
    JobAuthorizationError,
    JobFailure,
    JobRegistry,
    JobTransitionError,
    VoiceJob,
)


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def make_job(**changes):
    base = VoiceJob(
        id="job-1", parent_job_id=None,
        creating_role="concierge", specialist_role="mtg-judge",
        specialist_display_name="Judge",
        creator_peer="voice_speaker", creator_user_id=None,
        scope_id="scope-1", origin_route_id="entry-1",
        origin_device_id="device-kitchen",
        task="Does this target?", context="",
        created_at=100.0, started_at=None, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.ACCEPTED,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None,
        cancel_pending=False,
    )
    return replace(base, **changes)


def actor_for_job():
    return {
        "creator_peer": "voice_speaker",
        "creator_user_id": None,
        "scope_id": "scope-1",
    }


async def loaded_registry(tmp_path, job=None, *, now=100.0):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: now,
    )
    await registry.load()
    if job is not None:
        await registry.create(job)
    return registry


async def ready_claimed_authorized_registry(tmp_path, *, now=100.0):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            started_at=101.0,
            terminal_at=102.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            result="It targets.",
            delivery_sequence=1,
        ),
        now=now,
    )
    await registry.claim("job-1", "attempt-1")
    await registry.authorize("job-1", "attempt-1")
    return registry


async def authorized_cancel_pending_registry(
    tmp_path, *, lease_until, now,
):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            started_at=80.0,
            terminal_at=85.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.AUTHORIZED,
            result="It targets.",
            delivery_sequence=1,
            delivery_attempt_id="attempt-1",
            lease_until=lease_until,
            cancel_pending=True,
        ),
        now=now,
    )
    return registry


async def playing_registry(tmp_path, *, now=100.0):
    registry = await ready_claimed_authorized_registry(tmp_path, now=now)
    await registry.mark_playing("job-1", "attempt-1")
    return registry


async def test_create_is_atomic_and_survives_reload(tmp_path):
    registry = JobRegistry(tmp_path / "jobs.json", tmp_path / "delegations.json")
    await registry.load()
    await registry.create(make_job())
    reloaded = JobRegistry(tmp_path / "jobs.json", tmp_path / "delegations.json")
    await reloaded.load()
    assert reloaded.get("job-1") == make_job()


async def test_multiple_terminal_jobs_survive_reload_in_delivery_order(tmp_path):
    registry = await loaded_registry(tmp_path)
    await registry.create(make_job(id="job-1", created_at=101.0))
    await registry.create(make_job(id="job-2", created_at=102.0))

    await registry.finish("job-1", "first answer")
    await registry.fail("job-2", JobFailure("specialist_error", "second failed"))

    reloaded = JobRegistry(tmp_path / "jobs.json", tmp_path / "delegations.json")
    await reloaded.load()
    assert [job.id for job in reloaded.all()] == ["job-1", "job-2"]
    assert reloaded.get("job-1").execution_state is ExecutionState.SUCCEEDED
    assert reloaded.get("job-1").result == "first answer"
    assert reloaded.get("job-2").execution_state is ExecutionState.FAILED
    assert reloaded.get("job-2").failure == JobFailure(
        "specialist_error", "second failed",
    )


async def test_finish_voice_result_persists_clarification_contract_and_ttl(
    tmp_path,
):
    registry = await loaded_registry(tmp_path, make_job(), now=200.0)
    envelope = json.dumps({
        "status": "needs_clarification",
        "spoken_summary": "Which card do you mean?",
        "clarification": "Which card do you mean?",
        "delivery_ttl_s": 600,
    })

    finished = await registry.finish_voice_result(
        "job-1", envelope, awaiting_input=True, delivery_ttl_s=600,
    )

    assert finished.execution_state is ExecutionState.SUCCEEDED
    assert finished.delivery_state is DeliveryState.READY
    assert finished.result == envelope
    assert finished.awaiting_input is True
    assert finished.terminal_at == 200.0
    assert finished.expires_at == 800.0
    assert finished.continuable_until == 800.0

    reloaded = JobRegistry(
        tmp_path / "jobs.json", tmp_path / "delegations.json",
        clock=lambda: 200.0,
    )
    await reloaded.load()
    assert reloaded.get("job-1") == finished


async def test_finish_voice_result_answer_has_ttl_without_continuation(tmp_path):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    finished = await registry.finish_voice_result(
        "job-1", '{"status":"answered"}',
        awaiting_input=False, delivery_ttl_s=900,
    )
    assert finished.expires_at == 1200.0
    assert finished.awaiting_input is False
    assert finished.continuable_until is None


async def test_finish_voice_result_cancel_pending_wins(tmp_path):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    result = await registry.request_cancel("job-1", actor=actor_for_job())
    assert result.status == "stopping"

    finished = await registry.finish_voice_result(
        "job-1", '{"status":"answered"}',
        awaiting_input=True, delivery_ttl_s=900,
    )

    assert finished.execution_state is ExecutionState.CANCELLED
    assert finished.result is None
    assert finished.awaiting_input is False
    assert finished.continuable_until is None
    assert finished.failure == JobFailure("cancelled", "Cancelled by creator")


async def test_finish_voice_result_write_failure_is_atomic(tmp_path, monkeypatch):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    before = registry.get("job-1")

    def fail_write(*_args, **_kwargs):
        raise OSError("voice result disk full")

    monkeypatch.setattr("job_registry.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="voice result disk full"):
        await registry.finish_voice_result(
            "job-1", '{"status":"answered"}',
            awaiting_input=False, delivery_ttl_s=900,
        )

    assert registry.get("job-1") == before
    reloaded = JobRegistry(
        tmp_path / "jobs.json", tmp_path / "delegations.json",
        clock=lambda: 300.0,
    )
    await reloaded.load()
    assert reloaded.get("job-1") == before


@pytest.mark.parametrize("ttl", [True, 29, 3601])
async def test_finish_voice_result_rejects_invalid_ttl_without_mutation(
    tmp_path, ttl,
):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    with pytest.raises(ValueError, match="delivery_ttl_s"):
        await registry.finish_voice_result(
            "job-1", "{}", awaiting_input=False, delivery_ttl_s=ttl,
        )
    assert registry.get("job-1") == make_job()


async def test_finish_voice_result_rejects_non_boolean_awaiting_without_mutation(
    tmp_path,
):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    with pytest.raises(ValueError, match="awaiting_input"):
        await registry.finish_voice_result(
            "job-1", "{}", awaiting_input="yes", delivery_ttl_s=900,
        )
    assert registry.get("job-1") == make_job()


async def test_cancel_after_replace_waits_for_memory_publication_under_lock(
    tmp_path, monkeypatch,
):
    import job_registry as job_registry_module

    registry = await loaded_registry(tmp_path)
    replaced = threading.Event()
    release_writer = threading.Event()
    real_write = job_registry_module.atomic_write_json

    def blocked_after_replace(*args, **kwargs):
        real_write(*args, **kwargs)
        replaced.set()
        assert release_writer.wait(timeout=5)

    monkeypatch.setattr(job_registry_module, "atomic_write_json", blocked_after_replace)
    mutation = asyncio.create_task(registry.create(make_job()))
    assert await asyncio.to_thread(replaced.wait, 5)
    mutation.cancel()

    observed = {}
    entered = asyncio.Event()

    async def observe_next_writer_view():
        async with registry._lock:
            observed["memory"] = registry.get("job-1") is not None
            observed["disk"] = [
                row["id"]
                for row in json.loads(
                    (tmp_path / "jobs.json").read_text(encoding="utf-8")
                )
            ]
            entered.set()

    observer = asyncio.create_task(observe_next_writer_view())
    try:
        await asyncio.sleep(0)
        assert not entered.is_set(), "lock released after disk replace before publication"
    finally:
        release_writer.set()

    with pytest.raises(asyncio.CancelledError):
        await mutation
    await observer
    assert observed == {"memory": True, "disk": ["job-1"]}


async def test_invalid_compare_and_set_does_not_mutate(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    with pytest.raises(JobTransitionError):
        await registry.mark_playing("job-1", "attempt-1")
    assert registry.get("job-1").delivery_state is DeliveryState.NONE


async def test_authorized_cancel_waits_for_preplay_outcome(tmp_path):
    registry = await ready_claimed_authorized_registry(tmp_path)
    result = await registry.request_cancel("job-1", actor=actor_for_job())
    assert result.status == "stopping"
    assert registry.get("job-1").cancel_pending is True
    await registry.nack("job-1", "attempt-1", "preempted_before_playback")
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.CANCELLED
    assert job.cancel_pending is False


async def test_anonymous_creator_identity_is_exact_not_a_wildcard(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    with pytest.raises(JobAuthorizationError):
        await registry.request_cancel("job-1", actor={
            "creator_peer": "voice_speaker",
            "creator_user_id": "different-user",
            "scope_id": "scope-1",
        })
    assert registry.get("job-1").cancel_pending is False


async def test_cancel_during_persist_still_signals_and_reaps_owned_task(
    tmp_path, monkeypatch,
):
    import job_registry as job_registry_module

    registry = await loaded_registry(tmp_path, make_job())
    registry.CANCEL_GRACE_SECONDS = 0.01
    worker_cancelled = asyncio.Event()

    class PermitProbe:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    async def work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            worker_cancelled.set()
            raise

    permit = PermitProbe()
    worker = asyncio.create_task(work())
    cancel_event = await registry.bind_task("job-1", worker, permit=permit)

    replaced = threading.Event()
    release_writer = threading.Event()
    real_write = job_registry_module.atomic_write_json

    def blocked_after_replace(*args, **kwargs):
        real_write(*args, **kwargs)
        replaced.set()
        assert release_writer.wait(timeout=5)

    monkeypatch.setattr(job_registry_module, "atomic_write_json", blocked_after_replace)
    request = asyncio.create_task(
        registry.request_cancel("job-1", actor=actor_for_job()),
    )
    try:
        assert await asyncio.to_thread(replaced.wait, 5)
        request.cancel()
        release_writer.set()

        with pytest.raises(asyncio.CancelledError):
            await request
        assert registry.get("job-1").cancel_pending is True
        assert cancel_event.is_set()

        reloaded = JobRegistry(
            tmp_path / "jobs.json", tmp_path / "delegations.json",
        )
        await reloaded.load()
        assert reloaded.get("job-1").cancel_pending is True

        await asyncio.wait_for(worker_cancelled.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await worker
        await asyncio.sleep(0)
        assert permit.releases == 1
    finally:
        release_writer.set()
        if not request.done():
            request.cancel()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(request, worker, return_exceptions=True)


async def test_cancel_pending_authorized_job_rejects_playback_start(tmp_path):
    registry = await ready_claimed_authorized_registry(tmp_path)
    await registry.request_cancel("job-1", actor=actor_for_job())
    with pytest.raises(JobTransitionError):
        await registry.mark_playing("job-1", "attempt-1")
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.AUTHORIZED
    assert job.cancel_pending is True


async def test_cancel_pending_authorized_lease_lapse_cancels_not_requeues(tmp_path):
    registry = await authorized_cancel_pending_registry(
        tmp_path, lease_until=90.0, now=100.0,
    )
    await registry.expire_leases()
    assert registry.get("job-1").delivery_state is DeliveryState.CANCELLED


async def test_playing_cancel_is_too_late(tmp_path):
    registry = await playing_registry(tmp_path)
    result = await registry.request_cancel("job-1", actor=actor_for_job())
    assert result.status == "too_late"
    assert registry.get("job-1").delivery_state is DeliveryState.PLAYING


async def test_delivery_compare_and_set_happy_path(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            terminal_at=100.0,
            result="answer",
            delivery_sequence=1,
        ),
    )
    await registry.claim("job-1", "attempt-1")
    assert registry.get("job-1").lease_until == 115.0
    await registry.authorize("job-1", "attempt-1")
    await registry.mark_playing("job-1", "attempt-1")
    await registry.mark_delivered("job-1", "attempt-1")
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.DELIVERED
    assert job.delivery_attempt_id is None
    assert job.lease_until is None


async def test_nack_without_cancel_requeues_with_fresh_attempt_required(tmp_path):
    registry = await ready_claimed_authorized_registry(tmp_path)
    await registry.nack("job-1", "attempt-1", "preempted_before_playback")
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.READY
    assert job.delivery_attempt_id is None
    assert job.lease_until is None


async def test_playing_lease_lapse_requeues_for_at_least_once_delivery(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.PLAYING,
            terminal_at=80.0,
            result="answer",
            delivery_sequence=1,
            delivery_attempt_id="attempt-1",
            lease_until=90.0,
        ),
        now=100.0,
    )
    await registry.expire_leases()
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.READY
    assert job.delivery_attempt_id is None


async def test_failed_snapshot_write_does_not_publish_memory_mutation(
    tmp_path, monkeypatch,
):
    import job_registry as job_registry_module

    registry = await loaded_registry(tmp_path, make_job())
    real_write = job_registry_module.atomic_write_json

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("job_registry.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        await registry.finish("job-1", "answer")
    assert registry.get("job-1") == make_job()

    monkeypatch.setattr("job_registry.atomic_write_json", real_write)
    finished = await registry.finish("job-1", "answer")
    assert finished.delivery_sequence == 1


async def test_bind_task_releases_permit_only_when_task_really_ends(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    gate = asyncio.Event()

    class PermitProbe:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    async def work():
        await gate.wait()

    permit = PermitProbe()
    task = asyncio.create_task(work())
    await registry.bind_task("job-1", task, permit=permit)
    assert registry.get("job-1").execution_state is ExecutionState.RUNNING
    assert permit.releases == 0
    gate.set()
    await task
    await asyncio.sleep(0)
    assert permit.releases == 1


async def test_expire_due_applies_result_delivery_ttl(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            terminal_at=80.0,
            expires_at=90.0,
            result="answer",
            delivery_sequence=1,
        ),
        now=100.0,
    )
    await registry.expire_due()
    assert registry.get("job-1").delivery_state is DeliveryState.EXPIRED


async def test_load_migrates_legacy_tombstone_once(tmp_path):
    legacy = tmp_path / "delegations.json"
    legacy.write_text(json.dumps([{
        "id": "old-1",
        "agent": "finance",
        "started_at": 42.0,
        "origin": {
            "role": "assistant",
            "channel": "telegram",
            "chat_id": "chat-7",
            "cid": "route-9",
        },
    }]), encoding="utf-8")

    registry = JobRegistry(tmp_path / "jobs.json", legacy, clock=lambda: 100.0)
    await registry.load()
    job = registry.get("old-1")
    assert job.execution_state is ExecutionState.ORPHANED
    assert job.failure == JobFailure(kind="restart_orphan", message="Lost on restart")
    assert job.creating_role == "assistant"
    assert job.specialist_role == "finance"
    assert job.scope_id == "chat-7"
    assert job.origin_route_id == "route-9"
    assert job.orphan_notification_pending is True
    assert json.loads(legacy.read_text(encoding="utf-8")) == []

    reloaded = JobRegistry(tmp_path / "jobs.json", legacy, clock=lambda: 101.0)
    await reloaded.load()
    assert [job.id for job in reloaded.all()] == ["old-1"]


async def test_legacy_migration_appends_after_existing_delivery_sequence(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    legacy = tmp_path / "delegations.json"
    seeded = JobRegistry(jobs_path, legacy, clock=lambda: 90.0)
    await seeded.load()
    await seeded.create(make_job(
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=80.0,
        result="answer",
        delivery_sequence=7,
    ))
    legacy.write_text(json.dumps([
        {
            "id": "old-2", "agent": "finance", "started_at": 42.0,
            "origin": {"channel": "telegram", "chat_id": "chat-2"},
        },
        {
            "id": "old-3", "agent": "weather", "started_at": 43.0,
            "origin": {"channel": "telegram", "chat_id": "chat-3"},
        },
    ]), encoding="utf-8")

    registry = JobRegistry(jobs_path, legacy, clock=lambda: 100.0)
    await registry.load()
    assert registry.get("old-2").delivery_sequence == 8
    assert registry.get("old-3").delivery_sequence == 9
    assert registry.get("old-2").orphan_notification_pending is True
    assert registry.get("old-3").orphan_notification_pending is True
    assert [job.id for job in registry.all()] == ["job-1", "old-2", "old-3"]
    assert json.loads(legacy.read_text(encoding="utf-8")) == []


async def test_cancel_during_migration_waits_for_publish_and_consume_under_lock(
    tmp_path, monkeypatch,
):
    import job_registry as job_registry_module

    jobs_path = tmp_path / "jobs.json"
    legacy = tmp_path / "delegations.json"
    legacy.write_text(json.dumps([{
        "id": "old-1", "agent": "finance", "started_at": 42.0,
        "origin": {"channel": "telegram", "chat_id": "chat-1"},
    }]), encoding="utf-8")
    replaced = threading.Event()
    release_writer = threading.Event()
    real_write = job_registry_module.atomic_write_json

    def blocked_after_jobs_replace(path, *args, **kwargs):
        real_write(path, *args, **kwargs)
        if str(path) == str(jobs_path):
            replaced.set()
            assert release_writer.wait(timeout=5)

    monkeypatch.setattr(job_registry_module, "atomic_write_json", blocked_after_jobs_replace)
    registry = JobRegistry(jobs_path, legacy, clock=lambda: 100.0)
    loading = asyncio.create_task(registry.load())
    assert await asyncio.to_thread(replaced.wait, 5)
    loading.cancel()

    observed = {}
    entered = asyncio.Event()

    async def observe_next_writer_view():
        async with registry._lock:
            observed["memory"] = registry.get("old-1") is not None
            observed["disk"] = [
                row["id"]
                for row in json.loads(jobs_path.read_text(encoding="utf-8"))
            ]
            observed["legacy"] = json.loads(legacy.read_text(encoding="utf-8"))
            entered.set()

    observer = asyncio.create_task(observe_next_writer_view())
    try:
        await asyncio.sleep(0)
        assert not entered.is_set(), "migration lock released before publication"
    finally:
        release_writer.set()

    with pytest.raises(asyncio.CancelledError):
        await loading
    await observer
    assert observed == {"memory": True, "disk": ["old-1"], "legacy": []}


async def test_empty_consumed_legacy_tombstone_is_not_rewritten(tmp_path, monkeypatch):
    import job_registry as job_registry_module

    jobs_path = tmp_path / "jobs.json"
    legacy = tmp_path / "delegations.json"
    legacy.write_text("[]\n", encoding="utf-8")
    seeded = JobRegistry(jobs_path, legacy)
    await seeded.load()

    calls = []
    real_write = job_registry_module.atomic_write_json

    def record_write(path, *args, **kwargs):
        calls.append(str(path))
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(job_registry_module, "atomic_write_json", record_write)
    reloaded = JobRegistry(jobs_path, legacy)
    await reloaded.load()
    assert str(legacy) not in calls


@pytest.mark.parametrize("legacy_payload", ["{not json", '{"not": "a list"}'])
async def test_corrupt_legacy_tombstone_is_logged_and_truncated(
    tmp_path, caplog, legacy_payload,
):
    import logging

    legacy = tmp_path / "delegations.json"
    legacy.write_text(legacy_payload, encoding="utf-8")
    registry = JobRegistry(tmp_path / "jobs.json", legacy)
    with caplog.at_level(logging.ERROR):
        await registry.load()
    assert registry.all() == []
    assert json.loads(legacy.read_text(encoding="utf-8")) == []
    assert any("legacy delegation tombstone" in row.message.lower()
               for row in caplog.records)


async def test_atomic_replace_failure_preserves_prior_snapshot_and_memory(
    tmp_path, monkeypatch,
):
    import atomic_io

    jobs_path = tmp_path / "jobs.json"
    registry = JobRegistry(jobs_path, tmp_path / "delegations.json")
    await registry.load()
    await registry.create(make_job())
    prior = json.loads(jobs_path.read_text(encoding="utf-8"))

    def fail_replace(*_args, **_kwargs):
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await registry.create(replace(make_job(), id="job-2"))
    assert json.loads(jobs_path.read_text(encoding="utf-8")) == prior
    assert registry.get("job-2") is None
    assert sorted(path.name for path in tmp_path.iterdir()) == ["jobs.json"]


async def test_restart_orphans_running_job_and_queues_voice_failure(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            started_at=101.0,
            execution_state=ExecutionState.RUNNING,
        ),
        now=120.0,
    )
    recovered = await registry.recover_after_restart()
    job = registry.get("job-1")
    assert recovered == [job]
    assert job.execution_state is ExecutionState.ORPHANED
    assert job.delivery_state is DeliveryState.READY
    assert job.failure.kind == "restart_orphan"
    assert job.delivery_sequence == 1
    assert job.orphan_notification_pending is False


async def test_restart_orphans_accepted_job_left_before_task_binding(tmp_path):
    registry = await loaded_registry(tmp_path, make_job(), now=120.0)
    recovered = await registry.recover_after_restart()
    job = registry.get("job-1")
    assert recovered == [job]
    assert job.execution_state is ExecutionState.ORPHANED
    assert job.delivery_state is DeliveryState.READY
    assert job.failure == JobFailure("restart_orphan", "Lost on restart")


async def test_continuation_create_atomically_consumes_parent(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=200.0,
        ),
        now=120.0,
    )
    child = replace(
        make_job(),
        id="job-child",
        parent_job_id="job-1",
        created_at=120.0,
    )

    assert await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    ) == child
    parent = registry.get("job-1")
    assert parent.awaiting_input is False
    assert parent.continuable_until == 200.0
    assert registry.get("job-child") == child

    with pytest.raises(JobTransitionError):
        await registry.create_continuation(
            "job-1",
            replace(child, id="job-duplicate"),
            actor=actor_for_job(),
        )
    assert registry.get("job-duplicate") is None


async def test_compensate_unbound_continuation_restores_fresh_parent(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=200.0,
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
    )
    await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )

    restored = await registry.compensate_unbound_continuation(
        "job-1", "job-child", actor=actor_for_job(),
    )

    assert restored is True
    assert registry.get("job-child") is None
    parent = registry.get("job-1")
    assert parent.awaiting_input is True
    assert parent.continuable_until == 200.0


async def test_compensate_unbound_continuation_does_not_restore_expired_parent(
    tmp_path,
):
    now = [120.0]
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: now[0],
    )
    await registry.load()
    await registry.create(make_job(
        terminal_at=100.0,
        expires_at=121.0,
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        result='{"status":"needs_clarification"}',
        awaiting_input=True,
        continuable_until=121.0,
    ))
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
    )
    await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )
    now[0] = 122.0

    restored = await registry.compensate_unbound_continuation(
        "job-1", "job-child", actor=actor_for_job(),
    )

    assert restored is False
    assert registry.get("job-child") is None
    parent = registry.get("job-1")
    assert parent.awaiting_input is False
    assert parent.continuable_until == 121.0


async def test_compensation_never_rewinds_a_bound_running_child(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=200.0,
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
    )
    await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )
    release = asyncio.Event()

    async def work():
        await release.wait()

    worker = asyncio.create_task(work())
    await registry.bind_task("job-child", worker)
    try:
        restored = await registry.compensate_unbound_continuation(
            "job-1", "job-child", actor=actor_for_job(),
        )
        assert restored is False
        assert registry.get("job-child").execution_state is ExecutionState.RUNNING
        assert registry.get("job-1").awaiting_input is False
        assert registry.owns_task("job-child", worker) is True
    finally:
        release.set()
        await worker


async def test_continuation_create_rejects_parent_that_expired_before_commit(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=119.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=119.0,
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
    )

    with pytest.raises(JobTransitionError):
        await registry.create_continuation(
            "job-1", child, actor=actor_for_job(),
        )
    assert registry.get("job-child") is None


async def test_terminal_waiter_observes_durable_terminal_transition(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    waiter = asyncio.create_task(registry.wait_for_terminal("job-1"))

    await registry.fail("job-1", JobFailure("safe", "safe failure"))

    terminal = await asyncio.wait_for(waiter, timeout=1)
    assert terminal.execution_state is ExecutionState.FAILED


async def test_failure_reconciliation_eventually_terminalizes_live_job(
    tmp_path, caplog,
):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: 120.0,
        reconciliation_retry_interval=0.01,
    )
    await registry.load()
    await registry.create(make_job(
        started_at=110.0,
        execution_state=ExecutionState.RUNNING,
    ))
    real_fail = registry.fail_compat
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("PRIVATE_RECONCILE_CANARY")
        return await real_fail(*args, **kwargs)

    registry.fail_compat = fail_once
    registry.schedule_failure_reconciliation("job-1")

    terminal = await asyncio.wait_for(
        registry.wait_for_terminal("job-1"), timeout=1,
    )
    await asyncio.wait_for(
        registry.wait_for_reconciliation("job-1"), timeout=1,
    )
    assert attempts == 2
    assert terminal.execution_state is ExecutionState.FAILED
    assert terminal.failure == JobFailure(
        "persistence_failed", "Specialist result could not be saved.",
    )
    assert registry.reconciliation_count == 0
    assert "PRIVATE_RECONCILE_CANARY" not in caplog.text


async def test_close_cancels_and_drains_sleeping_reconciliation(tmp_path):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        reconciliation_retry_interval=3600.0,
    )
    await registry.load()
    await registry.create(make_job(
        started_at=110.0,
        execution_state=ExecutionState.RUNNING,
    ))
    registry.schedule_failure_reconciliation("job-1")
    assert registry.reconciliation_count == 1

    await asyncio.wait_for(registry.close(), timeout=1)

    assert registry.reconciliation_count == 0
    assert registry.get("job-1").execution_state is ExecutionState.RUNNING


async def test_persistent_reconciliation_failure_stays_restart_recoverable(
    tmp_path, caplog,
):
    attempted = asyncio.Event()
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: 120.0,
        reconciliation_retry_interval=0.01,
    )
    await registry.load()
    await registry.create(make_job(
        started_at=110.0,
        execution_state=ExecutionState.RUNNING,
    ))

    async def fail_forever(*_args, **_kwargs):
        attempted.set()
        raise OSError("PRIVATE_RECONCILE_CANARY")

    registry.fail_compat = fail_forever
    registry.schedule_failure_reconciliation("job-1")
    await asyncio.wait_for(attempted.wait(), timeout=1)
    await registry.close()
    assert registry.reconciliation_count == 0
    assert registry.get("job-1").execution_state is ExecutionState.RUNNING
    assert "PRIVATE_RECONCILE_CANARY" not in caplog.text

    restarted = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: 121.0,
    )
    await restarted.load()
    recovered = await restarted.recover_after_restart()
    assert [job.id for job in recovered] == ["job-1"]
    assert restarted.get("job-1").execution_state is ExecutionState.ORPHANED


async def test_telegram_orphan_notification_retries_until_durable_ack(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    legacy = tmp_path / "delegations.json"
    first = JobRegistry(jobs_path, legacy, clock=lambda: 120.0)
    await first.load()
    await first.create(make_job(
        creator_peer="telegram",
        scope_id="chat-1",
        origin_route_id="route-1",
        origin_device_id=None,
        started_at=101.0,
        execution_state=ExecutionState.RUNNING,
    ))

    boot_one = await first.recover_after_restart()
    assert [job.id for job in boot_one] == ["job-1"]
    assert first.get("job-1").orphan_notification_pending is True

    second = JobRegistry(jobs_path, legacy, clock=lambda: 121.0)
    await second.load()
    boot_two = await second.recover_after_restart()
    assert [job.id for job in boot_two] == ["job-1"]
    await second.ack_orphan_notification("job-1")
    assert second.get("job-1").orphan_notification_pending is False

    third = JobRegistry(jobs_path, legacy, clock=lambda: 122.0)
    await third.load()
    assert await third.recover_after_restart() == []


async def test_snapshot_without_orphan_ack_field_decodes_as_not_pending(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    jobs_path = tmp_path / "jobs.json"
    snapshot = json.loads(jobs_path.read_text(encoding="utf-8"))
    snapshot[0].pop("orphan_notification_pending", None)
    jobs_path.write_text(json.dumps(snapshot), encoding="utf-8")

    reloaded = JobRegistry(jobs_path, tmp_path / "delegations.json")
    await reloaded.load()
    assert reloaded.get("job-1").orphan_notification_pending is False


@pytest.mark.parametrize("failure_phase", ["notify", "ack"])
async def test_recovered_orphan_failure_isolated_before_later_success(
    tmp_path, caplog, failure_phase,
):
    import logging

    from casa_core import _notify_recovered_delegations

    registry = await loaded_registry(tmp_path)
    failed_job = make_job(
        id="job-fail",
        creator_peer="telegram",
        scope_id="chat-1",
        origin_device_id=None,
        execution_state=ExecutionState.ORPHANED,
        failure=JobFailure("restart_orphan", "Lost on restart"),
        orphan_notification_pending=True,
        delivery_sequence=1,
    )
    next_job = replace(
        failed_job, id="job-next", scope_id="chat-2", delivery_sequence=2,
    )
    await registry.create(failed_job)
    await registry.create(next_job)
    events = []
    secret = "SECRET-notification-detail"

    class RegistryProbe:
        async def ack_orphan_notification(self, job_id):
            events.append(("ack", job_id))
            if failure_phase == "ack" and job_id == "job-fail":
                raise RuntimeError(secret)
            await registry.ack_orphan_notification(job_id)

    class BusProbe:
        queues = {"concierge": object()}

        async def notify(self, message):
            assert message.content.text == ""
            events.append(("notify", message.content.delegation_id))
            if (failure_phase == "notify"
                    and message.content.delegation_id == "job-fail"):
                raise RuntimeError(secret)

    with caplog.at_level(logging.ERROR, logger="casa_core"):
        await _notify_recovered_delegations(
            registry.all(), RegistryProbe(), BusProbe(),
            assistant_role="concierge",
        )
    expected = [("notify", "job-fail")]
    if failure_phase == "ack":
        expected.append(("ack", "job-fail"))
    expected.extend([("notify", "job-next"), ("ack", "job-next")])
    assert events == expected
    assert registry.get("job-fail").orphan_notification_pending is True
    assert registry.get("job-next").orphan_notification_pending is False
    assert secret not in caplog.text

    reloaded = JobRegistry(
        tmp_path / "jobs.json", tmp_path / "delegations.json",
    )
    await reloaded.load()
    assert reloaded.get("job-fail").orphan_notification_pending is True
    assert reloaded.get("job-next").orphan_notification_pending is False


async def test_recovered_orphan_notification_does_not_swallow_cancellation():
    from casa_core import _notify_recovered_delegations

    job = make_job(
        creator_peer="telegram",
        execution_state=ExecutionState.ORPHANED,
        failure=JobFailure("restart_orphan", "Lost on restart"),
        orphan_notification_pending=True,
    )

    class RegistryProbe:
        async def ack_orphan_notification(self, _job_id):
            raise AssertionError("ack must not run")

    class CancelledBus:
        queues = {"concierge": object()}

        async def notify(self, _message):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _notify_recovered_delegations(
            [job], RegistryProbe(), CancelledBus(), assistant_role="concierge",
        )


@pytest.mark.parametrize(
    "delivery_state",
    [DeliveryState.CLAIMED, DeliveryState.AUTHORIZED, DeliveryState.PLAYING],
)
async def test_restart_retains_delivery_attempt_for_one_full_lease(
    tmp_path, delivery_state,
):
    now = [100.0]
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: now[0],
    )
    await registry.load()
    await registry.create(make_job(
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=delivery_state,
        terminal_at=80.0,
        result="answer",
        delivery_sequence=1,
        delivery_attempt_id="attempt-1",
        lease_until=90.0,
    ))
    await registry.recover_after_restart()
    assert registry.get("job-1").delivery_state is delivery_state
    assert registry.get("job-1").lease_until == 115.0
    now[0] = 116.0
    await registry.expire_leases()
    assert registry.get("job-1").delivery_state is DeliveryState.READY
