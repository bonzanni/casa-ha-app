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
         "$CONFIG_DIR/agents/specialists" \
         "$CONFIG_DIR/agents/executors" \
         "$CONFIG_DIR/policies" \
         "$CONFIG_DIR/schema" \
         "$CONFIG_DIR/workspace/.claude/skills" \
         "$CONFIG_DIR/workspace/plugins" \
         "$CONFIG_DIR/workspace/mcp-servers" \
         "$DATA_DIR/sdk-sessions" \
         "$DATA_DIR/casa-s6-services" \
         "$DATA_DIR/engagements"

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
        [ "$name" = "specialists" ] && continue
        [ "$name" = "executors" ] && continue
        seed_agent_dir "$src" "$CONFIG_DIR/agents/$name"
    done
fi

if [ -d "$DEFAULTS_DIR/agents/specialists" ]; then
    for src in "$DEFAULTS_DIR/agents/specialists"/*/; do
        [ -d "$src" ] || continue
        name=$(basename "$src")
        seed_agent_dir "$src" "$CONFIG_DIR/agents/specialists/$name"
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

if [ ! -f "$CONFIG_DIR/policies/scopes.yaml" ] \
   && [ -f "$DEFAULTS_DIR/policies/scopes.yaml" ]; then
    cp "$DEFAULTS_DIR/policies/scopes.yaml" \
       "$CONFIG_DIR/policies/scopes.yaml"
    bashio::log.info "Seeded policies/scopes.yaml"
fi

# Pre-1.0.0 doctrine (see memory/feedback_ship_gate_doctrine.md): no
# migration blocks in this script. Breaking changes just update the
# defaults; the overlay at /addon_configs/casa-agent/ is expected to
# be wiped across updates in development mode. This keeps
# setup-configs.sh lean. Revisit when v1.0.0 ships.

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

# ------------------------------------------------------------------
# 3.2: opportunistic embedding-model pre-warm (non-fatal if offline).
# ------------------------------------------------------------------

if [ ! -d "$DATA_DIR/fastembed" ]; then
    mkdir -p "$DATA_DIR/fastembed"
    bashio::log.info "Priming fastembed cache at $DATA_DIR/fastembed (first boot)"
    FASTEMBED_CACHE_PATH="$DATA_DIR/fastembed" python3 -c "
from fastembed import TextEmbedding
TextEmbedding(model_name='intfloat/multilingual-e5-large')
print('fastembed model cached')
" 2>&1 || bashio::log.warning "fastembed pre-warm failed; ScopeRegistry will retry at Python init or degrade"
fi

# --- Plan 4b: plugin consumer infrastructure bootstrap ----------------------

# Seed the user-writable marketplace overlay (idempotent — only if absent).
if [ ! -f /addon_configs/casa-agent/marketplace/.claude-plugin/marketplace.json ]; then
    mkdir -p /addon_configs/casa-agent/marketplace/.claude-plugin
    cp /opt/casa/defaults/marketplace-user/.claude-plugin/marketplace.json \
       /addon_configs/casa-agent/marketplace/.claude-plugin/marketplace.json
    bashio::log.info "Seeded user marketplace at /addon_configs/casa-agent/marketplace/"
fi

# Ensure casa-main's HOME is cc-home (required by binding layer + CC CLI).
export HOME=/addon_configs/casa-agent/cc-home
mkdir -p "$HOME/.claude"

# Trigger seed-marketplace auto-register into cc-home's in-memory view.
# The API call fails with the bogus key; the startup path runs first.
# Exit 0 expected. Spike §Key learning 7 — plain `claude plugin ...` calls
# do NOT run full startup, so this `claude -p` is load-bearing.
ANTHROPIC_API_KEY=sk-ant-bootstrap-noop \
  claude -p "noop" --allow-dangerously-skip-permissions >/dev/null 2>&1 || true

# Register the user marketplace in casa-main's HOME. Idempotent.
claude plugin marketplace add /addon_configs/casa-agent/marketplace/ \
  --scope user 2>/dev/null || true

# For every plugin referenced by defaults/agents/**/plugins.yaml,
# ensure it's installed into cc-home so `claude plugin list --json`
# (used by the binding layer in /opt/casa/plugins_binding.py) sees it.
# An advisory flock serializes against any concurrent Configurator
# install_casa_plugin calls (spike §Key learning 5).
INSTALL_LOCK=/addon_configs/casa-agent/cc-home/.claude/plugins/.install.lock
mkdir -p "$(dirname "$INSTALL_LOCK")"

if command -v yq >/dev/null 2>&1; then
    shopt -s globstar 2>/dev/null || true
    for plugin_ref in $(yq -r '.plugins[] | "\(.name)@\(.marketplace)"' \
                           /opt/casa/defaults/agents/**/plugins.yaml \
                           /opt/casa/defaults/agents/executors/*/plugins.yaml \
                           2>/dev/null | sort -u); do
        if [ -z "$plugin_ref" ]; then continue; fi
        flock "$INSTALL_LOCK" claude plugin install "$plugin_ref" --scope user \
            >/dev/null 2>&1 \
            || bashio::log.warning "plugin install skipped: $plugin_ref"
    done
else
    bashio::log.warning "yq not found — skipping default-plugin install loop"
fi

bashio::log.info "Configuration setup complete."
