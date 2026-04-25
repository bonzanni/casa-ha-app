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

## Reload

**Hard**, big one (scope registry rebuild).

1. config_git_commit(message="add resident <role> with scope <scope>")
2. emit_completion
3. casa_reload()

## Register handling for delegated turns

Any resident's `prompts/system.md` should honor the framework-injected
`<delegation_context>::suggested_register` hint. When invoked via another
agent's `delegate_to_agent` call, the calling channel is text → answer in
conversational text register; voice → answer in spoken register. See
butler/prompts/system.md for the canonical paragraph; copy or adapt it
when authoring a new resident.
