#!/bin/sh
# resolve-op-ref.sh — resolve op:// references in bash context.
# Used by bashio-side init scripts that read addon options.
set -e

VALUE="$1"
case "$VALUE" in
    op://*)
        if [ -z "$OP_SERVICE_ACCOUNT_TOKEN" ]; then
            echo "resolve-op-ref: OP_SERVICE_ACCOUNT_TOKEN unset; cannot resolve $VALUE" >&2
            exit 1
        fi
        op read "$VALUE"
        ;;
    *)
        echo "$VALUE"
        ;;
esac
