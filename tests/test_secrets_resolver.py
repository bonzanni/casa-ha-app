"""secrets_resolver — op:// reference resolution at boot."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from secrets_resolver import resolve

pytestmark = pytest.mark.unit


def test_plaintext_passthrough() -> None:
    assert resolve("plaintext-value") == "plaintext-value"
    assert resolve("") == ""


def test_op_not_a_prefix() -> None:
    # Strings that contain 'op' but don't start with 'op://' pass through.
    assert resolve("options") == "options"


@patch("secrets_resolver.subprocess.run")
def test_op_resolves(mock_run) -> None:
    resolve.cache_clear()
    mock_run.return_value.stdout = "real-secret\n"
    mock_run.return_value.returncode = 0
    assert resolve("op://Casa/GitHub/token") == "real-secret"


@patch("secrets_resolver.subprocess.run")
def test_op_failure_raises(mock_run) -> None:
    resolve.cache_clear()
    import subprocess as sp
    mock_run.side_effect = sp.CalledProcessError(1, "op", stderr="auth error")
    with pytest.raises(RuntimeError, match="auth error"):
        resolve("op://Casa/X/y")


@patch("secrets_resolver.subprocess.run")
def test_op_timeout_raises(mock_run) -> None:
    resolve.cache_clear()
    import subprocess as sp
    mock_run.side_effect = sp.TimeoutExpired("op", 10)
    with pytest.raises(RuntimeError, match="Timeout"):
        resolve("op://Casa/X/y")


@patch("secrets_resolver.subprocess.run")
def test_cached(mock_run) -> None:
    resolve.cache_clear()
    mock_run.return_value.stdout = "v"
    mock_run.return_value.returncode = 0
    resolve("op://a/b/c")
    resolve("op://a/b/c")
    assert mock_run.call_count == 1
