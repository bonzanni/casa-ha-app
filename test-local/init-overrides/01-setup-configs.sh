#!/bin/sh
# Test override: reads options.json directly instead of bashio.
# Structurally mirrors casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh.
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
# Seed defaults on first boot (directory-copy).
# ------------------------------------------------------------------

seed_agent_dir() {
    src="$1"
    dst="$2"
    if [ -d "$src" ] && [ ! -d "$dst" ]; then
        cp -r "$src" "$dst"
        echo "[INFO] Seeded agent dir: $(basename "$dst")"
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
    echo "[INFO] Seeded policies/disclosure.yaml"
fi

# v0.8.5: refresh scope corpora to the keyword-style descriptions.
# Marker prevents re-running on every boot. Manual edits made by the
# user (or future Builder) AFTER the marker is written are preserved.
# Placed ABOVE the seed-if-missing block below so migrations run first
# (see reference_migrations_vs_seed_order memory note).
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
    echo "[INFO] Migrated policies/scopes.yaml to v0.8.5 corpora (backup: scopes.yaml.pre-v0.8.5.bak)"
fi

if [ ! -f "$CONFIG_DIR/policies/scopes.yaml" ] \
   && [ -f "$DEFAULTS_DIR/policies/scopes.yaml" ]; then
    cp "$DEFAULTS_DIR/policies/scopes.yaml" \
       "$CONFIG_DIR/policies/scopes.yaml"
    echo "[INFO] Seeded policies/scopes.yaml"
fi

# ------------------------------------------------------------------
# 3.2 migration: add memory.default_scope to existing resident
# runtime.yamls that predate v0.8.0. Idempotent via marker line.
# ------------------------------------------------------------------

migrate_default_scope() {
    runtime_file="$1"
    fallback_scope="$2"
    marker="# casa: default_scope v1"

    [ -f "$runtime_file" ] || return 0
    if grep -qF "$marker" "$runtime_file"; then
        return 0
    fi
    if grep -qE "^[[:space:]]*default_scope:" "$runtime_file"; then
        printf '\n%s\n' "$marker" >> "$runtime_file"
        echo "[INFO] Marked existing default_scope in $runtime_file"
        return 0
    fi
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
    sys.exit(0)
new = new.rstrip() + f"\n{marker}\n"
pathlib.Path(path).write_text(new, encoding="utf-8")
PY
    echo "[INFO] Migrated default_scope in $runtime_file → $fallback_scope"
}

migrate_default_scope "$CONFIG_DIR/agents/assistant/runtime.yaml" "personal"
migrate_default_scope "$CONFIG_DIR/agents/butler/runtime.yaml"    "house"

# ------------------------------------------------------------------
# 3.2 migration: shorten butler/disclosure.yaml override
# (drop redundant confidential categories; inherit deflection patterns).
# ------------------------------------------------------------------

migrate_butler_disclosure_v2() {
    disc_file="$CONFIG_DIR/agents/butler/disclosure.yaml"
    marker="# casa: butler_disclosure v2"

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
    echo "[INFO] Migrated butler/disclosure.yaml (v2 — categories empty, inherit deflections)"
}

migrate_butler_disclosure_v2

# Seed schemas (overwrite on every boot).
if [ -d "$DEFAULTS_DIR/schema" ]; then
    cp "$DEFAULTS_DIR/schema"/*.json "$CONFIG_DIR/schema/" 2>/dev/null || true
    echo "[INFO] Refreshed schema files"
fi

# ------------------------------------------------------------------
# Initialize git repo (idempotent) + snapshot manual edits.
# ------------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    echo "[WARN] git not installed — skipping config repo init"
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
    echo "[INFO] Initialized config git repo at $CONFIG_DIR"
else
    cd "$CONFIG_DIR"
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        git add -A
        git commit -qm "manual edit (boot-time snapshot)"
        echo "[INFO] Snapshotted manual edits in config repo"
    fi
fi

# Skip repo sync in local test mode
echo "[INFO] Skipping repo sync (local test mode)."

if [ ! -f "$DATA_DIR/sessions.json" ]; then
    echo '{}' > "$DATA_DIR/sessions.json"
fi

# Auto-generate webhook secret if auth is enabled.
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
