# Choosing a plugin pattern

## Receiving events (webhooks)

Plugins can't listen — no ports, no routes, no resident processes. If the
plugin must RECEIVE events (a provider webhook, a voicemail notification),
declare `casa.triggers` in the manifest and ship a setup tool that points
the provider at `POST /webhook/plg-<plugin>--<name>`. Full doctrine:
`ingress.md`.

## MCP server vs skill-only

**MCP server** — when you need a structured tool surface (typed inputs,
typed outputs, predictable error shapes). Example: `face-rec` exposes
`identify_face(image_url) → {name, confidence}`.

**Skill-only** — when you're teaching Claude *when* to invoke existing
tools (Bash, Read, Write, …) in a new domain. No subprocess needed. Faster
to author, lower runtime cost.

## Vendoring vs casa.systemRequirements (MCPB is NOT a Casa mechanism)

**Vendoring — the sanctioned pattern for Python MCP-server library deps.**
Commit your deps (`pip install --target server/vendor -r
server/requirements.txt`, pinned) and launch with `python3` +
`PYTHONPATH=${CLAUDE_PLUGIN_ROOT}/server/vendor`. Full recipe:
`casa-self-containment.md` §"Python MCP servers". Node servers: commit
the built `server/dist/` and launch with baseline `node`.

**casa.systemRequirements** — declare a third-party CLI your MCP server
shells out to (`aws`, `kubectl`, `terraform`, `ffmpeg` (via tarball), …).
Casa installs into `/config/tools/`. Use this for any
non-baseline CLI. Three strategies available: `tarball`, `venv`, `npm`.
This provisions *binaries on PATH* — it does NOT make libraries
importable by your server.

**MCPB — do NOT use for Casa plugins.** Casa reads `.mcp.json` from the
committed tree and provisions nothing at install time: no `mcpb.json`,
no requirements install, no bundle unpack, no venv creation. An MCPB
bundle (or any `.venv` reference) yields a server that can never spawn
in Casa (this exact failure shipped as gmail v0.2.0). The
`mcp-server-dev:build-mcpb` skill is for distributing to Claude
Desktop-style hosts — never for a Casa plugin.

## Apt / dpkg

**Not supported pre-1.0.0.** Any plugin declaring `{type: "apt", …}` is
rejected at `plugin_add` time (§4.3.2). If your plugin needs a
system library (e.g. `ffmpeg`), either:

- Use a tarball install strategy with a statically-linked build, or
- Request baseline expansion via Casa issue tracker (post-1.0.0).
