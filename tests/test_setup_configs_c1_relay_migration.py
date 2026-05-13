"""Tests for the c1-relay migration block in setup-configs.sh.

v0.37.4 hotfix: existing N150 installs (and any post-Phase-Z reseed
that landed before the new bundled defaults shipped) did not pick up
the ``engagement_permission_relay`` PreToolUse policy because the
seed-copy ``for f in ...`` loop in setup-configs.sh only writes
directories that don't already exist. Per memory
``reference_migrations_vs_seed_order``, only migrations can edit
pre-existing files.

The migration scans every
``/addon_configs/casa-agent/agents/executors/<name>/hooks.yaml`` whose
sibling ``definition.yaml`` has ``driver: claude_code`` and, if the
file lacks an ``engagement_permission_relay`` policy entry, appends
the three-line stanza with ``timeout: 600``. Idempotent — running
twice must not duplicate. Marked with ``# casa-migration:c1-relay``
so re-runs can recognise their own work.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SETUP_CONFIGS = Path("casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh")


def _extract_block() -> str:
    """Pull the c1-relay-migration block out of setup-configs.sh as a
    standalone sh fragment runnable via ``sh -c`` against fixture dirs."""
    src = SETUP_CONFIGS.read_text(encoding="utf-8")
    start = src.find("# === c1-relay-migration: begin")
    end = src.find("# === c1-relay-migration: end")
    assert start >= 0 and end > start, (
        "c1-relay-migration block markers missing in setup-configs.sh — "
        "see memory project_v037_2_v037_3_c1_shipped.md follow-up #1"
    )
    return src[start:end]


def _run_block(config_dir: Path) -> tuple[int, str, str]:
    """Run the migration block under POSIX sh against the given fixture
    root. Returns (returncode, stdout, stderr)."""
    block = _extract_block()
    env = {
        "PATH": "/usr/bin:/bin",
        "CONFIG_DIR": config_dir.as_posix(),
    }
    proc = subprocess.run(
        ["sh", "-c", block],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _seed_executor(
    config_dir: Path,
    name: str,
    *,
    driver: str = "claude_code",
    hooks_body: str | None = None,
) -> Path:
    """Create a fake executor dir with definition.yaml + hooks.yaml.

    Returns the path to hooks.yaml so the test can read it back.
    """
    exec_dir = config_dir / "agents" / "executors" / name
    exec_dir.mkdir(parents=True)
    (exec_dir / "definition.yaml").write_text(
        f"schema_version: 1\ntype: {name}\ndriver: {driver}\n",
        encoding="utf-8",
    )
    if hooks_body is None:
        hooks_body = (
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: block_dangerous_bash\n"
        )
    hooks_file = exec_dir / "hooks.yaml"
    hooks_file.write_text(hooks_body, encoding="utf-8")
    return hooks_file


class TestC1RelayMigration:
    def test_appends_entry_when_missing(self, tmp_path: Path) -> None:
        """The common upgrade case: an existing claude_code executor's
        hooks.yaml lacks the policy → migration appends the stanza."""
        hooks_file = _seed_executor(tmp_path, "plugin-developer")

        rc, out, err = _run_block(tmp_path)
        assert rc == 0, f"rc={rc} stderr={err!r}"

        content = hooks_file.read_text(encoding="utf-8")
        assert "engagement_permission_relay" in content
        assert "matcher:" in content
        assert "timeout: 600" in content
        # Marker comment lands so re-runs can recognise their own work.
        assert "# casa-migration:c1-relay" in content

    def test_idempotent_when_entry_already_present(self, tmp_path: Path) -> None:
        """Running the migration twice (or against a file that already
        has the entry from the bundled defaults) must not duplicate."""
        existing = (
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: block_dangerous_bash\n"
            "  - policy: engagement_permission_relay\n"
            '    matcher: ".*"\n'
            "    timeout: 600\n"
        )
        hooks_file = _seed_executor(
            tmp_path, "plugin-developer", hooks_body=existing,
        )
        mtime_before = hooks_file.stat().st_mtime_ns

        rc, _out, err = _run_block(tmp_path)
        assert rc == 0, f"rc={rc} stderr={err!r}"

        content = hooks_file.read_text(encoding="utf-8")
        # Exactly one occurrence — no duplication.
        assert content.count("engagement_permission_relay") == 1
        # mtime preserved → the file was not rewritten.
        assert hooks_file.stat().st_mtime_ns == mtime_before, (
            "idempotent migration must not touch the file when entry exists"
        )

    def test_idempotent_across_two_runs(self, tmp_path: Path) -> None:
        """Run the migration twice on a file that initially lacks the
        entry. First run appends; second run is a no-op."""
        hooks_file = _seed_executor(tmp_path, "plugin-developer")

        rc1, _o1, _e1 = _run_block(tmp_path)
        assert rc1 == 0
        first_content = hooks_file.read_text(encoding="utf-8")
        assert first_content.count("engagement_permission_relay") == 1

        rc2, _o2, _e2 = _run_block(tmp_path)
        assert rc2 == 0
        second_content = hooks_file.read_text(encoding="utf-8")
        assert second_content == first_content, (
            "second run modified the file — migration is not idempotent"
        )

    def test_skips_non_claude_code_driver(self, tmp_path: Path) -> None:
        """Configurator uses driver: in_casa — the hook's cwd resolver
        cannot match its tool calls, so the policy must NOT be appended
        (it would deny every tool call)."""
        hooks_file = _seed_executor(
            tmp_path, "configurator", driver="in_casa",
        )
        rc, out, err = _run_block(tmp_path)
        assert rc == 0, f"rc={rc} stderr={err!r}"
        content = hooks_file.read_text(encoding="utf-8")
        assert "engagement_permission_relay" not in content

    def test_skips_when_hooks_yaml_missing(self, tmp_path: Path) -> None:
        """Executor dir exists with definition.yaml but no hooks.yaml
        → migration skips silently (does not create the file)."""
        exec_dir = tmp_path / "agents" / "executors" / "plugin-developer"
        exec_dir.mkdir(parents=True)
        (exec_dir / "definition.yaml").write_text(
            "schema_version: 1\ndriver: claude_code\n", encoding="utf-8",
        )
        # No hooks.yaml.
        rc, _out, err = _run_block(tmp_path)
        assert rc == 0, f"rc={rc} stderr={err!r}"
        assert not (exec_dir / "hooks.yaml").exists()

    def test_skips_when_definition_yaml_missing(self, tmp_path: Path) -> None:
        """Executor dir has hooks.yaml but no definition.yaml → can't
        determine driver → skip safely."""
        exec_dir = tmp_path / "agents" / "executors" / "orphan"
        exec_dir.mkdir(parents=True)
        hooks_file = exec_dir / "hooks.yaml"
        hooks_file.write_text(
            "schema_version: 1\npre_tool_use:\n  - policy: block_dangerous_bash\n",
            encoding="utf-8",
        )
        rc, _out, err = _run_block(tmp_path)
        assert rc == 0, f"rc={rc} stderr={err!r}"
        assert "engagement_permission_relay" not in hooks_file.read_text(encoding="utf-8")

    def test_no_executors_dir(self, tmp_path: Path) -> None:
        """Fresh-install boot before seed-copy has populated executors/
        → migration is a no-op (no crash)."""
        # No $CONFIG_DIR/agents/executors/ at all.
        rc, _out, err = _run_block(tmp_path)
        assert rc == 0, f"rc={rc} stderr={err!r}"

    def test_handles_multiple_claude_code_executors(self, tmp_path: Path) -> None:
        """If two claude_code executors are present (e.g. plugin-developer
        + a future one), both get the entry."""
        a = _seed_executor(tmp_path, "plugin-developer")
        b = _seed_executor(tmp_path, "future-cc-executor")
        c = _seed_executor(tmp_path, "configurator", driver="in_casa")

        rc, _out, err = _run_block(tmp_path)
        assert rc == 0, f"rc={rc} stderr={err!r}"

        assert "engagement_permission_relay" in a.read_text(encoding="utf-8")
        assert "engagement_permission_relay" in b.read_text(encoding="utf-8")
        # Configurator NOT touched — wrong driver.
        assert "engagement_permission_relay" not in c.read_text(encoding="utf-8")

    def test_does_not_crash_on_malformed_yaml(self, tmp_path: Path) -> None:
        """A user with hand-edited / broken hooks.yaml must not crash
        the boot script. The migration is grep-based and tolerant — it
        either appends (if no marker text is present) or skips. The
        contract is 'must not crash boot', not 'must produce valid YAML
        from a broken file' — that's the operator's responsibility."""
        broken_body = "this: is not\nvalid: : yaml [[\n"
        hooks_file = _seed_executor(
            tmp_path, "plugin-developer", hooks_body=broken_body,
        )
        rc, _out, err = _run_block(tmp_path)
        assert rc == 0, f"rc={rc} stderr={err!r}"
        # File still exists (we did not delete it).
        assert hooks_file.exists()

    def test_marker_comment_lands_exactly_once(self, tmp_path: Path) -> None:
        """The migration marker comment '# casa-migration:c1-relay'
        should land exactly once even across multiple runs."""
        hooks_file = _seed_executor(tmp_path, "plugin-developer")

        rc1, _o1, _e1 = _run_block(tmp_path)
        assert rc1 == 0
        rc2, _o2, _e2 = _run_block(tmp_path)
        assert rc2 == 0

        content = hooks_file.read_text(encoding="utf-8")
        assert content.count("# casa-migration:c1-relay") == 1
