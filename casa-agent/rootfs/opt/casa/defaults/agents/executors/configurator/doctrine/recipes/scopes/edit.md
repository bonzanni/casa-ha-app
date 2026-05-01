# Recipe: edit scope corpus

`policies/scopes.yaml` is the corpus of keyword phrases used by fastembed
to classify user messages into scopes. Affects memory routing for every
agent.

## Caution level: HIGH

A bad edit silently degrades memory routing. Always show the diff to the
user before committing.

## Ask the user

1. **Which scope?**
2. **What change?** Add keywords, remove, rename, add a new scope.
3. If ADDING a new scope: does the user understand a resident's
   `memory.scopes_owned` needs updating? What `kind` (topical or
   system)?

## Format of `policies/scopes.yaml` (schema v2)

Map-style: each scope is a top-level key under `scopes:`. Two kinds:

- **topical** — has a `description` (the embedding corpus, comma-
  separated keyword phrases, 30-60 phrases). Classified per turn.
- **system** — has NO `description`. Always-on for any agent that
  includes the scope in `scopes_readable` and clears the trust gate.
  Only `meta` is system today (engagement-summary aggregator).

```yaml
schema_version: 2
scopes:
  personal:
    minimum_trust: authenticated
    kind: topical
    description: |
      private life, friendships, weekend plans, hobbies,
      ...30-60 comma-separated phrases...
  meta:
    minimum_trust: authenticated
    kind: system
```

## Edit

For a topical scope: edit the `description` value. Keep it
comma-separated keyword phrases (NOT prose).

For a system scope: edits are limited to `minimum_trust`. There is no
embedding to tune.

## Verify BEFORE committing

Sanity-check via container:

```bash
python -c "
from scope_registry import load_scope_library
lib = load_scope_library('/addon_configs/casa-agent/policies/scopes.yaml')
print('kinds:', {n: lib.kind(n) for n in lib.names()})
"
```

Then run the eval suite to confirm topical-scope routing has not
regressed:

```bash
CASA_REAL_EMBED=1 pytest tests/test_scope_routing_eval.py
```

If a probe you expect to land in `<scope>` lands elsewhere, the corpus
is wrong. Iterate with the user before committing.

## Reload — MANDATORY before emit_completion

**Hard** - fastembed classifier cache init at boot. Canonical order:

```python
config_git_commit(message="update scopes: <scope> <add|remove> <keywords>")
casa_reload()
emit_completion(status="ok", text="...committed SHA <sha>, called casa_reload to rebuild the fastembed classifier cache.")
```
