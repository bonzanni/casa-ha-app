# Safety - destructive ops and what hooks block

Hooks run BEFORE your tool call. If a hook denies, you'll see a message starting with the policy name (casa_config_guard, commit_size_guard, path_scope, block_dangerous_bash). Do not try to work around it - ask the user in the engagement topic.

## Fully blocked (no override)

- Write/Edit anywhere under /data/** - runtime state; touching it corrupts memory.
- Write/Edit under /addon_configs/casa-agent/schema/** - authoritative code; breaks load path.
- Write/Edit under /opt/casa/** - addon source tree.
- rm -rf, shutdown, reboot, dd if=, curl with POST/data, ssh, scp in Bash.

## Destructive-adjacent (ask the user first)

- rm -rf /addon_configs/casa-agent/agents/<resident> - removing a resident. Hook denies by default.
- Any change to policies/scopes.yaml - classifier is trained on this corpus. Show the user the diff before committing.
- Changes that touch more than 20 files in one commit - commit_size_guard will deny.

## Things that LOOK destructive but aren't

- Editing prompts/*.md - no reload, file is read per-turn.
- Editing doctrine/*.md (your own doctrine) - authorized.
- Deleting a specialist - common; casa_config_guard allows it.
- Deleting an executor (not a resident) - allowed.

## Rollback

config_git_commit creates a proper commit. If something goes wrong, Ellen or the user can roll back via git checkout <prev-sha> -- <path>. The repo is local-only - no propagation concern.

The bus receives your emit_completion before any reload - even if the reload goes badly, Ellen has your summary.

## One rule you shouldn't forget

**Commit THEN reload.** Always.
