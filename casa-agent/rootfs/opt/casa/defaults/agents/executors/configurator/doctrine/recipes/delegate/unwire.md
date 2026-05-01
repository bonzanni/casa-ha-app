# Recipe: unwire an agent from a resident's delegates

The target may be a resident or a specialist — same edit either way.

## Edit agents/<resident-role>/delegates.yaml

Remove the matching entry. Leave `delegates: []` if it was the last.

## Don't forget the reverse cases

- Unwiring because the specialist is being DELETED: part of recipes/specialist/delete.md.
- Unwiring because this resident shouldn't delegate (but another does): confirm which resident.

## Reload — MANDATORY before emit_completion

**Hard.** Canonical order:

    config_git_commit(message="unwire <target> from <resident>'s delegates")
    casa_reload()
    emit_completion(status="ok", text="...committed SHA <sha>, called casa_reload to drop the delegate from the registry.")
