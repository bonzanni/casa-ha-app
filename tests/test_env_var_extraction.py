"""Extract ${VAR} references from a plugin's .mcp.json, minus CC built-ins."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin_env_extractor import CC_BUILTIN_VARS, extract_env_vars

pytestmark = pytest.mark.unit


def _write(p: Path, data: dict) -> None:
    p.write_text(json.dumps(data), encoding="utf-8")


def test_extracts_user_vars(tmp_path: Path) -> None:
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {
        "mcpServers": {
            "face-rec": {
                "command": "python",
                "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"],
                "env": {
                    "AWS_ACCESS_KEY_ID": "${AWS_ACCESS_KEY_ID}",
                    "AWS_REGION": "${AWS_REGION}",
                    "HOME": "${HOME}",
                },
            }
        }
    })
    assert extract_env_vars(mcp) == {"AWS_ACCESS_KEY_ID", "AWS_REGION"}


def test_filters_cc_builtins(tmp_path: Path) -> None:
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {
        "mcpServers": {"s": {"command": "x", "env": {
            "CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}",
            "CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}",
            "PATH": "${PATH}",
            "USER": "${USER}",
            "TMPDIR": "${TMPDIR}",
            "LANG": "${LANG}",
            "LC_ALL": "${LC_ALL}",
            "PWD": "${PWD}",
            "MY_THING": "${MY_THING}",
        }}}
    })
    result = extract_env_vars(mcp)
    assert result == {"MY_THING"}, f"unexpected: {result - {'MY_THING'}}"


def test_missing_mcp_json_returns_empty(tmp_path: Path) -> None:
    assert extract_env_vars(tmp_path / "none.json") == set()


def test_malformed_json_raises(tmp_path: Path) -> None:
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{{}", encoding="utf-8")
    with pytest.raises(ValueError):
        extract_env_vars(mcp)


def test_no_env_block(tmp_path: Path) -> None:
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"s": {"command": "x"}}})
    assert extract_env_vars(mcp) == set()


def test_cc_builtin_list_is_stable() -> None:
    # Locks the P-7 + Q7 resolved set. Changes require spec update.
    assert CC_BUILTIN_VARS == {
        "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
        "HOME", "PATH", "USER", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
        "LC_MESSAGES", "PWD",
    }
