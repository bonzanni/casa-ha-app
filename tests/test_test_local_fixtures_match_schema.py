"""The test-local harness fixtures must track config.yaml's option schema.

Why this exists (#262): when a release removes an add-on option, the running
add-on is protected — setup-configs.sh prunes the stored key — but nothing
detected the *harness* still carrying it. That rot is not cosmetic. v0.125.0
removed `webhook_auth_enabled` and made webhook auth mandatory, yet
test-local/init-overrides/01-setup-configs.sh kept branching on it with a
`// false` default and deleted /data/webhook_secret in the else branch. Because
the default fixture omitted the key, every plain `start_container` booted into
a secretless state production can no longer reach. The same class of leftover
(v0.125.0 dropped `delivery_mode` but left eight callers in the E-block e2e)
kept CI red for three releases.

Scope note: this asserts on option *keys parsed from the JSON fixtures*, not on
a grep of test-local/**. A text search would fire on the comments that
legitimately name a removed option to explain why it is gone — including the
ones in 01-setup-configs.sh and test_voice_sse.sh written alongside this test.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_YAML = REPO_ROOT / "casa" / "config.yaml"
SETUP_CONFIGS = (
    REPO_ROOT / "casa" / "rootfs" / "etc" / "s6-overlay" / "scripts"
    / "setup-configs.sh"
)
FIXTURES = (
    # Gitignored (.gitignore:14) — a developer's local file, absent in CI and
    # on a fresh checkout. Checked when present, skipped when not; asserting
    # unconditionally would make this test error everywhere but a dev box.
    REPO_ROOT / "test-local" / "options.json",
    REPO_ROOT / "test-local" / "options.auth.json",
    REPO_ROOT / "test-local" / "options.json.example",
)


def _load_fixture(fixture: Path) -> set[str]:
    if not fixture.exists():
        pytest.skip(f"{fixture.name} not present (gitignored local fixture)")
    return set(json.loads(fixture.read_text(encoding="utf-8")))


def _schema_keys() -> set[str]:
    """Option names under config.yaml's `schema:` block.

    Parsed textually rather than with a YAML loader to keep the unit gate free
    of a PyYAML dependency, matching the sibling setup-configs tests.
    """
    lines = CONFIG_YAML.read_text(encoding="utf-8").splitlines()
    keys: set[str] = set()
    in_schema = False
    for line in lines:
        if line.startswith("schema:"):
            in_schema = True
            continue
        if in_schema:
            # Dedented non-blank line ends the block.
            if line.strip() and not line.startswith((" ", "\t")):
                break
            match = re.match(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*):", line)
            if match:
                keys.add(match.group(1))
    assert keys, "parsed no option keys from config.yaml's schema block"
    return keys


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_fixture_keys_all_exist_in_schema(fixture: Path) -> None:
    """No fixture may carry an option config.yaml no longer declares."""
    fixture_keys = _load_fixture(fixture)
    schema = _schema_keys()
    unknown = sorted(fixture_keys - schema)
    assert not unknown, (
        f"{fixture.relative_to(REPO_ROOT)} carries option(s) absent from "
        f"casa/config.yaml's schema: {unknown}. Remove them — a fixture is the "
        "template contributors copy, and a stale key misrepresents the real "
        "option surface (#262)."
    )


def test_harness_env_export_reads_only_declared_options() -> None:
    """The harness env-export must not read options the add-on removed.

    Every option key 03-export-env.sh reads must still be declared in
    config.yaml's schema. Narrow by design: only the `jq -r '.<key>'` reads
    are checked, so prose naming a removed option stays legal.
    """
    export_env = (
        REPO_ROOT / "test-local" / "init-overrides" / "03-export-env.sh"
    )
    src = export_env.read_text(encoding="utf-8")
    schema = _schema_keys()

    read_keys = set(re.findall(r"jq -r ['\"]\.([A-Za-z_][A-Za-z0-9_]*)", src))
    assert read_keys, (
        "parsed no `jq -r '.<key>'` reads from 03-export-env.sh — the script's "
        "shape changed and this guard is now vacuous, not passing"
    )

    # The `for key in …` loop reads each listed name the same way.
    loop = re.search(r"for key in (.*?); do", src, re.DOTALL)
    assert loop, (
        "no `for key in …; do` loop found in 03-export-env.sh — this guard "
        "would silently stop covering the loop's option list"
    )
    loop_keys = {
        token for token in loop.group(1).split()
        if re.fullmatch(r"[a-z_][a-z0-9_]*", token)
    }
    assert loop_keys, "the `for key in …` loop parsed to no option names"
    read_keys |= loop_keys

    stale = sorted(read_keys - schema)
    assert not stale, (
        f"test-local/init-overrides/03-export-env.sh reads option(s) absent "
        f"from casa/config.yaml's schema: {stale}. The real svc-casa/run no "
        "longer exports them."
    )
