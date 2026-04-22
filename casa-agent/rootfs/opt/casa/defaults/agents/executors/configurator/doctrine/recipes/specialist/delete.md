# Recipe: delete a specialist

Common and allowed. casa_config_guard does NOT block deleting specialists.

## Ask the user

1. **Which specialist?**
2. **Are you sure?** Mostly for the re-wiring.

## Steps

1. Find all references:

    grep -r "<role>" /addon_configs/casa-agent/agents/*/delegates.yaml

2. For each resident delegating to this specialist, remove the entry from their delegates.yaml. See recipes/delegate/unwire.md.
3. Delete the directory:

    rm -rf /addon_configs/casa-agent/agents/specialists/<role>

4. config_git_commit(message="remove <role> specialist + unwire delegates")
5. emit_completion
6. casa_reload() - **hard**.

## What NOT to do

- Don't leave dangling delegate entries.
- Don't delete while a delegation is in flight.

## Rollback

git revert <sha> + hard reload.
