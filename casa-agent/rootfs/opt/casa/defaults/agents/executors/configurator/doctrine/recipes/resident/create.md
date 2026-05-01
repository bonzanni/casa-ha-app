# Recipe: create a new resident

RARE. Tier 1 agents are usually enough (Ellen + Tina). Adding a third rebalances scopes and channels.

## Ask the user

1. **Why?** Is a specialist the right answer instead?
2. **Role name?** Lowercase.
3. **Channels?** telegram, voice, both? Must be configured on HA side.
4. **Scopes owned?** This is zero-sum - ownership moves from another resident. Be explicit.
5. **Scopes readable?** Can overlap with another resident's owned.
6. **Session strategy?** durable (like Ellen) or ephemeral.
7. **Token budget?**

Confirm the rebalance explicitly. Wait for "yes, do that" before proceeding.

## Files to create

Under /addon_configs/casa-agent/agents/<role>/:

1. character.yaml
2. runtime.yaml
3. delegates.yaml
4. disclosure.yaml
5. response_shape.yaml
6. voice.yaml
7. prompts/system.md
8. Optional: triggers.yaml, hooks.yaml

## Also update the OTHER resident

Scope rebalance may require editing another resident's memory.scopes_owned/readable. Same commit.

## Reload — MANDATORY before emit_completion

**Hard**, big one (scope registry rebuild). Canonical order:

1. config_git_commit(message="add resident <role> with scope <scope>")
2. casa_reload()
3. emit_completion(status="ok", text="Added resident <role>; committed SHA <sha>; called casa_reload to rebuild the scope registry.")

Skipping the reload leaves the new resident on disk but **not in the
live channel/scope routing** — see completion.md.

## Register handling for delegated turns

Any resident's `prompts/system.md` should honor the framework-injected
`<delegation_context>::suggested_register` hint. When invoked via another
agent's `delegate_to_agent` call, the calling channel is text → answer in
conversational text register; voice → answer in spoken register. See
butler/prompts/system.md for the canonical paragraph; copy or adapt it
when authoring a new resident.

## Engagement-memory readability (M4, v0.16.0)

If the new resident may be a target of `engage_executor` engagements
(today: only assistant/Ellen, but a future second resident could
acquire that capability), include `meta` in `memory.scopes_readable`:

```yaml
memory:
  scopes_readable: [<your-topical-scopes>, meta]
  scopes_owned:    [<your-topical-scopes>]   # NOT meta
  default_scope:   <your-default-topical-scope>
```

`meta` is the system scope where `_finalize_engagement` writes
engagement summaries (specialist + executor). Including it in
`scopes_readable` makes Ellen's "what did Configurator just do?" /
"what did Finance say?" turns answerable.

DO NOT add `meta` to `scopes_owned` — meta is exclusively
tool-written. Routing the resident's own `add_turn` to meta would
dilute the engagement-summary signal with conversational noise.

For voice-only / `household-shared`-trust residents (today: butler/
Tina), exclude `meta` from `scopes_readable`. Even if added, the
trust filter at `agent.py:296` would drop it (meta requires
`authenticated`); excluding it makes the intent explicit.

## Cross-role recall tool (M6, v0.18.0)

If the new resident should be able to consult another agent's
accumulated memory of the user without delegating a full agent turn,
include the tool in `tools.allowed` and configure the token budget:

```yaml
tools:
  allowed:
    - mcp__casa-framework__consult_other_agent_memory   # M6 — cross-role recall
    # ... other tools

memory:
  token_budget: 4000
  cross_peer_token_budget: 2000   # M6 — consult_other_agent_memory budget
  # ... other memory fields
```

The tool reads `peer.context(target=user, search_query=query)` from
Honcho — it does NOT spawn a turn against the other agent. Use it for
"what does Finance know about my budget priorities?" — but for "what
was my last invoice?" the resident should still call
`delegate_to_agent("finance", ...)` because only the specialist's
tools fetch live data.

**Trust posture.** DO NOT add this tool to a voice-only /
`household-shared`-trust resident's `tools.allowed`. The voice
channel is open to guests; granting this tool there would let a
guest pull a specialist's view of the user. Tina (butler) ships
without it for this reason.

Add the matching routing-rule paragraph to the resident's
`prompts/system.md` so Claude knows when to use the tool vs
`delegate_to_agent` (see the canonical Ellen paragraph for the
template).

**Disabled-but-consultable specialist memory (Phase 5 / E-15, v0.26.1+):**
A specialist that's bundled but `enabled: false` in user config has its
peer-level Honcho memory persisted independently of operational
enablement. Ellen (or any resident with `consult_other_agent_memory` in
`tools.allowed`) can still call the tool against a disabled specialist's
role name — the tool falls through to `cross_peer_context` and Honcho
returns whatever `peer_card` / `representation` has accumulated from
past delegations or earlier-enabled phases.

This decouples memory readability from operational availability: an
operator can disable Finance to stop new delegations from running while
still allowing Ellen to recall what Finance previously knew. If you
want a hard memory gate (disabled = unconsultable), open a follow-up
to add a `cfg.allow_memory_when_disabled: bool` field on specialist
runtime.yaml — currently out of scope per Phase 5 spec § 3.3.
