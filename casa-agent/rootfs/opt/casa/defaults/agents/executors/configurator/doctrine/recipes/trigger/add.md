# Recipe: add a trigger to an agent

Triggers are per-agent scheduled or webhook-driven events. Residents only (specialists and executors don't have triggers).

## Ask the user

1. **Which agent?** Usually assistant or butler.
2. **Trigger type?** interval (every N minutes), cron, or webhook.
3. **Trigger name?** Lowercase (e.g. morning_briefing, garbage_reminder). For a
   webhook this name IS the endpoint: `POST /webhook/<name>`.
4. **Schedule?** (interval/cron only)
   - interval: how many minutes (e.g., 30).
   - cron: five-field cron string (e.g. "0 7 * * 1-5" = weekdays 7am).
5. **Channel?** interval/cron: telegram or voice (must be a channel the agent
   already owns). **A webhook trigger requires the agent to declare the
   `webhook` channel.**
6. **Prompt?** (interval/cron only) One imperative sentence.
7. **Webhook auth?** (webhook only) how does the caller authenticate — see below.

## Files to touch

### Add to agents/<role>/triggers.yaml

    schema_version: 2
    triggers:
      # interval / cron
      - name: <trigger_name>
        type: interval | cron
        minutes: <N>          # interval only
        schedule: "<cron>"    # cron only
        channel: <telegram|voice>
        prompt: <one-line imperative>
      # webhook — served ONLY at POST /webhook/<name> (no `path` field; it was
      # removed in v0.97.0). The agent must declare the `webhook` channel.
      - name: <trigger_name>
        type: webhook
        clearance: public       # public|friends|family — memory tiers this
                                # webhook's turns may recall (NEVER private).
        auth:
          mode: static_header   # hmac_body | static_header | timestamped_hmac
          header: X-API-Key     # static_header / timestamped_hmac
          tolerance_secs: 300   # timestamped_hmac only

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

- **Endpoint:** `POST /webhook/<name>` on port 18065 (publicly, the operator's
  configured `public_url`). There is no `path` field — the trigger NAME is the
  endpoint. Names must be unique across all agents' webhooks.
- **Auth is per-trigger and fail-closed** (spec A1). Pick the mode that fits the
  caller:
  - `hmac_body` (default) — caller sends `X-Webhook-Signature` = HMAC-SHA256 hex
    of the body, using the global `webhook_secret`. Requires
    `webhook_auth_enabled` on, else the trigger is refused.
  - `static_header` — caller sends a shared secret in a header (default
    `X-API-Key`). For services that can only send static headers (many SaaS
    webhooks). The secret is auto-generated at `/data/webhook_secrets/<name>`;
    read it and give it to the caller.
  - `timestamped_hmac` — caller sends `t=<unix>,v0=<hmac>` (default header
    `ElevenLabs-Signature`). For providers using a timestamped signature.
- **Containment:** a webhook turn is UNTRUSTED third-party content. It runs in a
  restricted runtime (no shell/filesystem/network tools, no plugins) and reads
  memory only at the declared `clearance` (never private). It can notify the
  operator and recall public memory — nothing more. Don't promise a webhook
  trigger can do privileged work; that needs operator-signed `/invoke`.
