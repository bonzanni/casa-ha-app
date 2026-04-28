# Recipe: create a new specialist

A Specialist is a Tier 2 agent, role-keyed (e.g. finance, fitness). Residents invoke specialists via delegate_to_agent. Specialists are ephemeral - no persistent session, no scopes_owned.

## Ask the user

Before creating anything, confirm these (ask in the topic):

1. **Role name.** Lowercase, hyphens/underscores ok. Example: fitness, travel-planner. No collision with existing specialists.
2. **Human-readable name.** Used in character.yaml.
3. **Persona style.** One-sentence description.
4. **Model tier.** haiku/sonnet/opus. Default: sonnet.
5. **Who should be able to delegate to it?** Usually the main resident. Ask.
6. **Does it need external tools?** MCP servers? Default: no MCP servers.

## Files to create

Under /addon_configs/casa-agent/agents/specialists/<role>/:

1. character.yaml
2. runtime.yaml
3. prompts/system.md
4. response_shape.yaml
5. voice.yaml

Optional: hooks.yaml.

**Not allowed** (loader rejects): disclosure.yaml, delegates.yaml, triggers.yaml.

## Exact content templates

### character.yaml

    schema_version: 1
    name: <Human-readable name>
    archetype: specialist
    card: |
      <One-paragraph self-introduction in first person.>
    prompt: ""

### runtime.yaml

    schema_version: 1
    role: <role>
    model: <haiku|sonnet|opus>
    enabled: true
    tools:
      allowed: []
      disallowed: []
      permission_mode: default
      max_turns: 10
    mcp_server_names: []
    memory:
      token_budget: 0
      read_strategy: per_turn
      scopes_owned: []
      scopes_readable: []
      default_scope: ""
    session:
      strategy: ephemeral
      idle_timeout: 0
    tts:
      tag_dialect: square_brackets
    voice_errors: {}
    channels: []
    cwd: /addon_configs/casa-agent/workspace

**CRITICAL** for specialists: memory.token_budget 0, memory.scopes_owned [], session.strategy ephemeral, channels []. Loader REJECTS specialists violating these.

### prompts/system.md

    You are <name>, a specialist agent focused on <domain>. Your style is <persona style>.

    You are invoked by a resident agent (Ellen) to handle <domain-specific requests>. You have no memory across invocations - every call is a fresh turn.

    Be concise. Answer the question or complete the task, then stop.

### response_shape.yaml

    schema_version: 1
    max_sentences_confirmation: 2
    max_sentences_status: 3
    register: written
    format: plain
    rules: []

### voice.yaml

    schema_version: 1
    tone:
      - warm
      - concise
    cadence: natural
    forbidden_patterns: []
    signature_phrases: {}

## Wire into delegates.yaml

The new specialist won't be callable until the resident's delegates.yaml lists it. See recipes/delegate/wire.md.

## Reload

**Hard** - creating a new agent requires agent_loader re-scan.

1. config_git_commit(message="add <role> specialist")
2. emit_completion(status="ok", text="Created specialist <role>...")
3. casa_reload()

## Common mistakes

- Forgetting to wire into delegates.yaml.
- Setting memory.token_budget > 0 - loader rejects.
- Including channels: [telegram] - loader rejects.
- Copying a resident's disclosure.yaml - loader rejects.

## Memory-bearing specialist (M4b, v0.17.0)

Specialists are per-`(role, user_peer)` Honcho peers — channel-agnostic
and scope-agnostic. To opt a specialist into memory, set
`memory.token_budget > 0` in `runtime.yaml`:

```yaml
memory:
  token_budget: 4000        # 0 = stateless (legacy); >0 enables Honcho memory
  read_strategy: per_turn   # cached not yet supported for specialists
  scopes_owned: []          # MUST stay empty — specialists don't partition by scope
  scopes_readable: []       # MUST stay empty — specialists don't partition by scope
```

The session id is `f"{role}-{user_peer}"` (e.g. `finance-nicola`).
Built via `honcho_ids.honcho_session_id(role, user_peer)` — direct
f-string concatenation is forbidden because Honcho's server regex
rejects characters outside `[A-Za-z0-9_-]`.

Specialists become richer over time at their own domain reasoning;
Honcho's `peer_representation` accumulates the specialist's
theory-of-mind of the user across all delegate-call channels.

Trust gating happens one level up: a specialist is callable from a
channel iff some resident on that channel has it in `delegates`.
Once invoked, the specialist's full unified memory is in scope.
