# Reload granularity

Casa supports in-process reload at six scopes. None of them restart the
addon. For changes that genuinely need a process restart, use
`casa_restart_supervised` (rare).

## Six reload scopes

| `scope` | Tool | Downtime | Required `role` | When to use |
|---|---|---|---|---|
| `agent` | `casa_reload(scope='agent', role=...)` | <1s | yes | character/runtime/delegates/disclosure/voice/hooks/response_shape edits for ONE role; plugin install/uninstall on ONE role |
| `triggers` | `casa_reload_triggers(role=...)` | <1s | yes | triggers.yaml edits for an EXISTING agent (legacy alias for `casa_reload(scope='triggers', role=...)`) |
| `policies` | `casa_reload(scope='policies')` | <1s | no | `policies/disclosure.yaml` or `policies/scopes.yaml` edits |
| `plugin_env` | `casa_reload(scope='plugin_env')` | <1s | no | `set_plugin_env_reference` calls / `plugin-env.conf` edits |
| `agents` | `casa_reload(scope='agents')` | <1s | no | created or deleted ANY resident or specialist directory under `agents/` |
| `full` | `casa_reload(scope='full')` | <1s | no | catch-all when unsure or multiple categories edited |

`casa_restart_supervised` (~10-15s) is reserved for s6 service-tree
changes, addon options.json mutations, or kernel concerns.

## What requires what

| Change | Reload |
|---|---|
| Edit prompts/system.md or prompts/<trigger>.md | none (lazy-read per turn) |
| Edit response_shape.yaml | none |
| Edit executor's doctrine/*.md | none |
| Edit user marketplace.json (`marketplace_*` tools) | none — read on demand |
| Edit existing agent's triggers.yaml (no other change) | `triggers` |
| Edit character.yaml | `agent` for that role |
| Edit runtime.yaml | `agent` for that role |
| Edit delegates.yaml | `agent` for that role |
| Edit disclosure.yaml | `agent` for that role |
| Edit voice.yaml | `agent` for that role |
| Edit hooks.yaml | `agent` for that role |
| Edit policies/scopes.yaml | `policies` |
| Edit policies/disclosure.yaml | `policies` |
| Create a NEW resident or specialist | `agents` |
| Delete a resident or specialist | `agents` |
| `install_casa_plugin` for a role | `agent` for that role |
| `uninstall_casa_plugin` for a role | `agent` for that role |
| `set_plugin_env_reference` | `plugin_env` |
| Multiple categories edited in one engagement | `full` |
| Unsure | `full` |

## Order of operations — MANDATORY

1. Make your file edits.
2. Call `config_git_commit(message=...)`.
3. Call the appropriate `casa_reload(scope=...)` tool **before** `emit_completion`.
4. Call `emit_completion(...)` with the summary.

**Never call `emit_completion` BEFORE the reload step.** The model
treats `emit_completion` as the terminal action; once it fires, the
engagement closes and you do not get another chance to call the reload.
A skipped reload leaves the artifact **committed but inert**. See
`completion.md`.

`casa_reload(...)` returns immediately with `{status: "ok", ms: <int>,
actions: [...]}`. There is no Supervisor restart, so no race against
your subprocess being killed mid-emission.

## When in doubt

- Touched only triggers for one agent → `triggers`.
- Touched a single role's other YAMLs → `agent` for that role.
- Touched policies/*.yaml → `policies`.
- Created or deleted an agent → `agents`.
- Touched `plugin-env.conf` (via `set_plugin_env_reference`) → `plugin_env`.
- Touched anything else, or multiple of the above → `full`.
- Need a process restart (rare) → `casa_restart_supervised`.

`full` is always safe — does policies + agents + per-role agent. Add
`include_env=True` to also re-source plugin-env.conf.
