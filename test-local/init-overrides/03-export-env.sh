#!/bin/sh
# Test override: exports env vars from options.json for the casa service.
# In HA, the service run script uses bashio::config. Here we write to
# /var/run/s6/container_environment/ which s6 reads automatically.
OPTIONS=/data/options.json
S6_ENV="/var/run/s6/container_environment"

mkdir -p "$S6_ENV"

# Export each option as an s6 container env var
for key in claude_oauth_token telegram_bot_token telegram_chat_id \
           honcho_api_url honcho_api_key webhook_secret \
           primary_agent_name voice_agent_name \
           primary_agent_model voice_agent_model subagent_model; do
    val=$(jq -r ".${key} // empty" "$OPTIONS")
    upper_key=$(echo "$key" | tr '[:lower:]' '[:upper:]')
    if [ -n "$val" ]; then
        printf '%s' "$val" > "${S6_ENV}/${upper_key}"
    fi
done

echo "[INFO] Environment exported from options.json (local test mode)."
