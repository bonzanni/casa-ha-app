#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

NAME="casa-voice-ws-$$"
trap "stop_container $NAME" EXIT

build_image
# #193 (v0.117.0): the WS route is fail-CLOSED, so the smoke driver must sign
# its upgrade — boot the auth-ON fixture. The driver also asserts that an
# UNSIGNED upgrade is refused with 401.
start_authed_container "$NAME" >/dev/null
wait_healthy "$NAME"

# Invoke the python driver. Requires aiohttp on host (already a Casa dep
# via the tests/ suite).
HOST_PORT="$HOST_PORT" WEBHOOK_SECRET_E2E="$WEBHOOK_SECRET_E2E" \
    python3 "$HERE/test_voice_ws.py"
