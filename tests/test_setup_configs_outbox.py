"""Boot wiring for the plugin outbox: setup-configs.sh creates/exports it, and
casa_core actually calls plugin_outbox.wire (v0.73.0)."""
from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = _ROOT / "casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh"
CASA_CORE = _ROOT / "casa-agent/rootfs/opt/casa/casa_core.py"


def test_script_creates_and_exports_outbox():
    text = SCRIPT.read_text()
    assert 'mkdir -p "$DATA_DIR/plugin-outbox/.claims"' in text
    assert 'chmod 0770 "$DATA_DIR/plugin-outbox" "$DATA_DIR/plugin-outbox/.claims"' in text
    assert "/run/s6/container_environment/CASA_PLUGIN_OUTBOX_DIR" in text


def test_casa_core_wires_and_closes_outbox():
    # Guards against an omitted boot call — the wire() BEHAVIOUR is unit-tested
    # separately (test_wire_inits_and_registers_hourly_job); this asserts casa_core
    # actually invokes it + closes on shutdown.
    text = CASA_CORE.read_text()
    assert "plugin_outbox.wire(" in text
    assert "plugin_outbox.get_outbox()" in text
