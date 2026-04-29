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

## Adding `consult_other_agent_memory` (cross-role recall — M6)

If the user wants this resident to be able to ask "what does Finance/
Health/etc. know about me?" without spawning a full agent turn, add
the cross-role recall tool:

```yaml
tools:
  allowed:
    - mcp__casa-framework__consult_other_agent_memory   # M6 — added
    # ... existing tools

memory:
  cross_peer_token_budget: 2000   # M6 — defaults to 2000 if omitted
  # ... existing memory fields
```

This is a **none reload** in M6 v0.18.0+ — the tool surface is
re-read at agent-load time.

**Trust gate is structural.** Do NOT add to voice-only / `household-
shared`-trust residents — the voice channel is open to guests. The
tool flows directly from `tools.allowed`; there's no per-channel
filter at the memory layer. Same pattern M4b uses for delegation
(`delegates`).

Also update the resident's `prompts/system.md` with the routing-rule
paragraph (Case 1: cross-role recall via this tool; Case 2: delegate
when the answer needs the specialist's tools). Copy the canonical
Ellen paragraph as a starting point.

## Always

- Commit.
- emit_completion.
- Reload per reload.md.
