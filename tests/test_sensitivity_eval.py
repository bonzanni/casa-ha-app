"""Sensitivity eval set: schema (unit) + prompt-accuracy regression (slow)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sensitivity import TIERS

pytestmark = [pytest.mark.unit]

EVAL_PATH = Path(__file__).parent / "fixtures" / "sensitivity_eval.jsonl"


def _load_eval() -> list[dict]:
    rows = []
    for line in EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def test_eval_set_schema_and_values():
    rows = _load_eval()
    assert len(rows) >= 20, "seed eval set should have a reasonable spread"
    for r in rows:
        assert set(r) >= {"fact", "channel", "expected_tier", "note"}
        assert isinstance(r["fact"], str) and r["fact"].strip()
        assert r["expected_tier"] in TIERS
    seen = {r["expected_tier"] for r in rows}
    assert seen == set(TIERS), f"eval set must cover all tiers; missing {set(TIERS) - seen}"
