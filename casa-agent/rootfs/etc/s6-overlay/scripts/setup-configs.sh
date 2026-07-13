#!/command/with-contenv bashio
# 5.5 item 3: strip ANSI from bashio output for clean docker logs.
export BASHIO_LOG_NO_COLORS=true
export NO_COLOR=1

CONFIG_DIR="/config"
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

# Pre-1.0.0 doctrine (see memory/feedback_ship_gate_doctrine.md): no
# migration blocks in this script. Breaking changes just update the
# defaults; the overlay at /config/ is expected to
# be wiped across updates in development mode. This keeps
# setup-configs.sh lean. Revisit when v1.0.0 ships.
#
# Narrow exception: v0.37.4 c1-relay-migration (below) injects the
# engagement_permission_relay PreToolUse policy into pre-existing
# claude_code executor hooks.yaml files that the seed-copy
# directory-level skip leaves untouched. Required because the v0.37.2
# C-1 fix is a runtime contract (no operator keyboard = silent broken
# permission relay); a wipe-on-update would work but burns operator
# state for every install path. Idempotent grep-then-append; no
# version-gated shims, no marker files, no backups — same lean shape
# as the seed-copy block. Remove when v1.0.0 ships and the migration
# doctrine takes over.

# === c1-relay-migration: begin ==================================
# v0.37.4: backfill engagement_permission_relay into pre-existing
# claude_code executor hooks.yaml files. See spec
# 2026-05-13-c1-permission-relay-fix.md §4.6, memory
# project_v037_2_v037_3_c1_shipped.md follow-up #1, and
# reference_migrations_vs_seed_order (seed_agent_dir is dir-level
# no-op when the executor dir already exists, so file-level edits
# need a migration).

# _crm_log: portable wrapper — bashio in production, printf in tests.
# Defined locally because the seed-copy's _sc_log is declared later
# in this script.
_crm_log() { if command -v bashio >/dev/null 2>&1; then bashio::log.info "$*"; else printf '[INFO] %s\n' "$*"; fi; }

if [ -d "$CONFIG_DIR/agents/executors" ]; then
    for _crm_exec_dir in "$CONFIG_DIR/agents/executors"/*/; do
        [ -d "$_crm_exec_dir" ] || continue
        _crm_hooks="$_crm_exec_dir/hooks.yaml"
        _crm_def="$_crm_exec_dir/definition.yaml"
        [ -f "$_crm_hooks" ] || continue
        [ -f "$_crm_def" ] || continue
        # Driver gate: only claude_code executors get this policy. The
        # hook resolves engagement-id from cwd against
        # ^/data/engagements/<32-hex>$ which only claude_code
        # subprocess cwd matches — in_casa drivers (e.g. configurator)
        # would deny every tool call.
        grep -qE '^driver:[[:space:]]*claude_code[[:space:]]*$' "$_crm_def" || continue
        # Idempotency gate: the policy name appears nowhere else in
        # a normal hooks.yaml, so a substring grep is sufficient and
        # cheaper than YAML parsing.
        grep -q 'engagement_permission_relay' "$_crm_hooks" && continue
        # Append the stanza + marker comment. Relies on pre_tool_use
        # being the last (or only) top-level key — true for all
        # shipped defaults pre-v0.37.2.
        cat >> "$_crm_hooks" <<'CASA_C1_RELAY_EOF'
  # casa-migration:c1-relay
  - policy: engagement_permission_relay
    matcher: ".*"
    timeout: 600
CASA_C1_RELAY_EOF
        _crm_log "Migrated $(basename "$_crm_exec_dir") hooks.yaml: added engagement_permission_relay"
    done
    unset _crm_exec_dir _crm_hooks _crm_def
fi
# === c1-relay-migration: end ====================================

# === deprecated-options-prune: begin ===========================
# HA Supervisor only WARNS (it does not crash) when stored add-on options
# carry a key no longer in config.yaml's schema:. Per HA docs, delete the
# stale key via the add-on options API — bashio::addon.option <key> with no
# value deletes it (/usr/lib/bashio/addons.sh:537). Warning-level hygiene;
# casa already ignores unknown option keys.
#
# ADDITIVE LIST: when you REMOVE an option from config.yaml (options:/schema:),
# add its key to DEPRECATED_OPTION_KEYS below. Idempotent — only deletes keys
# actually present, so it is a no-op on clean installs. Seeded from a full
# git-history audit of every option key ever removed (2026-06-08).
DEPRECATED_OPTION_KEYS="github_token heartbeat_enabled heartbeat_interval_minutes honcho_api_key honcho_api_url repos scope_threshold telegram_webhook_url subagent_model"
_dop_opts="$(bashio::addon.options 2>/dev/null || echo '{}')"
for _dop_key in $DEPRECATED_OPTION_KEYS; do
    if bashio::jq.exists "$_dop_opts" ".${_dop_key}"; then
        if bashio::addon.option "$_dop_key"; then
            bashio::log.info "Pruned deprecated add-on option: $_dop_key"
        else
            bashio::log.warning "Failed to prune deprecated add-on option: $_dop_key"
        fi
    fi
done
unset _dop_key _dop_opts
# === deprecated-options-prune: end =============================

# Seed schemas (overwrite on every boot — schemas ship with the Casa
# image and the image is the source of truth; hand-edits under
# /config/schema/ get clobbered by design).
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
    # Whitelist mirrors config_git._GITIGNORE_CONTENT — keep in sync; the
    # python side reconciles drift on every boot (P-3, v0.69.1).
    cat > .gitignore <<'EOF'
# Casa config repo — track configs only.
*
!agents/
!agents/**
!policies/
!policies/**
!schema/
!schema/**
# Unified plugin architecture (v0.71.0): the registry is config — the single
# plugin-assignment authority — and versioning it gives an audit trail.
# ONLY registry.json: the artifact store and staging under plugins/ are
# content-addressed binaries, never tracked.
!plugins/
!plugins/registry.json
plugins/store/
plugins/.staging/
!.gitignore
EOF
    git add -A 2>/dev/null || true
    git commit -qm "initial config snapshot" 2>/dev/null || true
    bashio::log.info "Initialized config git repo at $CONFIG_DIR"
else
    # Idempotent boot-time snapshot of any uncommitted manual edits.
    cd "$CONFIG_DIR"
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        if git add -A && git commit -qm "manual edit (boot-time snapshot)"; then
            bashio::log.info "Snapshotted manual edits in config repo"
        else
            bashio::log.warning "Boot-time config snapshot failed (git error) — reconciler will fall back to .casabak"
        fi
    fi
fi

# ------------------------------------------------------------------
# Default-sync reconciler (three-way merge defaults → /config).
# Spec: docs/superpowers/specs/2026-06-08-config-sync-reconciler-design.md.
# Runs AFTER git-repo init (commit-first pre-sync needs /config/.git) and
# BEFORE svc-casa's load_all_agents. Subsumes the old seed_agent_dir block
# and the warn-only drift-check. Non-fatal by contract.
# ------------------------------------------------------------------
export CASA_CONFIG_DIR="$CONFIG_DIR"
export CASA_DEFAULTS_DIR="$DEFAULTS_DIR"
export CASA_DATA_DIR="$DATA_DIR"
export CASA_IMAGE_VERSION="$(bashio::addon.version 2>/dev/null || echo unknown)"
# D1 (2026-07-09 bug review): config_sync's post-sync boot-parity pass runs the
# real agent loader, which resolves ${PRIMARY_AGENT_MODEL}/${VOICE_AGENT_MODEL}
# in runtime.yaml via resolve_model(). svc-casa/run exports these for the actual
# boot, but this oneshot runs in a separate s6 process that did NOT — so the
# validator saw the literal "${...}" and reported a bogus "Unknown model
# shortname" in config-sync-report.json. Export them here for env-parity with
# boot so the validation is faithful (a genuinely bad model still fails).
export PRIMARY_AGENT_MODEL="$(bashio::config 'primary_agent_model')"
export VOICE_AGENT_MODEL="$(bashio::config 'voice_agent_model')"
python3 /opt/casa/config_sync.py || bashio::log.warning "config_sync exited non-zero (non-fatal)"

# Initialize session registry if missing
if [ ! -f "$DATA_DIR/sessions.json" ]; then
    echo '{}' > "$DATA_DIR/sessions.json"
fi

# Persist CC CLI conversation transcripts across container rebuilds.
# As of v0.37.8 (H-1), HOME is propagated to cc-home via
# /run/s6/container_environment/HOME (see claude-home-propagation
# block below), so the CC CLI writes transcripts to
# cc-home/.claude/projects/ directly. This defensive symlink at
# /root/.claude/projects remains as belt-and-braces in case anything
# is ever invoked with explicit HOME=/root (the prior default).
# Pre-v0.37.8 history: CC CLI used $HOME=/root → /root/.claude/projects;
# /root/ is wiped on every rebuild, so --resume <sid> failed on the
# first turn after a deploy (sessions.json persisted, transcript file
# did not — see agent.py resume-recovery comment).
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

# --- cc-home HOME setup -----------------------------------------------------
# casa-main + the CC CLI both require HOME=cc-home. Plugin materialization
# (bundled-artifact import + registry seed/migration) now lives in the
# init-plugin-store s6 oneshot (plugin_boot.py), which runs AFTER this script
# and BEFORE svc-casa (unified plugin architecture §3.6). The marketplace seed,
# the load-bearing `claude -p noop`, and the marketplace registration/install
# loop are all removed with the marketplace itself.
export HOME=/config/cc-home
mkdir -p "$HOME/.claude"

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

# === claude-oauth-token: begin ==================================
# K-1 (v0.34.1): propagate Claude Code OAuth token to engagement
# subprocesses launched by claude_code_driver. Mirror of the
# GITHUB_TOKEN block above. Pre-fix the token was only exported into
# svc-casa's process env (svc-casa/run:13), which feeds casa_core
# itself but NOT s6-rc child services launched via `with-contenv`
# (which read /run/s6/container_environment/). Result: every
# claude_code_driver subprocess (plugin-developer) got
# "Not logged in · Please run /login" and produced no useful output.
# Latently broken since v0.13.0 (Plan 4a) — ~8 days.
#
# bug-review-2026-05-01-exploration4.md::K-1 has the full evidence
# chain. Fix shape: same op:// resolution path as GITHUB_TOKEN above,
# write the (possibly-resolved) token to container_environment with
# mode 0600.
CC_OAUTH="$(bashio::config 'claude_oauth_token')"
if [ -n "$CC_OAUTH" ] && [ "$CC_OAUTH" != "null" ]; then
    case "$CC_OAUTH" in
        op://*)
            OP_TOK2="$(bashio::config 'onepassword_service_account_token')"
            if [ -n "$OP_TOK2" ] && [ "$OP_TOK2" != "null" ]; then
                CC_OAUTH=$(OP_SERVICE_ACCOUNT_TOKEN="$OP_TOK2" \
                    op read "$CC_OAUTH" 2>/dev/null) || CC_OAUTH=""
            else
                CC_OAUTH=""
            fi
            unset OP_TOK2
            ;;
    esac
fi
if [ -n "$CC_OAUTH" ] && [ "$CC_OAUTH" != "null" ]; then
    umask 077
    printf "%s" "$CC_OAUTH" > /run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN
    umask 022
    bashio::log.info "Claude OAuth: token propagated to engagement subprocesses"
else
    rm -f /run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN
    bashio::log.warning "Claude OAuth not configured — claude_code_driver engagements will fail (K-1)"
fi
unset CC_OAUTH
# === claude-oauth-token: end ====================================

# === claude-home-propagation: begin =============================
# H-1 (v0.37.8): propagate HOME=cc-home to every s6-supervised service +
# child subprocess. A shell-level `export HOME=...` only governs this
# script's own process; casa-main and svc-casa-mcp boot with HOME=/root
# unless we write to /run/s6/container_environment/. cc-home is still the
# CC CLI's home for residents/specialists (SDK plugin loading via
# --plugin-dir, agent-home settings) and engagement subprocesses.
printf '%s' "/config/cc-home" \
    > /run/s6/container_environment/HOME
bashio::log.info "HOME propagated to s6 services: /config/cc-home"
# === claude-home-propagation: end ===============================

# Plugin materialization (bundled-artifact import → content-addressed store,
# registry seed + one-time migration) moved to the init-plugin-store s6
# oneshot (plugin_boot.py) under the unified plugin architecture (§3.6). The
# old /opt/claude-seed → cc-home seed-copy + `claude plugin enable` loop is
# deleted with the marketplace.

# --- Plan 4b: plugin-runtime tool dir + PATH propagation (P-9) -------------
# Ensure the persistent tools bin dir exists, and add it to PATH for every
# s6-supervised service (casa-main, svc-casa-mcp, engagements). Writing to
# /run/s6/container_environment/PATH is how s6-overlay propagates env to
# children; /etc/profile.d/* is NOT sourced by non-interactive services.
TOOLS_ROOT=/config/tools
TOOLS_BIN="$TOOLS_ROOT/bin"
mkdir -p "$TOOLS_BIN"

# Merge TOOLS_BIN into s6 container env PATH. NOTE: it is prepended ahead of
# the ENTIRE image PATH including /opt/casa/venv/bin — intentional for
# engagement tool overrides; core services must therefore exec the venv
# interpreter by absolute path (/opt/casa/venv/bin/python3), never bare python3.
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
MANIFEST=/config/system-requirements.yaml
STATUS_FILE=/config/system-requirements.status.yaml
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
