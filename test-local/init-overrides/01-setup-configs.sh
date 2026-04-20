#!/bin/sh
# Test override: reads options.json directly instead of bashio
CONFIG_DIR="/addon_configs/casa-agent"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

mkdir -p "$CONFIG_DIR/agents" "$CONFIG_DIR/agents/executors" \
         "$CONFIG_DIR/workspace/.claude/skills" \
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

# ------------------------------------------------------------------
# One-shot 2.3 migration: inject tts.tag_dialect and voice_errors
# into butler.yaml if absent. Mirrors the production migrate_voice_fields
# block (test variant uses plain echo instead of bashio::log.info).
# ------------------------------------------------------------------

migrate_voice_fields() {
    local file="$1"

    [ -f "$file" ] || return 0

    sed -i 's/\r$//' "$file"

    if ! grep -qE '^tts:' "$file"; then
        cat >> "$file" <<'YAML'
tts:
  tag_dialect: square_brackets
YAML
        echo "[INFO] Injected tts block into $(basename "$file")"
    fi

    if ! grep -qE '^voice_errors:' "$file"; then
        cat >> "$file" <<'YAML'
voice_errors:
  timeout:       "[apologetic] Hm, that took too long. Try again?"
  rate_limit:    "[flat] My brain is busy — give me a minute."
  sdk_error:     "[apologetic] I couldn't reach my brain. Try again?"
  memory_error:  ""
  channel_error: "[flat] Something went wrong sending that."
  unknown:       "[flat] Sorry, something went wrong."
YAML
        echo "[INFO] Injected voice_errors block into $(basename "$file")"
    fi
}

migrate_voice_fields "$CONFIG_DIR/agents/butler.yaml"

# ------------------------------------------------------------------
# One-shot 5.1 migration: replace layer-1 Disclosure clause with v2.
# Mirrors migrate_disclosure_clause in setup-configs.sh (bashio →
# plain echo). Idempotent; gated by marker `# casa: disclosure v2`.
# ------------------------------------------------------------------

migrate_disclosure_clause() {
    local file="$1"

    [ -f "$file" ] || return 0

    sed -i 's/\r$//' "$file"

    if grep -qE '^# casa: disclosure v2$' "$file"; then
        return 0
    fi

    python3 - "$file" <<'PY'
import pathlib, re, sys

p = pathlib.Path(sys.argv[1])
text = p.read_text(encoding="utf-8")

NEW_BLOCK = """\
  Disclosure (on untrusted channels):
  - The <channel_context> block names the current channel's trust.
    If trust starts with "household-shared" or "public", anyone
    nearby can hear you. Treat Nicola's personal data as confidential
    on those channels.
  - Confidential on untrusted channels (do NOT say out loud):
    * Financial — bank names, account or card numbers, balances,
      amounts of recent payments, names of specific vendors Nicola
      pays.
    * Medical — conditions, medications, doctor or clinic names,
      appointment times, therapy topics.
    * Contacts — phone numbers, email addresses, physical addresses,
      full names of people not already public in this conversation.
    * Schedule — specific times, dates, or locations of Nicola's
      upcoming personal events (meetings, trips, appointments).
    * Credentials — API keys, passwords, door codes, Wi-Fi passwords.
  - When asked about any of the above on an untrusted channel:
    * Do not hedge. Do not invent. Do not partial-disclose.
    * Deflect crisply: "I'll tell you that on Telegram." or
      "That's private — check Telegram."
    * Then continue the conversation normally.
  - Safe on any channel: device control (lights, heating, locks),
    sensor state ("it's 22 degrees in the kitchen"), general
    knowledge answers, public information already said aloud in
    this session.
"""

lines = text.splitlines(keepends=True)
out, i, replaced = [], 0, False
while i < len(lines):
    line = lines[i]
    # Disclosure heading lives at 2-space indent inside the personality
    # block scalar. Match it exactly.
    if not replaced and re.match(r"^  Disclosure:[ \t]*\n?$", line):
        # Skip the heading + every following line that belongs to the
        # block: the continuation lines start with "  -" (bullet) or
        # "    " (4-space continuation). Stop at the first line that
        # does not, which is the boundary back to the block scalar's
        # surrounding prose or to an outdented top-level key.
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if nxt.startswith("  -") or nxt.startswith("    "):
                i += 1
                continue
            break
        out.append(NEW_BLOCK)
        replaced = True
        continue
    out.append(line)
    i += 1

new_text = "".join(out)
if not new_text.endswith("\n"):
    new_text += "\n"
new_text += "# casa: disclosure v2\n"
p.write_text(new_text, encoding="utf-8")
PY

    echo "[INFO] Migrated disclosure clause to v2 in $(basename "$file")"
}

migrate_disclosure_clause "$CONFIG_DIR/agents/butler.yaml"

# ------------------------------------------------------------------
# Mirrors migrate_scope_metadata in setup-configs.sh (bashio →
# plain echo for local test mode). Source of truth: setup-configs.sh.
# ------------------------------------------------------------------

migrate_scope_metadata() {
    local file="$1"
    local default_owned="$2"
    local default_readable="$3"

    [ -f "$file" ] || return 0

    sed -i 's/\r$//' "$file"

    if grep -qE '^# casa: scopes v1$' "$file"; then
        return 0
    fi

    if ! python3 - "$file" "$default_owned" "$default_readable" <<'PY'
import pathlib, re, sys

p = pathlib.Path(sys.argv[1])
default_owned = sys.argv[2]
default_readable = sys.argv[3]

try:
    import yaml
except ImportError:
    print(f"[ERROR] yaml unavailable; skipping {p.name}", file=sys.stderr)
    sys.exit(1)

try:
    text = p.read_text(encoding="utf-8")
    yaml.safe_load(text)
except Exception as exc:
    print(f"[ERROR] could not parse {p.name}: {exc}", file=sys.stderr)
    sys.exit(1)

lines = text.splitlines(keepends=True)
has_memory = any(re.match(r"^memory:[ \t]*$", ln) for ln in lines)
has_owned = any(re.match(r"^  scopes_owned:", ln) for ln in lines)
has_readable = any(re.match(r"^  scopes_readable:", ln) for ln in lines)

out = []
if has_memory:
    for ln in lines:
        out.append(ln)
        if re.match(r"^memory:[ \t]*$", ln):
            if not has_owned:
                out.append(f"  scopes_owned: {default_owned}\n")
            if not has_readable:
                out.append(f"  scopes_readable: {default_readable}\n")
else:
    out = list(lines)
    if out and not out[-1].endswith("\n"):
        out[-1] = out[-1] + "\n"
    out.append("memory:\n")
    out.append(f"  scopes_owned: {default_owned}\n")
    out.append(f"  scopes_readable: {default_readable}\n")

new_text = "".join(out)
if not new_text.endswith("\n"):
    new_text += "\n"
new_text += "# casa: scopes v1\n"
p.write_text(new_text, encoding="utf-8")
PY
    then
        echo "[ERROR] migrate_scope_metadata: python step failed for $(basename "$file"); skipping"
        return 0
    fi

    echo "[INFO] Migrated scope metadata in $(basename "$file")"
}

migrate_scope_metadata "$CONFIG_DIR/agents/assistant.yaml" \
    "[personal, business, finance]" \
    "[personal, business, finance, house]"
migrate_scope_metadata "$CONFIG_DIR/agents/butler.yaml" \
    "[house]" "[house]"

# ------------------------------------------------------------------
# Mirror of migrate_channels in setup-configs.sh. Source of truth:
# setup-configs.sh. Backfills `channels:` on pre-2.1 YAMLs that went
# through migrate_rename but never gained a channels block.
# ------------------------------------------------------------------

migrate_channels() {
    local file="$1"
    local default_channels="$2"

    [ -f "$file" ] || return 0

    sed -i 's/\r$//' "$file"

    if grep -qE '^# casa: channels v1$' "$file"; then
        return 0
    fi

    if ! python3 - "$file" "$default_channels" <<'PY'
import pathlib, re, sys

p = pathlib.Path(sys.argv[1])
default_channels = sys.argv[2]

try:
    import yaml
except ImportError:
    print(f"[ERROR] yaml unavailable; skipping {p.name}", file=sys.stderr)
    sys.exit(1)

try:
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
except Exception as exc:
    print(f"[ERROR] could not parse {p.name}: {exc}", file=sys.stderr)
    sys.exit(1)

existing = data.get("channels")
needs_backfill = (not existing) or (isinstance(existing, list) and len(existing) == 0)

lines = text.splitlines(keepends=True)
out = []

if needs_backfill:
    skip_next_indent = False
    for ln in lines:
        if re.match(r"^channels:[ \t]*\[[ \t]*\][ \t]*$", ln):
            continue
        if re.match(r"^channels:[ \t]*$", ln):
            skip_next_indent = True
            continue
        if skip_next_indent and re.match(r"^[ \t]+-", ln):
            continue
        skip_next_indent = False
        out.append(ln)
    if out and not out[-1].endswith("\n"):
        out[-1] = out[-1] + "\n"
    out.append(f"channels: {default_channels}\n")
else:
    out = list(lines)
    if out and not out[-1].endswith("\n"):
        out[-1] = out[-1] + "\n"

new_text = "".join(out)
if not new_text.endswith("\n"):
    new_text += "\n"
new_text += "# casa: channels v1\n"
p.write_text(new_text, encoding="utf-8")
PY
    then
        echo "[ERROR] migrate_channels: python step failed for $(basename "$file"); skipping"
        return 0
    fi

    echo "[INFO] Migrated channels backfill in $(basename "$file")"
}

migrate_channels "$CONFIG_DIR/agents/assistant.yaml" "[telegram, webhook]"
migrate_channels "$CONFIG_DIR/agents/butler.yaml" "[ha_voice]"

# Mirror of migrate_executor_rename in setup-configs.sh
migrate_executor_rename() {
    local old="$CONFIG_DIR/agents/executors/alex.yaml"
    local new="$CONFIG_DIR/agents/executors/finance.yaml"

    if [ -f "$old" ] && [ ! -f "$new" ]; then
        mv "$old" "$new"
        sed -i 's/\r$//' "$new"
        sed -i 's/^role:[[:space:]]*alex[[:space:]]*$/role: finance/' "$new"
        sed -i 's/^name:[[:space:]]*alex[[:space:]]*$/name: Alex/' "$new"
        echo "[INFO] Migrated executor alex.yaml -> finance.yaml"
    fi
}

migrate_executor_rename

# Mirror of migrate_mcp_allowed in setup-configs.sh
migrate_mcp_allowed() {
    local file="$1"

    [ -f "$file" ] || return 0

    sed -i 's/\r$//' "$file"

    if grep -qE '^# casa: mcp-tools v1$' "$file"; then
        return 0
    fi

    if ! python3 - "$file" <<'PY'
import pathlib, re, sys

p = pathlib.Path(sys.argv[1])

try:
    import yaml
except ImportError:
    print(f"[ERROR] yaml unavailable; skipping {p.name}", file=sys.stderr)
    sys.exit(1)

try:
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
except Exception as exc:
    print(f"[ERROR] could not parse {p.name}: {exc}", file=sys.stderr)
    sys.exit(1)

needed = [
    "mcp__casa-framework__delegate_to_agent",
    "mcp__casa-framework__send_message",
]
existing = (data.get("tools") or {}).get("allowed") or []
to_add = [t for t in needed if t not in existing]

if not to_add:
    new_text = text if text.endswith("\n") else text + "\n"
    new_text += "# casa: mcp-tools v1\n"
    p.write_text(new_text, encoding="utf-8")
    sys.exit(0)

lines = text.splitlines(keepends=True)
out = []
i = 0
injected = False
while i < len(lines):
    line = lines[i]
    out.append(line)
    if re.match(r"^tools:[ \t]*$", line):
        j = i + 1
        while j < len(lines):
            inner = lines[j]
            m_inline = re.match(r"^(  allowed:[ \t]*)\[(.*)\][ \t]*$", inner)
            m_block = re.match(r"^  allowed:[ \t]*$", inner)
            if m_inline:
                prefix = m_inline.group(1)
                current = m_inline.group(2).strip()
                combined_items = [
                    s.strip() for s in current.split(",") if s.strip()
                ] + to_add
                lines[j] = f"{prefix}[{', '.join(combined_items)}]\n"
                injected = True
                break
            if m_block:
                k = j + 1
                last_item_idx = j
                while k < len(lines) and re.match(r"^    - ", lines[k]):
                    last_item_idx = k
                    k += 1
                extra = "".join(f"    - {t}\n" for t in to_add)
                lines[last_item_idx] = lines[last_item_idx] + extra
                injected = True
                break
            if re.match(r"^\S", inner):
                break
            j += 1
        if injected:
            out = lines[: i + 1]
            out.extend(lines[i + 1 :])
            break
    i += 1

new_text = "".join(out)
if not new_text.endswith("\n"):
    new_text += "\n"
new_text += "# casa: mcp-tools v1\n"
p.write_text(new_text, encoding="utf-8")
PY
    then
        echo "[ERROR] migrate_mcp_allowed: python step failed for $(basename "$file"); skipping"
        return 0
    fi

    echo "[INFO] Migrated mcp-tools permissions in $(basename "$file")"
}

migrate_mcp_allowed "$CONFIG_DIR/agents/assistant.yaml"

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

# Residents + top-level config (hand-enumerated — part of the core
# install contract and individually required by Casa startup).
for f in agents/assistant.yaml agents/butler.yaml agents/subagents.yaml \
         schedules.yaml webhooks.yaml; do
    if [ ! -f "$CONFIG_DIR/$f" ]; then
        cp "$DEFAULTS_DIR/$f" "$CONFIG_DIR/$f"
        echo "[INFO] Created default config: $f"
    fi
done

# Executors (glob-discovered — drop a YAML in defaults/agents/executors/
# and it seeds on next boot; "config not code" per Phase 3.4 spec §3).
if [ -d "$DEFAULTS_DIR/agents/executors" ]; then
    for src in "$DEFAULTS_DIR/agents/executors"/*.yaml; do
        [ -f "$src" ] || continue
        name=$(basename "$src")
        dest="$CONFIG_DIR/agents/executors/$name"
        if [ ! -f "$dest" ]; then
            cp "$src" "$dest"
            echo "[INFO] Created default config: agents/executors/$name"
        fi
    done
fi

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
