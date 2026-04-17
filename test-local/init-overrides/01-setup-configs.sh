#!/bin/sh
# Test override: reads options.json directly instead of bashio
CONFIG_DIR="/addon_configs/casa-agent"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

mkdir -p "$CONFIG_DIR/agents" "$CONFIG_DIR/workspace/.claude/skills" \
         "$CONFIG_DIR/workspace/plugins" "$CONFIG_DIR/workspace/mcp-servers" \
         "$DATA_DIR/sdk-sessions"

# ------------------------------------------------------------------
# One-shot migration: rename legacy display-name YAMLs to role-based.
# Mirrors the production migrate_rename logic in setup-configs.sh
# (without bashio; uses plain echo instead).
# ------------------------------------------------------------------
migrate_rename() {
    local old="$1"
    local new="$2"
    local canonical_role="$3"
    local old_peer="$4"
    local new_peer="$5"

    if [ -f "$CONFIG_DIR/agents/$old" ] && [ ! -f "$CONFIG_DIR/agents/$new" ]; then
        mv "$CONFIG_DIR/agents/$old" "$CONFIG_DIR/agents/$new"
        sed -i 's/\r$//' "$CONFIG_DIR/agents/$new"
        sed -i "s/^role:[[:space:]]*.*$/role: ${canonical_role}/" \
            "$CONFIG_DIR/agents/$new"
        sed -i "s/^  peer_name:[[:space:]]*${old_peer}[[:space:]]*$/  peer_name: ${new_peer}/" \
            "$CONFIG_DIR/agents/$new"
        echo "[INFO] Migrated $old -> $new"
    fi
}

migrate_rename "ellen.yaml" "assistant.yaml" "assistant" "ellen" "assistant"
migrate_rename "tina.yaml"  "butler.yaml"    "butler"    "tina"  "butler"

# ------------------------------------------------------------------
# One-shot 2.2a migration: strip obsolete memory.peer_name and
# memory.exclude_tags from user YAMLs; inject memory.read_strategy
# if missing. Mirrors the production migrate_memory_fields block
# (test variant uses plain echo instead of bashio::log.info).
# ------------------------------------------------------------------

migrate_memory_fields() {
    local file="$1"
    local default_strategy="$2"

    [ -f "$file" ] || return 0

    sed -i 's/\r$//' "$file"
    sed -i '/^  peer_name:/d' "$file"
    sed -i '/^  exclude_tags:[[:space:]]*\[.*\]$/d' "$file"

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

    if ! grep -qE '^  read_strategy:' "$file"; then
        if grep -qE '^memory:' "$file"; then
            sed -i "/^memory:/a\\  read_strategy: ${default_strategy}" "$file"
            echo "[INFO] Injected read_strategy=${default_strategy} into $(basename "$file")"
        fi
    fi
}

migrate_memory_fields "$CONFIG_DIR/agents/assistant.yaml" "per_turn"
migrate_memory_fields "$CONFIG_DIR/agents/butler.yaml"    "cached"

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
    print("[INFO] Migrated sessions.json: dropped memory_session_id")
PY
fi

for f in agents/assistant.yaml agents/butler.yaml agents/subagents.yaml \
         schedules.yaml webhooks.yaml; do
    if [ ! -f "$CONFIG_DIR/$f" ]; then
        cp "$DEFAULTS_DIR/$f" "$CONFIG_DIR/$f"
        echo "[INFO] Created default config: $f"
    fi
done

# Skip repo sync in local test mode
echo "[INFO] Skipping repo sync (local test mode)."

if [ ! -f "$DATA_DIR/sessions.json" ]; then
    echo '{}' > "$DATA_DIR/sessions.json"
fi

# Auto-generate webhook secret if auth is enabled
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
