#!/command/with-contenv bashio

if ! bashio::config.has_value 'claude_oauth_token'; then
    bashio::exit.nok "claude_oauth_token is required. Run 'claude setup-token' on your local machine."
fi

bashio::log.info "Configuration validated."
