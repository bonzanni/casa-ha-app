#!/usr/bin/env bash
# Manual smoke for v0.13.0 claude_code driver on the N150. Not in CI.
# Prerequisite: N150 deployed with v0.13.0; real `claude` CLI authenticated;
# TELEGRAM_ENGAGEMENT_SUPERGROUP_ID set; bot promoted with can_manage_topics.
set -euo pipefail

CONTAINER="${CONTAINER:-addon_c071ea9c_casa-agent}"

echo "Step 1: Dump /opt/casa/claude-plugins/base contents (Tier 1 baseline)"
docker exec "$CONTAINER" ls /opt/casa/claude-plugins/base/

echo
echo "Step 2: Engage hello-driver via direct MCP call inside the container."
echo "(Requires flipping hello-driver enabled=true in /addon_configs/casa-agent/agents/executors/hello-driver/definition.yaml)"
echo "Manually edit that file, then restart the addon, then run:"
echo "  docker exec $CONTAINER /opt/casa/venv/bin/python /opt/casa/scripts/smoke_engage_hello.py"
echo "(smoke_engage_hello.py is a 10-line helper calling engage_executor)"

echo
echo "Step 3: Observe topic post in engagement supergroup. Confirm:"
echo "  (a) A new topic named '#[hello-driver] ...' appears."
echo "  (b) 'Remote control: https://...' posts within 60s."

echo
echo "Step 4: Open the URL in Claude iOS app; confirm session attaches."

echo
echo "Step 5: Send '/mock emit_completion' from iOS; confirm topic closes."

echo
echo "Step 6: Verify: docker exec $CONTAINER cat /data/engagements.json | python3 -m json.tool"
echo "  — the hello-driver engagement should NOT be in the file (tombstone drops COMPLETED)."
echo
echo "Step 7: Confirm Honcho got the executor-type summary:"
echo "  Check Honcho via its UI or API for peer 'executor:hello-driver'"
echo "  — one new session with the completion summary."
echo
echo "Smoke complete."
