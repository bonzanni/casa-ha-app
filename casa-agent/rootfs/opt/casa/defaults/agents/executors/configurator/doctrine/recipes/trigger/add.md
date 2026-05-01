# Recipe: add a trigger to an agent

Triggers are per-agent scheduled or webhook-driven events. Residents only (specialists and executors don't have triggers).

## Ask the user

1. **Which agent?** Usually assistant or butler.
2. **Trigger type?** interval (every N minutes), cron, or webhook.
3. **Trigger name?** Lowercase (e.g. morning_briefing, garbage_reminder).
4. **Schedule / path?**
   - interval: how many minutes (e.g., 30).
   - cron: five-field cron string (e.g. "0 7 * * 1-5" = weekdays 7am).
   - webhook: URL path (e.g. /webhooks/garbage_reminder).
5. **Channel?** telegram or voice. Must be a channel the agent already owns.
6. **Prompt?** One imperative sentence.

## Files to touch

### Add to agents/<role>/triggers.yaml

    schema_version: 1
    triggers:
      - name: <trigger_name>
        type: interval | cron | webhook
        minutes: <N>          # interval only
        schedule: "<cron>"    # cron only
        path: "/webhooks/..." # webhook only
        channel: <telegram|voice>
        prompt: <one-line imperative>

### Add agents/<role>/prompts/<trigger_name>.md (cron/interval only)

    You are <name>. The <trigger-name> trigger just fired. <Task description.>

## Reload — MANDATORY before emit_completion

**Soft** - casa_reload_triggers(role). No restart needed. Canonical order:

1. config_git_commit(message="add <trigger-name> trigger to <role>")
2. casa_reload_triggers(role="<role>")
3. emit_completion(status="ok", text="...committed SHA <sha>, reloaded triggers for <role>.")

Skipping step 2 leaves the trigger committed to YAML but **NOT registered** in the live scheduler — it never fires. See completion.md for the full doctrine.

## Verify the cron syntax

Five fields: minute hour day month day_of_week. "0 7 * * 1-5" = 7:00 on weekdays. APScheduler uses casa_tz (default Europe/Amsterdam).

## Webhook triggers

Path becomes HTTP POST endpoint on port 18065. Path must not collide with other agents' webhooks.
