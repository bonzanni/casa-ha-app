# Recipe: unwire a specialist from a resident's delegates

## Edit agents/<resident-role>/delegates.yaml

Remove the matching entry. Leave `delegates: []` if it was the last.

## Don't forget the reverse cases

- Unwiring because the specialist is being DELETED: part of recipes/specialist/delete.md.
- Unwiring because this resident shouldn't delegate (but another does): confirm which resident.

## Reload

**Hard.**

    config_git_commit(message="unwire <specialist> from <resident>'s delegates")
    emit_completion
    casa_reload()
