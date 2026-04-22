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
- **Memory not working**: By default, memory persists to `/data/memory.sqlite` (SQLite backend). If `HONCHO_API_KEY` is set but memory still appears empty, check container logs for `SQLite memory init failed` or Honcho connection errors. To disable memory entirely, set `MEMORY_BACKEND=noop`.
- **502 errors on ingress**: The Python process may still be starting. Wait up to 60 seconds after add-on start.

## Engagements (v0.11.0)

Casa supports **engagements** — bounded conversational threads where a
specialist (Tier 2) or executor (Tier 3, Plan 3+) works with you on a
specific task, separate from your 1:1 chat with Ellen.

### Setup

1. In Telegram, create a new **forum supergroup** (Topics enabled) dedicated
   to Casa engagements. Different from your 1:1 chat with the bot.
2. Add the Casa bot to the supergroup as **administrator** with
   "Manage Topics" permission enabled.
3. Get the chat ID (negative integer). You can read it from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` after posting a message
   in the supergroup.
4. Set `telegram_engagement_supergroup_id` in addon options to the chat ID.
5. Restart the Casa addon.

On boot Casa registers three slash commands on the supergroup:

- `/cancel` — cancel the current engagement in this topic.
- `/complete` — mark the engagement complete (no agent summary).
- `/silent` — stop Ellen's proactive notifications for this engagement.

Type `/` in any topic to see them in the autocomplete menu.

### Starting an engagement

Ask Ellen in the main chat for something multi-turn, e.g.
*"let's work through Q2 invoicing with Alex"*. Ellen may open an
engagement — if so she'll tell you which topic to head to. The
specialist is waiting there.

### Idle reminders

Engagements have no hard timeout. If an engagement sits idle for 3 days
(specialists) or 7 days (executors, Plan 3+), Ellen will nudge you in the
main 1:1 chat. Reminder re-fires weekly. Suspend/resume is automatic —
after 24 hours of inactivity Casa tears down the underlying SDK client to
free resources; it resumes seamlessly on your next message in the topic.

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
