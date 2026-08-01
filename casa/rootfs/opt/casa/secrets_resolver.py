"""op:// reference resolution via the `op` CLI (§8.2).

OP_SERVICE_ACCOUNT_TOKEN must be in process env. lru_cache so repeated
resolution of the same reference during one addon run is cheap.
"""
from __future__ import annotations

import logging
import subprocess
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def resolve(value: str) -> str:
    if not value or not value.startswith("op://"):
        return value
    try:
        out = subprocess.run(
            ["op", "read", value],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return out.stdout.rstrip("\n")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Failed to resolve {value!r} via op read: {exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timeout resolving {value!r}") from exc
    except OSError as exc:
        # #345: an absent/broken `op` binary raises FileNotFoundError (an
        # OSError) from subprocess.run. Callers handle only RuntimeError —
        # translate so a missing binary degrades with a warning instead of
        # aborting secret-consuming startup.
        raise RuntimeError(f"op CLI unavailable resolving {value!r}: {exc}") from exc


# Bound at import time so invalidate_cache always reaches the REAL cached
# function even when tests monkeypatch the `resolve` module attribute.
_CACHED_RESOLVE = resolve


def invalidate_cache() -> None:
    """#345: drop every cached resolution so the next :func:`resolve` re-reads
    1Password. Reload paths call this first — without it a rotated field keeps
    feeding the revoked plaintext (cached under the unchanged op:// reference)
    for the container's lifetime while the reload reports success."""
    _CACHED_RESOLVE.cache_clear()
