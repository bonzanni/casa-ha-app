"""The pre-commit guard is a thin wrapper over scripts/deny-sweep.sh.

The grammar itself is covered by tests/test_deny_sweep.py; this file covers the wrapper:
that it runs the sweep against the repository being committed to, surfaces its refusal,
and fails loudly rather than silently when the sweep is missing.

Synthetic tokens only — see the note in tests/test_deny_sweep.py.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "pre-commit"

PATTERNS = """
[paths]
(^|/)zzforbidden-
[content]
ZZ-DENIED-LITERAL-ZZ
"""


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    deny = tmp_path / "deny.txt"  # OUTSIDE the repo, on purpose
    deny.write_text(PATTERNS)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    return repo, deny


def _stage(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)


def _policy(repo: Path) -> None:
    """The sweep's policy files fail closed when absent; supply them as a checkout does."""
    hooks = repo / ".githooks"
    hooks.mkdir(exist_ok=True)
    roots = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    (hooks / "root-allowlist.txt").write_text(
        "".join(f"{r}\n" for r in sorted(roots) if "/" not in r)
    )
    if not (hooks / "gitleaks-allow-sites.txt").exists():
        (hooks / "gitleaks-allow-sites.txt").write_text("")


def _run_hook(repo: Path, deny: Path, hook: Path = HOOK):
    """The project's hook resolves the project's sweep (relative to the hook), and the
    sweep then scans whichever repo it is run in — here, the throwaway one."""
    _policy(repo)
    return subprocess.run(
        ["bash", str(hook)], cwd=repo, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "CASA_DENY_FILE": str(deny)},
    )


def test_a_clean_commit_passes(tmp_path):
    repo, deny = _repo(tmp_path)
    _stage(repo, "docs/architecture/overview.md", "Casa turn loop.\n")
    result = _run_hook(repo, deny)
    assert result.returncode == 0, result.stderr


def test_a_denied_path_is_rejected(tmp_path):
    repo, deny = _repo(tmp_path)
    _stage(repo, "docs/zzforbidden-notes.md", "harmless text\n")
    result = _run_hook(repo, deny)
    assert result.returncode == 1
    assert "zzforbidden-notes.md" in result.stderr


def test_denied_content_is_rejected_with_the_wrapper_guidance(tmp_path):
    repo, deny = _repo(tmp_path)
    _stage(repo, "docs/architecture/overview.md", "token ZZ-DENIED-LITERAL-ZZ here\n")
    result = _run_hook(repo, deny)
    assert result.returncode == 1
    assert "ZZ-DENIED" in result.stderr
    assert "Redaction is not the fix" in result.stderr


def test_a_filename_with_a_space_is_handled(tmp_path):
    """Word-splitting on staged filenames would silently skip this file."""
    repo, deny = _repo(tmp_path)
    _stage(repo, "docs/architecture/a file.md", "ZZ-DENIED-LITERAL-ZZ\n")
    assert _run_hook(repo, deny).returncode == 1


def test_a_missing_sweep_fails_loudly(tmp_path):
    """A guard that silently no-ops when its implementation is absent is worse than none."""
    repo, deny = _repo(tmp_path)
    fake_hooks = tmp_path / "hooks"
    fake_hooks.mkdir()
    (fake_hooks / "pre-commit").write_text(HOOK.read_text())
    (tmp_path / "scripts").mkdir()  # exists, but contains no deny-sweep.sh
    _stage(repo, "docs/architecture/overview.md", "fine\n")
    result = _run_hook(repo, deny, hook=fake_hooks / "pre-commit")
    assert result.returncode == 1
    assert "missing or not executable" in result.stderr
