# Recipe: upgrade an installed specialist

1. `specialist_install_inspect(repo=..., ref=<new ref>, mode="upgrade", target_slug=<slug>)` against
   the SAME repo, a newer ref. **Always pass `mode="upgrade"` + `target_slug`** — plain
   `specialist_install_inspect(repo=..., ref=...)` with no `mode` will refuse with `kind:
   "slug_collision"` because the slug is already installed (that refusal is correct for a FRESH
   install; upgrade mode is the only sanctioned way past it for the SAME slug).
2. Same consent flow as `recipes/specialist/install.md` steps 2-3 — an upgrade re-consents exactly
   like a fresh install (the identity binds `root_digest`, which changes with every version).
3. `specialist_upgrade(slug=..., component_id=..., version=..., root_digest=..., staged_dir=...,
   config={...}, secret_names_provided=[...])` using the EXACT `root_digest`
   `specialist_install_inspect` returned.
4. If `state == "pending-configuration"`: the OLD version is still live and answering delegations —
   tell the operator exactly which new config/secret names the new version needs; nothing broke.
5. If `state == "error"`: report the validation failure; the OLD version is still live, unchanged.
6. If `state == "active"`: `casa_reload(scope="agents")`, `config_git_commit`, `emit_completion`.

## Common mistakes

- Calling `specialist_install_inspect` without `mode="upgrade"` for an already-installed slug — it
  will correctly refuse with `kind: "slug_collision"`; this is not a bug to work around.
- Forgetting `casa_reload(scope="agents")` after `state == "active"` — the new tuple is committed on
  disk but the live registry keeps running the old compiled bundle until reload runs.
- Treating `state == "pending-configuration"` or `state == "error"` as a failed upgrade that needs
  retrying blind — in BOTH cases the previously-active version is still running unchanged; report
  the specific gap (missing config, or the validation error) and let the operator decide next steps.
