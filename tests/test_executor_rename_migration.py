"""Tests for the v0.6.1 setup-configs.sh migration: rename executor alex.yaml → finance.yaml."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "casa-agent" / "rootfs" \
    / "etc" / "s6-overlay" / "scripts" / "setup-configs.sh"


def _find_bash() -> str | None:
    candidates: list[str] = []
    which = shutil.which("bash")
    if which:
        candidates.append(which)
    if os.name == "nt":
        candidates.append(r"C:\Program Files\Git\usr\bin\bash.exe")
        candidates.append(r"C:\Program Files\Git\bin\bash.exe")
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            result = subprocess.run(
                [path, "-c", "echo ok"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0 and result.stdout.strip() == "ok":
                return path
        except (OSError, subprocess.SubprocessError):
            continue
    return None


_BASH = _find_bash()


_V060_ALEX = """\
name: alex
role: alex
description: >
  Finance executor.
enabled: false
model: sonnet
personality: |
  You are Alex.
memory:
  token_budget: 0
session:
  strategy: ephemeral
  idle_timeout: 0
"""


def _run_migration(config_dir: Path) -> subprocess.CompletedProcess:
    """Extract migrate_executor_rename from setup-configs.sh and run it."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("migrate_executor_rename() {")
    end = text.index("\n}\n", start) + 3
    func = text[start:end]
    script = textwrap.dedent(f"""\
        bashio::log.info() {{ echo "[INFO] $*"; }}
        CONFIG_DIR="{config_dir.as_posix()}"
        {func}
        migrate_executor_rename
    """)
    assert _BASH is not None, "bash unavailable; should have skipped"
    return subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, check=False,
    )


@pytest.mark.skipif(_BASH is None, reason="functional bash not available")
class TestMigrateExecutorRename:
    def _setup(self, tmp_path: Path) -> Path:
        config_dir = tmp_path / "casa-agent"
        (config_dir / "agents" / "executors").mkdir(parents=True)
        return config_dir

    def test_renames_and_patches_role_and_name(self, tmp_path):
        config_dir = self._setup(tmp_path)
        (config_dir / "agents" / "executors" / "alex.yaml").write_text(
            _V060_ALEX, encoding="utf-8",
        )
        r = _run_migration(config_dir)
        assert r.returncode == 0, r.stderr

        assert not (config_dir / "agents" / "executors" / "alex.yaml").exists()
        new = config_dir / "agents" / "executors" / "finance.yaml"
        assert new.exists()
        text = new.read_text(encoding="utf-8")
        assert "role: finance" in text
        assert "name: Alex" in text
        # Personality / description preserved
        assert "You are Alex." in text
        # No lingering role: alex / name: alex (lowercase)
        assert "role: alex" not in text
        assert "\nname: alex\n" not in text

    def test_idempotent_when_finance_already_exists(self, tmp_path):
        """If both old and new exist — new wins (assume user migrated already
        or is in mid-state). Do nothing, no error."""
        config_dir = self._setup(tmp_path)
        alex = config_dir / "agents" / "executors" / "alex.yaml"
        finance = config_dir / "agents" / "executors" / "finance.yaml"
        alex.write_text(_V060_ALEX, encoding="utf-8")
        finance.write_text(
            "name: Alex\nrole: finance\nmodel: sonnet\npersonality: x\n",
            encoding="utf-8",
        )

        r = _run_migration(config_dir)
        assert r.returncode == 0, r.stderr

        # old alex.yaml untouched (we don't clobber explicit user state)
        assert alex.exists()
        # finance.yaml untouched — still the minimal user version
        assert "personality: x" in finance.read_text(encoding="utf-8")

    def test_no_op_on_fresh_install(self, tmp_path):
        """No alex.yaml → nothing happens."""
        config_dir = self._setup(tmp_path)
        r = _run_migration(config_dir)
        assert r.returncode == 0, r.stderr
        assert not (config_dir / "agents" / "executors" / "alex.yaml").exists()
        assert not (config_dir / "agents" / "executors" / "finance.yaml").exists()

    def test_crlf_safe(self, tmp_path):
        """Windows-edited alex.yaml still migrates cleanly."""
        config_dir = self._setup(tmp_path)
        (config_dir / "agents" / "executors" / "alex.yaml").write_bytes(
            _V060_ALEX.replace("\n", "\r\n").encode("utf-8"),
        )
        r = _run_migration(config_dir)
        assert r.returncode == 0, r.stderr

        new = config_dir / "agents" / "executors" / "finance.yaml"
        assert new.exists()
        text = new.read_text(encoding="utf-8")
        assert "role: finance" in text
        assert "name: Alex" in text
