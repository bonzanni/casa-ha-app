"""Tests for the 2.3 setup-configs.sh migration: tts + voice_errors injection."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "casa-agent" / "rootfs" \
    / "etc" / "s6-overlay" / "scripts" / "setup-configs.sh"


def _find_bash() -> str | None:
    """Return a path to a functional bash, or None."""
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


def _run_migration(tmp_path: Path) -> None:
    """Execute just the 2.3 migrate_voice_fields block from setup-configs.sh.

    We extract the bash function and invoke it against the tmp agents dir.
    Keeps the test isolated from bashio / s6 requirements.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    # Pull out the single function we're testing + call it.
    start = text.index("migrate_voice_fields() {")
    end = text.index("\n}\n", start) + 3
    func = text[start:end]
    script = textwrap.dedent(f"""\
        {func}
        migrate_voice_fields "{(tmp_path / "butler.yaml").as_posix()}"
    """)
    assert _BASH is not None, "bash unavailable; should have skipped"
    result = subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_BASH is None, reason="functional bash not available")
class TestMigrateVoiceFields:
    def test_injects_tts_when_absent(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text("name: Tina\nrole: butler\nmodel: haiku\n", encoding="utf-8")
        _run_migration(tmp_path)
        text = f.read_text(encoding="utf-8")
        assert "tts:" in text
        assert "tag_dialect: square_brackets" in text

    def test_leaves_existing_tts_alone(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text(
            "name: Tina\nrole: butler\nmodel: haiku\n"
            "tts:\n  tag_dialect: parens\n",
            encoding="utf-8",
        )
        _run_migration(tmp_path)
        assert "tag_dialect: parens" in f.read_text(encoding="utf-8")

    def test_injects_voice_errors_when_absent(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text("name: Tina\nrole: butler\nmodel: haiku\n", encoding="utf-8")
        _run_migration(tmp_path)
        text = f.read_text(encoding="utf-8")
        assert "voice_errors:" in text
        assert "timeout:" in text

    def test_idempotent(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text("name: Tina\nrole: butler\nmodel: haiku\n", encoding="utf-8")
        _run_migration(tmp_path)
        once = f.read_text(encoding="utf-8")
        _run_migration(tmp_path)
        twice = f.read_text(encoding="utf-8")
        assert once == twice
