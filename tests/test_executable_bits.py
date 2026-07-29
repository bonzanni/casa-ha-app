"""Every hook and script must carry its executable bit IN GIT, not just on disk.

A hook without mode 100755 is one git silently skips, and a script without it fails with
"Permission denied" on a fresh clone — while local tests that invoke it via `bash` keep
passing. Both were observed here: `scripts/run-gitleaks.sh` was committed 100644 and only
surfaced when the gate ran it directly after a `git reset --hard`.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUST_BE_EXECUTABLE = [
    ".githooks/pre-commit",
    ".githooks/pre-push",
    "scripts/deny-sweep.sh",
    "scripts/run-gitleaks.sh",
    "scripts/setup-dev.sh",
]


def _mode(rel: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "--", rel],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out, f"{rel} is not tracked"
    return out.split()[0]


def test_hooks_and_scripts_are_executable_in_git():
    wrong = {rel: _mode(rel) for rel in MUST_BE_EXECUTABLE if _mode(rel) != "100755"}
    assert not wrong, f"not executable in git: {wrong}"
