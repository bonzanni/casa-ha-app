"""Tests for the v0.6.1 setup-configs.sh migration: inject casa-framework
MCP tool names into Ellen's tools.allowed list."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "casa-agent" / "rootfs" \
    / "etc" / "s6-overlay" / "scripts" / "setup-configs.sh"


def _find_bash() -> str | None:
    candidates: list[str] = []
    which = shutil.which("bash")
    if which:
        candidates.append(which)
    if os.name == "nt":
        candidates.append(r"C:\Program Files\Git\usr\bin\bash.exe")
        candidates.append(r"C:\Program Files\Git\bin\bash.exe")
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            result = subprocess.run(
                [path, "-c", "echo ok"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0 and result.stdout.strip() == "ok":
                return path
        except (OSError, subprocess.SubprocessError):
            continue
    return None


_BASH = _find_bash()


# v0.6.0 default — inline allowed list, no MCP tools
_INLINE_NO_MCP = """\
name: Ellen
role: assistant
model: opus
personality: |
  You are Ellen.
tools:
  allowed: [Read, Write, Edit, Bash, Skill]
  disallowed: []
  permission_mode: acceptEdits
  max_turns: 20
"""


# User-customized — block form allowed list
_BLOCK_NO_MCP = """\
name: Ellen
role: assistant
model: opus
personality: |
  You are Ellen.
tools:
  allowed:
    - Read
    - Skill
  disallowed: []
  permission_mode: acceptEdits
"""


# Already has the delegate tool but not send_message
_PARTIAL_MCP = """\
name: Ellen
role: assistant
model: opus
personality: |
  You are Ellen.
tools:
  allowed: [Read, mcp__casa-framework__delegate_to_agent]
  disallowed: []
"""


# Already has both (for idempotency / already-migrated hands case)
_ALL_MCP = """\
name: Ellen
role: assistant
model: opus
personality: |
  You are Ellen.
tools:
  allowed: [Read, mcp__casa-framework__delegate_to_agent, mcp__casa-framework__send_message]
"""


def _run_migration(tmp_path: Path, filename: str) -> subprocess.CompletedProcess:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("migrate_mcp_allowed() {")
    end = text.index("\n}\n", start) + 3
    func = text[start:end]
    script = textwrap.dedent(f"""\
        bashio::log.info() {{ echo "[INFO] $*"; }}
        {func}
        migrate_mcp_allowed "{(tmp_path / filename).as_posix()}"
    """)
    assert _BASH is not None
    return subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, check=False,
    )


@pytest.mark.skipif(_BASH is None, reason="functional bash not available")
class TestMigrateMcpAllowed:
    def test_inline_list_adds_both_tools(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_text(_INLINE_NO_MCP, encoding="utf-8")
        r = _run_migration(tmp_path, "assistant.yaml")
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "mcp__casa-framework__delegate_to_agent" in text
        assert "mcp__casa-framework__send_message" in text
        # Existing entries preserved
        assert "Read" in text and "Skill" in text
        # Marker present
        assert text.count("# casa: mcp-tools v1") == 1

    def test_block_list_adds_both_tools(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_text(_BLOCK_NO_MCP, encoding="utf-8")
        r = _run_migration(tmp_path, "assistant.yaml")
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "mcp__casa-framework__delegate_to_agent" in text
        assert "mcp__casa-framework__send_message" in text
        # Block form preserved — ensure new entries use same form
        assert "    - mcp__casa-framework__delegate_to_agent" in text
        assert "# casa: mcp-tools v1" in text

    def test_partial_mcp_only_injects_missing_tool(self, tmp_path):
        """If `delegate_to_agent` is already there, only add `send_message`."""
        f = tmp_path / "assistant.yaml"
        f.write_text(_PARTIAL_MCP, encoding="utf-8")
        r = _run_migration(tmp_path, "assistant.yaml")
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        # Both must be present
        assert text.count("mcp__casa-framework__delegate_to_agent") == 1
        assert text.count("mcp__casa-framework__send_message") == 1
        assert "# casa: mcp-tools v1" in text

    def test_all_mcp_already_present_only_appends_marker(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_text(_ALL_MCP, encoding="utf-8")
        r = _run_migration(tmp_path, "assistant.yaml")
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        # No duplicate tools
        assert text.count("mcp__casa-framework__delegate_to_agent") == 1
        assert text.count("mcp__casa-framework__send_message") == 1
        assert text.count("# casa: mcp-tools v1") == 1

    def test_marker_present_is_noop(self, tmp_path):
        """If the marker is already there, don't touch anything even if
        the tools are missing."""
        f = tmp_path / "assistant.yaml"
        f.write_text(
            _INLINE_NO_MCP + "# casa: mcp-tools v1\n",
            encoding="utf-8",
        )
        _run_migration(tmp_path, "assistant.yaml")
        text = f.read_text(encoding="utf-8")
        # Tools were NOT injected — marker short-circuited the migration
        assert "mcp__casa-framework__delegate_to_agent" not in text
        assert text.count("# casa: mcp-tools v1") == 1

    def test_idempotent_on_re_run(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_text(_INLINE_NO_MCP, encoding="utf-8")
        _run_migration(tmp_path, "assistant.yaml")
        once = f.read_text(encoding="utf-8")
        _run_migration(tmp_path, "assistant.yaml")
        twice = f.read_text(encoding="utf-8")
        assert once == twice
        assert twice.count("# casa: mcp-tools v1") == 1
        assert twice.count("mcp__casa-framework__delegate_to_agent") == 1

    def test_crlf_input_works(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_bytes(_INLINE_NO_MCP.replace("\n", "\r\n").encode("utf-8"))
        r = _run_migration(tmp_path, "assistant.yaml")
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "mcp__casa-framework__delegate_to_agent" in text
        assert "# casa: mcp-tools v1" in text

    def test_missing_file_is_no_op(self, tmp_path):
        r = _run_migration(tmp_path, "nonexistent.yaml")
        assert r.returncode == 0, r.stderr
        assert not (tmp_path / "nonexistent.yaml").exists()
