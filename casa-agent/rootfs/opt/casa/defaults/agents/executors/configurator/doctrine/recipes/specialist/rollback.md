# Recipe: roll back an installed specialist

Use after a bad upgrade. No re-fetch, no consent prompt — the prior version's bytes are already
pinned in CAS from when it was active.

1. Confirm WHICH specialist and that the operator wants the immediately-prior version specifically
   (rollback is one step back, not "pick a version").
2. `specialist_rollback(slug=...)`. If it returns `kind: "no_prior_tuple"`, there is nothing to roll
   back to (either never upgraded, or already rolled back once).
3. `casa_reload(scope="agents")`, `config_git_commit`, `emit_completion`.

## Common mistakes

- Calling `specialist_rollback` more than once expecting it to keep stepping further back — it
  restores the SINGLE retained `active.prior.yaml`, not a version history; a second call after a
  successful rollback has nothing further to restore and returns `kind: "no_prior_tuple"`.
- Forgetting `casa_reload(scope="agents")` — same as every other install/upgrade path, the committed
  tuple is not live until reload runs.
