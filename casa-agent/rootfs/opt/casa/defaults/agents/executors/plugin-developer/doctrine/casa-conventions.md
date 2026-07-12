# Casa conventions for plugin authors

## Repo + naming

- One plugin = one GitHub repo. Never group plugins into a monorepo.
- Default repo name: `casa-plugin-<slug>`. `<slug>` is the plugin's `name`
  field in `.claude-plugin/plugin.json` (kebab-case, no `casa-plugin-`
  prefix inside `plugin.json`).
- **Repos are created PRIVATE** (`gh repo create … --private`). Casa installs
  plugins from private repos (the in-container `GITHUB_TOKEN` authenticates the
  clone), so private is sufficient for the user's own agents. Making a plugin
  public — to *share* it beyond this Casa — is a deliberate later step the user
  runs themselves (`gh repo edit <repo> --visibility public`); Claude Code
  hard-blocks creating a public repo from within an engagement.
- Ship `origin` = just-created user-owned repo; `gh repo create --clone`
  sets this for you.

## Commit style

Match the superpowers convention:
`feat|fix|chore|test(<area>): <one-line>`

Examples:
- `feat(skill): teach Claude when to identify faces`
- `feat(mcp): wrap AWS Rekognition search_faces_by_image`

## What Casa consumes

Casa installs your plugin via `claude plugin install <name>@casa-plugins --scope project`
in the target agent-home. That means:

- `.claude-plugin/plugin.json` — required. `name`, `description`, `version`, and
  `author` **as an object** — `{"name": "..."}` (optionally `email`/`url`), NOT a
  bare string. Claude Code rejects a string `author` at install time
  (`author: Invalid input: expected object, received string`), which fails the
  whole install.
  - **`plugin.json::version` is THE plugin version** (P-2). Every change bumps
    it — Casa's marketplace pin + install/verify read it, and it's what
    `verify_plugin_state` reports. If the plugin ships an MCP server with its
    own `server/package.json`, keep that `version` in **sync** with
    `plugin.json` (bump both in the same commit) so the manifest and the
    running server never disagree.
- `skills/<name>/SKILL.md` — skills pack. Single-line description triggers
  right. Keep it specific.
- `agents/<name>.md` — optional subagents.
- `.mcp.json` — MCP server declaration. Use `${CLAUDE_PLUGIN_ROOT}/server.py`
  and similar relative anchors.
- `hooks/hooks.json` — optional.
- `README.md` — required. Document required env vars + any
  `casa.systemRequirements` declarations in the marketplace entry.

Your plugin does NOT know about Casa. It's a plain CC plugin.

## Env var declaration

`.mcp.json::env` is your implicit env declaration:

```json
"env": {
  "MY_VAR": "${MY_VAR}"
}
```

At install time Configurator extracts every `${VAR}` reference (minus CC
built-ins), asks the user for a 1P reference, and writes to
`plugin-env.conf`. Values never appear in transcripts.

## Completion schema

When you finish, emit:

```json
{
  "status": "ok",
  "text": "<human-readable summary>",
  "artifacts": [{
    "kind": "casa_plugin_repo",
    "repo_url": "https://github.com/<user>/casa-plugin-<slug>.git",
    "plugin_name": "<slug>",
    "ref": "<sha>",
    "version": "<semver>",
    "visibility": "public|private"
  }],
  "next_steps": [{
    "action": "add_to_marketplace_and_install_with_confirmation",
    "plugin_name": "<slug>",
    "repo_url": "...",
    "ref": "<sha>",
    "description": "<short>",
    "category": "productivity|data|security|...",
    "targets": ["<role>", ...]
  }]
}
```

## Operator approval for non-allow-listed tools

Casa enforces a permission gate on every tool call. Tools matching your
`tools.allowed` patterns run with no prompt. Tools that do NOT match
raise a `[Allow] [Deny]` inline keyboard in this engagement's
Telegram topic; your call blocks until the operator taps a button (or
10 min elapses, treated as deny).

When you see "Operator denied via Telegram" in a tool result, the
operator has rejected that specific call. Acknowledge in your next
turn, then either retry with a different tool, describe what you
would have done so the operator can decide whether to approve, or
ask the operator directly via `mcp__casa-engagement-channel__reply`.

## MCP server naming = the grant namespace

Your `.mcp.json` `mcpServers` key becomes part of every tool's callable name:
`mcp__plugin_<plugin-name>_<server-key>__<tool>`. Casa grants installed
plugins server-level (`mcp__plugin_<plugin-name>_<server-key>`), derived from
that key. Name the server after the plugin (one server per plugin unless you
truly need more), and never rename it in a version bump — a rename orphans
nothing functionally (grants re-derive) but changes the tool names agents and
skills reference.
