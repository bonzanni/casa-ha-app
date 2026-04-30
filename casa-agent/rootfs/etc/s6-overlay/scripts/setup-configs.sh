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

# === drift-check: begin =========================================
# E-C drift report (v0.29.0).
#
# The seed_agent_dir() helper above is no-op when the destination dir
# already exists — meaning every default-side change shipped via
# /opt/casa/defaults/ since the last operator wipe is silently dark on
# this addon's persistent /addon_configs/casa-agent/ overlay. Master
# CI runs against fresh volumes and never exercises the upgrade-over-
# existing-overlay path, so this drift is invisible to all gates.
#
# Walks the agents/ + policies/ default trees, byte-compares each file
# against its live counterpart, and logs WARNING per drifted or
# missing-in-live file plus a one-line summary. Operator decides when
# to wipe (Phase Z uninstall+reinstall per memory
# feedback_phase_z_via_uninstall).
#
# See docs/bug-review-2026-04-30-exploration2.md::E-C for the v0.26.1 →
# v0.27.0 → v0.28.0 dark-state evidence that drove this block.
#
# Pure POSIX sh (no `local`, no process substitution) so this block
# can be unit-tested via `sh -c` parallel to the seed-copy block.

# _dc_log_*: portable wrappers — bashio in production, printf in tests.
_dc_log_warn() {
    if command -v bashio >/dev/null 2>&1; then
        bashio::log.warning "$*"
    else
        printf '[WARN] %s\n' "$*"
    fi
}
_dc_log_info() {
    if command -v bashio >/dev/null 2>&1; then
        bashio::log.info "$*"
    else
        printf '[INFO] %s\n' "$*"
    fi
}

drift_count=0
missing_count=0
DRIFT_TMP=$(mktemp 2>/dev/null || echo /tmp/casa-drift-check.$$)

_drift_check_tree() {
    _default_root=$1
    _live_root=$2
    [ -d "$_default_root" ] || return 0
    [ -d "$_live_root" ] || return 0
    # diff -rq lists "Only in DIR: NAME" for one-sided files and
    # "Files A and B differ" for byte-mismatches. We ignore "Only in
    # live" (operator-added files are not drift).
    diff -rq "$_default_root" "$_live_root" > "$DRIFT_TMP" 2>/dev/null || true
    while IFS= read -r _line; do
        case "$_line" in
            "Only in $_default_root"*)
                _dc_log_warn "drift_check missing-in-live: $_line"
                missing_count=$((missing_count + 1))
                ;;
            "Files "*" differ")
                _dc_log_warn "drift_check drifted: $_line"
                drift_count=$((drift_count + 1))
                ;;
        esac
    done < "$DRIFT_TMP"
}

_drift_check_tree "$DEFAULTS_DIR/agents"   "$CONFIG_DIR/agents"
_drift_check_tree "$DEFAULTS_DIR/policies" "$CONFIG_DIR/policies"

if [ "$drift_count" -gt 0 ] || [ "$missing_count" -gt 0 ]; then
    _dc_log_warn "drift_check report: drifted=$drift_count missing=$missing_count"
    _dc_log_warn "drift_check: pre-1.0.0 wipe doctrine — run 'ha apps uninstall <slug> --remove-data' + reinstall to refresh defaults; operator-set options survive."
else
    _dc_log_info "drift_check report: clean (no drift vs defaults)"
fi
rm -f "$DRIFT_TMP"
unset drift_count missing_count DRIFT_TMP _default_root _live_root _line
# === drift-check: end ===========================================

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

# Persist CC CLI conversation transcripts across container rebuilds.
# The bundled CC CLI uses $HOME=/root → ~/.claude/projects/<cwd-encoded>/<sid>.jsonl;
# /root/ is wiped on every rebuild, so the SDK's --resume <sid> path fails on
# every first turn after a deploy (sessions.json under /data/ persists, but the
# transcript files vanish — see agent.py resume-recovery comment). Symlink the
# projects dir to a path under /addon_configs/ (persistent volume) so transcripts
# survive rebuilds and resume just works.
PERSIST_PROJECTS="$CONFIG_DIR/cc-home/.claude/projects"
mkdir -p "$PERSIST_PROJECTS" /root/.claude
if [ -e /root/.claude/projects ] && [ ! -L /root/.claude/projects ]; then
    cp -R /root/.claude/projects/. "$PERSIST_PROJECTS/" 2>/dev/null || true
    rm -rf /root/.claude/projects
fi
[ -L /root/.claude/projects ] || ln -s "$PERSIST_PROJECTS" /root/.claude/projects

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

# === github-token: begin ========================================
# v0.14.9: resolve op://VAULT/GitHub/credential at boot, export to s6
# container env so every supervised service + engagement subprocess
# inherits $GITHUB_TOKEN automatically. The token is consumed by
# /opt/casa/scripts/git-credential-casa.sh (wired in /etc/gitconfig)
# at git auth time — never written to disk.
#
# If 1P credentials aren't configured, leave $GITHUB_TOKEN unset →
# public-only mode: anonymous github clones still work via the
# /etc/gitconfig SSH→HTTPS rewrite; private clones return 404/403.
OP_TOK="$(bashio::config 'onepassword_service_account_token')"
VAULT="$(bashio::config 'onepassword_default_vault')"
GH_TOKEN=""
if [ -n "$OP_TOK" ] && [ "$OP_TOK" != "null" ] \
   && [ -n "$VAULT" ] && [ "$VAULT" != "null" ]; then
    GH_TOKEN=$(OP_SERVICE_ACCOUNT_TOKEN="$OP_TOK" \
        op read "op://${VAULT}/GitHub/credential" 2>/dev/null) || GH_TOKEN=""
fi
if [ -n "$GH_TOKEN" ]; then
    # s6-overlay's /run/s6/container_environment/<NAME> is read once at
    # service-spawn time and merged into each child process's env.
    # File mode 0600 root-only — same protection level as /data/ secrets.
    umask 077
    printf "%s" "$GH_TOKEN" > /run/s6/container_environment/GITHUB_TOKEN
    umask 022
    bashio::log.info "GitHub access: token-authenticated (public + private per PAT scope)"
else
    rm -f /run/s6/container_environment/GITHUB_TOKEN
    bashio::log.info "GitHub access: anonymous (public only)"
fi
unset OP_TOK VAULT GH_TOKEN
# === github-token: end ==========================================

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

    # Build-time `claude plugin install` against $CLAUDE_CODE_PLUGIN_CACHE_DIR
    # leaves enabled=false in the seed's installed_plugins.json. The binding
    # layer (plugins_binding.py::build_sdk_plugins) filters out enabled=false
    # entries, so without this enable loop, engagements get plugins=[] even
    # though all 5 are present. At runtime against cc-home (--scope user),
    # `claude plugin enable` flips the flag persistently and is idempotent.
    HOME="$CC_HOME" claude plugin enable superpowers@casa-plugins-defaults    >/dev/null 2>&1 || true
    HOME="$CC_HOME" claude plugin enable plugin-dev@casa-plugins-defaults     >/dev/null 2>&1 || true
    HOME="$CC_HOME" claude plugin enable skill-creator@casa-plugins-defaults  >/dev/null 2>&1 || true
    HOME="$CC_HOME" claude plugin enable mcp-server-dev@casa-plugins-defaults >/dev/null 2>&1 || true
    HOME="$CC_HOME" claude plugin enable document-skills@casa-plugins-defaults >/dev/null 2>&1 || true
    _sc_log "Enabled $(HOME="$CC_HOME" claude plugin list --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(1 for p in d if p.get('enabled')))") of $(HOME="$CC_HOME" claude plugin list --json 2>/dev/null | python3 -c "import json,sys; print(len(json.load(sys.stdin)))") seeded plugins"
fi
# === seed-copy: end =============================================

# v0.14.9: replaced by the seed-copy block above. Default plugins are
# baked at image build time; user-marketplace plugins are installed on
# demand by Configurator's `install_casa_plugin` MCP tool, which goes
# through /etc/gitconfig + git-credential-casa.sh.

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
