# Recipe: edit scope corpus

policies/scopes.yaml is the corpus of keyword phrases used by fastembed to classify user messages into scopes. Affects memory routing for every agent.

## Caution level: HIGH

A bad edit silently degrades memory routing. Always show the diff to the user before committing.

## Ask the user

1. **Which scope?**
2. **What change?** Add keywords, remove, rename, add a new scope.
3. If ADDING a new scope: does the user understand a resident's memory.scopes_owned needs updating?

## Format of policies/scopes.yaml

Comma-separated keyword phrase clusters per scope. 30-60 phrases per scope. Tenant-agnostic defaults; the user's override adds tenant-specific hints.

    schema_version: 1
    scopes:
      - name: personal
        description: >-
          phrase1, phrase2, phrase3, ...
      - name: business
        description: >-
          phrase1, phrase2, phrase3, ...

## Edit

Edit the target scope's description value. Keep it comma-separated keyword phrases (not prose).

## Verify BEFORE committing

Sanity-check via container:

    python -c "
    from scope_registry import ScopeRegistry
    r = ScopeRegistry('/addon_configs/casa-agent/policies/scopes.yaml')
    r.load()
    print(r.classify('your test query'))
    "

If a probe you expect to land in <scope> lands elsewhere, the corpus is wrong. Iterate with the user before committing.

## Reload

**Hard** - fastembed classifier cache init at boot.

    config_git_commit(message="update scopes: <scope> <add|remove> <keywords>")
    emit_completion
    casa_reload()
