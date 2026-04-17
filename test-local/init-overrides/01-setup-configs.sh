#!/bin/sh
# Test override: reads options.json directly instead of bashio
CONFIG_DIR="/addon_configs/casa-agent"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

mkdir -p "$CONFIG_DIR/agents" "$CONFIG_DIR/workspace/.claude/skills" \
         "$CONFIG_DIR/workspace/plugins" "$CONFIG_DIR/workspace/mcp-servers" \
         "$DATA_DIR/sdk-sessions"

# ------------------------------------------------------------------
# One-shot migration: rename legacy display-name YAMLs to role-based.
# Mirrors the production migrate_rename logic in setup-configs.sh
# (without bashio; uses plain echo instead).
# ------------------------------------------------------------------
migrate_rename() {
    local old="$1"
    local new="$2"
    local canonical_role="$3"
    local old_peer="$4"
    local new_peer="$5"

    if [ -f "$CONFIG_DIR/agents/$old" ] && [ ! -f "$CONFIG_DIR/agents/$new" ]; then
        mv "$CONFIG_DIR/agents/$old" "$CONFIG_DIR/agents/$new"
        sed -i 's/\r$//' "$CONFIG_DIR/agents/$new"
        sed -i "s/^role:[[:space:]]*.*$/role: ${canonical_role}/" \
            "$CONFIG_DIR/agents/$new"
        sed -i "s/^  peer_name:[[:space:]]*${old_peer}[[:space:]]*$/  peer_name: ${new_peer}/" \
            "$CONFIG_DIR/agents/$new"
        echo "[INFO] Migrated $old -> $new"
    fi
}

migrate_rename "ellen.yaml" "assistant.yaml" "assistant" "ellen" "assistant"
migrate_rename "tina.yaml"  "butler.yaml"    "butler"    "tina"  "butler"

for f in agents/assistant.yaml agents/butler.yaml agents/subagents.yaml \
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

# Auto-generate webhook secret if auth is enabled
SECRET_FILE="$DATA_DIR/webhook_secret"
AUTH_ENABLED=$(jq -r '.webhook_auth_enabled // false' /data/options.json)
if [ "$AUTH_ENABLED" = "true" ]; then
    USER_SECRET=$(jq -r '.webhook_secret // empty' /data/options.json)
    if [ -n "$USER_SECRET" ]; then
        printf '%s' "$USER_SECRET" > "$SECRET_FILE"
    elif [ ! -f "$SECRET_FILE" ]; then
        head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48 > "$SECRET_FILE"
        echo "[INFO] Auto-generated webhook secret (see /data/webhook_secret)"
    fi
    echo "[INFO] Webhook authentication enabled."
else
    rm -f "$SECRET_FILE"
fi

echo "[INFO] Configuration setup complete (local test mode)."
