# Recipe: update an existing trigger

User wants to change schedule, prompt, or channel for an existing trigger.

## Ask the user

1. **Which trigger, on which agent?**
2. **What specifically?**

## Update the YAML

Edit agents/<role>/triggers.yaml. Find the entry by name, change the field(s).

Per-trigger prompt in prompts/<trigger_name>.md - edit that too.

## Reload

**Soft** - casa_reload_triggers(role).

    config_git_commit(message="update <trigger-name> on <role>: <what>")
    emit_completion
    casa_reload_triggers(role="<role>")
