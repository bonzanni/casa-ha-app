# Recipe: update an existing specialist

User wants to change something about an existing specialist.

## Ask the user

1. **Which specialist?** (role name)
2. **What specifically?** Give a direct, focused question.

## Common changes

### Change model

Edit agents/specialists/<role>/runtime.yaml::model. Reload: **hard**.

### Change persona prompt

Edit agents/specialists/<role>/prompts/system.md. Reload: **none**.

### Change allowed tools

Edit agents/specialists/<role>/runtime.yaml::tools.allowed. Reload: **hard**. Also consider whether mcp_server_names needs to update.

### Change character

Edit agents/specialists/<role>/character.yaml. Reload: **hard**.

## Always — MANDATORY order

1. Commit via `config_git_commit`.
2. Reload (per `reload.md` — none / soft / hard) **before** emit_completion.
3. `emit_completion` with status=ok, text describing the change + commit SHA + the reload that ran.

Calling `emit_completion` before the reload leaves the change committed
to YAML but inert in the running Casa. See `completion.md`.

## Edge cases

- **Role rename** is delete + create, not update.

## Enabling memory on an existing stateless specialist

A stateless specialist has `memory.token_budget: 0` in its
`runtime.yaml`. To enable shared-bank memory:

1. Edit `/addon_configs/casa-agent/agents/specialists/<role>/runtime.yaml`:
   ```yaml
   memory:
     token_budget: 4000      # any positive int; resident parity is fine to start
     read_strategy: per_turn
   ```
2. Reload via `reload_agents`.
3. The next `delegate_to_agent` call to this specialist triggers a
   recall pass against the shared `casa` Hindsight bank at the
   engagement's inherited clearance, and starts accumulating saves.

There is no migration step from stateless calls. Past
delegate_to_agent invocations were not stored anywhere; the
specialist's first memory is its first post-flip call.
