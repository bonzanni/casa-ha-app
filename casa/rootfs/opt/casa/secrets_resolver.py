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
