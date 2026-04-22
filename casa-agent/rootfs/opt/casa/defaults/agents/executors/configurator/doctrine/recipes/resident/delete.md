# Recipe: delete a resident

**VERY DESTRUCTIVE.** casa_config_guard blocks rm -rf on resident directories by default. You CANNOT override from your side.

## Ask the user twice

1. "You want to remove resident <role>. This will orphan every memory and session tied to its scopes. Are you sure?"
2. On yes: "This requires manual follow-up - the hook will block my rm -rf. You'll need to either adjust forbid_delete_residents: false in my hooks.yaml for this engagement, or delete the directory manually via SSH. Which?"

If manual: emit_completion status="partial" with detailed text explaining what the user must do (SSH, rm, restart, rebalance scopes).

If hook adjust: edit agents/executors/configurator/hooks.yaml to flip forbid_delete_residents: false (you're allowed to edit your own hooks). Commit, reload, user engages you again with the same task.

This is the only recipe needing two engagements.

## Why we make it this hard

Deleting a resident is rarely reversible cleanly. Two-step friction is right.

## Request that ISN'T delete-resident

"Rename assistant to Ellen" - that's the PERSONA, not the ROLE. Clarify.
