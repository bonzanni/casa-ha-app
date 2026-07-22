# Recipe: install a specialist from a repository

A specialist component is a distributable package living in its own repository — NOT the legacy
hand-authored path (recipes/specialist/create.md, still valid for a throwaway/local specialist that
never needs upgrade/rollback/export). Installed specialists are managed: their identity, persona
binding, and runtime files are all derived from the component, never hand-edited.

## Ask the user

1. **Repository locator** (`owner/repo` + a ref — branch/tag/sha).
2. Nothing else up front — `specialist_install_inspect` reports the component's own declared
   mission, default persona, and required config/secret names; ask the operator to supply THOSE by
   name once inspection returns.

## Steps

1. `specialist_install_inspect(repo=..., ref=...)`. On any `ok: false`, report the `kind`/`detail`
   verbatim and stop — do NOT retry with fabricated inputs.
2. Summarize the inspection result in the topic (mission, default persona, dependencies, required
   config/secret names) and post it via `ask_user` so the operator sees it BEFORE the DM consent
   prompt fires — the DM keyboard (posted server-side by `prompt_specialist_install_consent`, not by
   this recipe) is the actual approval gate; this step is purely informational context in-topic.
3. Wait for the operator's DM tap (Approve/Deny) to resolve. There is no polling tool — the
   `specialist_install_commit` call in the next step will itself refuse with `kind:
   "consent_missing"` if the tap has not landed yet; on that specific error, tell the operator you
   are waiting for their DM response and stop (do not loop-retry).
4. Once approved: `specialist_install_commit(component_id=..., version=..., root_digest=...,
   slug=..., staged_dir=..., config={...}, secret_names_provided=[...])` using the EXACT values
   `specialist_install_inspect` returned.
5. If `state == "pending-configuration"`: report which config/secret names are still missing; the
   operator supplies them via a follow-up `specialist_install_commit` call with the SAME
   `staged_dir` (re-inspect if `staged_dir` has been cleaned up — staging is not guaranteed durable
   across a restart).
6. If `state == "active"`: `casa_reload(scope="agents")` (mandatory — see `completion.md`), then
   wire delegation the same way `recipes/specialist/create.md` describes
   (`recipes/delegate/wire.md`).
7. `config_git_commit(message="install specialist <slug> from <repo>@<ref>")`.
8. `emit_completion(status="ok", text="Installed specialist <slug> from <repo>@<ref>; reloaded and
   wired for delegation.")`.

## Common mistakes

- Calling `specialist_install_commit` before the operator has actually tapped Approve — it will
  correctly refuse (`kind: "consent_missing"`); this is not a bug to work around, it IS the consent
  gate.
- Forgetting `casa_reload(scope="agents")` — an `active` install is on disk but not in the live
  registry until reload runs (same as `recipes/specialist/create.md`).
- Re-approving a DIFFERENT re-fetch under the same slug: a second `specialist_install_inspect` call
  yields a NEW `root_digest` if the repo moved (or any bundled persona/corpus/plugin dependency
  changed), which requires a NEW consent DM — the old approval never carries over (see
  `install_consent_identity`'s four-field binding, now keyed on `root_digest`).
