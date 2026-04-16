#!/command/with-contenv bashio

CONFIG_DIR="/addon_configs/casa-agent"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

# Create directory structure (idempotent)
mkdir -p "$CONFIG_DIR/agents" "$CONFIG_DIR/workspace/.claude/skills" \
         "$CONFIG_DIR/workspace/plugins" "$CONFIG_DIR/workspace/mcp-servers" \
         "$DATA_DIR/sdk-sessions"

# ------------------------------------------------------------------
# One-shot migration: rename legacy display-name YAMLs to role-based.
# Safe to re-run: only acts when the old file exists AND the new
# file does not.
# ------------------------------------------------------------------

migrate_rename() {
    local old="$1"
    local new="$2"
    local old_role="$3"
    local new_role="$4"
    local old_peer="$5"
    local new_peer="$6"

    if [ -f "$CONFIG_DIR/agents/$old" ] && [ ! -f "$CONFIG_DIR/agents/$new" ]; then
        mv "$CONFIG_DIR/agents/$old" "$CONFIG_DIR/agents/$new"
        if [ -n "$old_role" ] && [ -n "$new_role" ]; then
            sed -i "s/^role:[[:space:]]*${old_role}[[:space:]]*$/role: ${new_role}/" \
                "$CONFIG_DIR/agents/$new"
        fi
        sed -i "s/^  peer_name:[[:space:]]*${old_peer}[[:space:]]*$/  peer_name: ${new_peer}/" \
            "$CONFIG_DIR/agents/$new"
        bashio::log.info "Migrated $old -> $new"
    fi
}

migrate_rename "ellen.yaml" "assistant.yaml" "main" "assistant" "ellen" "assistant"
migrate_rename "tina.yaml"  "butler.yaml"    ""     ""          "tina"  "butler"

# ------------------------------------------------------------------
# Copy defaults ONLY if not already present (first boot or new files)
# ------------------------------------------------------------------

for f in agents/assistant.yaml agents/butler.yaml agents/subagents.yaml \
         schedules.yaml webhooks.yaml; do
    if [ ! -f "$CONFIG_DIR/$f" ]; then
        cp "$DEFAULTS_DIR/$f" "$CONFIG_DIR/$f"
        bashio::log.info "Created default config: $f"
    fi
done

# Clone skill/plugin repos
/opt/casa/scripts/sync-repos.sh

# Initialize session registry if missing
if [ ! -f "$DATA_DIR/sessions.json" ]; then
    echo '{}' > "$DATA_DIR/sessions.json"
fi

# Auto-generate webhook secret if auth is enabled and no secret is set
SECRET_FILE="$DATA_DIR/webhook_secret"
if bashio::config.true 'webhook_auth_enabled'; then
    USER_SECRET=$(bashio::config 'webhook_secret')
    if [ -n "$USER_SECRET" ]; then
        printf '%s' "$USER_SECRET" > "$SECRET_FILE"
    elif [ ! -f "$SECRET_FILE" ]; then
        head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48 > "$SECRET_FILE"
        bashio::log.info "Auto-generated webhook secret (see /data/webhook_secret)"
    fi
    bashio::log.info "Webhook authentication enabled."
else
    # Auth disabled - remove stale secret file so Python doesn't load it
    rm -f "$SECRET_FILE"
fi

bashio::log.info "Configuration setup complete."
