# Recipe: update an existing resident

Residents own channels and are long-lived. Updating scoped fields is safe; touching channels/scopes_owned/session.strategy has wider implications.

## Low-risk updates

- Persona prompt - **no reload** (lazy-read per turn).
- Model - `casa_reload(scope='agent', role=<role>)`.
- Response shape - **no reload**; voice - `casa_reload(scope='agent', role=<role>)`.
- Adding a trigger - see recipes/trigger/add.md. `casa_reload_triggers(role=<role>)`.
- Adding a delegate - see recipes/delegate/wire.md. `casa_reload(scope='agent', role=<role>)`.

## Higher-risk updates (ask user explicitly)

- Changing channels - requires matching HA config for TTS/STT.
- Changing memory.scopes_owned or scopes_readable - rebalances visibility.
- Changing session.strategy - material behavior change.

For any of these, do a dry-run summary in the topic first.

## Adding `meta` to scopes_readable (engagement-memory access — M4)

If the user wants this resident to start seeing engagement summaries
("Ellen, what's been engaged lately?"), add `meta` to
`memory.scopes_readable` (NOT `scopes_owned`). Trust gate is
`authenticated`, so `household-shared`-trust residents (voice) are a
no-op even if added.

This is a **none reload** in M4 v0.16.0+ — the scope partition is
re-evaluated each turn from `scopes_readable`.

## Always — MANDATORY order

1. Commit via `config_git_commit`.
2. Reload per `reload.md` — **before** emit_completion (canonical
   order). Skip only for none-reload changes (prompts,
   response_shape, scopes_readable additions).
3. `emit_completion` with status=ok, text citing the SHA + the reload
   that ran (or "no reload — none-scope change" if applicable).

See `completion.md`.
