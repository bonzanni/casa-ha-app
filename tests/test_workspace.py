"""Unit tests for drivers.workspace — engagement workspace provisioner."""

from __future__ import annotations

import os

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
