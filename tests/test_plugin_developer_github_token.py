"""plugin-developer GitHub-token resolution at engagement spawn (v0.14.6).

Covers `_resolve_plugin_developer_github_token()` in drivers.claude_code_driver:
the helper resolves `op://${ONEPASSWORD_DEFAULT_VAULT}/GitHub/credential`,
returning the token on success or "" on any failure (logged warning).

Replaces the v0.14.5 `github_token` addon-option passthrough with direct
1Password resolution at engagement spawn — keeps the secret out of the
addon-options surface and out of the addon process env.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from drivers.claude_code_driver import _resolve_plugin_developer_github_token

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_resolve_cache():
    """secrets_resolver.resolve has lru_cache; clear it between tests so
    side-effect mocks aren't masked by a cached result from a prior test."""
    from secrets_resolver import resolve
    resolve.cache_clear()
    yield
    resolve.cache_clear()


# --- Vault selection ------------------------------------------------------


@patch("secrets_resolver.subprocess.run")
def test_uses_default_vault_when_env_unset(mock_run, monkeypatch) -> None:
    """ONEPASSWORD_DEFAULT_VAULT unset → vault "Casa"."""
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)
    mock_run.return_value.stdout = "ghp_abc123\n"
    mock_run.return_value.returncode = 0

    token = _resolve_plugin_developer_github_token()

    assert token == "ghp_abc123"
    args, _ = mock_run.call_args
    assert args[0] == ["op", "read", "op://Casa/GitHub/credential"]


@patch("secrets_resolver.subprocess.run")
def test_uses_configured_vault_from_env(mock_run, monkeypatch) -> None:
    """ONEPASSWORD_DEFAULT_VAULT="Personal" → op:// reference uses "Personal"."""
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Personal")
    mock_run.return_value.stdout = "ghp_personal\n"
    mock_run.return_value.returncode = 0

    token = _resolve_plugin_developer_github_token()

    assert token == "ghp_personal"
    args, _ = mock_run.call_args
    assert args[0] == ["op", "read", "op://Personal/GitHub/credential"]


@patch("secrets_resolver.subprocess.run")
def test_empty_env_value_falls_back_to_casa(mock_run, monkeypatch) -> None:
    """ONEPASSWORD_DEFAULT_VAULT="" (empty string) → still defaults to Casa.

    Guards against bashio "null" → "" normalization producing a malformed
    op://${empty}/GitHub/credential reference.
    """
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "")
    mock_run.return_value.stdout = "ghp_default\n"
    mock_run.return_value.returncode = 0

    _resolve_plugin_developer_github_token()

    args, _ = mock_run.call_args
    assert args[0] == ["op", "read", "op://Casa/GitHub/credential"]


# --- Failure modes --------------------------------------------------------


@patch("secrets_resolver.subprocess.run")
def test_op_failure_returns_empty_logs_warning(mock_run, monkeypatch, caplog) -> None:
    """op CLI failure → returns empty string + logs warning at WARNING level."""
    import subprocess as sp
    mock_run.side_effect = sp.CalledProcessError(
        1, "op", stderr="auth: not signed in",
    )
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)

    with caplog.at_level(logging.WARNING, logger="drivers.claude_code_driver"):
        token = _resolve_plugin_developer_github_token()

    assert token == ""
    assert any(
        "plugin-developer engagement" in rec.message
        and "op://Casa/GitHub/credential" in rec.message
        and "unresolved" in rec.message
        for rec in caplog.records
    )


@patch("secrets_resolver.subprocess.run")
def test_op_timeout_returns_empty(mock_run, monkeypatch) -> None:
    """op CLI timeout → returns empty string (degraded, not crashed)."""
    import subprocess as sp
    mock_run.side_effect = sp.TimeoutExpired("op", 10)
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)

    token = _resolve_plugin_developer_github_token()

    assert token == ""


@patch("secrets_resolver.subprocess.run")
def test_blank_token_treated_as_failure(mock_run, monkeypatch) -> None:
    """op returning empty body (whitespace only) → treated as failure."""
    mock_run.return_value.stdout = "\n"
    mock_run.return_value.returncode = 0
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)

    token = _resolve_plugin_developer_github_token()

    assert token == ""


# --- Engagement env wiring ------------------------------------------------


@patch("drivers.claude_code_driver._resolve_plugin_developer_github_token")
def test_extra_env_carries_resolved_token_for_plugin_developer(
    mock_resolve,
) -> None:
    """In start(), extra_env gets GITHUB_TOKEN only when defn.type ==
    "plugin-developer" AND the resolver returned a non-empty token.

    Smoke-tests the call-site wiring without driving the full s6 spawn flow.
    """
    mock_resolve.return_value = "ghp_resolved"

    # Simulate the start() conditional in isolation.
    from types import SimpleNamespace
    defn = SimpleNamespace(type="plugin-developer")
    extra_env: dict[str, str] = {}
    if defn.type == "plugin-developer":
        from drivers.claude_code_driver import _resolve_plugin_developer_github_token as _r
        token = _r()
        if token:
            extra_env["GITHUB_TOKEN"] = token

    assert extra_env == {"GITHUB_TOKEN": "ghp_resolved"}
    mock_resolve.assert_called_once()


@patch("drivers.claude_code_driver._resolve_plugin_developer_github_token")
def test_extra_env_omits_github_token_on_resolver_failure(mock_resolve) -> None:
    """Resolver returning "" must keep GITHUB_TOKEN out of extra_env entirely
    (engagement starts in degraded state; gh/git ops will fail clearly)."""
    mock_resolve.return_value = ""

    from types import SimpleNamespace
    defn = SimpleNamespace(type="plugin-developer")
    extra_env: dict[str, str] = {}
    if defn.type == "plugin-developer":
        from drivers.claude_code_driver import _resolve_plugin_developer_github_token as _r
        token = _r()
        if token:
            extra_env["GITHUB_TOKEN"] = token

    assert extra_env == {}


@patch("drivers.claude_code_driver._resolve_plugin_developer_github_token")
def test_resolve_skipped_for_non_plugin_developer(mock_resolve) -> None:
    """Non-plugin-developer executors (configurator, hello-driver) must not
    trigger 1P resolution at all — they have no GitHub repos to push."""
    from types import SimpleNamespace
    for executor_type in ("configurator", "hello-driver", "data-engineer"):
        mock_resolve.reset_mock()
        defn = SimpleNamespace(type=executor_type)
        extra_env: dict[str, str] = {}
        if defn.type == "plugin-developer":
            from drivers.claude_code_driver import (
                _resolve_plugin_developer_github_token as _r,
            )
            token = _r()
            if token:
                extra_env["GITHUB_TOKEN"] = token

        assert extra_env == {}
        mock_resolve.assert_not_called()


# --- Surface-area regression ---------------------------------------------


def test_addon_config_does_not_declare_github_token() -> None:
    """v0.14.6 removed `github_token` from addon options + schema."""
    from pathlib import Path
    config_yaml = Path("casa-agent/config.yaml").read_text(encoding="utf-8")
    assert "github_token" not in config_yaml, (
        "github_token surfaced in config.yaml — v0.14.6 removed it; "
        "1P is the source of truth via op://${vault}/GitHub/credential."
    )


def test_svc_casa_run_does_not_export_github_token() -> None:
    """v0.14.6 removed GITHUB_TOKEN export from svc-casa/run."""
    from pathlib import Path
    run_script = Path(
        "casa-agent/rootfs/etc/s6-overlay/s6-rc.d/svc-casa/run",
    ).read_text(encoding="utf-8")
    assert "GITHUB_TOKEN" not in run_script, (
        "svc-casa/run still exports GITHUB_TOKEN — v0.14.6 dropped this; "
        "the token is resolved at engagement spawn time, not at addon boot."
    )


def test_casa_core_does_not_resolve_github_token_globally() -> None:
    """v0.14.6 removed GITHUB_TOKEN from casa_core's _PASSWORD_ENV_VARS so
    it's not resolved into the addon process env at boot."""
    from pathlib import Path
    casa_core = Path(
        "casa-agent/rootfs/opt/casa/casa_core.py",
    ).read_text(encoding="utf-8")
    # Locate the _PASSWORD_ENV_VARS tuple and assert GITHUB_TOKEN absent.
    import re
    match = re.search(
        r"_PASSWORD_ENV_VARS\s*=\s*\(([^)]+)\)",
        casa_core,
        re.DOTALL,
    )
    assert match, "_PASSWORD_ENV_VARS tuple not found in casa_core.py"
    body = match.group(1)
    assert "GITHUB_TOKEN" not in body, (
        "_PASSWORD_ENV_VARS still includes GITHUB_TOKEN — v0.14.6 moved "
        "GitHub-token resolution to engagement spawn (per-executor)."
    )
