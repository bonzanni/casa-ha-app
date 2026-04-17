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
    local canonical_role="$3"
    local old_peer="$4"
    local new_peer="$5"

    if [ -f "$CONFIG_DIR/agents/$old" ] && [ ! -f "$CONFIG_DIR/agents/$new" ]; then
        mv "$CONFIG_DIR/agents/$old" "$CONFIG_DIR/agents/$new"
        # Strip CR so a Windows-edited YAML (\r\n) still matches our anchors.
        sed -i 's/\r$//' "$CONFIG_DIR/agents/$new"
        # Force the role line to the canonical value for this filename, whatever
        # the user (or the legacy default) had there. The filename is the
        # source of truth after Phase 2.1.
        sed -i "s/^role:[[:space:]]*.*$/role: ${canonical_role}/" \
            "$CONFIG_DIR/agents/$new"
        sed -i "s/^  peer_name:[[:space:]]*${old_peer}[[:space:]]*$/  peer_name: ${new_peer}/" \
            "$CONFIG_DIR/agents/$new"
        bashio::log.info "Migrated $old -> $new"
    fi
}

migrate_rename "ellen.yaml" "assistant.yaml" "assistant" "ellen" "assistant"
migrate_rename "tina.yaml"  "butler.yaml"    "butler"    "tina"  "butler"

# ------------------------------------------------------------------
# One-shot 2.2a migration: strip obsolete memory.peer_name and
# memory.exclude_tags from user YAMLs; inject memory.read_strategy
# if missing. Idempotent.
# ------------------------------------------------------------------

migrate_memory_fields() {
    local file="$1"
    local default_strategy="$2"

    [ -f "$file" ] || return 0

    # Strip CRs so sed anchors match on Windows-edited files.
    sed -i 's/\r$//' "$file"

    # Delete `peer_name: ...` under the memory: block.
    sed -i '/^  peer_name:/d' "$file"

    # Delete `exclude_tags: [...]` single-line form.
    sed -i '/^  exclude_tags:[[:space:]]*\[.*\]$/d' "$file"
    # Delete block form: `exclude_tags:` + following `    - item` lines.
    python3 - "$file" <<'PY'
import sys, re, pathlib

p = pathlib.Path(sys.argv[1])
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
out = []
i = 0
while i < len(lines):
    line = lines[i]
    if re.match(r"^  exclude_tags:[ \t]*$", line):
        i += 1
        while i < len(lines) and re.match(r"^    - ", lines[i]):
            i += 1
        continue
    out.append(line)
    i += 1
p.write_text("".join(out), encoding="utf-8")
PY

    # Inject read_strategy under memory: when absent. The memory: block
    # is identified by the anchor line `memory:` at column 0.
    if ! grep -qE '^  read_strategy:' "$file"; then
        if grep -qE '^memory:' "$file"; then
            sed -i "/^memory:/a\\  read_strategy: ${default_strategy}" "$file"
            bashio::log.info "Injected read_strategy=${default_strategy} into $(basename "$file")"
        fi
    fi
}

migrate_memory_fields "$CONFIG_DIR/agents/assistant.yaml" "per_turn"
migrate_memory_fields "$CONFIG_DIR/agents/butler.yaml"    "cached"

# ------------------------------------------------------------------
# One-shot: drop obsolete memory_session_id field from sessions.json.
# Lazy migration in SessionRegistry.touch() also handles this, but
# doing it at setup time keeps the on-disk schema current immediately.
# ------------------------------------------------------------------

if [ -f "$DATA_DIR/sessions.json" ]; then
    python3 - "$DATA_DIR/sessions.json" <<'PY'
import json, pathlib, sys

p = pathlib.Path(sys.argv[1])
try:
    data = json.loads(p.read_text() or "{}")
except json.JSONDecodeError:
    sys.exit(0)
dirty = False
for entry in data.values():
    if isinstance(entry, dict) and "memory_session_id" in entry:
        entry.pop("memory_session_id", None)
        dirty = True
if dirty:
    p.write_text(json.dumps(data, indent=2))
    print("Migrated sessions.json: dropped memory_session_id")
PY
fi

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
