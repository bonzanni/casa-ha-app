# Reload granularity

Not every change needs a full addon restart. Choose the minimum reload that propagates your change.

## Three reload scopes

| Scope | Tool | Downtime | When to use |
|---|---|---|---|
| none | - | 0 | Prompts, response_shape, doctrine - lazy-read per turn |
| soft | casa_reload_triggers(role) | 0 | triggers.yaml edits for an EXISTING agent |
| hard | casa_reload() | ~10-15s | Everything structural |

## What requires what

| Change | Reload |
|---|---|
| Edit prompts/system.md or prompts/<trigger>.md | none |
| Edit response_shape.yaml | none |
| Edit executor's doctrine/*.md | none |
| Edit existing agent's triggers.yaml (no other change) | soft |
| Edit character.yaml | hard |
| Edit runtime.yaml | hard |
| Edit delegates.yaml | hard |
| Edit disclosure.yaml | hard |
| Edit voice.yaml | hard |
| Edit hooks.yaml | hard |
| Edit policies/scopes.yaml | hard |
| Edit policies/disclosure.yaml | hard |
| Create a NEW agent | hard |
| Delete an agent | hard |
| Install or remove a plugin (`install_casa_plugin` / `uninstall_casa_plugin`) | hard |
| Set a plugin env var (`set_plugin_env_reference`) | hard |

## Order of operations — MANDATORY

1. Make your file edits.
2. Call `config_git_commit(message=...)`.
3. Call the appropriate reload tool **before** `emit_completion`.
4. Call `emit_completion(...)` with the summary.

**Never call `emit_completion` BEFORE the reload step.** The model
treats `emit_completion` as the terminal action; once it fires, the
engagement closes and you do not get another chance to call the reload.
A skipped reload leaves the artifact **committed but inert** — YAML on
disk is right, but the running Casa keeps the prior runtime. The
trigger never fires; the new agent never loads. See `completion.md` for
the canonical full sequence.

For hard reload, the addon will restart asynchronously a few seconds
after `casa_reload()` returns. Your `emit_completion` call has time to
land on the bus (which persists across restart) before the kill — but
do not interpose extra Read/Bash tool_uses between `casa_reload` and
`emit_completion`.

## When in doubt

- If the request touches only triggers for one agent -> soft.
- If the request touches anything else OR creates/deletes an agent -> hard.
- Two changes on the same engagement? Pick the strongest reload needed.
- Unsure? Hard reload is always safe (just slower).
