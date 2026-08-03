<div align="center">

<img src="casa/logo.png" alt="Casa — helpful AI for everyday life" width="356">

# Casa — Claude-powered agents for Home Assistant

**An always-on fleet of AI agents that help keep your life manageable — an assistant, a butler, a concierge, and specialists you add for whatever else you need. Reach them on Telegram or by voice. Home Assistant is where they live; your home is one of the things they look after.**

[![Open your Home Assistant instance and show the app store with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fbonzanni%2Fha-casa-app)

[![QA](https://github.com/bonzanni/ha-casa-app/actions/workflows/qa.yml/badge.svg)](https://github.com/bonzanni/ha-casa-app/actions/workflows/qa.yml)
[![Version](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbonzanni%2Fha-casa-app%2Fmain%2Fcasa%2Fconfig.yaml&query=%24.version&label=version&color=blue)](casa/CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

</div>

Casa packages the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)
as a Home Assistant app (formerly known as an add-on). Home Assistant is the vehicle —
always-on hardware you already own, a Supervisor to keep the container alive, and a voice
pipeline reaching every room. What runs inside it is broader than the house: a fleet of
Claude agents that answer questions, keep track of things for you, run errands on a
schedule, and hand specialised work to specialists you install. Controlling your home is
one capability among several, and it happens to be the one the platform makes easiest.

## The fleet

Three long-lived **residents** ship in the image, each with a persona, a voice, a scope, and
its own tool permissions:

| Resident | Default persona | What it is for |
|---|---|---|
| **Assistant** | *Ellen* | The one you chat with on Telegram. General help, orchestration, delegation, reminders, memory. |
| **Butler** | *Tina* | Voice-first house control — lights, climate, locks, media, sensors. Short spoken answers, no small talk. |
| **Concierge** | *Gary* | A medium-trust voice agent for anyone in the room: general questions and delegated lookups, no house control and no private data. |

Beyond them, two tiers exist so the fleet can grow without the residents growing:

- **Specialists** — ephemeral, role-keyed agents a resident delegates to for focused work
  (finances, a hobby domain, a mailbox, a research area). They have no channel of their own;
  they answer the resident that called them, and they are installed, upgraded, rolled back
  and uninstalled from git repositories.
- **Executors** — task-bounded agents that get a dedicated Telegram topic to work in. Two
  ship: the **configurator**, which edits Casa's own configuration on your behalf and
  reloads it, and the **plugin-developer**, which builds new plugins from scratch.

## Highlights

- **Telegram channel** — streaming replies, slash commands, and interactive
  "engagements" that open dedicated forum topics for longer-running work,
  including inline permission prompts and tappable choices.
- **Voice channel** — request- and socket-shaped transports for Home Assistant's Assist
  pipelines, consumed by the companion
  [`ha-casa-integration`](https://github.com/bonzanni/ha-casa-integration); a fast model,
  concise spoken answers, and deferred delivery so a slow answer can still arrive at the
  right speaker after the turn has ended.
- **Extensible by design** — specialists, plugins and personas all install from git
  repositories into a content-addressed store, pinned by identity and checksum, behind an
  explicit consent receipt. You add them by asking in chat; the configurator does the work
  and commits the change. A plugin that needs to talk to an outside service can complete an
  OAuth-style authorization flow, redirect leg included.
- **Home control** — the Home Assistant and Supervisor APIs, reached over MCP by the agents
  whose configuration names them (in the shipped fleet, the butler). *Which* entities are
  reachable stays where it belongs — in Home Assistant's own exposure settings, not in a
  second allowlist here.
- **Scheduling & reminders** — interval, cron, date and webhook triggers, plus durable
  reminders an agent can set for you mid-conversation. They survive restarts.
- **Long-term memory** *(optional)* — semantic memory backed by a
  [Hindsight](https://github.com/vectorize-io/hindsight) server. One shared bank, with
  sensitivity tiers and per-channel, per-sender read clearance that fails closed: what the
  assistant knows privately is not what a guest-facing voice agent can recall. Recall
  distinguishes "I searched and found nothing" from "I could not search", so an agent does
  not turn an outage into a confident denial.
- **Secrets done right** *(optional)* — reference credentials as 1Password
  `op://` URIs instead of pasting them into config.
- **Security-minded** — ingress-only UI, custom AppArmor profile, HMAC-authenticated
  webhooks and voice routes, an operator identity that gates protected tools, secret
  redaction in logs, and Cosign-signed images.

## Growing your own fleet

The residents are deliberately a fixed, small set — the interesting growth happens around
them. Casa treats three things as installable components, each fetched from a git
repository you name (`owner/repo@ref`) and each acknowledged before anything is written:

- **Specialists** — a whole agent: prompt, scope, tools, delegation rules. Install one for
  a domain you care about and wire it into a resident's delegates, and from then on "ask
  Ellen about the invoices" reaches the finance specialist.
- **Plugins** — [Claude Code](https://claude.com/claude-code) plugins that add tools,
  skills and MCP servers, assigned to specific agents rather than the whole fleet. Tools a
  plugin marks as protected can only be authorized by the configured operator.
- **Personas** — the character and voice an agent wears, swappable without touching what it
  is allowed to do, and resettable to the shipped default. A persona changes how an agent
  sounds; capability comes from the role and the tool layer, never from prose.

Because the configurator is itself an agent, all of this is conversational: you describe
what you want, it opens a topic, makes the change, and reloads Casa. Building a *new*
plugin is the same shape — the plugin-developer executor scaffolds, tests and ships one.

### Where this is heading

Casa is pre-1.0 and the extension story is the part still being built out in the open:

- **Ready-made specialists** — a published catalog of installable specialists for common
  jobs (mail, money, calendars, research, hobbies), so a new install can grow useful
  capabilities in a few sentences rather than a few hours. The mechanism ships today; the
  public catalog does not yet exist.
- **Author your own** — an authoring contract for specialists and plugins, so one you write
  for yourself installs exactly like a published one. The install and identity mechanics are
  already documented in the [architecture corpus](docs/README.md); an authoring guide is
  not, and is part of getting to 1.0.
- **More ways to reach the fleet** — the channel layer is deliberately separate from the
  agents, and Telegram and voice are the first two, not the last.

Feature direction is tracked in the open —
[issues](https://github.com/bonzanni/ha-casa-app/issues) are where it is discussed.

## Apps in this repository

### [Casa](./casa)

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

_An always-on fleet of Claude agents — assistant, butler, concierge and specialists you add. The main (and currently only) app._

## Installation

1. Click the button above, or add this repository URL manually in
   **Settings → Apps → App Store → ⋮ → Repositories** (on older Home Assistant
   versions: **Settings → Add-ons → Add-on Store**):
   `https://github.com/bonzanni/ha-casa-app`
2. Install **Casa** from the store.
3. Set your Claude OAuth token in the app configuration (run `claude setup-token`
   on your workstation to obtain one), optionally add a Telegram bot token, and
   start the app. The full walkthrough lives in the
   [documentation](casa/DOCS.md).

> [!NOTE]
> Installs pull a prebuilt, Cosign-signed container image from GHCR — no
> on-device build.

## Requirements

- Home Assistant OS or a Supervised installation (the app needs the Supervisor),
  Home Assistant 2025.4 or newer.
- An **amd64** (x86-64) or **aarch64** (arm64) machine.
- A **Claude Max subscription** for the OAuth token the agents run on.
- Optional: a Telegram bot (via [@BotFather](https://t.me/BotFather)), the
  [`ha-casa-integration`](https://github.com/bonzanni/ha-casa-integration) companion
  integration for Assist voice, a Hindsight server for long-term memory, a 1Password
  service account for secret references.

## Documentation & support

- [Casa documentation](casa/DOCS.md) — setup, configuration reference,
  channels, memory, troubleshooting.
- [Architecture corpus](docs/README.md) — how the system actually works, written for
  contributors and for the agents that help build it.
- [Changelog](casa/CHANGELOG.md)
- Found a bug or have a feature request?
  [Open an issue](https://github.com/bonzanni/ha-casa-app/issues).

## Development

Run `make setup` once on a fresh checkout (Linux/WSL2), then `make test-unit` for
the fast gate and `make test-docker` for the container-backed tiers. Changes land
via squash-merged pull requests; every release bumps `casa/config.yaml` and
adds a `casa/CHANGELOG.md` entry.

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
