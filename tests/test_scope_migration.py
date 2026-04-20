"""Tests for the 3.1 setup-configs.sh migration: inject scope metadata."""

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


_ASSISTANT_MINIMAL = """\
name: Ellen
role: assistant
model: opus
personality: |
  You are Ellen.
memory:
  token_budget: 4000
  read_strategy: per_turn
"""


_ASSISTANT_ALREADY_HAS_SCOPES = """\
name: Ellen
role: assistant
model: opus
personality: |
  You are Ellen.
memory:
  token_budget: 4000
  read_strategy: per_turn
  scopes_owned: [custom, scope]
  scopes_readable: [custom, scope]
# casa: scopes v1
"""


def _run_migration(tmp_path: Path, filename: str, default_owned: str,
                   default_readable: str) -> subprocess.CompletedProcess:
    """Extract migrate_scope_metadata from setup-configs.sh and run it."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("migrate_scope_metadata() {")
    end = text.index("\n}\n", start) + 3
    func = text[start:end]
    script = textwrap.dedent(f"""\
        bashio::log.info() {{ echo "[INFO] $*"; }}
        bashio::log.error() {{ echo "[ERROR] $*"; }}
        {func}
        migrate_scope_metadata "{(tmp_path / filename).as_posix()}" \\
            "{default_owned}" "{default_readable}"
    """)
    assert _BASH is not None, "bash unavailable; should have skipped"
    return subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, check=False,
    )


@pytest.mark.skipif(_BASH is None, reason="functional bash not available")
class TestMigrateScopeMetadata:
    def test_injects_missing_fields(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_text(_ASSISTANT_MINIMAL, encoding="utf-8")
        r = _run_migration(
            tmp_path, "assistant.yaml",
            "[personal, business, finance]",
            "[personal, business, finance, house]",
        )
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "scopes_owned:" in text
        assert "scopes_readable:" in text
        assert "personal" in text
        assert "business" in text
        assert "# casa: scopes v1" in text

    def test_idempotent_on_re_run(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_text(_ASSISTANT_MINIMAL, encoding="utf-8")
        _run_migration(
            tmp_path, "assistant.yaml",
            "[personal, business, finance]",
            "[personal, business, finance, house]",
        )
        once = f.read_text(encoding="utf-8")
        _run_migration(
            tmp_path, "assistant.yaml",
            "[personal, business, finance]",
            "[personal, business, finance, house]",
        )
        twice = f.read_text(encoding="utf-8")
        assert once == twice
        assert twice.count("# casa: scopes v1") == 1

    def test_preserves_existing_scopes(self, tmp_path):
        """Marker already present → function must short-circuit, leaving
        whatever the user has set for scopes_owned/readable intact."""
        f = tmp_path / "assistant.yaml"
        f.write_text(_ASSISTANT_ALREADY_HAS_SCOPES, encoding="utf-8")
        _run_migration(
            tmp_path, "assistant.yaml",
            "[personal, business, finance]",
            "[personal, business, finance, house]",
        )
        text = f.read_text(encoding="utf-8")
        assert "[custom, scope]" in text
        assert "[personal, business, finance]" not in text

    def test_crlf_input_works(self, tmp_path):
        """Windows-edited file (\\r\\n line endings) still migrates cleanly."""
        f = tmp_path / "assistant.yaml"
        f.write_bytes(_ASSISTANT_MINIMAL.replace("\n", "\r\n").encode("utf-8"))
        r = _run_migration(
            tmp_path, "assistant.yaml",
            "[personal, business, finance]",
            "[personal, business, finance, house]",
        )
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "scopes_owned:" in text
        assert "# casa: scopes v1" in text

    def test_missing_file_is_no_op(self, tmp_path):
        r = _run_migration(
            tmp_path, "nonexistent.yaml",
            "[personal]", "[personal]",
        )
        assert r.returncode == 0, r.stderr
        assert not (tmp_path / "nonexistent.yaml").exists()

    def test_no_memory_block_injects_one(self, tmp_path):
        """YAML without a memory: block at all — inject one with just scopes."""
        f = tmp_path / "a.yaml"
        f.write_text(
            "name: Test\nrole: assistant\nmodel: opus\npersonality: x\n",
            encoding="utf-8",
        )
        r = _run_migration(
            tmp_path, "a.yaml",
            "[personal]", "[personal, house]",
        )
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "memory:" in text
        assert "scopes_owned:" in text
        assert "# casa: scopes v1" in text

    def test_butler_defaults(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text(_ASSISTANT_MINIMAL.replace("role: assistant",
                                                "role: butler"),
                     encoding="utf-8")
        r = _run_migration(tmp_path, "butler.yaml", "[house]", "[house]")
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "scopes_owned: [house]" in text or "scopes_owned:\n  - house" in text
        assert "# casa: scopes v1" in text

    def test_malformed_yaml_logs_error_and_skips(self, tmp_path):
        """Malformed YAML: log ERROR, do not block startup (exit 0)."""
        f = tmp_path / "broken.yaml"
        f.write_text("name: Test\n  this is : not\n:indent aligned\n",
                     encoding="utf-8")
        r = _run_migration(
            tmp_path, "broken.yaml",
            "[personal]", "[personal]",
        )
        # Migration must not fail startup on a malformed file.
        assert r.returncode == 0, r.stderr
        # Marker must NOT have been appended — the parse failed.
        assert "# casa: scopes v1" not in f.read_text(encoding="utf-8")
