"""Tests for the deprecated-options-prune block in setup-configs.sh.

When a Casa release removes an option from config.yaml's schema, each
install's Supervisor-stored options still carry the removed key and HA
logs `Option '<key>' does not exist in the schema` every boot (warning,
not crash). The block deletes such keys via `bashio::addon.option <key>`
(no value → delete; /usr/lib/bashio/addons.sh:537). Additive list;
idempotent — only deletes keys actually present.

Spec: docs/superpowers/specs/2026-06-08-deprecated-options-prune-design.md.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SETUP_CONFIGS = Path("casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh")


def _extract_block() -> str:
    src = SETUP_CONFIGS.read_text(encoding="utf-8")
    start = src.find("# === deprecated-options-prune: begin")
    end = src.find("# === deprecated-options-prune: end")
    assert start >= 0 and end > start, (
        "deprecated-options-prune block markers missing in setup-configs.sh — "
        "see spec 2026-06-08-deprecated-options-prune-design.md"
    )
    return src[start:end]


def _run_block(tmp_path: Path, options_json: str) -> list[str]:
    """Run the prune block under bash with stubbed bashio functions.

    `bashio::addon.options` returns *options_json*; `bashio::jq.exists`
    does a quoted-key substring presence check (no jq dependency);
    `bashio::addon.option <key>` records the key to a log file. Returns
    the list of keys the block asked to delete, in order.
    """
    del_log = tmp_path / "deleted.txt"
    harness = f"""
set -u
__OPTS_JSON='{options_json}'
__DEL_LOG='{del_log.as_posix()}'
: > "$__DEL_LOG"
bashio::addon.options() {{ printf '%s' "$__OPTS_JSON"; }}
bashio::jq.exists() {{
    # $1=json  $2=.key  -> exit 0 if the quoted key name appears in json
    _k=$(printf '%s' "$2" | sed 's/^\\.//')
    case "$1" in *"\\"$_k\\""*) return 0 ;; *) return 1 ;; esac
}}
bashio::addon.option() {{ printf '%s\\n' "$1" >> "$__DEL_LOG"; return 0; }}
bashio::log.info() {{ :; }}
bashio::log.warning() {{ :; }}
"""
    full = harness + "\n" + _extract_block()
    subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=10, check=False)
    text = del_log.read_text(encoding="utf-8").strip()
    return text.splitlines() if text else []


def test_prunes_present_deprecated_keys(tmp_path: Path) -> None:
    # Two deprecated keys present (honcho_api_key, scope_threshold) + a valid one.
    opts = '{"claude_oauth_token":"x","honcho_api_key":"y","scope_threshold":0.3}'
    deleted = _run_block(tmp_path, opts)
    assert set(deleted) == {"honcho_api_key", "scope_threshold"}
    assert "claude_oauth_token" not in deleted  # valid key untouched


def test_noop_on_clean_options(tmp_path: Path) -> None:
    # Only valid current keys → zero deletions.
    opts = '{"claude_oauth_token":"x","telegram_bot_token":"z","casa_tz":"Europe/Amsterdam"}'
    assert _run_block(tmp_path, opts) == []


def test_all_seeded_keys_are_recognized(tmp_path: Path) -> None:
    # Every seeded deprecated key, when present, is deleted.
    seeded = [
        "github_token", "heartbeat_enabled", "heartbeat_interval_minutes",
        "honcho_api_key", "honcho_api_url", "repos", "scope_threshold",
        "telegram_webhook_url",
    ]
    opts = "{" + ",".join(f'"{k}":1' for k in seeded) + "}"
    assert set(_run_block(tmp_path, opts)) == set(seeded)


def test_block_markers_and_list_present() -> None:
    src = SETUP_CONFIGS.read_text(encoding="utf-8")
    assert "# === deprecated-options-prune: begin" in src
    assert "# === deprecated-options-prune: end" in src
    assert "DEPRECATED_OPTION_KEYS=" in src
