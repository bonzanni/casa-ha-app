"""Tests for the unified <field>/<field>_file prose externalisation idiom."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import jsonschema
import pytest


def _load_schema(name: str) -> dict:
    root = Path(__file__).resolve().parent.parent
    path = root / "casa-agent/rootfs/opt/casa/defaults/schema" / f"{name}.v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestTriggersSchemaPromptFile:
    def test_interval_accepts_prompt_inline(self):
        schema = _load_schema("triggers")
        doc = {
            "schema_version": 1,
            "triggers": [
                {"name": "hb", "type": "interval", "minutes": 60,
                 "channel": "telegram", "prompt": "tick"},
            ],
        }
        jsonschema.validate(doc, schema)

    def test_interval_accepts_prompt_file(self):
        schema = _load_schema("triggers")
        doc = {
            "schema_version": 1,
            "triggers": [
                {"name": "hb", "type": "interval", "minutes": 60,
                 "channel": "telegram", "prompt_file": "prompts/heartbeat.md"},
            ],
        }
        jsonschema.validate(doc, schema)

    def test_interval_rejects_both_prompt_and_prompt_file(self):
        schema = _load_schema("triggers")
        doc = {
            "schema_version": 1,
            "triggers": [
                {"name": "hb", "type": "interval", "minutes": 60,
                 "channel": "telegram", "prompt": "x",
                 "prompt_file": "prompts/heartbeat.md"},
            ],
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_interval_rejects_neither(self):
        schema = _load_schema("triggers")
        doc = {
            "schema_version": 1,
            "triggers": [
                {"name": "hb", "type": "interval", "minutes": 60,
                 "channel": "telegram"},
            ],
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_cron_accepts_prompt_file(self):
        schema = _load_schema("triggers")
        doc = {
            "schema_version": 1,
            "triggers": [
                {"name": "morning", "type": "cron", "schedule": "0 8 * * *",
                 "channel": "telegram", "prompt_file": "prompts/morning.md"},
            ],
        }
        jsonschema.validate(doc, schema)


class TestCharacterSchemaPromptFile:
    def test_accepts_prompt_and_card_inline(self):
        schema = _load_schema("character")
        doc = {
            "schema_version": 1, "name": "Ellen", "role": "assistant",
            "archetype": "assistant", "card": "x", "prompt": "y",
        }
        jsonschema.validate(doc, schema)

    def test_accepts_prompt_file_and_card_file(self):
        schema = _load_schema("character")
        doc = {
            "schema_version": 1, "name": "Ellen", "role": "assistant",
            "archetype": "assistant",
            "card_file": "prompts/card.md",
            "prompt_file": "prompts/system.md",
        }
        jsonschema.validate(doc, schema)

    def test_rejects_both_prompt_and_prompt_file(self):
        schema = _load_schema("character")
        doc = {
            "schema_version": 1, "name": "Ellen", "role": "assistant",
            "archetype": "assistant", "card": "x",
            "prompt": "y", "prompt_file": "prompts/system.md",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_rejects_neither_card_nor_card_file(self):
        schema = _load_schema("character")
        doc = {
            "schema_version": 1, "name": "Ellen", "role": "assistant",
            "archetype": "assistant", "prompt": "y",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_rejects_neither_prompt_nor_prompt_file(self):
        schema = _load_schema("character")
        doc = {
            "schema_version": 1, "name": "Ellen", "role": "assistant",
            "archetype": "assistant", "card": "x",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)
