"""Tests for the E-C drift-check block in setup-configs.sh.

The block walks /opt/casa/defaults/{agents,policies}/ vs the persistent
overlay at /addon_configs/casa-agent/{agents,policies}/, byte-compares
each file, and logs WARNING per drifted or missing-in-live file plus a
one-line summary. Visibility-only — does not mutate the overlay.

Pre-fix (v0.28.1 and prior): every default-side prompt/doctrine/YAML
change shipped after first boot was silently dark in production. Three
confirmed dark-state examples spanning v0.26.1 → v0.27.0 → v0.28.0
documented in docs/bug-review-2026-04-30-exploration2.md::E-C.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SETUP_CONFIGS = Path("casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh")


def _extract_drift_check_block() -> str:
    """Pull the drift-check block out of setup-configs.sh as a standalone
    sh fragment runnable via `sh -c` against fixture dirs."""
    src = SETUP_CONFIGS.read_text(encoding="utf-8")
    start = src.find("# === drift-check: begin")
    end = src.find("# === drift-check: end")
    assert start >= 0 and end > start, (
        "drift-check block markers missing in setup-configs.sh — see "
        "docs/bug-review-2026-04-30-exploration2.md::E-C"
    )
    return src[start:end]


def _run_block(defaults_dir: Path, config_dir: Path) -> tuple[int, str, str]:
    """Run the drift-check block under POSIX sh against the given fixture
    roots. Returns (returncode, stdout, stderr)."""
    block = _extract_drift_check_block()
    # POSIX-style paths so sh on Windows (Git Bash / MSYS) handles them
    # the same as Linux. as_posix() gives forward slashes; the production
    # script runs against absolute Linux paths so this is test-only.
    env = {
        "PATH": "/usr/bin:/bin",
        "DEFAULTS_DIR": defaults_dir.as_posix(),
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


@pytest.fixture
def fixture_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Build defaults + live trees side-by-side for drift-check fodder."""
    defaults = tmp_path / "defaults"
    live = tmp_path / "live"
    (defaults / "agents" / "assistant" / "prompts").mkdir(parents=True)
    (live / "agents" / "assistant" / "prompts").mkdir(parents=True)
    (defaults / "policies").mkdir(parents=True)
    (live / "policies").mkdir(parents=True)
    return defaults, live


class TestDriftCheck:
    def test_clean_when_trees_match(self, fixture_roots: tuple[Path, Path]) -> None:
        """No drift, no missing → INFO summary line, no WARN lines."""
        defaults, live = fixture_roots
        # Identical content in both trees.
        (defaults / "agents" / "assistant" / "prompts" / "system.md").write_text(
            "be helpful\n", encoding="utf-8",
        )
        (live / "agents" / "assistant" / "prompts" / "system.md").write_text(
            "be helpful\n", encoding="utf-8",
        )
        (defaults / "policies" / "scopes.yaml").write_text(
            "default_scope: personal\n", encoding="utf-8",
        )
        (live / "policies" / "scopes.yaml").write_text(
            "default_scope: personal\n", encoding="utf-8",
        )

        rc, out, err = _run_block(defaults, live)
        assert rc == 0, f"rc={rc} stderr={err!r}"
        assert "drift_check report: clean" in out
        assert "[WARN]" not in out

    def test_detects_drifted_file(self, fixture_roots: tuple[Path, Path]) -> None:
        """File present in both trees but byte-different → drifted+WARN."""
        defaults, live = fixture_roots
        (defaults / "agents" / "assistant" / "prompts" / "system.md").write_text(
            "v0.29.0 default content with new financial-arithmetic block\n",
            encoding="utf-8",
        )
        (live / "agents" / "assistant" / "prompts" / "system.md").write_text(
            "older content from before the v0.27.0 reseed\n",
            encoding="utf-8",
        )

        rc, out, err = _run_block(defaults, live)
        assert rc == 0
        assert "drift_check drifted:" in out
        assert "system.md" in out
        # Summary line must reflect drifted=1.
        assert "drifted=1" in out
        # And surface the wipe-doctrine reminder.
        assert "wipe doctrine" in out

    def test_detects_missing_in_live(self, fixture_roots: tuple[Path, Path]) -> None:
        """File in defaults but not in live → missing-in-live+WARN."""
        defaults, live = fixture_roots
        (defaults / "agents" / "assistant" / "prompts" / "system.md").write_text(
            "default content\n", encoding="utf-8",
        )
        # live/.../system.md intentionally absent.

        rc, out, err = _run_block(defaults, live)
        assert rc == 0
        assert "drift_check missing-in-live:" in out
        assert "system.md" in out
        assert "missing=1" in out

    def test_ignores_operator_added_files(
        self, fixture_roots: tuple[Path, Path],
    ) -> None:
        """File in live but not in defaults → ignored (operator addition).

        Operators may add specialists, scopes, or doctrine files between
        wipes; these are NOT drift."""
        defaults, live = fixture_roots
        (defaults / "agents" / "assistant" / "prompts" / "system.md").write_text(
            "default\n", encoding="utf-8",
        )
        (live / "agents" / "assistant" / "prompts" / "system.md").write_text(
            "default\n", encoding="utf-8",
        )
        # Operator-added file in live; should NOT be flagged.
        (live / "agents" / "specialists").mkdir(parents=True)
        (live / "agents" / "specialists" / "operator-added.yaml").write_text(
            "name: custom\n", encoding="utf-8",
        )

        rc, out, err = _run_block(defaults, live)
        assert rc == 0
        assert "operator-added.yaml" not in out, (
            "operator-added files MUST NOT be reported as drift "
            "(false positives would push operators to wipe their additions)"
        )
        assert "drift_check report: clean" in out

    def test_handles_missing_default_dir_gracefully(self, tmp_path: Path) -> None:
        """If DEFAULTS_DIR/agents/ doesn't exist (image-baked tree
        partially missing), drift_check_tree must early-return cleanly."""
        defaults = tmp_path / "defaults"
        defaults.mkdir()
        # No agents/ or policies/ subdirs.
        live = tmp_path / "live"
        (live / "agents").mkdir(parents=True)
        (live / "policies").mkdir(parents=True)

        rc, out, err = _run_block(defaults, live)
        assert rc == 0, f"rc={rc} stderr={err!r}"
        assert "[WARN]" not in out
