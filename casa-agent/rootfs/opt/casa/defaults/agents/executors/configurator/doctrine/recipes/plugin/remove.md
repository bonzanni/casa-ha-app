# Recipe: remove a plugin from one or more agents

Reverses `recipes/plugin/install.md`. Stage 3 only — leaves the
marketplace entry, the system-requirements binaries in
`/addon_configs/casa-agent/tools/`, and any `plugin-env.conf` lines
in place. To clean those too, see § "Full removal" below.

## Ask the user

1. **Plugin name?** Confirm by name.
2. **Targets?** A list of roles to remove from, or omit to remove from
   every agent that has it enabled (the tool auto-detects when
   `targets` is empty).
3. **Confirm:** the plugin's tools, skills, and MCP servers stop
   surfacing immediately after the post-removal reload.

## Uninstall

    uninstall_casa_plugin(
      plugin_name="<name>",
      targets=["<role>", ...],     # or omit for "every home that has it"
    )

Returns `{uninstalled_from: [<role>, ...]}`. Internally runs
`claude plugin uninstall <name>@casa-plugins --scope project` in each
target's `agent-home/<role>/` and rewrites that home's
`enabledPlugins` map in `.claude/settings.json`.

If `uninstalled_from` is shorter than the targets you asked for, one
or more uninstalls failed. Surface the gap to the user — most often
it's a target that didn't have the plugin enabled in the first place.

## Reload — MANDATORY before emit_completion

**Hard** — same reasoning as install. The Claude Code SDK caches
plugin discovery per agent. Canonical order:

    config_git_commit(message="remove <name> plugin from <roles>")
    casa_reload()
    emit_completion(status="ok", text="Removed <name> from <roles>; committed SHA <sha>; called casa_reload.")

Reload before emit_completion — see completion.md. Skipping the reload
leaves the plugin's tools / skills / hooks still surfacing in agents
until the next manual restart.

## Full removal (optional)

The Stage-1 (marketplace entry) and Stage-2 (system-requirements
binaries + `plugin-env.conf` entries) state is intentionally retained
through `uninstall_casa_plugin` so a later reinstall doesn't have to
re-download. If the user wants a clean slate:

1. `marketplace_remove_plugin(plugin_name="<name>")` — drops the
   marketplace entry. See `recipes/plugin/marketplace.md`.
2. `set_plugin_env_reference(plugin="<name>", var_name="<VAR>", op_ref_or_value="")`
   for each previously-set var — clears the `plugin-env.conf` line.
   (The current shape doesn't have a "delete" verb; setting empty
   neutralizes the entry.)
3. The system-requirements binaries persist on disk; the next
   `setup-configs.sh` reconcile pass will mark them orphaned but
   won't delete. Manual cleanup is an operator action, out of scope
   for the configurator.

## Common mistakes

- Removing the marketplace entry before uninstalling. The Stage-3
  uninstall still works (Claude Code's per-home `.claude/settings.json`
  is the source of truth there), but it's confusing — leave the
  marketplace tear-down for last.
- Forgetting `casa_reload()` between `config_git_commit` and
  `emit_completion`. Same trap as install — the disk is correct but
  the running Casa keeps the prior plugin surface.
