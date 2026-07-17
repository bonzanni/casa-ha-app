#!/usr/bin/env bash
# Select a bounded, aiohttp-capable Python for host-side e2e helpers.

set -u

if [ "$#" -ne 1 ]; then
    echo "usage: resolve_python.sh SHARED_REPO_ROOT" >&2
    exit 2
fi

shared_repo_root="$1"
check_timeout="${E2E_PYTHON_CHECK_TIMEOUT:-5}"

if [ -n "${E2E_PYTHON:-}" ]; then
    candidate="$E2E_PYTHON"
elif [ -x "$shared_repo_root/venv_test/bin/python3" ]; then
    candidate="$shared_repo_root/venv_test/bin/python3"
else
    candidate="$(command -v python3 2>/dev/null || true)"
fi

if [ -z "$candidate" ]; then
    echo "no Python 3 interpreter found for HA e2e" >&2
    exit 1
fi
if [ ! -x "$candidate" ]; then
    echo "selected E2E Python is not executable: $candidate" >&2
    exit 1
fi
if ! command -v timeout >/dev/null 2>&1; then
    echo "timeout command is required for the HA e2e dependency check" >&2
    exit 1
fi

timeout --kill-after=1 "$check_timeout" \
    "$candidate" -c 'import aiohttp' >/dev/null 2>&1
status=$?
if [ "$status" -eq 0 ]; then
    printf '%s\n' "$candidate"
    exit 0
fi
if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    echo "E2E Python aiohttp check timed out after $check_timeout seconds" >&2
else
    echo "selected E2E Python cannot import aiohttp; install aiohttp" >&2
fi
exit 1
