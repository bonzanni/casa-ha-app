# Casa architecture (what you're configuring)

## Directory layout

Everything you edit lives under `/addon_configs/casa-agent/`:

    /addon_configs/casa-agent/
    agents/
      <resident-role>/               # flat - tier 1 (e.g. assistant, butler)
        character.yaml
        runtime.yaml
        delegates.yaml
        disclosure.yaml
        response_shape.yaml
        voice.yaml
        triggers.yaml              # optional
        hooks.yaml                 # optional
        prompts/
          system.md
          <trigger-name>.md        # one per scheduled/webhook trigger
      specialists/
        <role>/                    # tier 2 (e.g. finance)
          character.yaml
          runtime.yaml
          response_shape.yaml
          voice.yaml
          hooks.yaml               # optional
          prompts/system.md
      executors/
        <type>/                    # tier 3 (e.g. configurator) - this is you
          definition.yaml
          prompt.md
          hooks.yaml               # optional
          observer.yaml            # optional
          doctrine/                # your own knowledge base
    policies/
      scopes.yaml
      disclosure.yaml
    schema/
      *.v1.json                    # READ-ONLY - editing breaks loaders

Read-only to you (hook-blocked): `/data/**` (runtime state), `/addon_configs/casa-agent/schema/**`, `/opt/casa/**`.

## Tier taxonomy

| Tier | Name | What it is | Where it lives |
|---|---|---|---|
| 1 | Resident | Long-lived agent owning a channel (Ellen=telegram+voice, Tina=voice). Has scopes, memory budget, delegates (to residents and specialists). | agents/<role>/ |
| 2 | Specialist | Role-keyed helper (e.g. finance/Alex). Called by residents via delegate_to_agent. No channel, no scopes_owned, ephemeral session. | agents/specialists/<role>/ |
| 3 | Executor | Task-bounded, ephemeral agent (e.g. you - configurator). Engaged via engage_executor. Runs in a dedicated Telegram topic. | agents/executors/<type>/ |

Any resident may delegate (`delegate_to_agent`) to any other agent listed in its `delegates.yaml`. Only the assistant (Ellen) may engage executors via `executors.yaml`.

## Key files per tier

| File | Resident | Specialist | Executor |
|---|---|---|---|
| character.yaml | required | required | forbidden (uses definition.yaml) |
| runtime.yaml | required | required | forbidden (fields in definition.yaml) |
| delegates.yaml | required | forbidden | forbidden |
| executors.yaml | required (assistant only) | forbidden | forbidden |
| disclosure.yaml | required | forbidden | forbidden |
| response_shape.yaml | required | required | forbidden |
| voice.yaml | required | required | forbidden |
| triggers.yaml | optional | forbidden | forbidden |
| hooks.yaml | optional | optional | optional |
| prompts/system.md | required | required | forbidden (uses prompt.md) |
| prompts/<name>.md | per-trigger | - | - |
| definition.yaml | forbidden | forbidden | required |
| prompt.md | forbidden | forbidden | required |
| observer.yaml | forbidden | forbidden | optional |

agent_loader.py enforces these rules. Adding a forbidden file or removing a required file makes the agent fail to load.

## Memory wiring per tier (v0.16.0 — Memory M4)

Residents (Tier 1) own per-scope sessions and read summaries from the
`meta` system scope each turn. Specialists (Tier 2) were stateless per
delegation through v0.16.0 — *Specialist memory (M4b, v0.17.0)* below
documents the per-`(role, user_peer)` Honcho memory opt-in. Tier 3
Executors are ephemeral per engagement, but may opt in to a
per-(channel, chat, executor_type) **archive** of prior engagement
summaries.

**Scope kinds.** `policies/scopes.yaml` v2 declares each scope with
`kind: topical | system`:

- `kind: topical` — embedded by fastembed; classifier picks active scopes
  per turn against the user utterance. `description` required.
- `kind: system` — always-on for any agent that includes the scope in
  `scopes_readable` and clears the trust gate. No embedding, no classifier
  routing. `description` forbidden. Only `meta` is system today.

**Executor memory opt-in.** A Tier 3 executor's `definition.yaml` may
include:

    memory:
      enabled: true       # default false
      token_budget: 2000

When `enabled: true`, `engage_executor` reads the archive at
`{channel}-{chat_id}-executor-{type}` (built via `honcho_session_id`) and substitutes the digest into the
prompt template's `{executor_memory}` slot before driver dispatch. The
archive is populated by `_finalize_engagement` (one summary per terminal
engagement) — no separate writer code.

For Configurator (you), `memory.enabled: true` is shipped — every
engagement starts with the prior engagement summaries already in your
prompt under "## Prior engagements (lessons learned)".

## Specialist memory (M4b, v0.17.0)

Specialists carry **per-`(role, user_peer)` Honcho memory** —
channel-agnostic, scope-agnostic, mixed-domain. Session id is
`f"{role}-{user_peer}"` (2-segment, distinct from residents'
4-segment `{channel}-{chat_id}-{scope}-{role}`). Both shapes are
built via `honcho_session_id` to satisfy Honcho's
`^[A-Za-z0-9_-]+$` server-side regex.

Honcho's existing `observe_others=True`-on-agent-peer setup
(`memory.py:185-204`) populates `peer_representation` automatically
over time, giving each specialist a domain-narrow theory-of-mind of
the user. `search_query` retrieval at read time is scoped to the
specialist's own corpus — denser per token than searching a
coordinator's mixed-domain memory.

Specialists do **not** participate in scope partitioning; their
`scopes_owned` and `scopes_readable` MUST stay empty
(`agent_loader.py:562-573` enforces this). Specialists also do **not**
participate in trust filtering at the memory layer — trust is one
level up at the resident's `delegates` decision.

Enabling memory on a specialist: set `memory.token_budget > 0` in
`runtime.yaml`. Disabling: set `token_budget: 0` (back to stateless;
prior session messages stay in Honcho but are no longer read).

## MCP service topology (v0.14.0)

The `casa-framework` MCP server runs as its own s6-supervised service
called `svc-casa-mcp`, NOT inside casa-main. It listens on
`127.0.0.1:8100` and forwards every tool call and hook decision to
casa-main over a Unix socket at `/run/casa/internal.sock`.

This means:
- An engagement subprocess's MCP TCP connection survives a casa-main
  restart (addon update, in-container respawn). Mid-restart tool calls
  return JSON-RPC `-32000 casa_temporarily_unavailable` (a recoverable
  error the model handles), not a connection drop.
- Casa-main's public port 8099 still serves `/mcp/casa-framework` and
  `/hooks/resolve` as a back-compat fallback for pre-v0.14.0 workspaces;
  these routes will be removed in v0.14.2 or later.
- New engagement workspaces have `.mcp.json` pointing at port 8100;
  pre-v0.14.0 workspaces still point at 8099 and continue to function
  via the fallback.

You (Configurator) do NOT need to touch any of this — workspace
provisioning + hook proxying are framework concerns. If a user asks why
their engagement survived a Casa restart cleanly, this is the reason.

## Plugin consumer infrastructure (v0.14.1)

Casa uses Claude Code's native plugin machinery via a two-marketplace model.

### Two marketplaces

- **`casa-plugins-defaults`** — seed-managed, read-only. Catalog at
  `/opt/casa/defaults/marketplace-defaults/.claude-plugin/marketplace.json`.
  Pre-installed cache at `/opt/claude-seed/` (built at image build, `chmod a-w`).
  Contains: superpowers, plugin-dev, skill-creator, mcp-server-dev, document-skills.
  Configurator MUST NOT mutate this marketplace.
- **`casa-plugins`** — user-writable. Catalog at
  `/addon_configs/casa-agent/marketplace/.claude-plugin/marketplace.json`.
  Configurator mutates via `marketplace_ops` helpers (add/remove/update).

### Plugin cache

- Seed cache: `/opt/claude-seed/cache/casa-plugins-defaults/<plugin>/<version>/`
  (read-only, baked into image).
- User cache: `/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/<plugin>/<version>/`
  (managed by `claude plugin install --scope user`).

### Binding layer

`/opt/casa/plugins_binding.py::build_sdk_plugins` — shells
`claude plugin list --json`, translates `installPath` entries into SDK
`plugins=[{"type":"local","path":...}]` shape. Called from every
`ClaudeAgentOptions` construction in `agent.py` and `tools.py`.

### Per-agent enablement

Each in_casa agent has a project-scope settings.json at
`/addon_configs/casa-agent/agent-home/<role>/.claude/settings.json` with:

```json
{"enabledPlugins": {"<plugin>@<marketplace>": true}}
```

Provisioned at boot by `casa_core.provision_agent_home()` — idempotent,
preserves user-added entries.

### Workspace-template (claude_code executors)

For Tier 3 executors, `defaults/agents/executors/<type>/workspace-template/`
is rendered per engagement via `drivers.workspace.render_workspace_template`.
`CLAUDE.md.tmpl` is interpolated with `{task}/{context}/{world_state_summary}/{executor_type}`.
`.claude/settings.json::enabledPlugins` is generated from the executor's
`plugins.yaml`.

### Plugin environment variables

`/addon_configs/casa-agent/plugin-env.conf` — POSIX `VAR=value` lines,
mode 0600, managed by Configurator via `set_plugin_env_reference` MCP tool.
Values accept `op://vault/item/field` references; resolved at boot by
`secrets_resolver.resolve`. Sourced into the addon process env before
SDK clients spawn.

### System requirements (plugin-runtime tools)

Plugins may declare `casa.systemRequirements` in their marketplace entry:

- `{type: tarball, url, sha256, verify_bin}` — HTTPS download + sha256 check.
- `{type: venv, package, verify_bin}` — `python -m venv` + pip install.
- `{type: npm, package, verify_bin}` — `npm install --prefix` + .bin symlink.
- `{type: apt}` — **REJECTED at marketplace-add time** (§4.3.2 / P-10).

Install into `/addon_configs/casa-agent/tools/` with symlinks in `tools/bin/`
(on `PATH` via `/run/s6/container_environment/PATH`).

### Manifest + reconciler

`/addon_configs/casa-agent/system-requirements.yaml` records
`{name, winning_strategy, install_dir, verify_bin, pin_sha256?, pin_version?, declared_at}`
for every installed requirement. `setup-configs.sh` invokes
`scripts/reconcile_system_requirements.py` at each boot to check
`verify_bin` resolves; writes `system-requirements.status.yaml` and exits
non-zero on any `degraded`. Non-blocking (degrades affected plugins,
never crashes boot).

### Configurator MCP tools (v0.14.1)

These tools are wired into your `definition.yaml::tools.allowed` and
exercised through the recipes under `recipes/plugin/` (install,
remove, marketplace, secrets).

Core:
- `marketplace_add_plugin` — append entry to user marketplace + refresh CC.
- `marketplace_remove_plugin` — remove from user marketplace.
- `marketplace_update_plugin` — bump ref on existing entry.
- `marketplace_list_plugins` — enumerate user marketplace entries.
- `install_casa_plugin` — two-stage commit: stage 1 = systemRequirements,
  stage 2 = `claude plugin install --scope project` per target agent-home.
  Stage 2 failure rolls back stage 1 via `rmtree`.
- `uninstall_casa_plugin` — `claude plugin uninstall --scope project`
  per target.
- `verify_plugin_state` — per-plugin readiness report: tools +
  secrets (from plugin-env.conf) + mcp_started + overall `ready` bool.

Helpers:
- `set_plugin_env_reference` — upsert `VAR=value` in plugin-env.conf.
- `list_vault_items` — `op item list --format json` filtered.
- `get_item_fields` — `op item get --format json` field metadata.

See `recipes/plugin/install.md` for the canonical install flow,
`recipes/plugin/remove.md` for uninstall, `recipes/plugin/marketplace.md`
for marketplace-only operations, and `recipes/plugin/secrets.md` for
wiring plugin env vars via 1Password references.

### 1P universal resolver

All password-typed addon options (`claude_oauth_token`, `honcho_api_key`,
`telegram_bot_token`, `webhook_secret`, `github_token`) accept
`op://vault/item/field` references. Resolved at boot by
`secrets_resolver.resolve` (lru_cache, shells `op read`).
`onepassword_service_account_token` is the single root of trust —
plaintext only.
