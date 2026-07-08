"""Tests for casa_config_guard hook policy (Plan 3)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestCasaConfigGuard:
    async def test_blocks_write_to_data(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=["/data", "/addon_configs/casa-agent/schema"],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": "/data/x.json"}},
            None, {},
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_blocks_write_to_schema(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=["/addon_configs/casa-agent/schema"],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "/addon_configs/casa-agent/schema/x.json"}},
            None, {},
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_allows_regular_write(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=["/data"],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": "/addon_configs/casa-agent/agents/specialists/x/character.yaml"}},
            None, {},
        )
        assert out == {}

    async def test_blocks_bash_rm_resident(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=[],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /config/agents/butler"}},
            None, {},
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_allows_bash_rm_specialist(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=[],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /config/agents/specialists/fitness"}},
            None, {},
        )
        assert out == {}

    async def test_registered_in_hook_policies(self):
        from hooks import HOOK_POLICIES
        assert "casa_config_guard" in HOOK_POLICIES

    async def test_blocks_write_with_double_leading_slash(self):
        """v0.50.0 security-review must-fix: '//data/...' is '/data/...' to
        the Linux kernel, so the forbid_write prefix check must deny it."""
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=["/data"],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Write",
             "tool_input": {"file_path": "//data/options.json"}},
            None, {},
        )
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# M16: the resident-deletion guard must be argv-aware, not a brittle regex.
# Every case below bypassed the old _RESIDENT_RM_RE (quoted paths, long
# flags, '--', wrapper shells, '..' traversal) while block_dangerous_bash
# stayed silent (only -r present, no -f), so the resident was deleted with
# no ask-the-user gate.
@pytest.mark.parametrize("cmd", [
    'rm -r "/config/agents/ellen"',        # quoted path
    "rm -r '/config/agents/ellen'",        # single-quoted path
    "rm --recursive /config/agents/ellen", # long flag
    "rm -r -- /config/agents/ellen",       # end-of-options marker
    "rm -rf /config/agents/butler",        # plain (old regex happy path)
    'bash -c "rm -r /config/agents/ellen"',# wrapper shell
    "rm -r /config/agents/x/../ellen",     # dot-dot traversal
    "rm -rf /config/agents",               # whole residents dir
    # v0.50.0 security-review must-fix: the kernel collapses '//' to '/',
    # so these double-slash spellings delete the same resident.
    "rm -r //config/agents/ellen",         # double leading slash
    "rm -r //config/agents//ellen",        # doubled interior slash too
    "rm -r ///config/agents/ellen",        # 3+ slashes
    'eval "rm -r /config/agents/ellen"',   # eval wrapper (same class as bash -c)
    # v0.50.0 round 2: '|&' was not a pipeline separator (stages merged, rm
    # never became argv[0]); exec-wrapper prefixes (nohup, timeout, env,
    # sudo, ...) hid rm behind a benign argv[0].
    "echo x |& rm -r /config/agents/ellen",
    "nohup rm -r /config/agents/ellen",
    "timeout 5 rm -r /config/agents/ellen",
    "env A=B rm -r /config/agents/ellen",
    "sudo rm -r /config/agents/ellen",
    "nice -n 5 rm -r /config/agents/ellen",
    "setsid rm -rf /config/agents",
    'nohup bash -c "rm -r /config/agents/ellen"',  # wrapper around wrapper-shell
])
async def test_blocks_resident_rm_variants(cmd):
    from hooks import make_casa_config_guard_hook
    hook = make_casa_config_guard_hook(
        forbid_write_paths=[], forbid_delete_residents=True,
    )
    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", cmd


@pytest.mark.parametrize("cmd", [
    'rm -rf "/config/agents/specialists/fitness"',
    "rm -r /config/agents/executors/configurator/tmp.txt",
    "rm -rf /tmp/scratch",
    # Slash collapsing must not break the exempt-subtree carve-out.
    "rm -r //config/agents/specialists/fitness",
    # Wrapper unwrapping must not break the exempt-subtree carve-out
    # or flag non-resident targets.
    "nohup rm -r /config/agents/specialists/fitness",
    "timeout 5 rm -r /tmp/scratch",
])
async def test_allows_non_resident_rm(cmd):
    from hooks import make_casa_config_guard_hook
    hook = make_casa_config_guard_hook(
        forbid_write_paths=[], forbid_delete_residents=True,
    )
    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd
