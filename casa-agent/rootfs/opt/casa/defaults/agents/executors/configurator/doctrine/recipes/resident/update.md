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

**Disabled-but-consultable specialist memory (Phase 5 / E-15, v0.26.1+):**
When an operator flips a specialist's `enabled: true → false` (e.g.
disabling Finance to stop new delegations), the specialist's peer-level
Honcho memory is NOT torn down — it stays persisted in Honcho. Any
resident with `consult_other_agent_memory` in `tools.allowed` can still
recall what the disabled specialist previously knew about the user; the
tool falls through to `cross_peer_context` instead of returning
`unknown_role`.

This is intentional: memory is data, enablement is operational. The
trade-off (a disabled specialist's accumulated facts remain readable)
is acknowledged in spec § 3.3. If a future deploy needs a hard gate
(disabled = unconsultable), open a follow-up to add a
`cfg.allow_memory_when_disabled: bool` field on the specialist's
runtime.yaml — currently out of scope.

## Always — MANDATORY order

1. Commit via `config_git_commit`.
2. Reload per `reload.md` — **before** emit_completion (canonical
   order). Skip only for none-reload changes (prompts,
   response_shape, scopes_readable additions).
3. `emit_completion` with status=ok, text citing the SHA + the reload
   that ran (or "no reload — none-scope change" if applicable).

See `completion.md`.
