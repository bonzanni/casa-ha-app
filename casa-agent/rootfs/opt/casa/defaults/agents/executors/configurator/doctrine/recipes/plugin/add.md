# Recipe: add a plugin to the registry

A Casa plugin is a Claude Code plugin (commands / skills / hooks / MCP
servers) consumed by Casa agents. Under the unified plugin architecture the
registry (`/config/plugins/registry.json`) is the single assignment authority:
one `plugin_add` call publishes an immutable, content-addressed artifact,
installs any system requirements, assigns it to targets, and reloads + verifies
— all inside the tool.

## Ask the user

1. **Plugin name?** Lowercase, hyphenated (e.g. `casa-probe-greet`) — must equal
   the plugin's own `plugin.json` `name`.
2. **Source repo?** `<owner>/<name>` (or a `https://github.com/<owner>/<name>`
   URL). For a plugin-developer build, take it from that engagement's topic.
3. **Pin (ref)?** A git ref (sha, tag, or branch). Prefer a tag/sha for
   reproducibility; the tool resolves it to an exact commit.
4. **Targets?** One or more of `resident:<role>`, `specialist:<role>`,
   `executor:<type>` (e.g. `specialist:finance`).

## Do it

Call `plugin_add(name, repo, ref, subdir?, targets)`. There is **no version
argument** — the version is derived from the fetched `plugin.json` (a caller-
supplied version is exactly the stale-code bug this architecture removes).

The tool, internally and in order: resolves `ref`→commit (a 404 is a hard
`ref_not_found`, never silent), fetches + validates + atomically publishes the
artifact, installs system requirements BEFORE activation, writes the registry
entry, then reloads the affected in-casa agents and verifies desired==active.
**No separate casa_reload is needed** — reload + verify happen inside
`plugin_add` (and `plugin_update` / `plugin_assign` / `plugin_unassign` /
`plugin_remove`).

## Result

The result carries `artifact_id`, `version`, `revision`, `granted_tools`,
`required_env_vars`, and a `verify` summary. If `required_env_vars` is
non-empty, wire the secrets next (see `secrets.md`) — the plugin's MCP server
won't start without them. Then read `verify_plugin_state(name)`: `ready:true`
means every target agrees. Report the outcome and `emit_completion(...)`.
