"""Durable specialist voice-job state machine tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from job_registry import (
    DeliveryState,
    ExecutionState,
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
    legacy.write_text(json.dumps([{
        "id": "old-2", "agent": "finance", "started_at": 42.0,
        "origin": {"channel": "telegram", "chat_id": "chat-2"},
    }]), encoding="utf-8")

    registry = JobRegistry(jobs_path, legacy, clock=lambda: 100.0)
    await registry.load()
    assert registry.get("old-2").delivery_sequence == 8
    assert json.loads(legacy.read_text(encoding="utf-8")) == []


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


async def test_restart_retains_delivery_attempt_for_one_full_lease(tmp_path):
    now = [100.0]
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: now[0],
    )
    await registry.load()
    await registry.create(make_job(
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.CLAIMED,
        terminal_at=80.0,
        result="answer",
        delivery_sequence=1,
        delivery_attempt_id="attempt-1",
        lease_until=90.0,
    ))
    await registry.recover_after_restart()
    assert registry.get("job-1").delivery_state is DeliveryState.CLAIMED
    assert registry.get("job-1").lease_until == 115.0
    now[0] = 116.0
    await registry.expire_leases()
    assert registry.get("job-1").delivery_state is DeliveryState.READY
