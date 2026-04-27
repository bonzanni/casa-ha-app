# Recipe: update an existing resident

Residents own channels and are long-lived. Updating scoped fields is safe; touching channels/scopes_owned/session.strategy has wider implications.

## Low-risk updates

- Persona prompt - **none reload**.
- Model - **hard reload**.
- Response shape - **none reload**; voice - **hard**.
- Adding a trigger - see recipes/trigger/add.md. **soft reload**.
- Adding a delegate - see recipes/delegate/wire.md. **hard reload**.

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

## Always

- Commit.
- emit_completion.
- Reload per reload.md.
