# Recipe: add a plugin to the registry

A Casa plugin is a Claude Code plugin (commands / skills / hooks / MCP
servers) consumed by Casa agents. Under the unified plugin architecture the
registry (`/config/plugins/registry.json`) is the single assignment authority:
one `plugin_add` call publishes an immutable, content-addressed artifact,
installs any system requirements, assigns it to targets, and reloads + verifies
— all inside the tool.

## Ask the user

1. **Plugin name?** The plugin's own `.claude-plugin/plugin.json` `name` —
   lowercase, hyphenated. **The repo name is NOT the plugin name:** keeper
   repos follow `casa-plugin-<name>` (repo `casa-plugin-gmail` → plugin
   `gmail`; repo `casa-plugin-lesina-invoice` → plugin `lesina-invoice`),
   and a repo may host the plugin in a `subdir`. Never pass the repo
   basename as `name`. Sources of truth, in order: the plugin-developer
   completion handoff (it states the plugin name), or the repo's
   `plugin.json`. `plugin_add` hard-rejects a wrong name with
   `name_mismatch` + the canonical `manifest_name` — retry the ADD with
   that exact name, don't guess again. (On `plugin_update` a
   `name_mismatch` means the NEW manifest renamed the plugin — that is
   never a retry: a rename is an explicit migration, `plugin_add` under
   the new name + `plugin_remove` of the old, operator-confirmed.)
2. **Source repo?** `<owner>/<repo>` (or a `https://github.com/<owner>/<repo>`
   URL). For a plugin-developer build, take it from that engagement's topic.
3. **Pin (ref)?** The release tag (`vX.Y.Z`) for a plugin-developer build —
   take it, plus the `revision` sha, verbatim from that engagement's
   completion handoff. (A sha or branch is acceptable only for a manual add
   of a third-party repo with no release handoff.)
4. **Targets?** One or more of `resident:<role>`, `specialist:<role>`,
   `executor:<type>` (e.g. `specialist:finance`).

## Do it

Call `plugin_add(name, repo, ref, subdir?, targets, expected_revision?)`.
For a plugin-developer handoff, ALWAYS pass `expected_revision` = the
producer's freshly-built `revision` — a tag that moved since the build
aborts hard (`revision_mismatch`) before anything is installed or activated.
There is **no version argument** — the version is derived from the fetched
`plugin.json` (a caller-supplied version is exactly the stale-code bug this
architecture removes).

The tool, internally and in order: resolves `ref`→commit (a 404 is a hard
`ref_not_found`, never silent), fetches + validates + atomically publishes the
artifact, installs system requirements BEFORE activation, writes the registry
entry, then reloads the affected in-casa agents and verifies desired==active.
**No separate casa_reload is needed** — reload + verify happen inside
`plugin_add` (and `plugin_update` / `plugin_assign` / `plugin_unassign` /
`plugin_remove`).

## Result

The result carries `artifact_id`, `version`, `revision`, `granted_tools`,
`required_env_vars`, and a `verify` summary. If `required_env_vars` is
non-empty, wire the secrets next (see `secrets.md`) — the plugin's MCP server
won't start without them. Then read `verify_plugin_state(name)`: `ready:true`
means every target agrees. The result carries the same phase fields as
`plugin_update` (`activation_committed` / `runtime_ready`) — on
`activation_committed:true, runtime_ready:false` the registry entry exists
but a target didn't come up; retry the reload/verify, not the add. Exception:
`ok:true` with a non-empty `pending_targets` (e.g. `["specialist:mtg"]`) is
**success, not a failure** — the plugin targets a specialist that is not
installed yet. `plugin_add` (this recipe) is needed only for an
OPERATOR-owned plugin: a specialist with bundled or repo-declared plugin
dependencies (its manifest's `dependencies`, `kind: "plugin/implementation"`)
installs them in the SAME flow as the specialist itself —
`specialist_install_inspect` + `specialist_install_commit`, see
`recipes/specialist/install.md` — never a separate `plugin_add`. Do NOT
retry, work around, or remove the plugin; if `pending_targets` names a
specialist slug, proceed to that specialist's install and it will pick the
plugin up automatically as one of its own declared dependencies (or, if the
specialist is never going to declare it, the plugin simply stays pending —
that is a valid end state, not an error). Report the outcome and
`emit_completion(...)`.

## Setup-tool hand-back — MANDATORY when the plugin ships one

Some plugins ship an MCP **setup tool** (naming convention `setup_*`, e.g.
`setup_elevenlabs_voicemail`) that (re)points an external service at Casa —
typically writing a freshly-minted per-trigger secret into the external
caller's config. Install/update and consent re-approval mint FRESH secrets,
so until the setup tool runs the external side still holds stale credentials
and the integration is dead — even though this install "succeeded". The
operator must never need to remember a follow-up incantation.

You cannot run it yourself: plugin tools surface only on the plugin's
target agents, never in this engagement. Hand it back instead:

1. **Detect.** The plugin-developer completion handoff is the authoritative
   source when this install follows one (it names the setup tool). Otherwise
   check the plugin's manifest `description`/README, or Grep the published
   artifact (`/config/plugins/store/<name>/<artifact-id>/`) server source
   for an MCP tool named `setup_*`. If you cannot establish that a setup
   tool exists, hand back nothing — NEVER guess a tool name.
2. **Hand back.** Your `emit_completion` MUST then carry ONE `next_steps`
   entry PER setup tool:
   `{"action": "run_plugin_setup_tool", "plugin": "<registry name>",
   "tool": "<setup-tool name>", "targets": [<plugin targets>],
   "consent_pending": <bool>}`,
   and the completion `text` must state that the integration is NOT live
   until the setup tool has run. Set `consent_pending: true` only when a
   plugin-declared trigger is still awaiting the operator's consent ack at
   completion time (normally false — the Approve tap resumes this
   engagement before you get here).

Setup tools in this contract are **argument-free and idempotent** (that is
the plugin-developer authoring doctrine); a tool that demands arguments is
not a valid hand-back target — report it in `text` instead of emitting the
next-step. Wiring is global to the plugin, so it runs ONCE, not per target.
The engager runs the tool immediately on receiving the completion — no
operator ask; the install/consent that started this engagement already
authorizes the wiring. Plugins without a setup tool: `next_steps` stays
empty as usual.

A plugin registry entry a specialist's install/upgrade OWNS (registry name
`<slug>.<name>`) can never be managed through this recipe or `remove.md` /
`update.md` / an assign-unassign step: `plugin_add` itself can't collide with
one (owned names are scoped), but `plugin_update` / `plugin_remove` /
`plugin_assign` / `plugin_unassign` all refuse an owned entry outright with
`kind: "owned_by_specialist"` (plus the owning slug). Use
`specialist_upgrade` / `specialist_uninstall` on the SLUG instead of trying to
touch the owned entry directly.
