"""Tests for the 3.1 upgrade-path fix: inject channels for pre-2.1 residents."""

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


_NO_CHANNELS = """\
name: Ellen
role: assistant
model: opus
personality: |
  You are Ellen.
memory:
  token_budget: 4000
  read_strategy: per_turn
"""


def _run_migration(tmp_path: Path, filename: str,
                   default_channels: str) -> subprocess.CompletedProcess:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("migrate_channels() {")
    end = text.index("\n}\n", start) + 3
    func = text[start:end]
    script = textwrap.dedent(f"""\
        bashio::log.info() {{ echo "[INFO] $*"; }}
        bashio::log.error() {{ echo "[ERROR] $*"; }}
        {func}
        migrate_channels "{(tmp_path / filename).as_posix()}" "{default_channels}"
    """)
    assert _BASH is not None
    return subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, check=False,
    )


@pytest.mark.skipif(_BASH is None, reason="functional bash not available")
class TestMigrateChannels:
    def test_injects_missing_channels(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_text(_NO_CHANNELS, encoding="utf-8")
        r = _run_migration(tmp_path, "assistant.yaml", "[telegram, webhook]")
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "channels:" in text
        assert "telegram" in text
        assert "webhook" in text
        assert "# casa: channels v1" in text

    def test_idempotent_on_re_run(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_text(_NO_CHANNELS, encoding="utf-8")
        _run_migration(tmp_path, "assistant.yaml", "[telegram, webhook]")
        once = f.read_text(encoding="utf-8")
        _run_migration(tmp_path, "assistant.yaml", "[telegram, webhook]")
        twice = f.read_text(encoding="utf-8")
        assert once == twice
        assert twice.count("# casa: channels v1") == 1

    def test_preserves_existing_channels_when_marker_present(self, tmp_path):
        existing = (
            "name: Ellen\nrole: assistant\nmodel: opus\n"
            "personality: |\n  You are Ellen.\n"
            "channels: [custom_transport]\n"
            "# casa: channels v1\n"
        )
        f = tmp_path / "assistant.yaml"
        f.write_text(existing, encoding="utf-8")
        _run_migration(tmp_path, "assistant.yaml", "[telegram, webhook]")
        text = f.read_text(encoding="utf-8")
        assert "custom_transport" in text
        assert "telegram" not in text

    def test_preserves_existing_channels_without_marker(self, tmp_path):
        """If channels already present but marker missing — legacy user
        who hand-wrote channels — leave channels alone, add marker."""
        existing = (
            "name: Ellen\nrole: assistant\nmodel: opus\n"
            "personality: |\n  You are Ellen.\n"
            "channels: [only_my_channel]\n"
        )
        f = tmp_path / "assistant.yaml"
        f.write_text(existing, encoding="utf-8")
        _run_migration(tmp_path, "assistant.yaml", "[telegram, webhook]")
        text = f.read_text(encoding="utf-8")
        assert "only_my_channel" in text
        assert "telegram" not in text  # defaults NOT injected
        assert "# casa: channels v1" in text

    def test_crlf_input_works(self, tmp_path):
        f = tmp_path / "assistant.yaml"
        f.write_bytes(_NO_CHANNELS.replace("\n", "\r\n").encode("utf-8"))
        r = _run_migration(tmp_path, "assistant.yaml", "[telegram, webhook]")
        assert r.returncode == 0, r.stderr
        text = f.read_text(encoding="utf-8")
        assert "channels:" in text
        assert "# casa: channels v1" in text

    def test_missing_file_is_no_op(self, tmp_path):
        r = _run_migration(tmp_path, "nonexistent.yaml", "[telegram]")
        assert r.returncode == 0, r.stderr
        assert not (tmp_path / "nonexistent.yaml").exists()

    def test_explicit_empty_channels_replaced(self, tmp_path):
        """`channels: []` without marker — treat as missing, inject defaults."""
        f = tmp_path / "assistant.yaml"
        f.write_text(
            "name: Ellen\nrole: assistant\nmodel: opus\n"
            "personality: |\n  You are Ellen.\n"
            "channels: []\n",
            encoding="utf-8",
        )
        _run_migration(tmp_path, "assistant.yaml", "[telegram, webhook]")
        text = f.read_text(encoding="utf-8")
        assert "telegram" in text
        # The literal `channels: []` line is replaced or removed.
        assert "channels: [telegram, webhook]" in text or \
               "channels:\n  - telegram\n  - webhook\n" in text
        assert "# casa: channels v1" in text
