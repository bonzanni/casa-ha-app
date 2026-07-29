"""The pre-push hook and the attestation it requires.

These were verified by hand at the shell but had no automated test, which is exactly the
kind of gap that rots: the hook is the only thing standing between a local mistake and an
irreversible publication.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "pre-push"
ATTEST = ROOT / "scripts" / "attest.sh"
ZERO = "0" * 40


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    return repo


def _commit(repo: Path, rel: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("body\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", rel], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _push(repo: Path, sha: str, env: dict | None = None):
    return subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        input=f"refs/heads/main {sha} refs/heads/main {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), **(env or {})},
    )


def _receipt(repo: Path, name: str, body: str) -> None:
    (repo / ".git" / name).write_text(body)


def test_a_gated_push_without_an_attestation_is_refused(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "not attested" in result.stderr


def test_a_push_touching_only_the_guard_is_gated(tmp_path):
    """PR-0's shape exactly: publication machinery and boundary prose, no corpus."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "CLAUDE.md")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "not attested" in result.stderr


def test_an_ordinary_source_push_is_gated_too(tmp_path):
    """This used to assert the opposite, which enshrined a real hole: a push adding
    private prose or an address to casa/ was an ordinary, fully-verified path to a public
    ref, gated by nothing. Every push to a public repo publishes."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "casa/rootfs/opt/casa/thing.py")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "not attested" in result.stderr


def test_an_empty_commit_is_gated(tmp_path):
    """It changes no file, so a path-based trigger let it through — but its MESSAGE,
    author and committer are published. A test previously asserted this was allowed."""
    repo = _repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "private text here"],
        check=True,
    )
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "not attested" in result.stderr


def test_an_empty_tip_does_not_hide_an_earlier_commit(tmp_path):
    """What the removed test was reaching for: the hook enumerates the range, so a push
    whose TIP is empty is still gated for the commits behind it."""
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "empty tip"],
                   check=True)
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert _push(repo, tip).returncode == 1


def test_a_non_branch_namespace_is_refused(tmp_path):
    """An arbitrary namespace can carry a tag object whose target is already reachable, so
    it introduces no commits and would slip past the enumeration entirely."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _tag_push(repo, "refs/heads/main", sha, remote_ref="refs/archive/x")
    assert result.returncode == 1
    assert "only refs/heads/* is allowed" in result.stderr


def test_the_automated_receipt_alone_does_not_authorise_a_push(tmp_path):
    """gate.sh proves the automated half only; the read and the review are separate, and
    an earlier design let the automated receipt stand in for both."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    assert _push(repo, sha).returncode == 1


def test_an_attested_tip_is_allowed(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-approved", f"{sha}\nread-in-full; reviewers=a,b\n")
    assert _push(repo, sha).returncode == 0


def test_a_stale_attestation_is_refused(tmp_path):
    """Applying a review finding makes a new commit; the old approval must not carry."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-approved", "f" * 40 + "\nold\n")
    assert _push(repo, sha).returncode == 1


def test_a_branch_deletion_is_not_gated(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    result = subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        input=f"(delete) {ZERO} refs/heads/main {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    assert result.returncode == 0


def test_an_explicit_override_is_allowed_and_announced(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _push(repo, sha, env={"CASA_GATE_OVERRIDE": "reason recorded in the PR"})
    assert result.returncode == 0
    assert "overridden" in result.stderr


# --- the attestation itself ----------------------------------------------------------

def _attest(repo: Path, *args):
    return subprocess.run(
        ["bash", str(ATTEST), *args], cwd=repo, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )


def test_attest_refuses_a_bare_invocation(tmp_path):
    """`attest.sh x` used to mint the receipt pre-push honours."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    result = _attest(repo)
    assert result.returncode == 1
    assert "--read-in-full" in result.stderr
    assert not (repo / ".git" / "casa-gate-approved").exists()


def test_attest_refuses_a_partial_claim(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    result = _attest(repo, "--read-in-full", "--reviewers", "a,b")
    assert result.returncode == 1
    assert not (repo / ".git" / "casa-gate-approved").exists()


def test_attest_refuses_without_a_matching_automated_receipt(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    result = _attest(repo, "--read-in-full", "--reviewers", "a,b",
                     "--findings-applied", "--re-reviewed")
    assert result.returncode == 1
    assert not (repo / ".git" / "casa-gate-approved").exists()


def test_attest_refuses_when_the_automated_receipt_is_for_another_commit(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", "f" * 40 + "\n")
    assert _attest(repo, "--read-in-full", "--reviewers", "a,b",
                   "--findings-applied", "--re-reviewed").returncode == 1


def test_attest_writes_the_receipt_for_the_repo_it_is_run_in(tmp_path):
    """It must touch the throwaway repo's receipt, never the real checkout's."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    result = _attest(repo, "--read-in-full", "--reviewers", "a,b",
                     "--findings-applied", "--re-reviewed")
    assert result.returncode == 0, result.stderr
    receipt = (repo / ".git" / "casa-gate-approved").read_text()
    assert receipt.splitlines()[0] == sha
    assert "reviewers=a,b" in receipt
    assert _push(repo, sha).returncode == 0


def test_attest_refuses_on_a_dirty_tree(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-automated", sha + "\n")
    (repo / "docs" / "architecture" / "a.md").write_text("changed\n")
    assert _attest(repo, "--read-in-full", "--reviewers", "a,b",
                   "--findings-applied", "--re-reviewed").returncode == 1


# --- tags are refused outright ---------------------------------------------------------

def _tag_push(repo: Path, local_ref: str, sha: str, remote_ref: str | None = None,
              env: dict | None = None):
    remote_ref = remote_ref or local_ref
    return subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
        input=f"{local_ref} {sha} {remote_ref} {ZERO}\n",
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), **(env or {})},
    )


def test_an_annotated_tag_push_is_refused(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/architecture/a.md")
    subprocess.run(["git", "-C", str(repo), "tag", "-a", "v1", "-m", "release"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "v1"],
                         capture_output=True, text=True, check=True).stdout.strip()
    result = _tag_push(repo, "refs/tags/v1", sha)
    assert result.returncode == 1
    assert "only refs/heads/* is allowed" in result.stderr


def test_a_lightweight_tag_push_is_refused(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    assert _tag_push(repo, "refs/tags/light", sha).returncode == 1


def test_a_branch_source_pushed_to_a_tag_destination_is_refused(tmp_path):
    """`main:refs/tags/x` publishes a tag while the SOURCE ref is a branch — a
    local-ref-only test waved this through."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _tag_push(repo, "refs/heads/main", sha, remote_ref="refs/tags/sneaky")
    assert result.returncode == 1
    assert "only refs/heads/* is allowed" in result.stderr


def test_a_tag_name_with_regex_metacharacters_is_still_refused(tmp_path):
    """The removed binding interpolated the name into a grep expression, so a name like
    `v1|.*` matched any receipt line."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    assert _tag_push(repo, "refs/tags/v1|.*", sha).returncode == 1


def test_an_attested_commit_receipt_does_not_authorise_a_tag(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    _receipt(repo, "casa-gate-approved", f"{sha}\nread-in-full; reviewers=a,b\n")
    assert _push(repo, sha).returncode == 0, "the commit push is fine"
    assert _tag_push(repo, "refs/tags/v1", sha).returncode == 1, "the tag is not"


def test_a_tag_push_can_be_overridden_with_a_reason(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs/architecture/a.md")
    result = _tag_push(repo, "refs/tags/v1", sha,
                       env={"CASA_GATE_OVERRIDE": "documented exception"})
    assert result.returncode == 0
    assert "overridden" in result.stderr
