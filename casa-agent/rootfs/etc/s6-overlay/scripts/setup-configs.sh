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

# v0.8.5: refresh scope corpora to the keyword-style descriptions.
# Marker prevents re-running on every boot. Manual edits made by the
# user (or future Builder) AFTER the marker is written are preserved.
SCOPE_MIGRATION_MARKER="$CONFIG_DIR/migrations/scope_corpus_v0.8.5.applied"
if [ ! -f "$SCOPE_MIGRATION_MARKER" ] \
   && [ -f "$CONFIG_DIR/policies/scopes.yaml" ] \
   && [ -f "$DEFAULTS_DIR/policies/scopes.yaml" ]; then
    mkdir -p "$CONFIG_DIR/migrations"
    cp "$CONFIG_DIR/policies/scopes.yaml" \
       "$CONFIG_DIR/policies/scopes.yaml.pre-v0.8.5.bak"
    cp "$DEFAULTS_DIR/policies/scopes.yaml" \
       "$CONFIG_DIR/policies/scopes.yaml"
    touch "$SCOPE_MIGRATION_MARKER"
    bashio::log.info "Migrated policies/scopes.yaml to v0.8.5 corpora (backup: scopes.yaml.pre-v0.8.5.bak)"
fi

if [ ! -f "$CONFIG_DIR/policies/scopes.yaml" ] \
   && [ -f "$DEFAULTS_DIR/policies/scopes.yaml" ]; then
    cp "$DEFAULTS_DIR/policies/scopes.yaml" \
       "$CONFIG_DIR/policies/scopes.yaml"
    bashio::log.info "Seeded policies/scopes.yaml"
fi

# ------------------------------------------------------------------
# 3.2 migration: add memory.default_scope to existing resident
# runtime.yamls that predate v0.8.0. Idempotent via marker line.
# ------------------------------------------------------------------

migrate_default_scope() {
    local runtime_file="$1"
    local fallback_scope="$2"
    local marker="# casa: default_scope v1"

    [ -f "$runtime_file" ] || return 0
    if grep -qF "$marker" "$runtime_file"; then
        return 0  # already migrated
    fi

    if grep -qE "^\s*default_scope:" "$runtime_file"; then
        # Present but unmarked — mark it and move on.
        printf '\n%s\n' "$marker" >> "$runtime_file"
        bashio::log.info "Marked existing default_scope in $runtime_file"
        return 0
    fi

    # Insert `default_scope: <fallback>` as the last key inside the
    # `memory:` block using Python sed-alike.
    python3 - "$runtime_file" "$fallback_scope" "$marker" <<'PY'
import sys, re, pathlib
path, scope, marker = sys.argv[1], sys.argv[2], sys.argv[3]
text = pathlib.Path(path).read_text(encoding="utf-8")
new = re.sub(
    r"(memory:\s*\n(?:[ \t]+\S.*\n)*)",
    lambda m: m.group(1) + f"  default_scope: {scope}\n",
    text, count=1,
)
if new == text:
    sys.exit(0)  # no memory block — nothing to migrate
new = new.rstrip() + f"\n{marker}\n"
pathlib.Path(path).write_text(new, encoding="utf-8")
PY
    bashio::log.info "Migrated default_scope in $runtime_file → $fallback_scope"
}

migrate_default_scope "$CONFIG_DIR/agents/assistant/runtime.yaml" "personal"
migrate_default_scope "$CONFIG_DIR/agents/butler/runtime.yaml"    "house"

# ------------------------------------------------------------------
# 3.2 migration: shorten butler/disclosure.yaml override
# (drop redundant confidential categories; inherit deflection patterns).
# ------------------------------------------------------------------

migrate_butler_disclosure_v2() {
    local disc_file="$CONFIG_DIR/agents/butler/disclosure.yaml"
    local marker="# casa: butler_disclosure v2"

    [ -f "$disc_file" ] || return 0
    if grep -qF "$marker" "$disc_file"; then
        return 0
    fi

    cat > "$disc_file" <<EOF
$marker
schema_version: 1
policy: standard
overrides:
  categories: {}
EOF
    bashio::log.info "Migrated butler/disclosure.yaml (v2 — categories empty, inherit deflections)"
}

migrate_butler_disclosure_v2

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

bashio::log.info "Configuration setup complete."
