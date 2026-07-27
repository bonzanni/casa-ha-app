# Recipe: update an existing specialist — RETIRED

Hand-editing files under `agents/specialists/<slug>/` is no longer supported
(hooks deny it): those files are materialized from the installed component, and
any manual edit would be overwritten by the next reconcile.

- New component version: `recipes/specialist/upgrade.md`.
- Different persona: `recipes/persona/apply.md`.
- Config changes (model tier, memory budget, secrets): re-run
  `specialist_install_commit` with the new `config` for the installed component
  (see `recipes/specialist/install.md`, step 5).
- Bad upgrade: `recipes/specialist/rollback.md`.
