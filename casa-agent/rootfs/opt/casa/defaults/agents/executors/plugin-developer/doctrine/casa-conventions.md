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

Casa adds your plugin to its registry via the configurator's `plugin_add` /
`plugin_update` tools, which publish an immutable content-addressed artifact
from your pinned commit. That means:

- `.claude-plugin/plugin.json` — required. `name`, `description`, `version`, and
  `author` **as an object** — `{"name": "..."}` (optionally `email`/`url`), NOT a
  bare string. Claude Code rejects a string `author` at install time
  (`author: Invalid input: expected object, received string`), which fails the
  whole install.
  - **`plugin.json::version` is THE plugin version** (P-2). Every change bumps
    it — Casa derives the registry version from it, and it's what
    `verify_plugin_state` reports. If the plugin ships an MCP server with its
    own `server/package.json`, keep that `version` in **sync** with
    `plugin.json` (bump both in the same commit) so the manifest and the
    running server never disagree.
  - **Release ritual (v0.74.0 — REQUIRED, mechanically enforced):** every
    release ships as an **annotated tag** named exactly
    `"v" + plugin.json.version` (version `1.3.0` → tag `v1.3.0`):
    1. `VERSION=$(jq -r .version .claude-plugin/plugin.json)`
    2. `git tag -a "v${VERSION}" -m "release v${VERSION}"`
    3. Push branch **and** tag **atomically** — a half-push can leave a
       branch the configurator would mis-pin:
       `git push --atomic origin main "refs/tags/v${VERSION}"`
    4. **Existing conflicting tag → fail closed.** If the remote already has
       `v${VERSION}` at a different commit, **stop** — never `--force`/move a
       published release tag. Bump `plugin.json::version` and retry.
    5. **Remote peel-verify before completing** (executable — the tag is
       annotated, so compare the *peeled* commit, not the tag-object sha):
       ```bash
       [ "$(git ls-remote origin "refs/tags/v${VERSION}^{}" | cut -f1)" \
         = "$(git rev-parse HEAD)" ] && echo PEEL-OK || echo PEEL-FAILED
       ```
       (equivalently `gh api repos/<owner>/<repo>/commits/v${VERSION} --jq
       .sha` — the same call Casa's resolver makes, so your verify and the
       configurator's pin agree by construction). On PEEL-FAILED, stop and
       fix the push before emitting completion.
  - **Handoff:** hand the operator/configurator all **three** identity
    fields — `ref` (the `vX.Y.Z` tag), `revision` (the 40-hex commit sha,
    lowercase, that the tag peels to), `version` (`X.Y.Z`). Casa pins via
    `plugin_update(name, new_ref=<tag>, expected_revision=<sha>)`; pushing a
    tag alone changes nothing in Casa until the configurator points the
    registry at it (§3.13). **`emit_completion` mechanically validates every
    `casa_plugin_repo` artifact** (annotated tag exists, peels to
    `revision`, tag == `"v" + <remote plugin.json.version>`, `version`
    matches) and rejects the completion otherwise — the engagement stays
    live so you can fix the release and re-emit.
- `skills/<name>/SKILL.md` — skills pack. Single-line description triggers
  right. Keep it specific.
- `agents/<name>.md` — optional subagents.
- `.mcp.json` — MCP server declaration. Use `${CLAUDE_PLUGIN_ROOT}/server.py`
  and similar relative anchors.
- `hooks/hooks.json` — optional.
- `README.md` — required. Document required env vars + any
  `casa.systemRequirements` declarations in `.claude-plugin/plugin.json`.

Your plugin does NOT know about Casa. It's a plain CC plugin.

## Env var declaration

`.mcp.json::env` is your implicit env declaration:

```json
"env": {
  "MY_VAR": "${MY_VAR}"
}
```

When Configurator runs `plugin_add`, it reports every required `${VAR}`
reference (minus CC built-ins); the configurator asks the user for a 1P
reference and writes to `plugin-env.conf`. Values never appear in transcripts.

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
    "ref": "vX.Y.Z",
    "revision": "<40-hex commit sha the tag peels to, lowercase>",
    "version": "X.Y.Z",
    "visibility": "public|private"
  }],
  "next_steps": [{
    "action": "add_to_registry_and_assign_with_confirmation",
    "plugin_name": "<slug>",
    "repo_url": "...",
    "ref": "vX.Y.Z",
    "revision": "<same 40-hex sha>",
    "description": "<short>",
    "category": "productivity|data|security|...",
    "targets": ["<role>", ...]
  }]
}
```

`ref` is always the release tag (`vX.Y.Z` — never a bare sha, never a
branch); `revision` is the exact commit it peels to; `version` matches
`plugin.json`. All three are validated against the live remote when you call
`emit_completion` — a lightweight tag, a moved tag, or a version mismatch
rejects the completion.

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
