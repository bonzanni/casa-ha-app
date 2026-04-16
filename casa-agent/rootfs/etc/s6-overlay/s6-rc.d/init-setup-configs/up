#!/command/with-contenv bashio

CONFIG_DIR="/addon_configs/casa-agent"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

# Create directory structure (idempotent)
mkdir -p "$CONFIG_DIR/agents" "$CONFIG_DIR/workspace/.claude/skills" \
         "$CONFIG_DIR/workspace/plugins" "$CONFIG_DIR/workspace/mcp-servers" \
         "$DATA_DIR/sdk-sessions"

# Copy defaults ONLY if not already present (first boot)
for f in agents/ellen.yaml agents/tina.yaml agents/subagents.yaml \
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

bashio::log.info "Configuration setup complete."
