# Recipe: install a plugin into one or more agents

A Casa plugin is a Claude Code plugin (commands / skills / hooks /
MCP servers) consumed by Casa agents. Plugins are produced by the
`plugin-developer` executor (or an external GitHub repo) and installed
into per-agent Claude Code homes by you.

Install is a four-stage flow: marketplace entry → system requirements
→ per-agent install → secrets wiring → readiness check. The
`install_casa_plugin` tool runs stages 1-3 atomically with rollback on
failure; you handle stages 4-5 explicitly.

## Ask the user

1. **Plugin name?** Lowercase, hyphenated (e.g. `casa-probe-greet`).
2. **Source repo?** A GitHub URL like
   `https://github.com/<owner>/<name>` or `<owner>/<name>`. (For a
   plugin-developer build, take the URL from that engagement's topic.)
3. **Pin?** A git ref (sha, tag, or branch). Prefer a sha for
   reproducibility; `main` is acceptable for a probe.
4. **Targets?** One or more agent roles (e.g. `assistant`, `butler`).
   Each target must already have an agent home at
   `/addon_configs/casa-agent/agent-home/<role>/`. Residents only —
   specialists and executors don't carry plugins.
5. **One-line description?** Stored in the marketplace entry; shown
   later by `marketplace_list_plugins`.

If the plugin needs API keys or per-host secrets, surface that now and
follow the Secrets stage below before declaring done.

## Stage 1 — register in the user marketplace

If the plugin is not yet known, add it:

    marketplace_add_plugin(
      plugin_name="<name>",
      repo_url="https://github.com/<owner>/<repo>",
      ref="<sha-or-branch>",
      description="<one-line>",
      category="productivity",          # optional; defaults to productivity
      version="<semver-or-ref>",        # optional; defaults to ref
      casa_system_requirements=[...],   # optional; from plugin's casa.systemRequirements
    )

If the plugin is already in the marketplace and the operator wants a
newer pin, use `marketplace_update_plugin(plugin_name, new_ref)` instead.
See `recipes/plugin/marketplace.md` for marketplace-only operations.

## Stage 2-3 — install into agent-homes

    install_casa_plugin(
      plugin_name="<name>",
      targets=["<role>", ...],
    )

`install_casa_plugin` runs system-requirements provisioning first
(Stage 2 — populates `/addon_configs/casa-agent/tools/` and writes
`system-requirements.yaml`), then `claude plugin install --scope project`
in each target's `agent-home/<role>/` (Stage 3 — flips the role's
`enabledPlugins` map). On Stage-3 failure the tool rolls back Stage 2
via `rmtree`. The result is `{ok: bool, installed_on, required_env_vars,
system_requirements_installed}`.

If `ok: false` with `error: plugin_not_in_marketplace`, run Stage 1.
If `ok: false` with `error: system_requirements_failed` or
`agent_install_failed`, surface the detail to the user; do not retry
silently — system requirements failures usually mean a network issue
or a malformed `casa.systemRequirements` block in the marketplace
entry.

## Stage 4 — wire secrets (only if `required_env_vars` is non-empty)

The install result lists every env var the plugin's `.mcp.json`
declares. Each must resolve before the plugin's MCP servers will start.

For each var, ask the user whether to bind to a 1Password reference
(preferred) or a literal value:

    list_vault_items(query="<plugin-or-vendor-keyword>")
    # → operator picks an item id by name

    get_item_fields(item="<id>")
    # → operator picks the field that holds the secret

    set_plugin_env_reference(
      plugin="<name>",
      var_name="<VAR>",
      op_ref_or_value="op://<vault>/<item-id>/<field>",
    )

A literal value is also valid (`op_ref_or_value="<plain-value>"`); the
`set_plugin_env_reference` tool just upserts the line into
`/addon_configs/casa-agent/plugin-env.conf`. Casa resolves `op://...`
references at MCP-server-start time via the 1P universal resolver — see
`architecture.md` § "1P universal resolver".

Repeat for every var in `required_env_vars`. Skipping any will leave
the plugin's MCP server unable to start.

## Stage 5 — verify readiness

    verify_plugin_state(plugin_name="<name>")

Returns `{tools, secrets, mcp_started, mcp_errors, ready}`. `ready: true`
means every system requirement resolved, every required secret has a
plugin-env entry, and the plugin's `.mcp.json` is in the agent's cache.
If `ready: false`, the per-section status fields tell you what's
missing — re-run the relevant earlier stage.

## Reload — MANDATORY before emit_completion

**Hard** — installing a plugin changes per-agent runtime state that the
loader caches (Claude Code SDK plugin discovery in particular). Canonical order:

    config_git_commit(message="install <name> plugin into <roles>")
    casa_reload()
    emit_completion(status="ok", text="Installed <name> on <roles>; ready=<bool>; committed SHA <sha>; called casa_reload to refresh SDK option builders.")

`casa_reload()` returns immediately with `supervisor_status: 200,
deferred: true`. The platform defers the actual Supervisor restart
until after `emit_completion` runs and the engagement finalizes, so
the user's "Done" relay always lands before the container is killed.
Skipping the reload leaves the plugin installed on disk but **not
surfaced in the running agents** — the new tools / skills / hooks do
not appear until the next manual restart.

## Common mistakes

- Skipping Stage 4 when `required_env_vars` is non-empty. The MCP
  server fails silently at next agent boot; `verify_plugin_state`
  reports `mcp_started: false`.
- Targeting a specialist or executor role. Plugin install only makes
  sense for residents (Tier 1) — they're the agents Claude Code SDK
  consumes plugins through.
- Skipping `casa_reload()` between `config_git_commit` and
  `emit_completion`. The git tree on disk is correct but the running
  Casa keeps the prior agent option builders. Same trap as a YAML edit
  without reload (see `reload.md`). The model often treats
  `emit_completion` as the terminal step and skips the reload entirely
  — don't.
- Soft-reload (`casa_reload_triggers`) instead of hard. Triggers reload
  is for trigger YAML edits only — plugins need the full hard reload.
