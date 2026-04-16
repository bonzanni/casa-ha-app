#!/bin/sh
# Test override: reads options.json directly instead of bashio
OPTIONS=/data/options.json

TOKEN=$(jq -r '.claude_oauth_token // empty' "$OPTIONS")
if [ -z "$TOKEN" ]; then
    echo "[FATAL] claude_oauth_token is required."
    exit 1
fi

echo "[INFO] Configuration validated (local test mode)."
