"""Tests for the config_git_commit MCP tool."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def configurator_origin():
    """Bug 7 fix: tool refuses unless caller role == 'configurator'."""
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


class TestConfigGitCommitTool:
    async def test_happy_path(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value="abc123def"):
            result = await config_git_commit.handler({"message": "test"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == "abc123def"
        assert payload["message"] == "test"

    async def test_noop_returns_empty_sha(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value=""):
            result = await config_git_commit.handler({"message": "noop"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == ""

    async def test_noop_carries_loud_gitignore_warning(self, configurator_origin):
        """P-3 (v0.69.1): a silent {"sha": ""} left agents looping to
        reconcile "committed ok" vs "untracked" when their writes landed on
        gitignored paths. The no-op result must SAY that only whitelisted
        paths are tracked."""
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value=""):
            result = await config_git_commit.handler({"message": "noop"})
        payload = json.loads(result["content"][0]["text"])
        warning = payload.get("warning", "")
        assert "gitignored" in warning
        assert "plugin-env.conf" in warning   # the intentionally-secret case
        assert "registry.json" in warning     # the tracked exception

    async def test_real_commit_has_no_warning(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value="abc123def"):
            result = await config_git_commit.handler({"message": "test"})
        payload = json.loads(result["content"][0]["text"])
        assert "warning" not in payload

    async def test_raises_bubbles_as_error_kind(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config", side_effect=RuntimeError("git broke")):
            result = await config_git_commit.handler({"message": "x"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert "git broke" in payload["message"]


class TestConfigGitCommitReloadObligation:
    """#231/#222 (v0.114.0): config_git_commit arms the G-2 reload obligation
    (_ENGAGEMENTS_PENDING_RELOAD) on a real SHA — EXCEPT when the commit merely
    persists a plugin change that _reload_and_verify_targets already activated
    in-process (engagement in _ENGAGEMENTS_PREACTIVATED) AND the commit is
    confined to the plugin registry. A commit that also touches agents/ or
    policies/ still arms, so a genuinely-unactivated change is never masked."""

    @pytest.fixture
    def _bound_engagement(self):
        import types
        import tools as tools_mod
        eng = types.SimpleNamespace(id="c" * 32)
        tok = tools_mod.engagement_var.set(eng)
        tools_mod._ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
        tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)
        try:
            yield eng
        finally:
            tools_mod.engagement_var.reset(tok)
            tools_mod._ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
            tools_mod._ENGAGEMENTS_PREACTIVATED.discard(eng.id)

    async def test_plain_commit_arms_obligation(
            self, configurator_origin, _bound_engagement):
        import tools as tools_mod
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value="sha1"), \
                patch("config_git.changed_paths",
                      return_value=["agents/assistant/runtime.yaml"]):
            await config_git_commit.handler({"message": "edit persona"})
        assert _bound_engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD

    async def test_preactivated_plugins_only_commit_does_not_arm(
            self, configurator_origin, _bound_engagement):
        import tools as tools_mod
        from tools import config_git_commit
        tools_mod._ENGAGEMENTS_PREACTIVATED.add(_bound_engagement.id)
        with patch("config_git.commit_config", return_value="sha2"), \
                patch("config_git.changed_paths",
                      return_value=["plugins/registry.json"]):
            await config_git_commit.handler({"message": "persist plugin"})
        assert _bound_engagement.id not in tools_mod._ENGAGEMENTS_PENDING_RELOAD
        # The pre-activation credit is consumed by the persist commit.
        assert _bound_engagement.id not in tools_mod._ENGAGEMENTS_PREACTIVATED

    async def test_preactivated_mixed_commit_still_arms(
            self, configurator_origin, _bound_engagement):
        """Terra: a single commit carrying BOTH plugin registry AND an agent
        YAML edit must still arm — the YAML change was not activated."""
        import tools as tools_mod
        from tools import config_git_commit
        tools_mod._ENGAGEMENTS_PREACTIVATED.add(_bound_engagement.id)
        with patch("config_git.commit_config", return_value="sha3"), \
                patch("config_git.changed_paths",
                      return_value=["plugins/registry.json",
                                    "agents/assistant/runtime.yaml"]):
            await config_git_commit.handler({"message": "plugin + persona"})
        assert _bound_engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD

    async def test_preactivated_but_changed_paths_empty_arms_failsafe(
            self, configurator_origin, _bound_engagement):
        """A git error in changed_paths returns [] — fail safe by ARMING
        rather than wrongly suppressing the obligation."""
        import tools as tools_mod
        from tools import config_git_commit
        tools_mod._ENGAGEMENTS_PREACTIVATED.add(_bound_engagement.id)
        with patch("config_git.commit_config", return_value="sha4"), \
                patch("config_git.changed_paths", return_value=[]):
            await config_git_commit.handler({"message": "persist plugin"})
        assert _bound_engagement.id in tools_mod._ENGAGEMENTS_PENDING_RELOAD


class TestConfigGitCommitRoleGuard:
    """Bug 7 (v0.14.6): config_git_commit must refuse non-configurator
    callers even if their runtime.yaml::tools.allowed lists the tool."""

    async def test_no_origin_refused(self):
        from tools import config_git_commit
        result = await config_git_commit.handler({"message": "x"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"

    async def test_assistant_role_refused(self):
        import agent as agent_mod
        from tools import config_git_commit
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            result = await config_git_commit.handler({"message": "x"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"

    async def test_plugin_developer_role_refused(self):
        """Plugin-developer is an executor but not authorised for these tools."""
        import agent as agent_mod
        from tools import config_git_commit
        tok = agent_mod.origin_var.set({"role": "plugin-developer"})
        try:
            result = await config_git_commit.handler({"message": "x"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"


class TestConfigGitCommitSchemaGate:
    """E-G v0.31.0 pre-commit schema-validation gate. The tool must
    refuse a commit that would land schema-invalid YAML and FATAL the
    addon on next boot. See `bug-review-2026-05-01-exploration.md` and
    `project_eg_configurator_schema_invalid_yaml`."""

    async def test_refuses_when_validation_errors_found(
        self, configurator_origin,
    ):
        from tools import config_git_commit
        with patch(
            "agent_loader.validate_config_repo",
            return_value=[
                "/addon_configs/casa-agent/agents/assistant/character.yaml: "
                "schema violation at (root): Additional properties are not "
                "allowed ('TRAIT' was unexpected)",
            ],
        ), patch("config_git.commit_config") as mock_commit:
            result = await config_git_commit.handler(
                {"message": "add trait"},
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "schema_invalid"
        assert payload["errors"][0].endswith(
            "Additional properties are not allowed ('TRAIT' was unexpected)"
        )
        # The git commit MUST NOT be reached when validation fails.
        mock_commit.assert_not_called()

    async def test_proceeds_when_validation_clean(self, configurator_origin):
        from tools import config_git_commit
        with patch(
            "agent_loader.validate_config_repo", return_value=[],
        ), patch(
            "config_git.commit_config", return_value="abcd1234",
        ) as mock_commit:
            result = await config_git_commit.handler({"message": "ok"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == "abcd1234"
        assert payload["message"] == "ok"
        mock_commit.assert_called_once()

    async def test_aggregates_multiple_errors(self, configurator_origin):
        """The error list surfaces every offending file at once so the
        configurator can fix them all in one round-trip."""
        from tools import config_git_commit
        with patch(
            "agent_loader.validate_config_repo",
            return_value=[
                "/p/character.yaml: schema violation at (root): bad1",
                "/p/runtime.yaml: schema violation at (root): bad2",
            ],
        ), patch("config_git.commit_config") as mock_commit:
            result = await config_git_commit.handler({"message": "bulk"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "schema_invalid"
        assert len(payload["errors"]) == 2
        assert "2 schema validation failure" in payload["message"]
        mock_commit.assert_not_called()
