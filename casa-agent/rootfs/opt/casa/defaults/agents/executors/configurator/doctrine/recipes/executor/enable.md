# Enable an executor

Use when the operator wants to switch a bundled-but-disabled
executor (e.g. `plugin-developer`) to enabled.

## Steps

1. Read `agents/executors/<type>/definition.yaml` to confirm the
   executor exists and currently has `enabled: false`.
2. Edit the file: flip `enabled: false` → `enabled: true`.
3. `config_git_commit(message="enable executor: <type>")`.
4. `casa_reload(scope='executors')`. Returns
   `{status: ok, actions: ['rebuild_executor_registry']}`.
5. `emit_completion(text="...", status="ok")`.

## Why scope='executors' and not 'agents'

The `agents` scope rebuilds resident + specialist registries from
`agents/` and `agents/specialists/`. It explicitly excludes the
`executors/` directory (see `reload.py::reload_agents`). The new
`executors` scope (v0.37.1+) targets `executors/` specifically and
rebuilds the `ExecutorRegistry`. Without this scope, the on-disk
flip never reaches the in-process registry and Ellen continues to
report "X isn't enabled".

## What this picks up

- `enabled: false ↔ true` flips.
- `permission_mode` changes.
- `tools.allowed` / `tools.disallowed` edits.
- `model` changes.
- `mcp_server_names` additions/removals.
- `prompt_template_file` path changes (file content is lazy-read
  per turn so prompt-only edits don't need this reload).

## What it does NOT pick up

- New `prompt.md` / `hooks.yaml` / `observer.yaml` / `doctrine/*`
  content (lazy-read per turn).
- New executor TYPES (those need a fresh add of the directory —
  also handled by `executors`).
