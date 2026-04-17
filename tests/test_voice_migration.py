"""Tests for the 2.3 setup-configs.sh migration: tts + voice_errors injection."""

import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "casa-agent" / "rootfs" \
    / "etc" / "s6-overlay" / "scripts" / "setup-configs.sh"


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
        migrate_voice_fields "{tmp_path / "butler.yaml"}"
    """)
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    subprocess.run(["bash", "--version"], capture_output=True).returncode != 0,
    reason="bash not available",
)
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
