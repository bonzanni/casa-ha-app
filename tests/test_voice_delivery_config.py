"""Operator configuration for proactive voice-job delivery."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest
import yaml

from job_registry import DeliveryState, ExecutionState, JobRegistry, VoiceJob
import tools


pytestmark = pytest.mark.unit

_ENV_KEYS = (
    "VOICE_ROUTE_FRESHNESS_SECONDS",
    "VOICE_JOB_DELIVERY_TTL_SECONDS",
    "VOICE_JOB_ROUTE_CAP",
)


def _load_config_module():
    try:
        return importlib.import_module("voice_delivery_config")
    except ModuleNotFoundError:
        pytest.fail("voice_delivery_config module is absent")


def _clear_config(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_voice_delivery_config_defaults(monkeypatch):
    _clear_config(monkeypatch)

    config = _load_config_module().load_voice_delivery_config()

    assert config.route_freshness_s == 60
    assert config.delivery_ttl_s == 900
    assert config.route_cap == 5


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (("-1", "29", "0"), (0, 30, 1)),
        (("301", "3601", "21"), (300, 3600, 20)),
        (("0", "30", "1"), (0, 30, 1)),
        (("300", "3600", "20"), (300, 3600, 20)),
    ],
)
def test_voice_delivery_config_clamps_to_operator_rails(
    monkeypatch, values, expected,
):
    for key, value in zip(_ENV_KEYS, values, strict=True):
        monkeypatch.setenv(key, value)

    config = _load_config_module().load_voice_delivery_config()

    assert (
        config.route_freshness_s,
        config.delivery_ttl_s,
        config.route_cap,
    ) == expected


def test_voice_delivery_config_non_numeric_values_use_defaults(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.setenv(key, "not-a-number")

    config = _load_config_module().load_voice_delivery_config()

    assert (
        config.route_freshness_s,
        config.delivery_ttl_s,
        config.route_cap,
    ) == (60, 900, 5)


def test_voice_delivery_config_emits_one_static_sanitized_boot_event(
    monkeypatch, caplog,
):
    _clear_config(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SECRET", "voice-secret-canary")
    monkeypatch.setenv("VOICE_ROUTE_ID", "route-id-canary")
    monkeypatch.setenv("VOICE_PROMPT", "prompt-canary")
    monkeypatch.setenv("VOICE_RESULT", "result-canary")
    caplog.set_level(logging.INFO, logger="voice_delivery_config")

    _load_config_module().load_voice_delivery_config()

    events = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("voice_delivery_config ")
    ]
    assert events == [
        "voice_delivery_config route_freshness_s=60 ttl_s=900 route_cap=5",
    ]
    rendered = "\n".join(events)
    for canary in (
        "voice-secret-canary",
        "route-id-canary",
        "prompt-canary",
        "result-canary",
    ):
        assert canary not in rendered


def test_boot_wires_one_config_snapshot_into_all_voice_delivery_consumers():
    source = (
        Path(__file__).resolve().parent.parent
        / "casa-agent" / "rootfs" / "opt" / "casa" / "casa_core.py"
    ).read_text(encoding="utf-8")

    assert "voice_delivery_config = load_voice_delivery_config()" in source
    assert "result_ttl_seconds=voice_delivery_config.delivery_ttl_s" in source
    assert "freshness_s=voice_delivery_config.route_freshness_s" in source
    assert "voice_job_route_cap=voice_delivery_config.route_cap" in source


def test_addon_declares_exact_voice_delivery_options_and_schema():
    config_path = (
        Path(__file__).resolve().parent.parent / "casa-agent" / "config.yaml"
    )
    addon = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert {
        "voice_route_freshness_seconds": 60,
        "voice_job_delivery_ttl_seconds": 900,
        "voice_job_route_cap": 5,
    }.items() <= addon["options"].items()
    assert {
        "voice_route_freshness_seconds": "int(0,300)?",
        "voice_job_delivery_ttl_seconds": "int(30,3600)?",
        "voice_job_route_cap": "int(1,20)?",
    }.items() <= addon["schema"].items()


def test_voice_delivery_options_have_english_translations():
    translations_path = (
        Path(__file__).resolve().parent.parent
        / "casa-agent" / "translations" / "en.yaml"
    )
    translations = yaml.safe_load(translations_path.read_text(encoding="utf-8"))

    for option in (
        "voice_route_freshness_seconds",
        "voice_job_delivery_ttl_seconds",
        "voice_job_route_cap",
    ):
        entry = translations["configuration"][option]
        assert entry["name"]
        assert entry["description"]


def _accepted_job() -> VoiceJob:
    return VoiceJob(
        id="job-1",
        parent_job_id=None,
        creating_role="concierge",
        specialist_role="judge",
        specialist_display_name="Judge",
        creator_peer="voice_speaker",
        creator_user_id=None,
        scope_id="scope-1",
        origin_route_id="route-1",
        origin_device_id="device-1",
        task="Question",
        context="",
        created_at=100.0,
        started_at=None,
        terminal_at=None,
        expires_at=None,
        execution_state=ExecutionState.ACCEPTED,
        delivery_state=DeliveryState.NONE,
        result=None,
        failure=None,
        awaiting_input=False,
        continuable_until=None,
        delivery_sequence=0,
        delivery_attempt_id=None,
        lease_until=None,
        cancel_pending=False,
    )


@pytest.mark.asyncio
async def test_configured_result_ttl_reaches_durable_job_expiry(tmp_path):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: 100.0,
        result_ttl_seconds=900,
    )
    await registry.load()
    await registry.create(_accepted_job())

    completed = await registry.finish("job-1", "Done")

    assert completed.expires_at == 1000.0


@pytest.mark.asyncio
async def test_configured_result_ttl_caps_specialist_requested_delivery_ttl(
    tmp_path,
):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: 100.0,
        result_ttl_seconds=300,
    )
    await registry.load()
    await registry.create(_accepted_job())

    completed = await registry.finish_voice_result(
        "job-1", "Done", awaiting_input=False, delivery_ttl_s=900,
    )

    assert completed.expires_at == 400.0


@pytest.mark.asyncio
async def test_configured_result_ttl_preserves_shorter_specialist_expiry(tmp_path):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        tmp_path / "delegations.json",
        clock=lambda: 100.0,
        result_ttl_seconds=300,
    )
    await registry.load()
    await registry.create(_accepted_job())

    completed = await registry.finish_voice_result(
        "job-1", "Done", awaiting_input=False, delivery_ttl_s=60,
    )

    assert completed.expires_at == 160.0


def test_configured_route_cap_reaches_every_atomic_creation_path():
    source = Path(tools.__file__).read_text(encoding="utf-8")

    assert "_MAX_ACTIVE_READY_JOBS_PER_ROUTE" not in source
    assert source.count("max_active_ready_per_route=_voice_job_route_cap") == 3


def test_tools_accept_one_validated_route_cap_snapshot(monkeypatch):
    monkeypatch.setattr(tools, "_voice_job_route_cap", tools._voice_job_route_cap)
    tools.init_tools(None, None, None, voice_job_route_cap=7)

    assert tools._voice_job_route_cap == 7
