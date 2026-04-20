#!/command/with-contenv bashio
# 5.5 item 3: strip ANSI from bashio output for clean docker logs.
export BASHIO_LOG_NO_COLORS=true
export NO_COLOR=1

CONFIG_DIR="/addon_configs/casa-agent"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

# ------------------------------------------------------------------
# Directory scaffolding (idempotent).
# ------------------------------------------------------------------

mkdir -p "$CONFIG_DIR/agents" \
         "$CONFIG_DIR/agents/executors" \
         "$CONFIG_DIR/policies" \
         "$CONFIG_DIR/schema" \
         "$CONFIG_DIR/workspace/.claude/skills" \
         "$CONFIG_DIR/workspace/plugins" \
         "$CONFIG_DIR/workspace/mcp-servers" \
         "$DATA_DIR/sdk-sessions"

# ------------------------------------------------------------------
# Seed defaults on first boot (directory-copy — "cp -r" of each agent
# dir if it doesn't already exist).
# ------------------------------------------------------------------

seed_agent_dir() {
    local src="$1"   # e.g. /opt/casa/defaults/agents/butler
    local dst="$2"   # e.g. /addon_configs/casa-agent/agents/butler
    if [ -d "$src" ] && [ ! -d "$dst" ]; then
        cp -r "$src" "$dst"
        bashio::log.info "Seeded agent dir: $(basename "$dst")"
    fi
}

if [ -d "$DEFAULTS_DIR/agents" ]; then
    for src in "$DEFAULTS_DIR/agents"/*/; do
        [ -d "$src" ] || continue
        name=$(basename "$src")
        [ "$name" = "executors" ] && continue
        seed_agent_dir "$src" "$CONFIG_DIR/agents/$name"
    done
fi

if [ -d "$DEFAULTS_DIR/agents/executors" ]; then
    for src in "$DEFAULTS_DIR/agents/executors"/*/; do
        [ -d "$src" ] || continue
        name=$(basename "$src")
        seed_agent_dir "$src" "$CONFIG_DIR/agents/executors/$name"
    done
fi

# Seed shared policy library.
if [ ! -f "$CONFIG_DIR/policies/disclosure.yaml" ] \
   && [ -f "$DEFAULTS_DIR/policies/disclosure.yaml" ]; then
    cp "$DEFAULTS_DIR/policies/disclosure.yaml" \
       "$CONFIG_DIR/policies/disclosure.yaml"
    bashio::log.info "Seeded policies/disclosure.yaml"
fi

# Seed schemas (overwrite on every boot — schemas ship with the Casa
# image and the image is the source of truth; hand-edits under
# /addon_configs/casa-agent/schema/ get clobbered by design).
if [ -d "$DEFAULTS_DIR/schema" ]; then
    cp "$DEFAULTS_DIR/schema"/*.json "$CONFIG_DIR/schema/" 2>/dev/null || true
    bashio::log.info "Refreshed schema files"
fi

# ------------------------------------------------------------------
# Initialize git repo (idempotent) + snapshot manual edits.
# ------------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    bashio::log.warning "git not installed — skipping config repo init"
elif [ ! -d "$CONFIG_DIR/.git" ]; then
    cd "$CONFIG_DIR"
    git init -q
    git config user.email "casa-agent@local"
    git config user.name  "Casa Agent"
    cat > .gitignore <<'EOF'
# Track configs only.
*
!agents/
!agents/**
!policies/
!policies/**
!schema/
!schema/**
!.gitignore
EOF
    git add .gitignore agents/ policies/ schema/ 2>/dev/null || true
    git commit -qm "initial config snapshot" 2>/dev/null || true
    bashio::log.info "Initialized config git repo at $CONFIG_DIR"
else
    # Idempotent boot-time snapshot of any uncommitted manual edits.
    cd "$CONFIG_DIR"
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        git add -A
        git commit -qm "manual edit (boot-time snapshot)"
        bashio::log.info "Snapshotted manual edits in config repo"
    fi
fi

# Clone skill/plugin repos
/opt/casa/scripts/sync-repos.sh

# Initialize session registry if missing
if [ ! -f "$DATA_DIR/sessions.json" ]; then
    echo '{}' > "$DATA_DIR/sessions.json"
fi

# Auto-generate webhook secret if auth is enabled and no secret is set.
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
    rm -f "$SECRET_FILE"
fi

bashio::log.info "Configuration setup complete."
