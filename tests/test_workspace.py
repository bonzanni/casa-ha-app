"""Unit tests for drivers.workspace — engagement workspace provisioner."""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.asyncio


class TestRenderRunScript:
    def test_substitutes_all_placeholders(self):
        from drivers.workspace import render_run_script

        out = render_run_script(
            engagement_id="abc12345def67890",
            permission_mode="acceptEdits",
            extra_dirs=["/data/casa-plugins-repo"],
        )

        assert "{ID}" not in out
        assert "{ID_SHORT}" not in out
        assert "{PERMISSION_MODE}" not in out
        assert "{ADD_DIR_FLAGS}" not in out
        assert "{EXTRA_UNSET}" not in out

        assert 'HOME="/data/engagements/abc12345def67890/.home"' in out
        assert "engagement-abc12345" in out             # 8-char slug in CLI name
        assert "--permission-mode acceptEdits" in out
        assert "--add-dir /data/engagements/abc12345def67890/" in out
        assert "--add-dir /data/casa-plugins-repo" in out

    def test_default_extra_dirs_still_includes_workspace(self):
        from drivers.workspace import render_run_script

        out = render_run_script(
            engagement_id="xxxxxxxxxxxxxxxx",
            permission_mode="dontAsk",
            extra_dirs=[],
        )
        assert "--add-dir /data/engagements/xxxxxxxxxxxxxxxx/" in out
        assert "--permission-mode dontAsk" in out

    def test_extra_unset_names_appear_in_unset_line(self):
        from drivers.workspace import render_run_script

        out = render_run_script(
            engagement_id="xxxxxxxxxxxxxxxx",
            permission_mode="dontAsk",
            extra_dirs=[],
            extra_unset=["MY_SECRET", "ANOTHER_TOKEN"],
        )
        # The template unsets base secrets then "{EXTRA_UNSET}" — after
        # rendering, the extras should appear in the unset command.
        assert "MY_SECRET" in out
        assert "ANOTHER_TOKEN" in out
        assert "{EXTRA_UNSET}" not in out


class TestProvisionWorkspace:
    def _make_defn(self, tmp_path, executor_type="hello-driver", plugins=None):
        """Build an ExecutorDefinition stub for workspace tests."""
        from config import ExecutorDefinition

        exec_dir = tmp_path / "defaults-executors" / executor_type
        exec_dir.mkdir(parents=True)
        (exec_dir / "prompt.md").write_text(
            "You are the {executor_type} executor. Task: {task}. Context: {context}."
        )

        plugins_dir = ""
        if plugins is not None:
            pdir = exec_dir / "plugins"
            pdir.mkdir()
            for pname in plugins:
                (pdir / pname).mkdir()
            plugins_dir = str(pdir)

        return ExecutorDefinition(
            type=executor_type,
            description="test executor with twenty characters exactly today",
            model="sonnet",
            driver="claude_code",
            prompt_template_path=str(exec_dir / "prompt.md"),
            plugins_dir=plugins_dir,
            mcp_server_names=["casa-framework"],
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_creates_workspace_tree(self, tmp_path):
        import json
        from pathlib import Path
        from drivers.workspace import provision_workspace

        defn = self._make_defn(tmp_path)
        base_plugins_dir = tmp_path / "opt-casa-claude-plugins-base"
        base_plugins_dir.mkdir()
        (base_plugins_dir / "superpowers").mkdir()

        ws = tmp_path / "engagements"
        ws.mkdir()

        path = await provision_workspace(
            engagements_root=str(ws),
            base_plugins_root=str(base_plugins_dir),
            engagement_id="eng1",
            defn=defn,
            task="do a thing",
            context="because",
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )

        p = Path(path)
        assert p == ws / "eng1"
        assert (p / "CLAUDE.md").exists()
        claude_md = (p / "CLAUDE.md").read_text()
        assert "hello-driver" in claude_md
        assert "do a thing" in claude_md
        assert "because" in claude_md

        assert (p / ".mcp.json").exists()
        mcp = json.loads((p / ".mcp.json").read_text())
        assert "casa-framework" in mcp["mcpServers"]

        assert (p / ".claude" / "settings.json").exists()
        assert (p / ".home" / ".claude" / "plugins" / "superpowers").is_symlink()

        # FIFO
        assert os.path.exists(p / "stdin.fifo")
        import stat as _stat
        mode = os.stat(p / "stdin.fifo").st_mode
        assert _stat.S_ISFIFO(mode)

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_per_executor_plugins_override_baseline(self, tmp_path):
        """Tier 2 wins precedence over Tier 1 (same-named pack)."""
        from pathlib import Path
        from drivers.workspace import provision_workspace

        defn = self._make_defn(tmp_path, plugins=["superpowers", "plugin-dev"])

        base = tmp_path / "base-plugins"
        base.mkdir()
        (base / "superpowers").mkdir()

        ws = tmp_path / "engagements"
        ws.mkdir()

        path = await provision_workspace(
            engagements_root=str(ws),
            base_plugins_root=str(base),
            engagement_id="eng2",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
        )

        sp_link = Path(path) / ".home" / ".claude" / "plugins" / "superpowers"
        # The link target is the per-executor version, not the baseline.
        target = os.readlink(sp_link)
        assert "/plugins/superpowers" in target.replace("\\", "/")
        assert str(base) not in target


class TestCasaMeta:
    def test_write_and_load_roundtrip(self, tmp_path):
        from drivers.workspace import write_casa_meta, load_casa_meta

        ws = tmp_path / "w"
        ws.mkdir()
        write_casa_meta(
            workspace_path=str(ws),
            engagement_id="e1", executor_type="hello-driver",
            status="UNDERGOING", created_at="2026-04-23T10:00:00Z",
            finished_at=None, retention_until=None,
        )

        meta = load_casa_meta(str(ws))
        assert meta["engagement_id"] == "e1"
        assert meta["status"] == "UNDERGOING"
        assert meta["finished_at"] is None

    def test_load_returns_none_when_missing(self, tmp_path):
        from drivers.workspace import load_casa_meta
        ws = tmp_path / "w"
        ws.mkdir()
        assert load_casa_meta(str(ws)) is None
