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

### Optional -- Memory

| Option | Description |
|--------|-------------|
| `honcho_api_url` | Honcho API URL. Defaults to `https://api.honcho.dev`. |
| `honcho_api_key` | Honcho API key. When set, enables persistent cross-session memory. |

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
| `heartbeat_enabled` | Enable periodic heartbeat that prompts the primary agent to check for pending tasks. Default: `true`. |
| `heartbeat_interval_minutes` | Heartbeat interval in minutes (15--1440). Default: `60`. |
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
5. **Heartbeat**: A configurable periodic prompt checks for pending tasks, upcoming events, and proactive actions.

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

Each agent config supports: `name`, `role`, `model`, `personality`, `tools`, `mcp_server_names`, `memory`, `session`, `channels`, and `cwd`. See the default `assistant.yaml` for a full example.

## Web terminal

When `enable_terminal` is enabled, a web terminal is available at the `/terminal/` path in the ingress panel. This gives you shell access to the add-on container for debugging and manual operations.

## Troubleshooting

- **Add-on won't start**: Check the log for "claude_oauth_token is required". You must set the token before starting.
- **No Telegram messages**: Verify `telegram_bot_token` and `telegram_chat_id` are correct. The bot must have been started (`/start` in Telegram).
- **Memory not working**: Honcho memory requires both `honcho_api_url` and `honcho_api_key`. Without them, agents run without persistent memory (conversation context is ephemeral).
- **502 errors on ingress**: The Python process may still be starting. Wait up to 60 seconds after add-on start.
