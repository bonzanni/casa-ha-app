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


def test_malformed_json_degrades_to_empty(tmp_path: Path) -> None:
    """Sol CI-review: env extraction now shares one parser with grants; a
    malformed .mcp.json degrades to no vars (the malformed status is surfaced
    separately by parse_mcp_servers → verify's mcp_invalid), never raises."""
    from plugin_store import parse_mcp_servers
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{{}", encoding="utf-8")
    assert extract_env_vars(mcp) == set()
    assert parse_mcp_servers(mcp)[1] is True          # flagged malformed


def test_no_env_block(tmp_path: Path) -> None:
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"s": {"command": "x"}}})
    assert extract_env_vars(mcp) == set()


def test_top_level_mcp_json_env_vars_extracted(tmp_path: Path) -> None:
    """Sol CI-review HIGH: a top-level (no-mcpServers-wrapper) plugin .mcp.json —
    the shape context7 ships — must still yield its required secrets, or verify
    could report ready without them."""
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"svc": {"command": "node", "env": {"TOKEN": "${MY_TOKEN}"}}})
    assert extract_env_vars(mcp) == {"MY_TOKEN"}


def test_cc_builtin_list_is_stable() -> None:
    # Locks the P-7 + Q7 resolved set. Changes require spec update.
    assert CC_BUILTIN_VARS == {
        "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
        "HOME", "PATH", "USER", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
        "LC_MESSAGES", "PWD",
    }


def test_extracts_vars_from_command_args_url_and_headers(tmp_path: Path) -> None:
    # #423 r2 (Sol 5): the CLI expands ${VAR} in command, args, url and
    # headers too — a reference there is as much a required secret as one in
    # env, and an unresolved one reaches the server as the literal string.
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {
        "mcpServers": {
            "a": {
                "command": "${RUNNER_BIN}",
                "args": ["--token", "${ARGS_TOKEN}",
                         "${CLAUDE_PLUGIN_ROOT}/x"],
                "env": {"K": "${ENV_KEY}"},
            },
            "b": {
                "url": "https://api.example/${URL_TENANT}/mcp",
                "headers": {"Authorization": "Bearer ${HDR_TOKEN}"},
            },
        }
    })
    assert extract_env_vars(mcp) == {
        "RUNNER_BIN", "ARGS_TOKEN", "ENV_KEY", "URL_TENANT", "HDR_TOKEN"}


def test_unknown_extension_fields_are_not_scanned(tmp_path: Path) -> None:
    # #423 r3 (Sol r2-4): the parser tolerates unknown server keys, but the
    # CLI expands ${VAR} only in the launch fields (command, args, url,
    # headers, env) — a reference inside a metadata-style extension field is
    # not a launch-time requirement and must not withhold the plugin.
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {
        "mcpServers": {"s": {
            "command": "x",
            "metadata": {"example": "${OPTIONAL_DOC_ONLY}"},
            "env": {"K": "${REAL_KEY}"},
        }}
    })
    assert extract_env_vars(mcp) == {"REAL_KEY"}
