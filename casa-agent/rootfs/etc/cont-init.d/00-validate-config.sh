#!/usr/bin/with-contenv bashio
# /etc/cont-init.d/00-validate-config.sh

if ! bashio::config.has_value 'claude_oauth_token'; then
    bashio::exit.nok "claude_oauth_token is required. Run 'claude setup-token' on your local machine."
fi

bashio::log.info "Configuration validated."
