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


def test_run_script_derives_memory_backend_from_hindsight_url():
    """v0.46.1: setting `hindsight_api_url` must turn long-term memory ON.

    `casa_core.resolve_semantic_memory_choice` requires
    `MEMORY_BACKEND=hindsight` (anything else → noop) AND nothing else in the
    add-on sets `MEMORY_BACKEND` — so `svc-casa/run` derives it inside the
    `hindsight_api_url` conditional. Without this, the URL option alone leaves
    casa on noop and long-term memory is unreachable. Regression guard."""
    script = _read_run_script()
    # MEMORY_BACKEND must be exported...
    assert "export MEMORY_BACKEND=" in script, (
        "svc-casa/run must export MEMORY_BACKEND (else hindsight is unreachable)"
    )
    # ...and it must be derived to "hindsight" inside the hindsight_api_url block,
    # i.e. between the `if [ "$_hindsight_url" ... ]` guard and its closing `fi`.
    start = script.index("_hindsight_url=")
    block = script[start:script.index("\nfi", start)]
    assert "export HINDSIGHT_API_URL=" in block
    assert 'export MEMORY_BACKEND="${MEMORY_BACKEND:-hindsight}"' in block, (
        "MEMORY_BACKEND=hindsight must be derived inside the hindsight_api_url "
        "conditional so the URL is the single toggle"
    )
