"""Tests for drivers.hook_bridge -- Casa hook policy -> CC settings.json."""

from __future__ import annotations

import json

import pytest


class TestHookBridgeTranslate:
    def test_emits_pretooluse_block_for_each_policy(self):
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "casa_config_guard", "matcher": "Write|Edit"},
                {"policy": "commit_size_guard", "matcher": "Bash"},
            ],
        }

        settings = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )

        assert "hooks" in settings
        pre = settings["hooks"]["PreToolUse"]
        assert len(pre) == 2

        first = pre[0]
        assert first["matcher"] == "Write|Edit"
        assert first["hooks"][0]["type"] == "command"
        assert first["hooks"][0]["command"].endswith(
            "hook_proxy.sh casa_config_guard"
        )

    def test_empty_hooks_yaml_produces_empty_hooks(self):
        from drivers.hook_bridge import translate_hooks_to_settings
        settings = translate_hooks_to_settings(
            {}, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )
        assert settings == {"hooks": {}}

    def test_translates_bundled_plugin_developer_hooks_yaml(self):
        """L-1b regression: bundled snake_case hooks.yaml must translate."""
        import yaml
        from pathlib import Path
        from drivers.hook_bridge import translate_hooks_to_settings

        here = Path(__file__).resolve().parent.parent
        hooks_path = (
            here / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"
            / "agents" / "executors" / "plugin-developer" / "hooks.yaml"
        )
        raw = yaml.safe_load(hooks_path.read_text(encoding="utf-8")) or {}
        settings = translate_hooks_to_settings(
            raw, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )
        assert "PreToolUse" in settings["hooks"]
        assert len(settings["hooks"]["PreToolUse"]) >= 1
