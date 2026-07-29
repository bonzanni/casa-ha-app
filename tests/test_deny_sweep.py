"""One deny-sweep implementation, exercised the way all three consumers use it.

Every fixture uses SYNTHETIC tokens. The real pattern file denies e-mail addresses and
private ranges, and the real hook runs over this file at commit time while the full-tree
sweep runs over it at the gate — so a real literal here would make the guard reject its
own test suite, and would be a leak in its own right.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / "scripts" / "deny-sweep.sh"
REAL_DENY = ROOT / ".githooks" / "deny-patterns.txt"

# Models the real shape: a BROAD deny rule and a NARROW allow rule for one exact value.
# That is the only shape in which the whole-match rule matters — and the shape in which
# destructive substring substitution used to leak.
PATTERNS = """
[paths]
(^|/)zzforbidden-
[content]
[A-Za-z0-9]+@zztest\\.zzdomain
ZZ-DENIED-LITERAL-ZZ
[allow-content]
allowed@zztest\\.zzdomain
"""

SUPPLEMENT = """
[paths]
(^|/)zzprivate-
[content]
ZZ-PRIVATE-EXACT-ZZ
"""


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    deny = tmp_path / "deny.txt"  # OUTSIDE the repo: `git add -A` must never stage it
    deny.write_text(PATTERNS)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    return repo, deny


def _policy(repo: Path) -> None:
    """Write the policy files the sweep now requires, the way Task 7 generates them.

    Both fail CLOSED when absent, which is the point — so every fixture has to supply
    them, exactly as a real checkout does.
    """
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


def _commit(repo: Path, rel: str, body: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", rel], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _sweep(repo: Path, deny: Path, *args, supplement: Path | None = None):
    """The script must scan the repo it is RUN IN, not the one it lives in."""
    _policy(repo)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo), "CASA_DENY_FILE": str(deny)}
    if supplement is not None:
        env["CASA_DENY_SUPPLEMENT"] = str(supplement)
    return subprocess.run(
        ["bash", str(SWEEP), *args], cwd=repo, capture_output=True, text=True, env=env
    )


def test_the_sweep_scans_the_repo_it_is_run_in(tmp_path):
    repo, deny = _repo(tmp_path)
    _commit(repo, "docs/zzforbidden-marker.md", "x\n")
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 1
    assert "zzforbidden-marker" in result.stderr


def test_allow_content_exempts_the_exact_allowed_value(tmp_path):
    repo, deny = _repo(tmp_path)
    _commit(repo, "README.md", "maintainer: allowed@zztest.zzdomain\n")
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 0, result.stderr


def test_an_allow_rule_cannot_exempt_a_value_it_merely_prefixes(tmp_path):
    """The bypass both reviewers found in the running guard: allow rules used to be
    deleted from the text with `sed s///g`, so `xallowed@...` had the allowed substring
    removed and the remainder no longer matched. An allow rule must cover the WHOLE match
    or be irrelevant to it."""
    repo, deny = _repo(tmp_path)
    _commit(repo, "README.md", "contact: notallowed@zztest.zzdomain\n")
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 1, "a distinct address must not inherit the exemption"
    assert "notallowed@zztest.zzdomain" in result.stderr


def test_a_structurally_invalid_policy_fails_closed(tmp_path):
    """"Fails closed" has to cover an INVALID policy, not only an unreadable one: a blank
    file parsed into empty arrays while every check still reported success, and the policy
    file is itself excluded from the primary content sweep."""
    repo, deny = _repo(tmp_path)
    _commit(repo, "README.md", "fine\n")
    deny.write_text("")
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 2
    assert "malformed policy" in result.stderr or "no path or content rules" in result.stderr


def test_a_policy_missing_a_section_fails_closed(tmp_path):
    repo, deny = _repo(tmp_path)
    _commit(repo, "README.md", "fine\n")
    deny.write_text("[content]\nZZ-DENIED-LITERAL-ZZ\n")
    assert _sweep(repo, deny, "tree").returncode == 2


def test_a_missing_pattern_file_fails_closed(tmp_path):
    """It used to load zero rules and exit 0 — so a commit deleting the policy file
    disabled its own guard, and in staged mode the deletion is not even in the ACMR
    filter that would have shown it."""
    repo, _deny = _repo(tmp_path)
    _commit(repo, "README.md", "fine\n")
    result = _sweep(repo, tmp_path / "does-not-exist.txt", "tree")
    assert result.returncode == 2
    assert "failing closed" in result.stderr


def test_a_missing_supplement_fails_closed_when_one_is_named(tmp_path):
    repo, deny = _repo(tmp_path)
    _commit(repo, "README.md", "fine\n")
    result = _sweep(repo, deny, "tree", supplement=tmp_path / "no-supplement.txt")
    assert result.returncode == 2


def test_a_denied_literal_is_caught(tmp_path):
    repo, deny = _repo(tmp_path)
    _commit(repo, "README.md", "contact: ZZ-DENIED-LITERAL-ZZ\n")
    assert _sweep(repo, deny, "tree").returncode == 1


def test_an_allow_rule_cannot_blind_the_private_supplement(tmp_path):
    """The bypass round 8 found: allow-substitution runs before the GENERIC rules only.
    Supplement patterns see the raw text, so an allow entry cannot hide an exact literal.
    """
    repo, deny = _repo(tmp_path)
    supplement = tmp_path / "supplement.txt"
    supplement.write_text("[content]\nZZ-DENIED-LITERAL-ZZ-BUT-ALLOWED\n")  # supplement needs no sections
    _commit(repo, "README.md", "ZZ-DENIED-LITERAL-ZZ-BUT-ALLOWED\n")
    result = _sweep(repo, deny, "tree", supplement=supplement)
    assert result.returncode == 1
    assert "content(private)" in result.stderr


def test_staged_mode_sees_the_index(tmp_path):
    repo, deny = _repo(tmp_path)
    (repo / "README.md").write_text("contact: ZZ-DENIED-LITERAL-ZZ\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert _sweep(repo, deny, "staged").returncode == 1


def test_range_mode_catches_content_added_then_removed(tmp_path):
    """The whole point: the endpoint tree is clean, but the blob is published."""
    repo, deny = _repo(tmp_path)
    base = _commit(repo, "a.txt", "benign\n")
    _commit(repo, "leak.txt", "ZZ-DENIED-LITERAL-ZZ\n")
    (repo / "leak.txt").unlink()
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "remove"], check=True)

    assert _sweep(repo, deny, "tree").returncode == 0, "endpoint really is clean"
    assert _sweep(repo, deny, "range", f"{base}..HEAD").returncode == 1


def test_range_mode_catches_a_path_added_then_removed(tmp_path):
    repo, deny = _repo(tmp_path)
    base = _commit(repo, "a.txt", "benign\n")
    _commit(repo, "docs/zzforbidden-note.md", "harmless\n")
    (repo / "docs" / "zzforbidden-note.md").unlink()
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "remove"], check=True)

    assert _sweep(repo, deny, "tree").returncode == 0
    assert _sweep(repo, deny, "range", f"{base}..HEAD").returncode == 1


def test_messages_mode_sweeps_commit_messages(tmp_path):
    repo, deny = _repo(tmp_path)
    base = _commit(repo, "a.txt", "benign\n")
    (repo / "b.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "leak ZZ-DENIED-LITERAL-ZZ in the subject"],
        check=True,
    )
    assert _sweep(repo, deny, "messages", f"{base}..HEAD").returncode == 1


def test_an_invalid_pattern_is_fatal_not_silent(tmp_path):
    repo, deny = _repo(tmp_path)
    deny.write_text("[paths]\nzz-never\n[content]\n[unclosed\n[allow-content]\n")
    _commit(repo, "README.md", "fine\n")
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 2
    assert "invalid pattern" in result.stderr


def test_a_valid_pattern_is_not_reported_invalid(tmp_path):
    """`status=$?` captured after `!` is always 0, which made every real rule read as
    invalid and the whole sweep fail closed."""
    repo, deny = _repo(tmp_path)
    deny.write_text("[paths]\nzz-never\n[content]\n^definitely-not-present-anywhere$\n[allow-content]\n")
    _commit(repo, "README.md", "fine\n")
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 0, result.stderr


def test_the_pattern_file_in_use_is_excluded_from_the_content_sweep(tmp_path):
    """Whichever file CASA_DENY_FILE names, not just the canonical path.

    The appended literal goes under an explicit [content] header. An earlier version of
    this test appended after [allow-content], so the literal became an ALLOW rule and the
    test passed for a reason unrelated to the exclusion it claimed to prove.
    """
    repo, deny = _repo(tmp_path)
    inside = repo / "patterns.txt"
    inside.write_text(PATTERNS + "\n[content]\nZZ-DENIED-LITERAL-ZZ\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "patterns"], check=True)
    assert _sweep(repo, inside, "tree").returncode == 0


def test_a_trivially_broad_allow_rule_is_refused(tmp_path):
    """One `.*` under [allow-content] would exempt every finding there is."""
    repo, deny = _repo(tmp_path)
    deny.write_text("[paths]\nzz-never\n[content]\nZZ-DENIED-LITERAL-ZZ\n[allow-content]\n.*\n")
    _commit(repo, "README.md", "contact: ZZ-DENIED-LITERAL-ZZ\n")
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 2
    assert "broad" in result.stderr


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("[paths]", "[content]", "[allow-content]"):
            current = stripped
            out.setdefault(current, [])
        elif current:
            out[current].append(stripped)
    return out


def test_the_real_pattern_file_parses_into_populated_sections():
    sections = _sections(REAL_DENY.read_text())
    assert sections["[paths]"], "no path patterns"
    assert sections["[content]"], "no content patterns"
    assert sections["[allow-content]"], "no allow patterns"


def test_every_real_pattern_compiles_under_the_enforcing_engine():
    """`grep -E` is what enforces these; Python's `re` accepts a different grammar."""
    for _section, patterns in _sections(REAL_DENY.read_text()).items():
        for pattern in patterns:
            result = subprocess.run(
                ["grep", "-E", pattern], input="", capture_output=True, text=True
            )
            assert result.returncode <= 1, f"grep -E rejects /{pattern}/: {result.stderr}"


def test_no_real_allow_rule_is_trivially_broad():
    """Allow rules are whole-match exemptions now, not sed substitutions, so the old
    no-slash restriction is obsolete. What matters is that none of them is broad enough
    to exempt an unrelated finding."""
    canaries = ["CANARY-9f3c", "nobody@nowhere" + ".invalid", "10.11.12" + ".13"]
    for pattern in _sections(REAL_DENY.read_text()).get("[allow-content]", []):
        for canary in canaries:
            result = subprocess.run(
                ["grep", "-qxE", "--", pattern], input=canary, capture_output=True, text=True
            )
            assert result.returncode != 0, f"/{pattern}/ whole-matches {canary!r}"


def test_the_public_pattern_file_carries_no_address_like_text():
    """It is excluded from the generic content sweep; keep it from becoming a hiding place."""
    for line in REAL_DENY.read_text().splitlines():
        if not line.strip().startswith("#"):
            continue
        # A complete address shape, not a bare `@` — the comments legitimately discuss
        # fragments like `123+someone@` when explaining why a domain-wide allow is unsafe.
        assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", line), (
            f"comment carries an address: {line}"
        )
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", line), line


# --- coverage the reviewers found missing in the running guard -----------------------

def test_a_binary_blob_is_refused_when_not_allowlisted(tmp_path):
    """`git grep -I` skips binaries and patches say only "Binary files differ", so one NUL
    byte would otherwise make any payload invisible to every content rule."""
    repo, deny = _repo(tmp_path)
    blob = repo / "payload.bin"
    blob.write_bytes(b"\x00\x01secret-in-a-binary\x00")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "binary"], check=True)
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 1
    assert "payload.bin" in result.stderr
    assert "no content rule can inspect" in result.stderr


def test_an_allowlisted_binary_blob_passes(tmp_path):
    repo, deny = _repo(tmp_path)
    (repo / "payload.bin").write_bytes(b"\x00\x01reviewed\x00")
    hooks = repo / ".githooks"
    hooks.mkdir()
    (hooks / "binary-allowlist.txt").write_text("payload.bin\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "binary"], check=True)
    assert _sweep(repo, deny, "tree").returncode == 0


def test_range_mode_sees_content_introduced_by_a_merge_resolution(tmp_path):
    """`git log -p` emits no diff at all for a merge commit, so a secret created only by
    conflict resolution — and removed afterwards — was invisible to both range passes."""
    repo, deny = _repo(tmp_path)
    base = _commit(repo, "f.txt", "base\n")
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "side"], check=True)
    _commit(repo, "f.txt", "side\n")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-"], check=True)
    _commit(repo, "f.txt", "main\n")
    subprocess.run(["git", "-C", str(repo), "merge", "--no-commit", "side"],
                   capture_output=True)          # conflicts; resolution follows
    (repo / "f.txt").write_text("resolved ZZ-DENIED-LITERAL-ZZ\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "merge"], check=True)
    (repo / "f.txt").write_text("cleaned\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "cleanup"], check=True)

    assert _sweep(repo, deny, "tree").returncode == 0, "endpoint is clean"
    assert _sweep(repo, deny, "range", f"{base}..HEAD").returncode == 1


def test_a_secret_hidden_in_the_pattern_file_is_caught(tmp_path):
    """The pattern file is excluded from the sweep because it holds the rules themselves.
    Excluding the WHOLE file made it a hiding place; only its declared rule lines are
    exempt now, and the residue is scanned like any other file."""
    repo, deny = _repo(tmp_path)
    hooks = repo / ".githooks"
    hooks.mkdir()
    (hooks / "deny-patterns.txt").write_text(
        PATTERNS + "\n# innocuous looking comment: notallowed@zztest.zzdomain\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "patterns"], check=True)
    result = _sweep(repo, hooks / "deny-patterns.txt", "tree")
    assert result.returncode == 1
    assert "pattern-file residue" in result.stderr


def test_a_later_commit_cannot_retroactively_authorise_an_earlier_marker(tmp_path):
    """The allow-site policy is read from the commit being assessed. Comparing every
    commit's markers against the working-tree policy let a commit added later authorise a
    scanner-silencing marker introduced earlier in the same pushed range."""
    repo, deny = _repo(tmp_path)
    hooks = repo / ".githooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "gitleaks-allow-sites.txt").write_text("")
    base = _commit(repo, "a.txt", "benign\n")

    marker = "gitleaks" ":" "allow"
    _commit(repo, "sneaky.py", f"token = 'x'  # {marker}\n")     # unauthorised here
    (hooks / "gitleaks-allow-sites.txt").write_text("sneaky.py\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "authorise it after the fact"],
                   check=True)

    result = _sweep(repo, deny, "range", f"{base}..HEAD")
    assert result.returncode == 1, "the marker was unauthorised in the commit that added it"
    assert "sneaky.py" in result.stderr


def test_a_path_with_a_control_character_is_refused(tmp_path):
    """git C-quotes such a name, which silently defeats anchored path rules."""
    repo, deny = _repo(tmp_path)
    _commit(repo, "a.txt", "benign\n")
    weird = repo / "line\nbreak.txt"
    weird.write_bytes(b"x\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "weird"], check=True)
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 1
    assert "control characters" in result.stderr


def test_an_empty_file_is_not_treated_as_a_binary_blob(tmp_path):
    """`git grep -Il` does not list an empty file, so it fell into the binary set — every
    zero-byte s6 marker and any empty __init__.py was reported as unscannable. An empty
    file carries nothing and can hide nothing."""
    repo, deny = _repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    _commit(repo, "a.txt", "benign\n")
    result = _sweep(repo, deny, "tree")
    assert result.returncode == 0, result.stderr
    assert "__init__.py" not in result.stderr
