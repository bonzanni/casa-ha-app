# Casa Agent

A personal AI agent framework running as a Home Assistant app (formerly add-on), powered by the Claude Agent SDK.

## What it does

Casa runs always-on AI agents inside your Home Assistant instance. The primary agent (Ellen) handles general queries, smart home control, and task delegation via Telegram. A voice agent (Tina) provides fast, concise responses optimized for HA voice pipelines. On-demand subagents handle specialized tasks like building automations or managing finances.

## Prerequisites

- **Claude Max subscription** with an OAuth token (run `claude setup-token` on your local machine to obtain one)
- **Home Assistant 2025.4+** on an amd64 or aarch64 system
- A Telegram bot token (optional, for Telegram channel)

## Configuration

### Required

| Option | Description |
|--------|-------------|
| `claude_oauth_token` | Your Claude OAuth token from `claude setup-token`. Required. |

### Optional -- Channels

| Option | Description |
|--------|-------------|
| `telegram_bot_token` | Telegram bot token from @BotFather. Enables the Telegram channel. |
| `telegram_chat_id` | Telegram chat ID to restrict messages to. Leave empty to accept all chats. |
| `telegram_engagement_supergroup_id` | Chat ID of the dedicated Telegram forum supergroup used for interactive engagements (Tier 2 Specialist interactive mode; Tier 3 Executor types, Plan 3+). Must be a negative integer. Leave at 0 to disable engagements. |
| `telegram_rich_text` | Render Markdown (bold, italic, inline code, and monospace code blocks) in agent replies. Default: `true`. Set to `false` to send all replies as plain text. |

### Optional -- Memory

Short-term conversation continuity always works via the Claude Agent SDK
session. **Long-term** memory (cross-session recall) is off by default and
is enabled by pointing Casa at a self-hosted **Hindsight** app.

| Option | Description |
|--------|-------------|
| `hindsight_api_url` | Internal base URL for the self-hosted Hindsight app (e.g. `http://5884eb17-hindsight:8888` or its IP), reached via the app's hassio network alias/IP — not the bare host `hindsight`. **This is the single toggle for long-term memory: set it to turn long-term semantic memory ON** (the app auto-derives `MEMORY_BACKEND=hindsight`) — both **save** (the freshness reaper retains ended conversations, each item tier-classified) and **recall** (a mental-model overlay + relevance-ranked recall on the read path, plus a `recall_memory` pull tool). **Leave empty to keep long-term memory disabled** (short-term continuity still works via the SDK session). |

The following env var is **auto-derived** from `hindsight_api_url` and rarely needs
setting by hand:

| Env var | Purpose | Default |
|---|---|---|
| `MEMORY_BACKEND` | Long-term memory backend: `hindsight` or `noop` (disabled). **Auto-set to `hindsight` by the app whenever `hindsight_api_url` is non-empty**; otherwise unset → `noop`. Any unrecognized value → `noop`. You normally just set `hindsight_api_url`. | derived from `hindsight_api_url` |

### Optional -- Agents

| Option | Description |
|--------|-------------|
| `primary_agent_name` | Name of the primary agent. Default: `Ellen`. |
| `voice_agent_name` | Name of the voice agent. Default: `Tina`. |
| `primary_agent_model` | Model for the primary agent: `opus`, `sonnet`, or `haiku`. Default: `opus`. |
| `voice_agent_model` | Model for the voice agent. Default: `haiku`. |

### Optional -- Features

| Option | Description |
|--------|-------------|
| `enable_terminal` | Enable a web terminal accessible via the ingress panel. Default: `false`. |
| `sdk_client_pool` | Reuse a warm Claude SDK client across resident turns for faster replies. Default: `true`. Disable to fall back to a fresh per-turn session (e.g. while diagnosing an issue). |
| `webhook_secret` | HMAC-SHA256 secret for authenticating webhook requests. Leave empty to skip verification. |
| `engagement_reap_days` | Auto-close engagements after this many days without activity (daily sweep cancels them and closes their Telegram topic; the engaging agent is notified). Set `0` to disable. Default: `7`. |
| `log_level` | Log verbosity: `debug`, `info`, `warning`, or `error`. Default: `info`. Flip to `debug` for verbose troubleshooting without rebuilding the image. |

## How it works

1. **Startup**: The app validates your OAuth token, copies default agent configs (if first boot), and starts nginx + the Casa core process.
2. **Message flow**: Incoming messages (Telegram, webhook, voice) are routed through an async message bus to the appropriate agent based on the originating channel.
3. **Agent processing**: Each agent builds a system prompt (personality + memory context), queries the Claude Agent SDK, stores the conversation in memory, and sends the response back through the originating channel.
4. **Home Assistant integration**: Agents interact with HA via the official HA MCP server, allowing them to control devices, read states, and create automations.
5. **Per-agent triggers**: Each agent declares scheduled triggers (cron or interval) in its own `agents/<role>/triggers.yaml`. The TriggerRegistry registers them at boot, and fires them via the agent's normal turn loop.

## API endpoints

All endpoints are accessible through the ingress proxy.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Health check |
| `POST` | `/webhook/{name}` | Fire-and-forget named webhook |
| `POST` | `/invoke/{agent}` | Synchronous agent invocation (returns response) |

Webhook and invoke endpoints accept JSON bodies. If `webhook_secret` is configured, include an `X-Webhook-Signature` header with the HMAC-SHA256 hex digest of the request body.

The target of `/invoke/{agent}` must declare the `webhook` capability in its `channels:` list to be invoke-reachable; a request for an agent that does not (for example the voice butler, which declares only `ha_voice`) returns `404 {"error": "unknown agent"}` — the same response as for an agent that does not exist, so the endpoint reveals nothing about which agents are configured. The default `assistant` (Ellen) declares `webhook` and stays reachable.

### Invoke example

```bash
curl -X POST http://homeassistant.local:8080/invoke/ellen \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is the temperature in the living room?"}'
```

## Agent configuration

Agent YAML files are stored in `/config/agents/`. Default configs are created on first boot and never overwritten. You can edit them freely.

Each agent config supports: `name`, `role`, `model`, `personality`, `tools`, `mcp_server_names`, `memory`, `session`, `channels`, `tts`, `voice_errors`, and `cwd`. See the default `assistant.yaml` for a full example.

## Voice pipeline

Casa exposes two transports for Home Assistant voice / generic voice clients. The HA-side integration that consumes them ships separately in `casa-ha-integration` (phase 2.4).

- `POST /api/converse` — Server-Sent Events, per-request. Body:
  `{"prompt", "agent_role", "scope_id", "context"}`. Stream: `event:
  block` frames then `event: done`. HMAC via `X-Webhook-Signature`
  (same scheme as `/invoke`).
- `/api/converse/ws` — persistent WebSocket. Inbound frames:
  `stt_start`, `utterance`, `stage`, `cancel`. Outbound: `block`,
  `done`, `error`. On `stt_start`, Casa prewarms the voice session's
  memory cache so first-utterance latency is bounded.

Toggle the transports via environment variables on the app:

| Variable | Default | Purpose |
|---|---|---|
| `VOICE_SSE_ENABLED` | `true` | Enable `POST /api/converse` |
| `VOICE_WS_ENABLED`  | `true` | Enable `/api/converse/ws` |
| `VOICE_SSE_PATH`    | `/api/converse` | Override SSE path |
| `VOICE_WS_PATH`     | `/api/converse/ws` | Override WS path |
| `VOICE_IDLE_TIMEOUT_SECONDS` | (butler.session.idle_timeout, 300) | Session pool eviction timeout |

Per-agent voice config (`butler.yaml`):

```yaml
tts:
  tag_dialect: square_brackets   # square_brackets | parens | none

voice_errors:
  timeout:       "[apologetic] Hm, that took too long. Try again?"
  rate_limit:    "[flat] My brain is busy — give me a minute."
  sdk_error:     "[apologetic] I couldn't reach my brain. Try again?"
  memory_error:  ""                    # silent degrade
  channel_error: "[flat] Something went wrong sending that."
  unknown:       "[flat] Sorry, something went wrong."
```

`tag_dialect` selects how inline emotion tags (`[confident]`, `[warm]`,
etc.) are rendered before Casa hands text off to HA's TTS. Use
`parens` for engines that expect `(tag)` and `none` to strip tags
entirely for plain-TTS providers like Piper. Voice and engine
selection itself is Home Assistant pipeline config — Casa does not
override it.

## Web terminal

When `enable_terminal` is enabled, a web terminal is available at the `/terminal/` path in the ingress panel. This gives you shell access to the app container for debugging and manual operations.

## Troubleshooting

- **App won't start**: Check the log for "claude_oauth_token is required". You must set the token before starting.
- **No Telegram messages**: Verify `telegram_bot_token` and `telegram_chat_id` are correct. The bot must have been started (`/start` in Telegram).
- **Engagements won't open (`engagement_not_configured`)**: See the "Troubleshooting engagements" subsection under [Engagements (v0.11.0)](#engagements-v0110) — most common cause is the bot missing "Manage topics" admin permission in the engagement supergroup.
- **Long-term memory not working**: Long-term recall requires the `hindsight_api_url` option to be set to a reachable Hindsight app (which auto-enables `MEMORY_BACKEND=hindsight`). If recall is empty, check that `hindsight_api_url` is set and the Hindsight app is running, and check container logs for Hindsight connection errors. With `hindsight_api_url` empty, only short-term per-session continuity works.
- **502 errors on ingress**: The Python process may still be starting. Wait up to 60 seconds after app start.

## DM button questions (v0.76.0)

Ellen (and the specialists she delegates to) can pause a turn to ask you a
quick multiple-choice question, posted as inline buttons right in your 1:1
Telegram DM — the same tap-to-answer pattern engagements use, without
opening a topic. Tap an option and the agent picks up from there; a
plain-text reply in the same DM answers it too. An unanswered question
expires after a few minutes, and starting a fresh session (`/new`)
cancels any question still pending.

## Engagements (v0.11.0)

Casa supports **engagements** — bounded conversational threads where a
specialist (Tier 2) or executor (Tier 3, Plan 3+) works with you on a
specific task, separate from your 1:1 chat with Ellen. Each engagement
lives in its own Telegram forum topic inside a dedicated supergroup.

The setup is a one-time Telegram configuration. Skip this section to
keep Casa running in 1:1-only mode (Ellen delegates synchronously and
returns a single response; `delegate_to_agent(mode="interactive")`
will return `engagement_not_configured`).

### Setup

#### 1. Create a dedicated forum supergroup

This must be a **different** chat from your 1:1 DM with the Casa bot.
Engagement topics live here, not in your personal chat.

1. In Telegram, tap the **✏️ pencil icon** (top right, most clients) → **New Group**.
2. Pick any co-owners you want (or just yourself) and give the group a name
   (e.g. "Casa Engagements"). Confirm.
3. Open the group's settings (tap the group name at top). Telegram will
   usually auto-convert small groups to a supergroup on first edit;
   if you see a **"Convert to supergroup"** button, tap it.
4. In group settings, find **"Topics"** (sometimes under "Group Type" or
   "Permissions"). Toggle it **ON**. The chat now shows individual topic
   threads instead of a single linear feed.

#### 2. Add the Casa bot as a topic-managing admin

1. Open the group → tap the group name → **Add Members** → search for your
   bot's `@username` → add it.
2. Tap the bot in the members list → **Promote to admin**.
3. Turn ON **"Manage topics"**. This permission is **required** —
   Casa refuses to enable engagements without it and logs
   `bot lacks can_manage_topics; engagements disabled`.
4. Other permissions ("Delete messages", pin, etc.) are optional —
   Casa does not require them; topic cleanup works with "Manage
   topics" alone (see step 6). Leave unused ones off to minimise the
   bot's surface.
5. Confirm. The bot is now a topic-managing admin.

#### 3. Find the supergroup's chat ID

Casa needs the **negative integer** Telegram assigns to the supergroup.

1. Post any message in the supergroup (e.g. "setup probe").
2. In a browser, open:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   (substitute your bot token from @BotFather — same one you put in
   `telegram_bot_token`).
3. Find the most recent `message` object. `message.chat.id` is your
   supergroup chat ID. It will be a negative integer starting with
   `-100`, e.g. `-1001234567890`.

Alternatives:
- Add a helper bot like `@getidsbot` or `@RawDataBot` to the supergroup
  temporarily; it will reply with the chat ID.
- If you already run Casa on the 1:1 chat, the app log also echoes
  the ID under the `CHANNEL_IN` traces when the bot sees any message
  in the supergroup.

#### 4. Configure Casa

1. Home Assistant → **Settings** → **Apps** (formerly Add-ons) → **Casa Agent** → **Configuration**.
2. Set `telegram_engagement_supergroup_id` to the negative integer from step 3.
   Leave it at `0` to disable engagements (Casa still boots; interactive
   mode returns `engagement_not_configured`).
3. **Save** → **Restart** the app.

#### 5. Verify

Once the app has restarted, check the log for:

```
Engagement supergroup -100…: commands registered (['cancel', 'complete', 'silent'])
```

If you see this line, engagements are live. If you see:

```
Engagement supergroup -100…: bot lacks can_manage_topics; engagements disabled
```

the bot wasn't promoted correctly — go back to step 2 and re-check the
**Manage topics** toggle. The log line uses Ellen's level-ERROR — it's
easy to grep for.

In Telegram, inside the engagement supergroup, type `/` in any topic.
The autocomplete should list `/cancel`, `/complete`, and `/silent`.
These commands are registered via Telegram's `setMyCommands` scoped to
the supergroup only — they do NOT appear in your 1:1 DM with Ellen.

#### 6. Automatic topic cleanup (works out of the box)

Since v0.65.0 Casa deletes a finished engagement's topic automatically
**7 days after the engagement ends** — the same retention window as its
workspace — so the sidebar doesn't fill up with dead topics.

**No extra admin right is needed** (v0.65.1 correction): Telegram lets a
topic's *creator* delete it with the "Manage topics" right the bot
already has from step 2, and every engagement topic is created by the
bot. The **"Delete messages"** admin right is optional insurance — if a
deletion is ever refused for rights reasons, Casa keeps the topic
scheduled, retries at the next sweep, and Ellen asks you once per boot
to grant it.

Notes:

- **Deletion is irreversible.** Deleting a topic removes the topic and
  **all its messages for every member**. Casa's durable record of each
  engagement is its memory summary plus Ellen's completion message in
  your 1:1 chat (and the workspace artifacts during the 7-day window).
- **On-demand cleanup:** ask Ellen to *"clean up the engagement group"* —
  she delegates to the configurator, whose `cleanup_engagement_topics`
  tool purges known finished topics immediately (optionally as a dry
  run) without waiting out the retention window. Only topics Casa has
  on record are deleted; active engagements are never touched.
- **Topics from before v0.65.0** are unknown to Casa (the Telegram Bot
  API cannot enumerate a group's topics), so the existing pile needs
  **one manual cleanup** in the Telegram UI. From this release on, Casa
  keeps the sidebar clean automatically.

### Starting an engagement

Ask Ellen in the main 1:1 chat for something multi-turn, e.g.
*"let's work through Q2 invoicing with Alex"*. Ellen may open an
engagement — if so she'll tell you which topic to head to. The
specialist is waiting there, already primed with context.

You do **not** post in the main supergroup feed — always open an
existing topic thread. Casa automatically creates new topics as new
engagements start; they're named `#[<role>] <task> · <engagement-id>`.
If you accidentally post in the main feed, the bot will reply once
per boot with a redirect hint, then silently ignore further main-feed
messages.

### While the engagement runs (v0.75.0)

The engaged agent's narration streams into the topic live as it works,
rather than arriving only as a single reply at the end — you can follow
along turn by turn. If it needs a decision from you mid-task, it may ask
via inline buttons (tap the option that applies) instead of waiting for a
free-text reply; the same buttoned pattern the permission-approval keyboard
already used for tool-use requests outside its pre-approved allow-list.

### In-topic slash commands

| Command | What it does |
|---------|--------------|
| `/cancel` | End this engagement now. Topic is closed, the engaged agent's client is torn down, Ellen is notified in the main chat. |
| `/complete` | Mark this engagement complete without requesting an agent summary. Same cleanup as `/cancel` but with a neutral status. |
| `/silent` | Stop the observer from interjecting to Ellen about this engagement. The specialist keeps working in the topic. |

The engaged agent can also end the engagement itself by calling the
`emit_completion` MCP tool — that produces a structured summary
(text + artifacts + next_steps) which Ellen relays to you.

### Idle reminders

Engagements have no hard timeout. If an engagement sits idle for 3 days
(specialists) or 7 days (executors, Plan 3+), Ellen will nudge you in the
main 1:1 chat. Reminder re-fires weekly.

Suspend/resume is automatic — after 24 hours of inactivity Casa tears
down the underlying SDK client to free resources. It resumes seamlessly
on your next message in the topic (the conversation-session state is
persisted and reloaded). If two consecutive resume attempts fail (e.g.
the SDK session was rotated server-side), Casa marks the engagement
as errored and tells you to start a fresh one.

### Troubleshooting engagements

- **"Engagements disabled" / `engagement_not_configured` on delegate call.**
  Most common: the bot wasn't promoted to admin with **Manage topics**.
  Re-check step 2 of Setup. Also check `telegram_engagement_supergroup_id`
  is not `0` and the app was restarted after the option was set.
- **`/cancel` / `/complete` / `/silent` don't appear in autocomplete.**
  They're scoped to the supergroup only. Make sure you're typing `/` in
  a topic inside the engagement supergroup, not your 1:1 DM with Ellen.
  Also: Telegram caches `setMyCommands` client-side — restart the
  Telegram client if they don't show up within 30 seconds of app boot.
- **"No active engagement in this topic" reply in a topic.**
  The engagement for that topic has already completed, cancelled, or
  errored (registry status transition). Start a fresh engagement from
  your 1:1 DM with Ellen — do not reuse old topics.
- **"Could not resume this engagement" reply after 24h+ idle.**
  The suspended SDK session rotated before you came back. The
  engagement is marked as errored after two failed resumes. Start a
  new one; your prior conversation is still in Ellen's long-term
  memory.
- **Ellen doesn't narrate completion in the main chat.**
  Ellen receives the `ENGAGEMENT_COMPLETION` notification but chooses
  how to surface it based on her system prompt. If you want louder
  narration, edit `prompts/system.md` in Ellen's agent folder and
  restart.

## Configurator (v0.12.0)

The `configurator` is the first Tier 3 Executor - knows Casa's configuration surface and can CRUD it on your behalf. Ask Ellen for a configuration change; she opens a dedicated engagement topic where you talk directly to the configurator.

### What's supported

| Surface | Create | Read | Update | Delete |
|---|---|---|---|---|
| Specialist agents (Tier 2) | yes | yes | yes | yes |
| Resident agents (Tier 1) | rare | yes | yes | blocked by default |
| Per-agent YAMLs | yes | yes | yes | yes |
| Per-agent prompts | yes | yes | yes | yes |
| Triggers (cron, interval, webhook) | yes | yes | yes | yes |
| Delegate wiring | yes | yes | yes | yes |
| Policies (disclosure) | - | yes | yes | - |
| Plugins (registry + store) | yes | yes | yes | yes |

Plugin management uses the registry tools (`plugin_add`, `plugin_update`,
`plugin_assign`, `plugin_unassign`, `plugin_remove`, `plugin_list`,
`verify_plugin_state`) — see [Plugins](#plugins-v0710).

Not yet supported:

- Eval running (configurator can shell to casa_eval, but no first-class recipe).
- Creating new executor types - waiting on Plans 4/5.

### Invocation

Ask Ellen for a configuration change in 1:1 chat. Examples:

- "Make a new specialist called fitness using sonnet"
- "Add a morning briefing trigger to yourself at 7am on weekdays"
- "Change Alex's prompt to be more concise"
- "Remove the garbage_reminder trigger"
- "Wire Alex into your delegates"

Ellen opens a topic `#[configurator] <short task>` in your engagement supergroup. The configurator reads its doctrine, asks questions as needed, edits YAMLs, commits, and reloads Casa.

### Reload behavior

- hard - Supervisor app restart (~10-15s). Agent-shape changes, runtime, policy corpus.
- soft - In-process casa_reload_triggers(role). Trigger-only edits; no downtime.
- none - Prompt, response_shape, doctrine edits take effect on next turn.

Hard reload: Ellen verifies the reload landed on her resumed turn, then narrates.

### Recipe discovery

Configurator reads short markdown recipes from its own doctrine tree at `/config/agents/executors/configurator/doctrine/`. Edit these recipes to customize per-instance (e.g., add house rules).

### Troubleshooting

- **"engagement_not_configured"** - you haven't set telegram_engagement_supergroup_id, or the bot doesn't have can_manage_topics.
- **Configurator stalls after first message** - check engagement's driver log. If you see prompt_template_missing, restart the app.
- **Hook denied, configurator cancelled** - expected for resident deletion. To override: edit configurator's hooks.yaml, commit, reload, retry.
- **Soft reload didn't take effect** - casa_reload_triggers requires the role to exist before the edit.
- **Doctrine references stale fields** - file an issue; maintainer forgot to sync doctrine with Casa-core change.

## Plugins (v0.71.0)

Casa loads Claude Code plugins for every agent tier from **one registry**. There
is no marketplace to browse and no per-agent install step — you pin a plugin
once, assign it to the agents that should have it, and Casa materializes an
immutable copy that every tier loads the same way.

### Layout

- `/config/plugins/registry.json` — the single source of truth: which plugins
  exist, the exact commit each is pinned to, and which agents each is assigned
  to. Tracked in the config git repo, so every change is snapshotted.
- `/config/plugins/store/<name>/<artifact-id>/` — the materialized plugin
  content. Each `<artifact-id>` is a content hash of the plugin's source
  (repo + commit + subdirectory), so a given artifact directory is immutable:
  a new commit produces a new artifact, never an in-place overwrite.

Residents and specialists load their assigned plugins directly through the
Claude Agent SDK. Executor engagements (e.g. plugin-developer) pin their exact
artifacts at launch and load them via `--plugin-dir`, so a plugin update never
changes the code a running engagement is already executing.

### Managing plugins (via the Configurator)

Ask Ellen for a plugin change; she opens a configurator engagement that uses
these tools:

- `plugin_add(name, repo, ref, subdir, targets, expected_revision?)` — publish
  a plugin's pinned artifact, install any system requirements it declares,
  assign it to targets, then reload and verify.
- `plugin_update(name, new_ref, expected_revision?)` — re-pin to a new release
  and re-verify. Plugin releases are identified by an annotated `vX.Y.Z` tag
  (v0.74.0); passing `expected_revision` (the commit the release was built at)
  makes a tag that moved afterwards abort before anything changes. The version
  is always read from the plugin's own manifest — you never supply it, so the
  stale-version class of bug is gone. Both tools report phase-aware outcomes
  (`activation_committed` / `runtime_ready`) so a "pin landed, reload pending"
  state is actionable.
- `plugin_assign(name, target)` / `plugin_unassign(name, target)` — change which
  agents load a plugin. Targets look like `resident:ellen`, `specialist:finance`,
  or `executor:plugin-developer`.
- `plugin_remove(name)` — drop a plugin from the registry (its artifact is left
  on disk for now; see disk usage).
- `plugin_list()` / `verify_plugin_state(name)` — inspect the registry and check
  that the running agents actually agree with it.

### Secrets

Unchanged from prior releases: a plugin declares required environment variables
via `${VAR}` references in its `.mcp.json`. When you add a plugin, the
configurator reports the required variables and asks for a 1Password reference
(`op://…`) for each, stored in `plugin-env.conf`. Secret values never appear in
transcripts.

### Protected plugin tools (v0.76.0)

A plugin can declare that one of its tools requires your approval before
Casa will run it (`casa.protectedTools` in the plugin's manifest). When an
agent tries to call a protected tool, Casa refuses the call and posts a
one-tap Approve/Deny button in your DM instead of running it blind. Approve
mints a grant that is:

- **single-use** — consumed the moment the retried call succeeds, so a
  second call needs a second approval;
- **argument-bound** — the grant only covers the exact arguments you
  approved; any change to the call needs a fresh approval;
- **time-limited** — a grant you never act on expires after 5 minutes.

You'll see this for actions a plugin author has flagged as consequential.
Deny leaves the call refused with no grant issued.

A plugin author can also upgrade the approval prompt's headline to a
plain-language action sentence (the exact arguments and tool id always
remain visible below it), by pairing the tool name with a `summary`
template in the manifest:

```json
"casa": {
  "protectedTools": [
    {"name": "invoice_reset", "summary": "Delete the invoice draft for {period}"}
  ]
}
```

`{period}` is filled in from that call's own arguments, so the prompt reads
"Alex (finance) wants to: Delete the invoice draft for 2025-05" — the exact
arguments still always appear below, unabridged.

### Disk usage

The store lives on `/config` (the `addon_config` volume), so artifacts persist
across app updates. Because artifacts are content-addressed, updating a plugin
adds a new artifact directory rather than replacing the old one, and removing a
plugin leaves its artifact in place. Automatic garbage collection is written but
**ships disabled in this release**; unreferenced artifacts can be pruned in a
later version. Typical plugins are small (skills + a small MCP server), so this
is not a concern for normal use.

### Integrity model

Artifact integrity rests on **content-addressing + checksum detection**: each
artifact directory is named by a hash of its source, and its bytes are checksum-
verified whenever the plugin snapshot is (re)loaded — a mismatch is reported
(`corrupt_artifact`) so a tampered or damaged artifact is never silently loaded. The write guards on
`/config/plugins` and the read-only freeze of published files are **best-effort
defense-in-depth** — the real trust boundary is each agent's minimal tool scope,
not a hard filesystem barrier. They are not designed to stop a deliberately
evasive process running as root; a true filesystem/privilege boundary is a
separate, later hardening item.

### Fresh install & rollback

The registry (`/config/plugins/registry.json`) is the single source of truth. On
a fresh install Casa seeds it with the bundled default plugins; a newer release
adds any newly-introduced defaults on the next boot, and a default you remove is
never re-added. Rollback is safe — the registry format is stable across
releases, so a downgraded app image reads the same registry as before —
with one boundary: a plugin version whose manifest uses the object form of
`casa.protectedTools` (with a `summary`, introduced in 0.78.0) is rejected
by pre-0.78.0 releases as invalid and excluded from loading; downgrade the
plugin to its last string-form release before (or after) downgrading the
app past 0.78.0.

### Troubleshooting

- **A plugin isn't loading / an agent complains it's missing** — run
  `verify_plugin_state(<name>)` (ask Ellen). It compares the registry's desired
  state against what each running agent has actually loaded and reports the
  reason (`artifact_missing`, `corrupt_artifact`, `reload_required`,
  `authorization_missing`, unresolved secret, …).
- **`reload_required`** — the registry was updated but the agent hasn't been
  reconstructed yet; a reload (or the configurator's own post-update reload)
  clears it.
- **Health at a glance** — `/data/plugin-health.json` summarizes current plugin
  issues; Casa also DMs the operator when a *new* issue appears and affected
  agents prepend a one-line first-contact notice.

## Enabling a bundled-disabled specialist

Casa ships some Tier 2 specialist agents disabled by default (`finance`
today; others in future releases). They are "bundled but disabled" —
the YAML is shipped, but the specialist is not registered for delegation
dispatch until you opt in.

To enable one:

1. Open the Casa app config folder at `/config/agents/specialists/`
   (host path: `/addon_configs/{REPO}_casa-agent/agents/specialists/`).
2. Edit the specialist's YAML file (for example `finance.yaml`).
3. Change `enabled: false` to `enabled: true`.
4. Restart the Casa app.

After restart, check the app log for the
`Specialists: enabled=[...] disabled=[...]` summary line to confirm
your specialist moved into the enabled set. Residents can now invoke
it via `delegate_to_agent(agent="<role>", ...)`.

To disable it again, set `enabled: false` and restart. Your edits to
the YAML file persist across app updates — Casa only seeds from
bundled defaults when the file is absent.

## Claude Code driver (v0.13.1)

Plan 4a infrastructure — does not change user-facing behavior by itself.
Enables Tier 3 executors (plugin-developer, ha-developer) to run inside
real Claude Code CLI sessions, driven through their Telegram engagement
topics.

### Architecture

Each `driver: claude_code` engagement becomes its own s6-rc-supervised
service inside the app container. s6 owns supervision; Casa orchestrates
lifecycle via `s6-rc-compile` + `s6-rc-update`. Engagement subprocesses
outlive Casa-main restarts (service dependencies are ordering-only, not
lifetime-coupled).

- **Workspace:** `/data/engagements/<id>/` — CLAUDE.md, `.mcp.json`,
  isolated `$HOME`, named FIFO for Casa → CLI turn delivery. Assigned plugins
  are pinned at launch and loaded from the immutable store via repeated
  `--plugin-dir` flags (see [Plugins](#plugins-v0710)), not symlinks.
- **Service dirs:** `/data/casa-s6-services/engagement-<id>/` — `run` script
  + `type: longrun` + ordering dependency on `init-setup-configs` — plus a
  sibling logger service `engagement-<id>-log/` wired to it via
  `producer-for`/`consumer-for` (an s6-rc pipeline). Both are created and
  removed together; don't delete one without the other.
- **Logs:** the CLI's stdout+stderr land in `/var/log/casa-engagement-<id>/`
  (`s6-log`, 1 MB × 20 rotation), survive per-turn respawns, and are removed
  with the workspace when retention expires.
- **Auth:** `CLAUDE_CODE_OAUTH_TOKEN` flows via s6-overlay's `/command/with-contenv`.
  No `ANTHROPIC_API_KEY` path.

### Security caveat

The `block_token_exfiltration` and `block_credential_file_reads` hook
policies are speedbumps against casual prompt injection, not
defense-in-depth. They do not stop determined malicious prompts (indirect
env reads via `/proc/self/environ`, Write-then-exec, variable obfuscation,
HTTP exfil via any allowed tool). The real perimeter is **trust in the
executor's prompt scope and minimal `tools.allowed` list**. Do not engage
a `claude_code` executor with a prompt from an untrusted source.

### MCP HTTP bridge (v0.13.1)

Real `claude` CLI subprocesses reach Casa's MCP tools via
`POST /mcp/casa-framework` — stateless JSON-RPC 2.0 over HTTP, no SSE.
Engagement identity propagates through the `X-Casa-Engagement-Id` header
(written into `.mcp.json` by `provision_workspace`) and the bridge binds
`tools.engagement_var` for the tool call's duration. Missing / unknown
header → bound to `None` and engagement-gated tools return
`not_in_engagement`. GET returns 405. The same `CASA_TOOLS` tuple backs
both the SDK path and the HTTP path, so every in-process tool is
automatically reachable from real CLI engagements.

### Hook enforcement (v0.13.1)

`/hooks/resolve` is the CC-side counterpart to the in-process hook layer.
`hook_proxy.sh` POSTs the CC hook payload to `http://127.0.0.1:8099/hooks/resolve`
with a policy name; the handler resolves `HOOK_POLICIES[name]["factory"]()`,
gates on the policy's matcher regex, and awaits the callback to produce a
CC-native `{"hookSpecificOutput": {...}}` response. Callback exceptions
deny (not fail-open). Unknown policy denies. Matcher mismatch returns an
empty `{}` (CC allow). Per-executor hook parameters on the HTTP path use
factory defaults (the Configurator's defaults match what it wants);
wiring YAML params into the HTTP path is a later item.

### Workspace lifecycle (v0.13.1)

- **Provisioning:** `/data/engagements/<id>/.casa-meta.json` is written
  with `status: "UNDERGOING"` on engagement start.
- **Termination:** `_finalize_engagement` updates status to
  `COMPLETED` / `CANCELLED` / `ERROR` and writes `retention_until =
  now + 7 days`.
- **Sweeper:** APScheduler job (id `workspace_sweep`) runs every 6 hours,
  deletes terminal workspaces past `retention_until`. Missing `.casa-meta.json`
  or missing `retention_until` → leave alone (operator prunes explicitly
  via the MCP tool). The N150 has > 30 GB free so disk-pressure aggressive
  mode is not implemented.

### Workspace inspection MCP tools (v0.13.1)

All three are exposed on both the SDK and HTTP paths:

- `list_engagement_workspaces(status?)` — enumerate `/data/engagements/`
  entries with status, size, created/finished/retention timestamps.
  Truncates at 100. Optional status filter.
- `delete_engagement_workspace(engagement_id, force=false)` — delete a
  workspace. Refuses UNDERGOING without `force=true`; with `force=true`
  cancels + finalizes first, then rmtrees.
- `peek_engagement_workspace(engagement_id, path?, max_bytes?)` —
  read-only. Empty path returns a 3-deep tree listing; otherwise reads
  file contents up to `max_bytes` (default 64 KB, hard cap 512 KB).
  Path-traversal guarded via resolved-path containment check.

### Boot-replay heal (v0.13.1)

When a UNDERGOING engagement's s6 service dir is missing but the workspace
still exists AND the executor type is in `executor_registry`, boot replay
re-renders the run + log/run scripts and re-plants the service dir. Missing
workspace still warns and skips — operator must cancel manually or let the
sweeper collect after retention. Missing executor in registry also warns
and skips.

### Known limitations

- Per-executor hook parameters (e.g. `casa_config_guard.forbid_write_paths`)
  on the HTTP path use factory defaults rather than the executor's
  `hooks.yaml` values. The Configurator's defaults happen to match what
  it wants; wiring YAML params into the HTTP path is tracked as a later
  item.

### Boot replay

On Casa boot, `replay_undergoing_engagements` reconstructs the s6
supervision tree: sweeps orphan service dirs (engagements not UNDERGOING),
compiles + updates once, starts each remaining service, and spawns
URL-capture + respawn-poller tasks. Self-healing: a finalize that died
mid-teardown leaves an orphan that the next boot removes.

### Idle + resume

Engagement subprocesses idle via s6-supervision (no idle timeout in the
driver). On HA host reboot, `/data/` persists; boot replay re-launches
every UNDERGOING engagement's service, and the `run` script reads the
persisted `.session_id` to resume the CLI conversation exactly where it
left off.

## svc-casa-mcp service (v0.14.0)

Casa runs the `casa-framework` MCP server as a separate s6-supervised
service called `svc-casa-mcp`, listening on `127.0.0.1:8100` inside the
app container. Engagement subprocesses connect to it via the URL
written into their `.mcp.json` at provisioning time.

**Why it exists.** Engagement subprocesses survive casa-main restarts
(app updates, container respawns, HA reboots) because they run under
their own s6 services. Before v0.14.0, the MCP server lived inside
casa-main, so a casa-main restart dropped every live MCP connection
and any mid-turn tool call would fail with a connection error. With
the extracted service, the engagement's MCP TCP connection stays open
across casa-main restarts; mid-restart tool calls return JSON-RPC
`-32000 casa_temporarily_unavailable` (a clean recoverable error the
model can handle), not a connection drop.

**On-disk artifacts.**
- `/run/casa/internal.sock` (mode 0600) — Unix socket created by
  casa-main; svc-casa-mcp connects to it for every forwarded tool call.
- New engagement workspaces' `.mcp.json` points at
  `http://127.0.0.1:8100/mcp/casa-framework` and includes the
  `X-Casa-Engagement-Id` header binding.
- New engagement workspaces' hook proxy script POSTs to
  `http://127.0.0.1:8100/hooks/resolve`.

**Back-compat for pre-v0.14.0 workspaces.** Casa-main's public listener
on port 8099 still serves `/mcp/casa-framework` and `/hooks/resolve`
as a fallback. Pre-v0.14.0 workspaces (whose `.mcp.json` was baked at
provisioning time with the 8099 URL) continue to function until they
cycle out (manual cancel + re-engage, or 7-day workspace retention
sweep). The fallback is removed in v0.14.2 or later.

**Operational env-var overrides.**
- `CASA_FRAMEWORK_MCP_URL` — overrides the default URL that gets baked
  into newly-provisioned workspaces' `.mcp.json`. Leave unset for the
  shipped default.
- `CASA_HOOK_RESOLVE_URL` — overrides where `hook_proxy.sh` POSTs hook
  decisions.

## Plugin consumer infrastructure (v0.71.0)

Plugins are managed through the unified registry + immutable store — see
[Plugins](#plugins-v0710) for the full model. There is no marketplace and no
`enabledPlugins`: the registry (`/config/plugins/registry.json`) is the single
assignment authority, and each pinned plugin resolves to an immutable artifact
under `/config/plugins/store/<name>/<artifact-id>/`. The five defaults
(superpowers, plugin-dev, skill-creator, mcp-server-dev, context7) are seeded
from the app image and assigned to the plugin-developer executor.

## 1Password integration (v0.14.1)

Set `onepassword_service_account_token` (from https://developer.1password.com/docs/service-accounts/)
as a plaintext app option — it's the single root of trust and cannot
self-reference. Set `onepassword_default_vault` to the vault name (default
"Casa"). Every other password-typed option (`claude_oauth_token`,
`telegram_bot_token`, `webhook_secret`) accepts either
plaintext OR an `op://` reference.

Plugin env vars resolved via `plugin-env.conf` (`/config/plugin-env.conf`),
managed by Configurator.

## Plugin-developer (v0.14.1)

Ask the primary assistant to build a plugin. It engages plugin-developer in
a dedicated Telegram topic. Plugin-developer asks public/private, authors
the plugin in its own GitHub repo, pushes, and emits completion. Assistant
relays; on your confirm, Configurator adds it to the registry (`plugin_add`)
and assigns it to the target agents + asks for secrets via 1P Q&A.

Prerequisites:

- `onepassword_service_account_token` set (plaintext).
- 1P item titled exactly `GitHub` in the vault named by
  `onepassword_default_vault` (default `Casa`), with a field labeled
  `credential` holding a GitHub PAT with `repo` scope. Plugin-developer
  resolves `op://${onepassword_default_vault}/GitHub/credential` at
  engagement spawn — no separate app option.
