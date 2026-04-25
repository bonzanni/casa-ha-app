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

# Register both marketplaces in casa-main's HOME. Idempotent.
# - casa-plugins-defaults: read-only seed at /opt/casa/defaults/marketplace-defaults/.
#   Required so `<name>@casa-plugins-defaults` install refs in defaults/agents/**/plugins.yaml
#   resolve. Without this, the install loop below silently fails for every default plugin
#   (CC CLI: 'Plugin "<name>" not found in marketplace "casa-plugins-defaults"').
# - casa-plugins: user-writable overlay at /addon_configs/casa-agent/marketplace/.
claude plugin marketplace add /opt/casa/defaults/marketplace-defaults/ \
  --scope user 2>/dev/null || true
claude plugin marketplace add /addon_configs/casa-agent/marketplace/ \
  --scope user 2>/dev/null || true

# For every plugin referenced by defaults/agents/**/plugins.yaml,
# ensure it's installed into cc-home so `claude plugin list --json`
# (used by the binding layer in /opt/casa/plugins_binding.py) sees it.
# An advisory flock serializes against any concurrent Configurator
# install_casa_plugin calls (spike §Key learning 5).
INSTALL_LOCK=/addon_configs/casa-agent/cc-home/.claude/plugins/.install.lock
mkdir -p "$(dirname "$INSTALL_LOCK")"

# === seed-copy: begin ===========================================
# v0.14.9: replace the boot install loop with a no-network seed copy.
# /opt/claude-seed/ is image-baked at Dockerfile build time and contains
# the full CC CLI install state for the 5 default plugins. We populate
# cc-home from it on first boot only (idempotent — sentinel is the
# presence of installed_plugins.json in cc-home's plugin dir).
#
# Spike result (Task D.1, 2026-04-25): option (a) symlink works.
# CC CLI tolerates installPath via symlink — `claude plugin list --json`
# resolves all 5 plugins via the seed cache. If you find this is broken
# in a future image, fall back to full copy + installPath rewrite per
# spec §4 option (b).
SEED_DIR="${SEED_DIR:-/opt/claude-seed}"
CC_HOME="${CC_HOME:-/addon_configs/casa-agent/cc-home}"
CC_PLUGINS_DIR="$CC_HOME/.claude/plugins"

# _sc_log: portable wrapper — uses bashio in production, printf in test envs.
# (bashio::log.info uses '::' which is a bash function-name extension not
#  valid in POSIX sh; defining a helper keeps the block sh-compatible.)
_sc_log() { if command -v bashio >/dev/null 2>&1; then bashio::log.info "$*"; else printf '[INFO] %s\n' "$*"; fi; }

if [ -d "$SEED_DIR" ] && [ ! -f "$CC_PLUGINS_DIR/installed_plugins.json" ]; then
    _sc_log "Seeding cc-home plugin state from $SEED_DIR"
    mkdir -p "$CC_PLUGINS_DIR/cache"
    # Symlink the seed cache dir; CC CLI tolerates installPath via symlink
    # (verified by Task D.1 spike). Pre-1.0.0 wipe-on-update means both
    # endpoints survive together.
    if [ ! -e "$CC_PLUGINS_DIR/cache/casa-plugins-defaults" ]; then
        ln -s "$SEED_DIR/cache/casa-plugins-defaults" \
              "$CC_PLUGINS_DIR/cache/casa-plugins-defaults"
    fi
    # Copy install state. installed_plugins.json contains absolute paths
    # under $SEED_DIR/cache/...; the symlink above makes them resolvable.
    cp "$SEED_DIR/installed_plugins.json" "$CC_PLUGINS_DIR/installed_plugins.json"
    # known_marketplaces.json + marketplaces/ are merged with what
    # `claude plugin marketplace add` already wrote — only copy if absent.
    if [ ! -f "$CC_PLUGINS_DIR/known_marketplaces.json" ]; then
        cp "$SEED_DIR/known_marketplaces.json" "$CC_PLUGINS_DIR/known_marketplaces.json"
    fi
    if [ ! -d "$CC_PLUGINS_DIR/marketplaces" ]; then
        cp -r "$SEED_DIR/marketplaces" "$CC_PLUGINS_DIR/marketplaces"
    fi
    _sc_log "Seeded cc-home with $(ls "$CC_PLUGINS_DIR/cache/casa-plugins-defaults/" 2>/dev/null | wc -l) default plugins"
fi
# === seed-copy: end =============================================

if command -v yq >/dev/null 2>&1; then
    shopt -s globstar 2>/dev/null || true
    for plugin_ref in $(yq -r '.plugins[] | "\(.name)@\(.marketplace)"' \
                           /opt/casa/defaults/agents/**/plugins.yaml \
                           /opt/casa/defaults/agents/executors/*/plugins.yaml \
                           2>/dev/null | sort -u); do
        if [ -z "$plugin_ref" ]; then continue; fi
        # Capture stderr into the warning so future failures stay diagnosable
        # instead of cryptic "skipped" lines hiding the CC CLI's real reason.
        install_err=$(flock "$INSTALL_LOCK" \
            claude plugin install "$plugin_ref" --scope user 2>&1 >/dev/null) \
            || bashio::log.warning "plugin install skipped: $plugin_ref — ${install_err:-no stderr}"
    done
else
    bashio::log.warning "yq not found — skipping default-plugin install loop"
fi

# --- Plan 4b: plugin-runtime tool dir + PATH propagation (P-9) -------------
# Ensure the persistent tools bin dir exists, and add it to PATH for every
# s6-supervised service (casa-main, svc-casa-mcp, engagements). Writing to
# /run/s6/container_environment/PATH is how s6-overlay propagates env to
# children; /etc/profile.d/* is NOT sourced by non-interactive services.
TOOLS_ROOT=/addon_configs/casa-agent/tools
TOOLS_BIN="$TOOLS_ROOT/bin"
mkdir -p "$TOOLS_BIN"

# Merge TOOLS_BIN into s6 container env PATH (takes precedence over /usr/local/bin).
CURRENT_PATH="${PATH}"
if ! printf "%s" "$CURRENT_PATH" | grep -q "^\(.*:\)\?${TOOLS_BIN}\(:\|$\)"; then
    NEW_PATH="$TOOLS_BIN:$CURRENT_PATH"
    printf "%s" "$NEW_PATH" > /run/s6/container_environment/PATH
fi

# Drop any legacy profile.d leftover from earlier drafts. Safe on fresh install.
rm -f /etc/profile.d/casa-tools.sh

# --- Plan 4b: system-requirements reconciliation (§4.3.4) -----------------
# Reconciler runs the declared install strategy for every plugin tool that is
# missing after upgrade (persistent volume survives, but failures / user-wipes
# happen). Non-blocking — degrades affected plugins, never crashes boot.
MANIFEST=/addon_configs/casa-agent/system-requirements.yaml
STATUS_FILE=/addon_configs/casa-agent/system-requirements.status.yaml
if [ -f "$MANIFEST" ]; then
    python3 /opt/casa/scripts/reconcile_system_requirements.py \
        --manifest "$MANIFEST" \
        --tools-root "$TOOLS_ROOT" \
        --status-file "$STATUS_FILE" \
        --log-level warning \
        || bashio::log.warning \
          "system-requirements reconciliation had failures — see $STATUS_FILE"
fi

bashio::log.info "Configuration setup complete."
