# Casa Agent

A personal AI agent framework running as a Home Assistant add-on, powered by the Claude Agent SDK.

## What it does

Casa runs always-on AI agents inside your Home Assistant instance. The primary agent (Ellen) handles general queries, smart home control, and task delegation via Telegram. A voice agent (Tina) provides fast, concise responses optimized for HA voice pipelines. On-demand subagents handle specialized tasks like building automations or managing finances.

## Prerequisites

- **Claude Max subscription** with an OAuth token (run `claude setup-token` on your local machine to obtain one)
- **Home Assistant 2025.4+** on an amd64 system
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

### Optional -- Memory

By default, Casa persists conversation history to a local SQLite
database at `/data/memory.sqlite`. Set `HONCHO_API_KEY` to use the
Honcho cloud backend instead (adds semantic retrieval + peer
representations). Set `MEMORY_BACKEND=noop` if you want no memory at
all.

| Option | Description |
|--------|-------------|
| `honcho_api_url` | Honcho API URL. Defaults to `https://api.honcho.dev`. |
| `honcho_api_key` | Honcho API key. When set, enables the Honcho cloud backend (overrides SQLite default). |

The following env vars can be set via the add-on environment (not the
options panel) for finer control:

| Env var | Purpose | Default |
|---|---|---|
| `MEMORY_BACKEND` | Force a specific backend (`honcho` / `sqlite` / `noop`). Fails fast on typos. | unset (auto-resolves) |
| `MEMORY_DB_PATH` | SQLite file location. | `/data/memory.sqlite` |

### Optional -- Agents

| Option | Description |
|--------|-------------|
| `primary_agent_name` | Name of the primary agent. Default: `Ellen`. |
| `voice_agent_name` | Name of the voice agent. Default: `Tina`. |
| `primary_agent_model` | Model for the primary agent: `opus`, `sonnet`, or `haiku`. Default: `opus`. |
| `voice_agent_model` | Model for the voice agent. Default: `haiku`. |
| `subagent_model` | Model for on-demand subagents. Default: `sonnet`. |

### Optional -- Features

| Option | Description |
|--------|-------------|
| `enable_terminal` | Enable a web terminal accessible via the ingress panel. Default: `false`. |
| `webhook_secret` | HMAC-SHA256 secret for authenticating webhook requests. Leave empty to skip verification. |

### Optional -- Workspace

| Option | Description |
|--------|-------------|
| `repos` | List of git repositories to clone into the workspace. Each entry needs `url`, `path`, and optionally `branch`. |

Example:

```yaml
repos:
  - url: https://github.com/you/casa-skills
    path: casa-skills
    branch: main
```

Repos are cloned on first boot and pulled on subsequent boots (unless there are local changes).

## How it works

1. **Startup**: The add-on validates your OAuth token, copies default agent configs (if first boot), syncs workspace repos, and starts nginx + the Casa core process.
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

### Invoke example

```bash
curl -X POST http://homeassistant.local:8080/invoke/ellen \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is the temperature in the living room?"}'
```

## Agent configuration

Agent YAML files are stored in `/addon_configs/casa-agent/agents/`. Default configs are created on first boot and never overwritten. You can edit them freely.

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

Toggle the transports via environment variables on the add-on:

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

When `enable_terminal` is enabled, a web terminal is available at the `/terminal/` path in the ingress panel. This gives you shell access to the add-on container for debugging and manual operations.

## Troubleshooting

- **Add-on won't start**: Check the log for "claude_oauth_token is required". You must set the token before starting.
- **No Telegram messages**: Verify `telegram_bot_token` and `telegram_chat_id` are correct. The bot must have been started (`/start` in Telegram).
- **Engagements won't open (`engagement_not_configured`)**: See the "Troubleshooting engagements" subsection under [Engagements (v0.11.0)](#engagements-v0110) — most common cause is the bot missing "Manage topics" admin permission in the engagement supergroup.
- **Memory not working**: By default, memory persists to `/data/memory.sqlite` (SQLite backend). If `HONCHO_API_KEY` is set but memory still appears empty, check container logs for `SQLite memory init failed` or Honcho connection errors. To disable memory entirely, set `MEMORY_BACKEND=noop`.
- **502 errors on ingress**: The Python process may still be starting. Wait up to 60 seconds after add-on start.

## Engagements (v0.11.0)

Casa supports **engagements** — bounded conversational threads where a
specialist (Tier 2) or executor (Tier 3, Plan 3+) works with you on a
specific task, separate from your 1:1 chat with Ellen. Each engagement
lives in its own Telegram forum topic inside a dedicated supergroup.

The setup is a one-time Telegram configuration. Skip this section to
keep Casa running in 1:1-only mode (Ellen delegates synchronously and
returns a single response; `delegate_to_specialist(mode="interactive")`
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
4. Other permissions (delete messages, pin, etc.) are optional. Casa does
   not require them. Leave unused ones off to minimise the bot's surface.
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
- If you already run Casa on the 1:1 chat, the addon log also echoes
  the ID under the `CHANNEL_IN` traces when the bot sees any message
  in the supergroup.

#### 4. Configure Casa

1. Home Assistant → **Settings** → **Add-ons** → **Casa Agent** → **Configuration**.
2. Set `telegram_engagement_supergroup_id` to the negative integer from step 3.
   Leave it at `0` to disable engagements (Casa still boots; interactive
   mode returns `engagement_not_configured`).
3. **Save** → **Restart** the addon.

#### 5. Verify

Once the addon has restarted, check the log for:

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
  is not `0` and the addon was restarted after the option was set.
- **`/cancel` / `/complete` / `/silent` don't appear in autocomplete.**
  They're scoped to the supergroup only. Make sure you're typing `/` in
  a topic inside the engagement supergroup, not your 1:1 DM with Ellen.
  Also: Telegram caches `setMyCommands` client-side — restart the
  Telegram client if they don't show up within 30 seconds of addon boot.
- **"No active engagement in this topic" reply in a topic.**
  The engagement for that topic has already completed, cancelled, or
  errored (registry status transition). Start a fresh engagement from
  your 1:1 DM with Ellen — do not reuse old topics.
- **"Could not resume this engagement" reply after 24h+ idle.**
  The suspended SDK session rotated before you came back. The
  engagement is marked as errored after two failed resumes. Start a
  new one; your prior conversation is still in Ellen's meta-scope
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
| Policies (scopes corpus, disclosure) | - | yes | yes | - |

Not yet supported:

- Eval running (configurator can shell to casa_eval, but no first-class recipe).
- Plugin/skill installation - waiting on Plan 5 plugin-developer.
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

- hard - Supervisor addon restart (~10-15s). Agent-shape changes, runtime, scope corpus.
- soft - In-process casa_reload_triggers(role). Trigger-only edits; no downtime.
- none - Prompt, response_shape, doctrine edits take effect on next turn.

Hard reload: Ellen verifies the reload landed on her resumed turn, then narrates.

### Recipe discovery

Configurator reads short markdown recipes from its own doctrine tree at `/addon_configs/casa-agent/agents/executors/configurator/doctrine/`. Edit these recipes to customize per-instance (e.g., add house rules).

### Troubleshooting

- **"engagement_not_configured"** - you haven't set telegram_engagement_supergroup_id, or the bot doesn't have can_manage_topics.
- **Configurator stalls after first message** - check engagement's driver log. If you see prompt_template_missing, restart the addon.
- **Hook denied, configurator cancelled** - expected for resident deletion. To override: edit configurator's hooks.yaml, commit, reload, retry.
- **Soft reload didn't take effect** - casa_reload_triggers requires the role to exist before the edit.
- **Doctrine references stale fields** - file an issue; maintainer forgot to sync doctrine with Casa-core change.

## Enabling a bundled-disabled specialist

Casa ships some Tier 2 specialist agents disabled by default (`finance`
today; others in future releases). They are "bundled but disabled" —
the YAML is shipped, but the specialist is not registered for delegation
dispatch until you opt in.

To enable one:

1. Open your Home Assistant addon config folder at
   `/addon_configs/<addon-uuid>/agents/specialists/`.
2. Edit the specialist's YAML file (for example `finance.yaml`).
3. Change `enabled: false` to `enabled: true`.
4. Restart the Casa addon.

After restart, check the addon log for the
`Specialists: enabled=[...] disabled=[...]` summary line to confirm
your specialist moved into the enabled set. Residents can now invoke
it via `delegate_to_specialist(specialist="<role>", ...)`.

To disable it again, set `enabled: false` and restart. Your edits to
the YAML file persist across addon updates — Casa only seeds from
bundled defaults when the file is absent.

## Claude Code driver (v0.13.1)

Plan 4a infrastructure — does not change user-facing behavior by itself.
Enables future Tier 3 executors (plugin-developer, ha-developer) to run
inside real Claude Code CLI sessions, reachable from the iOS app or
claude.ai/code via remote control.

### Architecture

Each `driver: claude_code` engagement becomes its own s6-rc-supervised
service inside the addon container. s6 owns supervision; Casa orchestrates
lifecycle via `s6-rc-compile` + `s6-rc-update`. Engagement subprocesses
outlive Casa-main restarts (service dependencies are ordering-only, not
lifetime-coupled).

- **Workspace:** `/data/engagements/<id>/` — CLAUDE.md, `.mcp.json`,
  isolated `$HOME`, Tier 1 baseline + Tier 2 per-executor plugin symlinks,
  named FIFO for Casa → CLI turn delivery.
- **Service dir:** `/data/casa-s6-services/engagement-<id>/` — `run` script
  + `type: longrun` + ordering dependency on `init-setup-configs`.
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
addon container. Engagement subprocesses connect to it via the URL
written into their `.mcp.json` at provisioning time.

**Why it exists.** Engagement subprocesses survive casa-main restarts
(addon updates, container respawns, HA reboots) because they run under
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
