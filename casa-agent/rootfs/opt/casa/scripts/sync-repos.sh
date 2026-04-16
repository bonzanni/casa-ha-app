#!/bin/bash
# /opt/casa/scripts/sync-repos.sh
# shellcheck source=/dev/null
source /usr/lib/bashio/bashio.sh

WORKSPACE="/addon_configs/casa-agent/workspace"

# Read repos from add-on options (JSON array)
repos=$(bashio::config 'repos')

# Exit early if repos is empty, null, or []
if [ -z "$repos" ] || [ "$repos" = "null" ] || [ "$repos" = "[]" ]; then
    bashio::log.info "No repos configured, skipping sync."
    exit 0
fi

count=$(echo "$repos" | jq 'length')

for i in $(seq 0 $((count - 1))); do
    url=$(echo "$repos" | jq -r ".[$i].url")
    path=$(echo "$repos" | jq -r ".[$i].path")
    branch=$(echo "$repos" | jq -r ".[$i].branch // \"main\"")
    target="$WORKSPACE/$path"

    if [ ! -d "$target/.git" ]; then
        # First boot: clone
        bashio::log.info "Cloning $url -> $path"
        mkdir -p "$(dirname "$target")"
        if ! timeout 30 git clone --depth 1 --branch "$branch" "$url" "$target"; then
            bashio::log.warning "Failed to clone $url -- skipping"
            continue
        fi
    else
        # Subsequent boot: pull latest (skip if local changes)
        bashio::log.info "Updating $path from $url"
        if git -C "$target" diff --quiet && git -C "$target" diff --cached --quiet; then
            if ! git -C "$target" pull origin "$branch"; then
                bashio::log.warning "Failed to pull $path -- using existing version"
            fi
        else
            bashio::log.warning "$path has local changes -- skipping pull"
        fi
    fi
done

bashio::log.info "Repo sync complete."
