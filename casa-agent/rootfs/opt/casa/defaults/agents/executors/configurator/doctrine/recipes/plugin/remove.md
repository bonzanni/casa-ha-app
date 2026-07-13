# Recipe: unassign or remove a plugin

Two tools, two scopes:

- **`plugin_unassign(name, target)`** — drop ONE target's assignment; the
  plugin stays registered (and assigned to its other targets). Use this to stop
  a specific agent from loading a plugin.
- **`plugin_remove(name)`** — remove the plugin from the registry entirely. The
  immutable artifact is retained on disk for GC (`artifact_retained: true`); a
  removed default stays removed across upgrades (the registry's `seeded_defaults`
  bookkeeping is intentionally untouched — no resurrection).

## Do it

1. `plugin_list()` to confirm the name + its current targets.
2. `plugin_unassign(name, target)` or `plugin_remove(name)`.
3. The tool reloads the affected in-casa agents and verifies the plugin is GONE
   from their bindings (an `absent` postcondition). A non-ok result means an
   agent still binds it — surface it.

If the plugin required secrets, clear its plugin-env.conf entries afterward (see
`secrets.md`). **No separate casa_reload is needed** — reload + verify happen
inside the tool. Report the outcome and `emit_completion(...)`.
