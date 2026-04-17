#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

NAME="casa-voice-ws-$$"
trap "stop_container $NAME" EXIT

build_image
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

# Invoke the python driver. Requires aiohttp on host (already a Casa dep
# via the tests/ suite).
HOST_PORT="$HOST_PORT" python3 "$HERE/test_voice_ws.py"
