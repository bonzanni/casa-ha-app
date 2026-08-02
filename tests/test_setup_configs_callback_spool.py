"""Task 9 — boot wiring for the callback spool: setup-configs.sh creates
``/data/callbacks`` at 0770 and exports ``CASA_CALLBACK_SPOOL_ROOT`` into the
s6 container environment, mirroring the plugin-outbox block (v0.73.0) it sits
next to. ``callback_spool.spool_root()`` already honours the env override
(default ``/data/callbacks``) — this pins the boot-side half of that wiring.
"""
from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = _ROOT / "casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh"


def test_script_creates_and_exports_callback_spool():
    text = SCRIPT.read_text()
    assert 'mkdir -p "$DATA_DIR/callbacks"' in text
    assert 'chmod 0770 "$DATA_DIR/callbacks"' in text
    assert "/run/s6/container_environment/CASA_CALLBACK_SPOOL_ROOT" in text


def test_callback_spool_block_follows_outbox_block():
    """Not load-bearing, but keeps the two sibling drop-box blocks adjacent
    for future readers (mirrors the outbox idiom deliberately)."""
    text = SCRIPT.read_text()
    outbox_idx = text.index("CASA_PLUGIN_OUTBOX_DIR")
    spool_idx = text.index("CASA_CALLBACK_SPOOL_ROOT")
    assert outbox_idx < spool_idx
