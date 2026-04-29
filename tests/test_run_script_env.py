"""Regression test for v0.18.1: bashio->env wiring of telegram_engagement_supergroup_id.

Reads the s6-overlay/s6-rc.d/svc-casa/run script and asserts every
schema option that casa_core.py::os.environ.get reads is exported.
Catches the v0.11.0 -> v0.18.0 regression where
telegram_engagement_supergroup_id was added to schema + casa_core but
not to the run script."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _read_run_script() -> str:
    p = (
        Path(__file__).resolve().parent.parent
        / "casa-agent" / "rootfs" / "etc" / "s6-overlay"
        / "s6-rc.d" / "svc-casa" / "run"
    )
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("var", [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_TRANSPORT",
    "TELEGRAM_DELIVERY_MODE",
    "TELEGRAM_ENGAGEMENT_SUPERGROUP_ID",  # v0.18.1 - was missing pre-fix
])
def test_run_script_exports_telegram_env_var(var):
    """svc-casa/run must export every TELEGRAM_* env var that
    casa_core.py reads at startup. Regression guard for v0.11.0 ->
    v0.18.0 regression where TELEGRAM_ENGAGEMENT_SUPERGROUP_ID was
    silently dropped."""
    script = _read_run_script()
    # Match `export VAR=` or `export VAR="..."` patterns
    assert (
        f"export {var}=" in script or f'export {var}="' in script
    ), f"Missing `export {var}=...` in svc-casa/run"


def test_run_script_exports_log_level():
    """v0.18.1: operator-facing log_level addon option must export to env.

    Uses null-normalize pattern (matches CASA_TZ / CASA_SCOPE_THRESHOLD
    handling) so install_logging() defaults to INFO when unset."""
    script = _read_run_script()
    assert "LOG_LEVEL" in script, "Missing LOG_LEVEL handling in svc-casa/run"
