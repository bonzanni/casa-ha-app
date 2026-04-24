#!/bin/sh
# Manual end-to-end smoke for plugin-developer + Configurator install flow.
# Not CI-gated — produces DOCS.md screenshots.
#
# Prerequisites:
#   - github_token addon option set (or op:// ref + 1P configured)
#   - onepassword_service_account_token set
#   - telegram_engagement_supergroup_id set + bot promoted with Manage Topics
#   - .env.smoke sourced (TELEGRAM_BOT_TOKEN, TEST_CHAT_ID, TEST_SUPERGROUP_ID)
#
# Walks a trivial `hello-casa` plugin (one skill that prints "hello") through:
#   1. User to Ellen: "Build a hello plugin."
#   2. Ellen engages plugin-developer.
#   3. plugin-developer asks public/private; user picks private.
#   4. plugin-developer authors + pushes.
#   5. plugin-developer emits completion.
#   6. Ellen relays; user confirms install.
#   7. Configurator installs on Ellen's agent-home.
#   8. User invokes Ellen; hello-casa skill responds.
set -e

. .env.smoke
. test-local/common.sh

send_telegram "Build a hello-casa plugin for Ellen. One skill that responds with 'hello'."
wait_for_message_match "public or private" 120
send_telegram "Private."
wait_for_message_match "Built casa-plugin-hello-casa" 600
send_telegram "Yes, install."
wait_for_message_match "installed on Ellen" 300
send_telegram "Ellen, say hello using the skill."
wait_for_message_match "hello" 60
echo "SMOKE OK"
