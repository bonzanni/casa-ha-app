"""ManagedSdkClient unit tests (sdk_client_pool.py). Spec: docs/superpowers/
specs/2026-07-11-resident-sdk-client-pooling-design.md (AR-1..AR-10)."""
from __future__ import annotations

import asyncio
import logging

import pytest

pytestmark = pytest.mark.asyncio


def test_cidbox_str_and_default():
    from sdk_client_pool import _CidBox
    box = _CidBox()
    assert str(box) == "-"
    box.value = "abc123"
    assert str(box) == "abc123"


async def test_cid_filter_coerces_box_to_str():
    from log_cid import CidFilter, cid_var
    from sdk_client_pool import _CidBox
    box = _CidBox()
    box.value = "turn-2-cid"
    token = cid_var.set(box)  # type: ignore[arg-type]
    try:
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "m", (), None)
        CidFilter().filter(rec)
        assert rec.cid == "turn-2-cid"
        assert isinstance(rec.cid, str)
    finally:
        cid_var.reset(token)
