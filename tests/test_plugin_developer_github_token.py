"""v0.14.9: plugin-developer engagements no longer resolve GITHUB_TOKEN
per-engagement. The token is set at addon boot via setup-configs.sh
(written to /run/s6/container_environment/GITHUB_TOKEN) and inherited
automatically by every s6-supervised service and child subprocess.

This file replaces the v0.14.6 contract that required
ClaudeCodeDriver to call _resolve_plugin_developer_github_token and
inject GITHUB_TOKEN into extra_env at start time."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_resolver_function_is_gone() -> None:
    """The per-engagement token resolver was deleted in v0.14.9."""
    from drivers import claude_code_driver
    assert not hasattr(claude_code_driver, "_resolve_plugin_developer_github_token"), (
        "v0.14.6's per-engagement resolver should be deleted; token now "
        "comes from addon-wide /run/s6/container_environment/GITHUB_TOKEN"
    )


def test_run_template_no_explicit_github_token_export() -> None:
    """The engagement run template does NOT explicitly export GITHUB_TOKEN.
    s6-overlay's container_environment merges it in automatically."""
    template = Path(
        "casa-agent/rootfs/opt/casa/scripts/engagement_run_template.sh"
    ).read_text()
    # An explicit `export GITHUB_TOKEN=` would mean v0.14.6's pattern is
    # still alive; assert it isn't.
    assert "GITHUB_TOKEN=" not in template, (
        "engagement_run_template.sh must not export GITHUB_TOKEN itself; "
        "s6 container env handles propagation"
    )


def test_etc_gitconfig_credential_helper_present() -> None:
    """github-access goes through the credential helper, not URL embedding."""
    cfg = Path("casa-agent/rootfs/etc/gitconfig").read_text()
    assert "git-credential-casa.sh" in cfg
    assert "x-access-token" not in cfg, (
        "Token must come from helper at request time, never be embedded in "
        "the gitconfig URL section"
    )
