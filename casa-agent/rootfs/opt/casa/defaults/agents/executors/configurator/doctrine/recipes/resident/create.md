# Recipe: create a new resident

RARE. Tier 1 agents are usually enough (Ellen + Tina). Adding a third rebalances channels.

## Ask the user

1. **Why?** Is a specialist the right answer instead?
2. **Role name?** Lowercase.
3. **Channels?** telegram, voice, both? Must be configured on HA side.
4. **Session strategy?** durable (like Ellen) or ephemeral.
5. **Token budget?**

Confirm explicitly. Wait for "yes, do that" before proceeding.

## Files to create

Under /config/agents/<role>/:

1. character.yaml
2. runtime.yaml
3. delegates.yaml
4. disclosure.yaml
5. response_shape.yaml
6. voice.yaml
7. prompts/system.md
8. Optional: triggers.yaml, hooks.yaml

## Reload — MANDATORY before emit_completion

Adding a resident requires the runtime to re-scan `agents/`. Use the
`agents` scope. Canonical order:

1. config_git_commit(message="add resident <role>")
2. casa_reload(scope="agents")
3. emit_completion(status="ok", text="Added resident <role>; committed SHA <sha>; called casa_reload(scope='agents') to register the new agent.")

Skipping the reload leaves the new resident on disk but **not in the
live channel routing** — see completion.md.

## Register handling for delegated turns

Any resident's `prompts/system.md` should honor the framework-injected
`<delegation_context>::suggested_register` hint. When invoked via another
agent's `delegate_to_agent` call, the calling channel is text → answer in
conversational text register; voice → answer in spoken register. See
butler/prompts/system.md for the canonical paragraph; copy or adapt it
when authoring a new resident.
