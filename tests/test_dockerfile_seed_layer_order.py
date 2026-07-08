"""L31: the network-bound plugin-seed install must precede the broad rootfs
COPY so a routine code change does not re-run the 5 GitHub plugin installs.

Also asserts every COPY before the seed layer references only narrow, rarely
changing inputs — so nobody reintroduces a broad COPY above the seed.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]

# Narrow inputs allowed to be COPY'd before the seed install.
_ALLOWED_PRE_SEED_COPY = (
    "requirements.txt",
    "etc/gitconfig",
    "git-credential-casa.sh",
    "defaults/marketplace-defaults",
    "defaults/marketplace-user",
    "mock-claude-sdk",
)


def _lines(rel: str) -> list[str]:
    return (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "dockerfile, broad_copy_re",
    [
        ("casa-agent/Dockerfile", re.compile(r"^COPY\s+rootfs\s+/\s*$")),
        ("test-local/Dockerfile.test", re.compile(r"^COPY\s+casa-agent/rootfs\s+/\s*$")),
    ],
)
def test_seed_install_precedes_broad_rootfs_copy(dockerfile, broad_copy_re):
    lines = _lines(dockerfile)
    idx_seed = next(
        (i for i, ln in enumerate(lines) if "claude plugin marketplace add" in ln),
        None,
    )
    idx_broad = next(
        (i for i, ln in enumerate(lines) if broad_copy_re.match(ln)),
        None,
    )
    assert idx_seed is not None, f"{dockerfile}: no seed install line found"
    assert idx_broad is not None, f"{dockerfile}: no broad rootfs COPY found"
    assert idx_seed < idx_broad, (
        f"{dockerfile}: the seed install (line {idx_seed + 1}) must precede the "
        f"broad rootfs COPY (line {idx_broad + 1}) so a code change does not "
        f"re-run the 5 GitHub plugin installs"
    )

    # Every COPY before the seed must reference only narrow, allowed inputs.
    for i in range(idx_seed):
        ln = lines[i].strip()
        if ln.startswith("COPY "):
            assert any(tok in ln for tok in _ALLOWED_PRE_SEED_COPY), (
                f"{dockerfile}: broad/unexpected COPY before seed layer: {ln!r}"
            )
