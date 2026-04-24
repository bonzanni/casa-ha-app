# Casa conventions for plugin authors

## Repo + naming

- One plugin = one GitHub repo. Never group plugins into a monorepo.
- Default repo name: `casa-plugin-<slug>`. `<slug>` is the plugin's `name`
  field in `.claude-plugin/plugin.json` (kebab-case, no `casa-plugin-`
  prefix inside `plugin.json`).
- Visibility (public/private) is a user decision per engagement.
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

- `.claude-plugin/plugin.json` — required. name, description, version, author.
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
