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
5. casa_reload() — **hard**.
6. emit_completion(status="ok", text="Removed <role>; committed SHA <sha>; called casa_reload to drop the agent from the live registry.")

Reload **before** emit_completion (canonical order — see completion.md). Skipping the reload leaves Ellen still trying to delegate to a deleted specialist until the next manual restart.

## What NOT to do

- Don't leave dangling delegate entries.
- Don't delete while a delegation is in flight.

## Rollback

git revert <sha> + hard reload.
