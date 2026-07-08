<div align="center">

<img src="casa-agent/logo.png" alt="Casa logo" width="256">

# Casa — Claude-powered agents for Home Assistant

**An always-on fleet of AI agents living inside your Home Assistant: chat with them on Telegram, talk to them through Assist, put your home on a schedule.**

[![Open your Home Assistant instance and show the app store with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fbonzanni%2Fcasa-ha-app)

[![QA](https://github.com/bonzanni/casa-ha-app/actions/workflows/qa.yml/badge.svg)](https://github.com/bonzanni/casa-ha-app/actions/workflows/qa.yml)
[![Version](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbonzanni%2Fcasa-ha-app%2Fmaster%2Fcasa-agent%2Fconfig.yaml&query=%24.version&label=version&color=blue)](casa-agent/CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

</div>

Casa packages the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)
as a Home Assistant app (formerly known as an add-on). It runs a small fleet of
Claude agents next to your smart home: a primary agent you chat with over Telegram,
a low-latency voice agent for Home Assistant's Assist pipelines, and on-demand
specialist subagents for focused jobs — all supervised inside one container, with
the Home Assistant APIs at their fingertips.

## Highlights

- **Telegram channel** — streaming replies, slash commands, and interactive
  "engagements" that open dedicated forum topics for longer-running tasks,
  including inline permission prompts.
- **Voice channel** — SSE/WebSocket endpoints tuned for Assist voice pipelines;
  concise responses from a fast model.
- **Agent fleet** — a primary agent (default: *Ellen*), a voice agent (default:
  *Tina*), and specialist subagents with scoped tool permissions, built on the
  Claude Agent SDK and MCP.
- **Home control** — first-class access to the Home Assistant and Supervisor APIs.
- **Scheduling** — cron-style triggers and heartbeats with proper timezone handling.
- **Long-term memory** *(optional)* — semantic memory backed by a
  [Hindsight](https://github.com/vectorize-io/hindsight) server, with tiered
  read/write access per channel.
- **Secrets done right** *(optional)* — reference credentials as 1Password
  `op://` URIs instead of pasting them into config.
- **Security-minded** — ingress-only UI, custom AppArmor profile, HMAC-authenticated
  webhooks, secret redaction in logs.

## Apps in this repository

### [Casa Agent](./casa-agent)

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

_Personal AI agent framework powered by Claude — the main (and currently only) app._

## Installation

1. Click the button above, or add this repository URL manually in
   **Settings → Apps → App Store → ⋮ → Repositories** (on older Home Assistant
   versions: **Settings → Add-ons → Add-on Store**):
   `https://github.com/bonzanni/casa-ha-app`
2. Install **Casa Agent** from the store.
3. Set your Claude OAuth token in the app configuration (run `claude setup-token`
   on your workstation to obtain one), optionally add a Telegram bot token, and
   start the app. The full walkthrough lives in the
   [documentation](casa-agent/DOCS.md).

> [!NOTE]
> Installs pull a prebuilt, Cosign-signed container image from GHCR — no
> on-device build.

## Requirements

- Home Assistant OS or a Supervised installation (the app needs the Supervisor),
  Home Assistant 2025.4 or newer.
- An **amd64** (x86-64) or **aarch64** (arm64) machine.
- A **Claude Max subscription** for the OAuth token the agents run on.
- Optional: a Telegram bot (via [@BotFather](https://t.me/BotFather)), a Hindsight
  server for long-term memory, a 1Password service account for secret references.

## Documentation & support

- [Casa Agent documentation](casa-agent/DOCS.md) — setup, configuration reference,
  channels, memory, troubleshooting.
- [Changelog](casa-agent/CHANGELOG.md)
- Found a bug or have a feature request?
  [Open an issue](https://github.com/bonzanni/casa-ha-app/issues).

## Development

Run `make setup` once on a fresh checkout (Linux/WSL2), then `make test-unit` for
the fast gate and `make test-docker` for the container-backed tiers. Changes land
via squash-merged pull requests; every release bumps `casa-agent/config.yaml` and
adds a `casa-agent/CHANGELOG.md` entry.

### AI-assisted development

Casa is a Claude-powered agent, and it is largely built with one: development
happens with [Claude Code](https://claude.com/claude-code), with every change
reviewed, tested, and shipped by the maintainer, who takes full responsibility
for the code. AI assistance is disclosed with `Assisted-by:` trailers in the
commit history.

## License & disclaimer

[MIT](LICENSE). This project is not affiliated with, endorsed by, or sponsored by
Anthropic, Nabu Casa, or the Home Assistant project. *Claude* is a trademark of
Anthropic, PBC.
