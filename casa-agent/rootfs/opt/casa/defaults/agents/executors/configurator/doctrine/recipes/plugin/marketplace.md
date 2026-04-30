# Recipe: manage the user marketplace

The user marketplace lives at
`/addon_configs/casa-agent/cc-home/.claude/plugins/marketplaces/casa-plugins/`
and lists every plugin Casa knows about. `install_casa_plugin` only
operates on plugins already in the marketplace, so adding/updating
marketplace entries is a precondition for the install flow. Most of
the time you'll do this inline as part of `recipes/plugin/install.md`;
this recipe is for marketplace-only operations (browse, bump pin,
unregister without uninstalling).

## List entries

    marketplace_list_plugins()

Returns `{plugins: [<entry>, ...]}` — every entry currently in the
user marketplace. Each entry includes `name`, `description`, `version`,
`source.repo`, `source.sha`, and `category`. Use this to confirm a
plugin is registered before calling `install_casa_plugin`, or as the
"what's available" answer when the operator asks.

## Register a new entry

    marketplace_add_plugin(
      plugin_name="<name>",
      repo_url="https://github.com/<owner>/<repo>",
      ref="<sha-or-branch>",
      description="<one-line>",
      category="productivity",          # optional; defaults to productivity
      version="<semver-or-ref>",        # optional; defaults to ref
      casa_system_requirements=[...],   # optional; pass through from plugin's casa.systemRequirements
    )

The tool normalizes `repo_url` to the `<owner>/<repo>` shape under
the hood and runs `claude plugin marketplace update casa-plugins` so
Claude Code's view stays in sync. Returns `{added: bool, entry?, error?}`.

`casa_system_requirements` mirrors the plugin's `plugin.json::casa.systemRequirements`
block. Without it, `install_casa_plugin`'s Stage 2 is a no-op for the
plugin — fine for plugins that ship pure markdown/skills, required
for plugins that need binaries (NPM packages, Python venvs, fastembed
weights, etc.).

## Update the pin

    marketplace_update_plugin(
      plugin_name="<name>",
      new_ref="<sha-or-branch>",
    )

Bumps the `source.sha` and `version` fields, then refreshes Claude Code's
view. The next `install_casa_plugin` for this plugin pulls the new ref.
**Updating the pin does not reinstall existing homes** — operators who
want the new ref live must `uninstall_casa_plugin` + `install_casa_plugin`
in sequence (or call `casa_reload()` if the new ref is already cached).

## Unregister an entry

    marketplace_remove_plugin(plugin_name="<name>")

Removes the entry from the marketplace. **Does not uninstall the plugin
from any agent home.** If the operator's intent is "remove from
everywhere", run `recipes/plugin/remove.md`'s "Full removal" sequence.

## Reload

Marketplace edits don't change agent runtime — no Casa reload needed
when the operation is marketplace-only. Always commit the change:

    config_git_commit(message="marketplace: <add|update|remove> <name>")
    emit_completion(status="ok", text="<one-line summary>.")

If the marketplace edit is the prelude to an install, batch the
commit + reload at the end of the install flow instead (see
`recipes/plugin/install.md`).
