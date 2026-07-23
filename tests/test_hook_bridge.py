"""Tests for drivers.hook_bridge -- Casa hook policy -> CC settings.json."""

from __future__ import annotations

import json

import pytest

PROXY = "/opt/casa/scripts/hook_proxy.sh"


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
        # Two declared entries + the code-mandatory managed_component_guard
        # (round-4 Terra P0: yaml policies are additive-only).
        assert len(pre) == 3

        first = pre[0]
        assert first["matcher"] == "Write|Edit"
        assert first["hooks"][0]["type"] == "command"
        assert first["hooks"][0]["command"].endswith(
            "hook_proxy.sh casa_config_guard"
        )
        assert pre[-1]["hooks"][0]["command"].endswith(
            "hook_proxy.sh managed_component_guard"
        )

    def test_empty_hooks_yaml_still_carries_managed_guard(self):
        """Round-4 Terra P0 regression: a hollow hooks.yaml (definition.yaml's
        `hooks_file:` repointed at an empty file) used to emit ZERO hooks —
        the next session then loaded no pre_tool_use policies at all. The
        managed guard entry is now code-mandatory."""
        from drivers.hook_bridge import translate_hooks_to_settings
        settings = translate_hooks_to_settings(
            {}, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )
        pre = settings["hooks"]["PreToolUse"]
        assert len(pre) == 1
        assert pre[0]["matcher"] == "Write|Edit|Bash"
        assert pre[0]["hooks"][0]["command"].endswith(
            "hook_proxy.sh managed_component_guard"
        )

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


class TestTimeoutPassthrough:
    """C-1 follow-up: per-hook ``timeout`` must propagate to CC settings.

    CC's hook-runner default is 60s; engagement_permission_relay needs
    ~600s for the operator-response window (C-1 spec section 4.6).
    Without pass-through CC kills the hook before the operator can reply.
    """

    def test_timeout_emitted_when_present(self):
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "foo", "matcher": ".*", "timeout": 600},
            ],
        }
        out = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path=PROXY,
        )
        entry = out["hooks"]["PreToolUse"][0]
        assert entry["matcher"] == ".*"
        cc_hook = entry["hooks"][0]
        assert cc_hook["type"] == "command"
        assert cc_hook["command"].endswith("hook_proxy.sh foo")
        assert cc_hook["timeout"] == 600

    def test_timeout_omitted_when_absent(self):
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "foo", "matcher": ".*"},
            ],
        }
        out = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path=PROXY,
        )
        cc_hook = out["hooks"]["PreToolUse"][0]["hooks"][0]
        assert "timeout" not in cc_hook

    def test_timeout_coerced_to_int(self):
        """YAML may parse numeric strings or floats; we want int seconds."""
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "foo", "matcher": ".*", "timeout": "600"},
            ],
        }
        out = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path=PROXY,
        )
        cc_hook = out["hooks"]["PreToolUse"][0]["hooks"][0]
        assert cc_hook["timeout"] == 600
        assert isinstance(cc_hook["timeout"], int)

    def test_none_timeout_omitted(self):
        """Explicit None should not emit a bogus 0 or null timeout."""
        from drivers.hook_bridge import translate_hooks_to_settings

        hooks_yaml = {
            "pre_tool_use": [
                {"policy": "foo", "matcher": ".*", "timeout": None},
            ],
        }
        out = translate_hooks_to_settings(
            hooks_yaml, proxy_script_path=PROXY,
        )
        cc_hook = out["hooks"]["PreToolUse"][0]["hooks"][0]
        assert "timeout" not in cc_hook

    def test_bundled_engagement_permission_relay_has_600s_timeout(self):
        """Bundled C-1 policy must surface timeout=600 in CC settings.

        v0.37.4: only claude_code-driver executors carry this policy.
        Configurator was reverted (driver: in_casa — the hook's cwd
        resolver can't match its tool calls; see spec §4.6).
        """
        import yaml
        from pathlib import Path
        from drivers.hook_bridge import translate_hooks_to_settings

        here = Path(__file__).resolve().parent.parent
        for executor in ("plugin-developer",):
            hooks_path = (
                here / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"
                / "agents" / "executors" / executor / "hooks.yaml"
            )
            raw = yaml.safe_load(hooks_path.read_text(encoding="utf-8")) or {}
            settings = translate_hooks_to_settings(
                raw, proxy_script_path=PROXY,
            )
            relay_entries = [
                entry
                for entry in settings["hooks"].get("PreToolUse", [])
                if any(
                    "engagement_permission_relay" in h["command"]
                    for h in entry["hooks"]
                )
            ]
            assert relay_entries, (
                f"{executor} hooks.yaml missing engagement_permission_relay"
            )
            cc_hook = relay_entries[0]["hooks"][0]
            assert cc_hook.get("timeout") == 600, (
                f"{executor}: expected timeout=600, got "
                f"{cc_hook.get('timeout')!r}"
            )

    def test_configurator_does_not_carry_relay_policy(self):
        """v0.37.4: configurator's bundled defaults must NOT include
        engagement_permission_relay — its driver: in_casa setup means
        the hook's cwd-based engagement resolver cannot match its tool
        calls, and including it would deny every configurator tool
        call. See memory project_v037_2_v037_3_c1_shipped.md
        follow-up #2."""
        import yaml
        from pathlib import Path

        here = Path(__file__).resolve().parent.parent
        hooks_path = (
            here / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"
            / "agents" / "executors" / "configurator" / "hooks.yaml"
        )
        raw = yaml.safe_load(hooks_path.read_text(encoding="utf-8")) or {}
        policies = [
            entry.get("policy")
            for entry in raw.get("pre_tool_use", [])
        ]
        assert "engagement_permission_relay" not in policies, (
            "configurator must not carry engagement_permission_relay — "
            "wrong driver (in_casa vs claude_code); see spec §4.6"
        )
