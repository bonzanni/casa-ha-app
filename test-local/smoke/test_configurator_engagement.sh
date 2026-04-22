#!/usr/bin/env bash
# Manual smoke for Plan 3 configurator against the live Telegram Bot API.
# Requires .env.smoke with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
# TELEGRAM_ENGAGEMENT_SUPERGROUP_ID.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

if [ -f .env.smoke ]; then
    set -a; . ./.env.smoke; set +a
fi

: "${TELEGRAM_BOT_TOKEN:?set in .env.smoke}"
: "${TELEGRAM_CHAT_ID:?set in .env.smoke}"
: "${TELEGRAM_ENGAGEMENT_SUPERGROUP_ID:?set in .env.smoke}"

API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

echo "=== Configurator smoke test (v0.12.0) ==="
echo "Bot:        $(curl -s "$API/getMe" | jq -r .result.username)"
echo "User chat:  $TELEGRAM_CHAT_ID"
echo "Engagement: $TELEGRAM_ENGAGEMENT_SUPERGROUP_ID"
echo ""

echo "Step 1: Send config request to Ellen (1:1 chat)"
echo "  'Please add a hourly_check trigger to assistant that fires every hour'"
read -p "    Press enter after you send the message in Telegram... "

echo "Step 2: Watch for new topic in the engagement supergroup"
echo "  Expected: '#[configurator] add hourly_check trigger...'"
read -p "    Press enter after you see the topic... "

echo "Step 3: Configurator should ask a confirmation question in the topic"
echo "  Reply 'yes do it' in the engagement topic."
read -p "    Press enter after you reply... "

echo "Step 4: Wait for emit_completion + casa_reload_triggers (~5-10s)"
read -p "    Press enter after you see the completion summary... "

echo "Step 5: Verify trigger landed via /ha-prod-console:ssh:"
echo "  cat /addon_configs/casa-agent/agents/assistant/triggers.yaml"
read -p "    Press enter after manual verification... "

echo "Step 6: Verify Ellen narrated the outcome in 1:1 chat"
read -p "    Press enter after confirming narration... "

echo "Step 7: Verify no addon restart (soft reload only):"
echo "  /ha-prod-console:info -> uptime should be unchanged"
read -p "    Press enter after manual verification... "

echo "=== PASS ==="
echo ""
echo "Cleanup: ask Ellen to remove hourly_check trigger from assistant."
echo "Verify triggers.yaml back to pre-test state."
