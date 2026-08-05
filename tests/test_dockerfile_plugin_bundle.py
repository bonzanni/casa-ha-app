"""Dockerfile structural guards for the deterministic bundled-artifact build
(spec 3.6): the build helper runs BEFORE the broad `COPY rootfs /`, and no
marketplace / seed machinery survives."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent
_DOCKERFILE = (_REPO / "casa" / "Dockerfile").read_text(encoding="utf-8")
_TEST_DOCKERFILE = (_REPO / "test-local"
                    / "Dockerfile.test").read_text(encoding="utf-8")


def test_build_helper_runs_before_broad_copy():
    # Match the COMMAND at line-start (not the comment that mentions it).
    build = _DOCKERFILE.find("\nRUN python3 /opt/casa/scripts/build_plugin_bundle.py")
    broad_copy = _DOCKERFILE.find("\nCOPY rootfs /\n")
    assert build != -1 and broad_copy != -1
    assert build < broad_copy, "bundle build must precede COPY rootfs / (cache)"


def test_no_claude_plugin_or_seed_env():
    assert "claude plugin" not in _DOCKERFILE
    assert "CLAUDE_CODE_PLUGIN_SEED_DIR" not in _DOCKERFILE
    assert "claude-seed" not in _DOCKERFILE
    assert "marketplace-defaults" not in _DOCKERFILE


def test_bundle_dir_is_read_only():
    assert "chmod -R a-w /opt/casa/plugin-bundle" in _DOCKERFILE


def _narrow_copy_line(dockerfile: str, prefix: str) -> str:
    """The narrow COPY of bundle-build inputs, located by its first two
    entries (plugin_registry.py then plugin_store.py)."""
    marker = (f"\nCOPY {prefix}plugin_registry.py {prefix}plugin_store.py")
    start = dockerfile.find(marker)
    assert start != -1, "bundle-stage narrow COPY line not found"
    return dockerfile[start:dockerfile.index("\n", start + 1)]


_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+(\w+)|from\s+(\w+)\s+import\s)", re.MULTILINE)
_CASA_SRC = _REPO / "casa" / "rootfs" / "opt" / "casa"


def _bundle_import_closure() -> set[str]:
    """Every top-level /opt/casa module STATICALLY reachable (module-scope OR
    lazy import) from the bundle build helper. Over-approximates on purpose:
    a lazy import that today's build never executes still lands in the COPY,
    so the next module someone adds fails HERE (unit gate) instead of at
    image-build time."""
    local = {p.stem for p in _CASA_SRC.glob("*.py")}
    seed = _CASA_SRC / "scripts" / "build_plugin_bundle.py"
    closure: set[str] = set()
    frontier = [seed]
    while frontier:
        text = frontier.pop().read_text(encoding="utf-8")
        for m in _IMPORT_RE.finditer(text):
            name = m.group(1) or m.group(2)
            if name in local and name not in closure:
                closure.add(name)
                frontier.append(_CASA_SRC / f"{name}.py")
    return closure


@pytest.mark.parametrize("dockerfile, prefix", [
    pytest.param(_DOCKERFILE, "rootfs/opt/casa/", id="casa/Dockerfile"),
    pytest.param(_TEST_DOCKERFILE, "casa/rootfs/opt/casa/",
                 id="test-local/Dockerfile.test"),
])
def test_narrow_copy_ships_the_whole_bundle_import_closure(dockerfile, prefix):
    """The bundle build runs BEFORE the broad `COPY rootfs /`, so every local
    module its import graph can reach must ride the narrow COPY — only the
    image build catches an omission (the unit gate runs against the full
    rootfs checkout and can't see it). This shipped broken TWICE with a
    known-string guard: v0.78.0 (text_util, then missed AGAIN in
    Dockerfile.test — QA red 2026-07-14) and v0.152.0 (plugin_events —
    no image published, deploy + QA red 2026-08-05). Both Dockerfiles carry
    their own copy of the line; compute the closure instead of naming files.
    Red case: drop any closure module (e.g. plugin_events.py) from either
    narrow COPY line and this test fails naming it."""
    line = _narrow_copy_line(dockerfile, prefix)
    missing = sorted(
        mod for mod in _bundle_import_closure()
        if f"{prefix}{mod}.py" not in line)
    assert not missing, (
        f"bundle-build import closure missing from narrow COPY: {missing}")
