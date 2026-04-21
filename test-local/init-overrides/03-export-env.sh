#!/bin/sh
# Test override: exports env vars from options.json for the casa service.
# In HA, the service run script uses bashio::config. Here we write to
# /var/run/s6/container_environment/ which s6 reads automatically.
OPTIONS=/data/options.json
S6_ENV="/var/run/s6/container_environment"

mkdir -p "$S6_ENV"

# Export each option as an s6 container env var
for key in public_url telegram_bot_token telegram_chat_id telegram_transport telegram_delivery_mode \
           honcho_api_url honcho_api_key webhook_secret enable_terminal \
           primary_agent_name voice_agent_name \
           primary_agent_model voice_agent_model subagent_model \
           heartbeat_enabled heartbeat_interval_minutes; do
    val=$(jq -r ".${key} // empty" "$OPTIONS")
    upper_key=$(echo "$key" | tr '[:lower:]' '[:upper:]')
    if [ -n "$val" ]; then
        printf '%s' "$val" > "${S6_ENV}/${upper_key}"
    fi
done

# Special case: the Claude Code CLI expects CLAUDE_CODE_OAUTH_TOKEN
# (not CLAUDE_OAUTH_TOKEN). The real HA run script maps this explicitly.
val=$(jq -r '.claude_oauth_token // empty' "$OPTIONS")
if [ -n "$val" ]; then
    printf '%s' "$val" > "${S6_ENV}/CLAUDE_CODE_OAUTH_TOKEN"
fi

# Map scope_threshold -> CASA_SCOPE_THRESHOLD (casa_core.py reads the
# CASA_ prefix). Mirrors the real svc-casa/run behaviour.
val=$(jq -r '.scope_threshold // empty' "$OPTIONS")
if [ -n "$val" ]; then
    printf '%s' "$val" > "${S6_ENV}/CASA_SCOPE_THRESHOLD"
fi

# Static version for test mode
printf 'dev' > "${S6_ENV}/CASA_VERSION"

echo "[INFO] Environment exported from options.json (local test mode)."
