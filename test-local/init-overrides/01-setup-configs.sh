#!/bin/sh
# Test override: reads options.json directly instead of bashio
CONFIG_DIR="/addon_configs/casa-agent"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

mkdir -p "$CONFIG_DIR/agents" "$CONFIG_DIR/workspace/.claude/skills" \
         "$CONFIG_DIR/workspace/plugins" "$CONFIG_DIR/workspace/mcp-servers" \
         "$DATA_DIR/sdk-sessions"

for f in agents/ellen.yaml agents/tina.yaml agents/subagents.yaml \
         schedules.yaml webhooks.yaml; do
    if [ ! -f "$CONFIG_DIR/$f" ]; then
        cp "$DEFAULTS_DIR/$f" "$CONFIG_DIR/$f"
        echo "[INFO] Created default config: $f"
    fi
done

# Skip repo sync in local test mode
echo "[INFO] Skipping repo sync (local test mode)."

if [ ! -f "$DATA_DIR/sessions.json" ]; then
    echo '{}' > "$DATA_DIR/sessions.json"
fi

# Auto-generate webhook secret if not set
SECRET_FILE="$DATA_DIR/webhook_secret"
USER_SECRET=$(jq -r '.webhook_secret // empty' /data/options.json)
if [ -n "$USER_SECRET" ]; then
    printf '%s' "$USER_SECRET" > "$SECRET_FILE"
elif [ ! -f "$SECRET_FILE" ]; then
    head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48 > "$SECRET_FILE"
    echo "[INFO] Auto-generated webhook secret (see /data/webhook_secret)"
fi

echo "[INFO] Configuration setup complete (local test mode)."
