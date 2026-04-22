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

## Always

- Commit.
- emit_completion.
- Reload per reload.md.
