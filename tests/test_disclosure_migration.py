"""Tests for the 5.1 setup-configs.sh migration: disclosure-clause v2."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "casa-agent" / "rootfs" \
    / "etc" / "s6-overlay" / "scripts" / "setup-configs.sh"


def _find_bash() -> str | None:
    """Return a path to a functional bash, or None. Mirrors test_voice_migration."""
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


V1_DISCLOSURE = """\
name: Tina
role: butler
model: haiku
personality: |
  You are Tina.

  Disclosure:
  - Check <channel_context> each turn. On household-shared voice (anyone
    nearby can hear you), do not disclose financial details, medical
    information, contact details, personal schedule, or credentials.
    Defer: "I can tell you that privately on Telegram."
tts:
  tag_dialect: square_brackets
"""


def _run_migration(tmp_path: Path) -> subprocess.CompletedProcess:
    """Extract migrate_disclosure_clause from setup-configs.sh and run it."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("migrate_disclosure_clause() {")
    end = text.index("\n}\n", start) + 3
    func = text[start:end]
    script = textwrap.dedent(f"""\
        {func}
        migrate_disclosure_clause "{(tmp_path / "butler.yaml").as_posix()}"
    """)
    assert _BASH is not None, "bash unavailable; should have skipped"
    return subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, check=False,
    )


@pytest.mark.skipif(_BASH is None, reason="functional bash not available")
class TestMigrateDisclosureClause:
    def test_replaces_v1_block_with_v2(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text(V1_DISCLOSURE, encoding="utf-8")

        r = _run_migration(tmp_path)
        assert r.returncode == 0, r.stderr

        text = f.read_text(encoding="utf-8")
        # v1 single-line clause is gone
        assert "I can tell you that privately on Telegram." not in text
        # v2 markers present
        assert "Disclosure (on untrusted channels):" in text
        assert "Credentials — API keys, passwords" in text
        assert "I'll tell you that on Telegram." in text
        assert "Safe on any channel: device control" in text
        # marker written exactly once
        assert text.count("# casa: disclosure v2") == 1

    def test_preserves_prose_above_disclosure_block(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text(V1_DISCLOSURE, encoding="utf-8")
        _run_migration(tmp_path)
        text = f.read_text(encoding="utf-8")
        assert "You are Tina." in text

    def test_preserves_tts_block_below_disclosure(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text(V1_DISCLOSURE, encoding="utf-8")
        _run_migration(tmp_path)
        text = f.read_text(encoding="utf-8")
        assert "tts:" in text
        assert "tag_dialect: square_brackets" in text

    def test_idempotent_on_re_run(self, tmp_path):
        f = tmp_path / "butler.yaml"
        f.write_text(V1_DISCLOSURE, encoding="utf-8")
        _run_migration(tmp_path)
        once = f.read_text(encoding="utf-8")
        _run_migration(tmp_path)
        twice = f.read_text(encoding="utf-8")
        assert once == twice
        # Marker still exactly once, not duplicated.
        assert twice.count("# casa: disclosure v2") == 1

    def test_file_with_marker_already_is_left_untouched(self, tmp_path):
        """A butler.yaml already bearing the v2 marker is a no-op."""
        already_v2 = V1_DISCLOSURE + "# casa: disclosure v2\n"
        f = tmp_path / "butler.yaml"
        f.write_text(already_v2, encoding="utf-8")

        _run_migration(tmp_path)
        text = f.read_text(encoding="utf-8")
        # The v1 block survives unchanged because the marker short-circuits.
        assert "I can tell you that privately on Telegram." in text
        assert "Disclosure (on untrusted channels):" not in text

    def test_missing_file_is_no_op(self, tmp_path):
        """No butler.yaml → function returns cleanly without error."""
        r = _run_migration(tmp_path)
        assert r.returncode == 0, r.stderr
