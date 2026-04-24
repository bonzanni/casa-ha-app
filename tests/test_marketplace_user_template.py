"""User marketplace template ships empty — populated at runtime only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEMPLATE = Path("casa-agent/rootfs/opt/casa/defaults/marketplace-user/.claude-plugin/marketplace.json")


def test_template_parses_and_is_empty() -> None:
    data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert data["name"] == "casa-plugins"
    assert data["plugins"] == []
