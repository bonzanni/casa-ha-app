#!/bin/sh
# Casa git credential helper.
#
# Stateless: reads $GITHUB_TOKEN from process env, never writes to disk.
# Invoked by git when an HTTPS clone needs credentials. Wired in
# /etc/gitconfig as `[credential "https://github.com"] helper = ...`.
#
# Token is propagated to addon-wide processes via s6-overlay's
# /run/s6/container_environment/GITHUB_TOKEN file (written by
# setup-configs.sh at boot from `op read op://VAULT/GitHub/credential`).
#
# Actions (per gitcredentials(7)):
#   get   — emit username + password
#   store — git asks the helper to save creds (no-op here, stateless)
#   erase — git asks the helper to forget creds (no-op here, stateless)
#
# Public-only mode: if $GITHUB_TOKEN is unset/empty, emit nothing →
# git proceeds anonymously. Public clones still work; private clones
# return 404/403 from github.com.

set -e

case "$1" in
    get)
        if [ -n "$GITHUB_TOKEN" ]; then
            # Strip CR/LF before emitting — `op read` can emit a trailing
            # newline on some versions, and git's credential protocol parses
            # key=value pairs line-by-line, so an extra newline in the
            # password value breaks parsing.
            _token=$(printf '%s' "$GITHUB_TOKEN" | tr -d '\r\n')
            printf 'username=x-access-token\npassword=%s\n' "$_token"
        fi
        ;;
    *)
        # store/erase — explicit no-op for stateless helper.
        ;;
esac
exit 0
