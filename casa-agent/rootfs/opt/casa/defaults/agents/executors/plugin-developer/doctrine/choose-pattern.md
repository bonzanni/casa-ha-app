# Choosing a plugin pattern

## MCP server vs skill-only

**MCP server** — when you need a structured tool surface (typed inputs,
typed outputs, predictable error shapes). Example: `face-rec` exposes
`identify_face(image_url) → {name, confidence}`.

**Skill-only** — when you're teaching Claude *when* to invoke existing
tools (Bash, Read, Write, …) in a new domain. No subprocess needed. Faster
to author, lower runtime cost.

## MCPB vs casa.systemRequirements vs vendoring

**MCPB** — package your MCP server + its Python/Node runtime deps into a
sealed bundle. Best for: pure-Python or pure-Node plugins. Use
`mcp-server-dev:build-mcpb` skill.

**casa.systemRequirements** — declare a third-party CLI your MCP server
shells out to (`aws`, `kubectl`, `terraform`, `ffmpeg` (via tarball), …).
Casa installs into `/addon_configs/casa-agent/tools/`. Use this for any
non-baseline CLI. Three strategies available: `tarball`, `venv`, `npm`.

**Vendoring** — small pure-Python deps only. Avoid; MCPB is nearly always
better.

## Apt / dpkg

**Not supported pre-1.0.0.** Any plugin declaring `{type: "apt", …}` is
rejected at marketplace_add_plugin time (§4.3.2). If your plugin needs a
system library (e.g. `ffmpeg`), either:

- Use a tarball install strategy with a statically-linked build, or
- Request baseline expansion via Casa issue tracker (post-1.0.0).
