# Self-containment axiom (§2.0)

**A plugin must be fully operational on a fresh Casa install solely by
marketplace-add + `install_casa_plugin`.**

No Dockerfile forks. No "please install X manually." No out-of-band
configuration. Everything a plugin needs — system tools, env vars,
persistent state — is declared in the plugin's marketplace entry or its
own `.mcp.json::env`.

## What this forbids

- Hardcoded `/usr/bin/<X>` paths for `<X>` outside the §4.5 baseline
  runtime. Use relative anchors (`${CLAUDE_PLUGIN_ROOT}`) or declare
  via `casa.systemRequirements`.
- README lines like "please install X manually" or "fork the Casa
  Dockerfile."
- `apt install` or `yum install` in shell scripts inside the plugin.
- Env vars set "externally" — declare via `.mcp.json::env ${VAR}`.
- Writes outside `${CLAUDE_PLUGIN_DATA}` and the MCP subprocess cwd.
- `casa.systemRequirements: [{type: "apt", …}]` (hard-rejected).

## Decision tree

I need tool X. Is X in the §4.5 baseline (bash, curl, jq, git, python3,
node, npm, gh, op, yq, …)?

- **Yes** — use it freely. No declaration needed.
- **No** — declare via `casa.systemRequirements`:
  - Single binary / tarball available upstream → `type: tarball`.
  - PyPI package → `type: venv`.
  - npm package → `type: npm`.
  - Only available as apt package → plugin NOT shippable pre-1.0.0.
    Find an alternative or request baseline expansion.

## Pre-push enforcement

The `self_containment_guard` hook greps your staged tree pre-push for:

- Hardcoded absolute paths to non-baseline binaries.
- README strings matching `please install|manually install|fork the Dockerfile`.
- `apt install` / `yum install` in shell scripts.
- `casa.systemRequirements` entries with `type: apt`.

Matches block the push. Fix and retry. Use `--allow-anti-pattern` only
for genuine false positives (logged).
