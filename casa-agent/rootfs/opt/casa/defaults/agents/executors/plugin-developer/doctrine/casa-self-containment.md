# Self-containment axiom (§2.0)

**A plugin must be fully operational on a fresh Casa install solely by the
configurator adding it to the registry (`plugin_add`) and assigning it.**

No Dockerfile forks. No "please install X manually." No out-of-band
configuration. Everything a plugin needs — system tools, env vars,
persistent state — is declared in the plugin's own manifest
(`.claude-plugin/plugin.json`, incl. `casa.systemRequirements`) or its
`.mcp.json::env`.

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
  - PyPI package **providing a CLI binary** → `type: venv` (installs the
    package and symlinks its `verify_bin` into the shared tools bin).
  - npm package → `type: npm`.
  - Only available as apt package → plugin NOT shippable pre-1.0.0.
    Find an alternative or request baseline expansion.

## Python MCP servers (library dependencies)

`casa.systemRequirements type: venv` is for CLI *binaries* — it is NOT a
way to make libraries importable by your `server.py`. There is **no
requirements-provisioning step at install time**: the installed artifact
is exactly the committed tree. In particular:

- **Never reference a per-plugin venv** (`server/.venv/...`) from
  `.mcp.json` — venvs are dev-only, gitignored, and absent from the
  installed artifact (the gmail-v0.2.0 incident: the MCP server could
  never spawn).
- **`mcpb.json` is not a Casa mechanism.** Nothing in Casa reads it; do
  not rely on it (or any MCPB packaging assumption) for provisioning.

The sanctioned pattern — vendor the deps and commit them:

    pip install --target server/vendor -r server/requirements.txt

with every version pinned (hashes / a lockfile preferred), then:

    { "mcpServers": { "<name>": {
        "command": "python3",
        "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"],
        "env": { "PYTHONPATH": "${CLAUDE_PLUGIN_ROOT}/server/vendor" }
    } } }

Pure-Python dependencies are portable and the default. Native/compiled
dependencies are shippable ONLY as platform-matched artifacts (linux,
the image's Python 3.11, the image's arch) committed intentionally with
a reproducibility note in the README — otherwise find a pure-Python
alternative or request baseline expansion. Before tagging a release,
smoke-test the vendored set imports under `python3` (§release ritual).

`.mcp.json` `command`/`args` may reference only **committed** files or
§4.5 baseline binaries.

## Pre-push enforcement

The `self_containment_guard` hook scans your working tree when you run
`git push` for:

- Hardcoded absolute paths to non-baseline binaries.
- README strings matching `please install|manually install|fork the Dockerfile`.
- `apt install` / `yum install` in shell scripts.
- `casa.systemRequirements` entries with `type: apt`.
- `.mcp.json` `command`/`args` entries whose `${CLAUDE_PLUGIN_ROOT}/<path>`
  is **not tracked by git** (untracked or gitignored — e.g. a dev venv),
  escapes the plugin root via `..`, or is absolute after interpolation.

Matches block the push. Fix and retry. For a genuine false positive
(only), re-run as `CASA_ALLOW_ANTI_PATTERN=1 git push ...` — the
override is logged with the findings it waved through.
