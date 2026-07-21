"""Tests for casactl's Task 14 subcommands (persona/specialist/explain).

Follows the black-box subprocess pattern already established by
``tests/test_casactl.py`` (casactl is a shebang script with no ``.py``
extension, so it's exercised as a subprocess rather than imported).
None of these tests require a live ``/run/casa/internal.sock`` — they
either assert argparse wiring (help text, required args, choices) or the
``explain --show-sensitive`` TTY confirmation gate, which runs BEFORE any
socket connection is attempted.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CASACTL = Path(__file__).resolve().parent.parent / "casa-agent" / \
          "rootfs" / "usr" / "local" / "bin" / "casactl"


def _run(*args: str, stdin_devnull: bool = True) -> subprocess.CompletedProcess:
    kwargs = {"capture_output": True, "text": True}
    if stdin_devnull:
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run([sys.executable, str(CASACTL), *args], **kwargs)


# ---------------------------------------------------------------------------
# Top-level wiring
# ---------------------------------------------------------------------------


def test_help_lists_all_five_new_subcommands():
    result = _run("--help")
    assert result.returncode == 0
    for name in ("persona", "specialist", "explain"):
        assert name in result.stdout


# ---------------------------------------------------------------------------
# persona inspect/render/diff
# ---------------------------------------------------------------------------


def test_persona_help_lists_inspect_render_diff():
    result = _run("persona", "--help")
    assert result.returncode == 0
    for name in ("inspect", "render", "diff"):
        assert name in result.stdout


def test_persona_inspect_missing_ref_errors():
    result = _run("persona", "inspect")
    assert result.returncode != 0
    assert "persona" in (result.stderr + result.stdout).lower()


def test_persona_render_missing_role_and_projection_errors():
    result = _run("persona", "render", "gary@1")
    assert result.returncode != 0
    combined = (result.stderr + result.stdout).lower()
    assert "--role" in combined or "role" in combined


def test_persona_render_rejects_invalid_projection_choice():
    result = _run(
        "persona", "render", "gary@1", "--role", "concierge", "--projection", "bogus",
    )
    assert result.returncode != 0
    assert "projection" in (result.stderr + result.stdout).lower()


def test_persona_render_accepts_all_three_projection_choices():
    """No live socket -> connection fails, but argparse must accept the
    choice and get past parsing into the socket-call path (exit code 2,
    not an argparse usage error)."""
    for projection in ("text", "voice", "restricted_webhook"):
        result = _run(
            "persona", "render", "gary@1", "--role", "concierge",
            "--projection", projection,
        )
        assert "invalid choice" not in (result.stderr + result.stdout).lower()


def test_persona_diff_missing_role_and_to_errors():
    result = _run("persona", "diff")
    assert result.returncode != 0


def test_persona_diff_no_socket_reports_socket_error():
    """No live casa-main in the unit-test environment -> the shared
    _print_admin_result helper reports a socket error rather than a
    traceback."""
    result = _run("persona", "diff", "--role", "concierge", "--to", "gary@1")
    assert result.returncode == 2
    assert "socket" in result.stderr.lower()


# ---------------------------------------------------------------------------
# specialist status
# ---------------------------------------------------------------------------


def test_specialist_help_lists_status():
    result = _run("specialist", "--help")
    assert result.returncode == 0
    assert "status" in result.stdout


def test_specialist_status_missing_slug_errors():
    result = _run("specialist", "status")
    assert result.returncode != 0


def test_specialist_status_no_socket_reports_socket_error():
    result = _run("specialist", "status", "finance")
    assert result.returncode == 2
    assert "socket" in result.stderr.lower()


# ---------------------------------------------------------------------------
# explain [--show-sensitive]
# ---------------------------------------------------------------------------


def test_explain_help_lists_show_sensitive():
    result = _run("explain", "--help")
    assert result.returncode == 0
    assert "--show-sensitive" in result.stdout


def test_explain_missing_correlation_id_errors():
    result = _run("explain")
    assert result.returncode != 0


def test_explain_no_socket_reports_socket_error():
    result = _run("explain", "cid-123")
    assert result.returncode == 2
    assert "socket" in result.stderr.lower()


def test_explain_show_sensitive_requires_interactive_tty():
    """Load-bearing constraint #2: --show-sensitive with a non-TTY stdin
    (e.g. piped/redirected, as every non-interactive automation is) must
    refuse BEFORE ever reaching the socket, with exit code 2."""
    result = _run("explain", "cid-123", "--show-sensitive", stdin_devnull=True)
    assert result.returncode == 2
    assert "tty" in result.stderr.lower()
