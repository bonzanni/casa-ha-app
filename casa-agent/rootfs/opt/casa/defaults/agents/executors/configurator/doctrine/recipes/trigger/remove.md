# Recipe: remove a trigger

## Ask the user

1. **Which trigger?** Confirm by name.
2. **Confirm:** stops firing immediately after reload.

## Edit the YAML

Remove the entry from agents/<role>/triggers.yaml. If last, leave `triggers: []`.

Optionally delete agents/<role>/prompts/<trigger-name>.md if unused.

## Reload

**Soft** - casa_reload_triggers(role).

    config_git_commit(message="remove <trigger-name> from <role>")
    emit_completion
    casa_reload_triggers(role="<role>")
