#!/command/with-contenv bashio
# 5.5 item 3: strip ANSI from bashio output for clean docker logs.
export BASHIO_LOG_NO_COLORS=true
export NO_COLOR=1

CONFIG_DIR="/addon_configs/casa-agent"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

# Create directory structure (idempotent)
mkdir -p "$CONFIG_DIR/agents" "$CONFIG_DIR/agents/executors" \
         "$CONFIG_DIR/workspace/.claude/skills" \
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
# One-shot 2.3 migration: inject tts.tag_dialect and voice_errors
# into butler.yaml if absent. Idempotent on re-runs.
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
        # bashio may not be in scope for tests; use echo-fallback
        echo "Injected tts block into $(basename "$file")"
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
        echo "Injected voice_errors block into $(basename "$file")"
    fi
}

migrate_voice_fields "$CONFIG_DIR/agents/butler.yaml"

# ------------------------------------------------------------------
# One-shot 5.1 migration: replace the layer-1 Disclosure clause in
# butler.yaml with the v2 wording. Gated by the marker comment
# `# casa: disclosure v2` written as the final file line. Idempotent.
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

    bashio::log.info "Migrated disclosure clause to v2 in $(basename "$file")"
}

migrate_disclosure_clause "$CONFIG_DIR/agents/butler.yaml"

# ------------------------------------------------------------------
# One-shot 3.1 migration: inject memory.scopes_owned / scopes_readable
# into resident YAMLs with conservative defaults. Metadata only —
# runtime retrieval lands in 3.2. Gated by the trailing marker
# `# casa: scopes v1`. Idempotent.
# ------------------------------------------------------------------

migrate_scope_metadata() {
    local file="$1"
    local default_owned="$2"      # e.g. "[personal, business, finance]"
    local default_readable="$3"   # e.g. "[personal, business, finance, house]"

    [ -f "$file" ] || return 0

    # Strip CRs so sed / marker checks work on Windows-edited files.
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
    # Validate YAML parses before rewriting.
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
            # Inject missing fields directly under `memory:`.
            if not has_owned:
                out.append(f"  scopes_owned: {default_owned}\n")
            if not has_readable:
                out.append(f"  scopes_readable: {default_readable}\n")
else:
    # No memory: block at all — append a minimal one with just scopes.
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

    bashio::log.info "Migrated scope metadata in $(basename "$file")"
}

migrate_scope_metadata "$CONFIG_DIR/agents/assistant.yaml" \
    "[personal, business, finance]" \
    "[personal, business, finance, house]"
migrate_scope_metadata "$CONFIG_DIR/agents/butler.yaml" \
    "[house]" "[house]"

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
         agents/executors/alex.yaml schedules.yaml webhooks.yaml; do
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
