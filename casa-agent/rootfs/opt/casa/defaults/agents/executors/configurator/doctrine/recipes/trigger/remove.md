# Recipe: remove a trigger

## Ask the user

1. **Which trigger?** Confirm by name.
2. **Confirm:** stops firing immediately after reload.

## Edit the YAML

Remove the entry from agents/<role>/triggers.yaml. If last, leave `triggers: []`.

Optionally delete agents/<role>/prompts/<trigger-name>.md if unused.

## Reload — MANDATORY before emit_completion

**Soft** - casa_reload_triggers(role). Canonical order:

    config_git_commit(message="remove <trigger-name> from <role>")
    casa_reload_triggers(role="<role>")
    emit_completion(status="ok", text="...removed; reloaded triggers for <role>.")

Skipping the reload leaves the deleted trigger still registered in the
live scheduler — it keeps firing until the next addon restart. See
completion.md.
