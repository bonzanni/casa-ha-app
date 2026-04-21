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


class TestLoaderResolvesPromptFile:
    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")

    def _seed_resident(self, base: Path, *, character_yaml: str):
        """Write a minimal valid resident with caller-provided character.yaml."""
        d = base / "assistant"
        self._write(d / "character.yaml", character_yaml)
        self._write(d / "voice.yaml", "schema_version: 1\ntone: [direct]\n")
        self._write(d / "response_shape.yaml",
                    "schema_version: 1\nregister: written\nformat: plain\n")
        self._write(d / "disclosure.yaml",
                    "schema_version: 1\npolicy: standard\n")
        self._write(d / "runtime.yaml", """\
            schema_version: 1
            model: sonnet
            tools:
              allowed: [Read]
            channels: [telegram]
        """)
        return d

    def _make_policies(self):
        from policies import PolicyLibrary
        return PolicyLibrary({
            "standard": {
                "categories": {},
                "safe_on_any_channel": [],
                "deflection_patterns": {},
            },
        })

    def test_loader_reads_prompt_file_from_disk(self, tmp_path):
        from agent_loader import load_agent_from_dir

        policies = self._make_policies()

        char_yaml = """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: |
              Short card.
            prompt_file: prompts/system.md
        """
        d = self._seed_resident(tmp_path, character_yaml=char_yaml)
        (d / "prompts").mkdir()
        (d / "prompts" / "system.md").write_text(
            "You are Ellen.\nExternalised prompt body.\n",
            encoding="utf-8",
        )

        cfg = load_agent_from_dir(str(d), policies=policies)
        assert "You are Ellen." in cfg.character.prompt
        assert "Externalised prompt body." in cfg.character.prompt

    def test_loader_rejects_prompt_file_outside_agent_dir(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        policies = self._make_policies()

        char_yaml = """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: |
              Short card.
            prompt_file: ../../escape.md
        """
        d = self._seed_resident(tmp_path, character_yaml=char_yaml)
        (tmp_path / "escape.md").write_text("stolen", encoding="utf-8")

        with pytest.raises(LoadError, match="escapes"):
            load_agent_from_dir(str(d), policies=policies)

    def test_loader_rejects_missing_prompt_file(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        policies = self._make_policies()

        char_yaml = """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: |
              Short card.
            prompt_file: prompts/does-not-exist.md
        """
        d = self._seed_resident(tmp_path, character_yaml=char_yaml)

        with pytest.raises(LoadError, match="not found"):
            load_agent_from_dir(str(d), policies=policies)

    def test_loader_reads_trigger_prompt_file(self, tmp_path):
        from agent_loader import load_agent_from_dir

        policies = self._make_policies()

        char_yaml = """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: |
              x
            prompt: |
              You are Ellen.
        """
        d = self._seed_resident(tmp_path, character_yaml=char_yaml)
        self._write(d / "triggers.yaml", """\
            schema_version: 1
            triggers:
              - name: morning
                type: cron
                schedule: "0 8 * * *"
                channel: telegram
                prompt_file: prompts/morning.md
        """)
        (d / "prompts").mkdir(exist_ok=True)
        (d / "prompts" / "morning.md").write_text(
            "Morning briefing body.\n", encoding="utf-8",
        )

        cfg = load_agent_from_dir(str(d), policies=policies)
        assert len(cfg.triggers) == 1
        assert cfg.triggers[0].prompt == "Morning briefing body.\n"
