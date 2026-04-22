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

## Always

- Commit via config_git_commit.
- emit_completion with status=ok, text describing the change + commit SHA.
- Then reload.

## Edge cases

- **Role rename** is delete + create, not update.
