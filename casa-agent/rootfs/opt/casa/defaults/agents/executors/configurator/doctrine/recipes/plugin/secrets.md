# Recipe: wire plugin secrets

Most plugins that ship MCP servers declare environment variables in
their `.mcp.json` (API keys, host overrides, vault references). Casa
resolves those at MCP-server-start time from
`/config/plugin-env.conf` via the 1P universal
resolver — `op://...` references are resolved to plaintext, plain
values pass through unchanged.

This recipe covers wiring those vars. The flow is the same whether you
arrived from `recipes/plugin/install.md` Stage 4 or the operator just
asked to rotate a secret on an already-installed plugin.

## When to use

- `plugin_add` (or `plugin_update`) returned a non-empty `required_env_vars` list.
- `verify_plugin_state` reports `mcp_started: false` with one or more
  `secrets[*].status: unresolved`.
- The operator asks to update an existing secret (1P field changed,
  vendor rotated the key, etc.).

## Discover the source

If the operator already has a 1Password reference in mind
(`op://Casa/openai-key/credential`), skip to Set the entry below.
Otherwise help them pick:

    list_vault_items(query="<vendor-or-plugin-keyword>", vault="<vault-name>")
    # → { items: [ { name, id, category, updated_at }, ... ] }

`vault` defaults to the operator's configured `onepassword_default_vault`
(see `config.yaml`). Filter by the operator's keyword — don't enumerate
the whole vault. Return the candidate items and let the operator
choose by name.

Once the item is chosen, list its fields:

    get_item_fields(item="<id-or-title>", vault="<vault-name>")
    # → { fields: [ { label, section, type }, ... ] }

The resolver shells `op read op://<vault>/<id>/<field>` at boot, so the
field label here becomes the third path segment. Plain-typed values
(`type: STRING`, `type: CONCEALED`) work directly — file/document fields
don't.

## Set the entry

    set_plugin_env_reference(
      plugin="<plugin_name>",
      var_name="<VAR>",
      op_ref_or_value="op://<vault>/<id>/<field>",
    )

Or, if the operator wants a literal value:

    set_plugin_env_reference(
      plugin="<plugin_name>",
      var_name="<VAR>",
      op_ref_or_value="<plain-value>",
    )

The tool upserts the line in `plugin-env.conf` — call it once per
required var.

## Verify

    verify_plugin_state(plugin_name="<plugin_name>")

Look at `secrets[*].status`. Every required var should report
`resolved` (with `source: op` for 1P references, `source: plain` for
literals). `status: unresolved` with `reason: "not in plugin-env.conf"`
means a `set_plugin_env_reference` call is still missing.

## Reload — MANDATORY before emit_completion

`plugin-env.conf` is re-sourced into `os.environ` by
`casa_reload(scope='plugin_env')` — sub-second, in-process.
A live agent's MCP-server subprocesses inherit env at next spawn.
Canonical order:

    config_git_commit(message="<plugin>: wire <VAR> via 1Password")
    casa_reload(scope="plugin_env")
    emit_completion(status="ok", text="Wired <VAR> for <plugin>; ready=<bool>; committed SHA <sha>; called casa_reload(scope='plugin_env') to refresh MCP-server env.")

**`plugin-env.conf` is gitignored** (it's a mode-0600 secrets file). So
`config_git_commit` after a secret-only change stages nothing and returns
`sha=""` **plus a `warning` explaining that only whitelisted paths are
tracked — that is expected, NOT a failure. Do not retry the commit.** Still
call it (it's a harmless no-op that keeps the flow uniform), but in
`emit_completion` say "no SHA (secrets file is gitignored)" rather than
reporting a blank `<sha>`.

If you arrived here from the install flow, batch — call
`casa_reload(scope='plugin_env')` first, then `casa_reload(scope='agent', role=<role>)`
per target role at the end of the install.

## Common mistakes

- Setting the var without surfacing it through `get_item_fields` first.
  Misspelled field labels resolve to empty strings and the MCP server
  fails to start with no clear error in the agent log.
- Using `op://` syntax for a literal value, or omitting `op://` for a
  vault reference. The resolver only follows the prefix — anything
  else passes through verbatim.
- Calling `list_vault_items` without a `query`. The vault dump can be
  several hundred items long; constrain the search.
- Forgetting `casa_reload(scope='plugin_env')` between
  `config_git_commit` and `emit_completion`. The file on disk is
  correct but `os.environ` (and thus next MCP-server spawn) keeps the
  prior values.

## Optional keys NOT declared by the plugin (e.g. `context7`)

Some plugins ship an MCP server that **works without a key** and reads an
**optional** API key from the environment — the key is NOT declared in the
plugin's `.mcp.json`, so `plugin_add` returns no `required_env_vars`
and `verify_plugin_state` shows nothing unresolved. The "When to use" triggers
above won't fire, but the operator may still want the key wired (for higher rate
limits / reliability). **`context7`** is the canonical case: its MCP server
(`npx @upstash/context7-mcp`) reads **`CONTEXT7_API_KEY`** from env if present
and otherwise runs keyless (rate-limited).

Wire it exactly like any other secret — the var is **global** (the `plugin` arg
to `set_plugin_env_reference` is a label only; the line is written flat into
`plugin-env.conf` and re-sourced into `os.environ`, which the plugin's MCP
subprocess inherits):

    # operator gives the reference, e.g. op://Casa/Context7/credential
    set_plugin_env_reference(
      plugin="context7",
      var_name="CONTEXT7_API_KEY",
      op_ref_or_value="op://Casa/Context7/credential",
    )
    config_git_commit(message="context7: wire optional CONTEXT7_API_KEY via 1Password")
    casa_reload(scope="plugin_env")
    emit_completion(status="ok", text="Wired CONTEXT7_API_KEY (optional, raises context7 rate limits); no SHA (secrets file gitignored); reloaded plugin_env (set_1_vars).")

**Verify differently:** because context7 declares no required env var,
`verify_plugin_state` won't report it. Confirm instead that `plugin-env.conf`
contains the `CONTEXT7_API_KEY=...` line (it does after `set_plugin_env_reference`).
The key takes effect for the next MCP-server spawn (e.g. the next plugin-developer
engagement). A bad/empty key does NOT break context7 — it falls back to keyless.
