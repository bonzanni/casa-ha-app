"""#396 — residents get the reminder tools and lose the session-only ones."""

from __future__ import annotations

import pathlib

import pytest
import yaml

pytestmark = pytest.mark.unit

DEFAULTS = pathlib.Path("casa/rootfs/opt/casa/defaults/agents")

RESIDENTS = ("assistant", "butler", "concierge")

# Session-scoped schedulers and the built-in cloud `schedule` skill. None of
# these can produce anything that survives a restart, and two of them appear
# to succeed — which is the whole of #396.
DENIED = {
    "CronCreate", "CronDelete", "CronList", "ToolSearch", "Skill(schedule)",
}


def _runtime(role):
    return yaml.safe_load((DEFAULTS / role / "runtime.yaml").read_text())


def test_assistant_can_set_and_cancel_reminders():
    allowed = set(_runtime("assistant")["tools"]["allowed"])
    assert "mcp__casa-framework__set_reminder" in allowed
    assert "mcp__casa-framework__cancel_reminder" in allowed


@pytest.mark.parametrize("role", RESIDENTS)
def test_no_resident_can_reach_a_session_only_scheduler(role):
    """tools.allowed is an AUTO-APPROVE list, not a deny list — which is how
    CronCreate was reachable without ever being listed. tools.disallowed is
    the lever that actually enforces."""
    disallowed = set(_runtime(role)["tools"].get("disallowed") or [])
    assert DENIED <= disallowed, f"{role} is missing {sorted(DENIED - disallowed)}"


@pytest.mark.parametrize("role", ("butler", "concierge"))
def test_only_the_assistant_gets_the_reminder_tools(role):
    """Least privilege: the butler and concierge have no reason to write
    triggers, so denying them the tools keeps the surface honest."""
    allowed = set(_runtime(role)["tools"]["allowed"])
    assert "mcp__casa-framework__set_reminder" not in allowed
    assert "mcp__casa-framework__cancel_reminder" not in allowed
