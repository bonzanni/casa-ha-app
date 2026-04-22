# Recipe: edit a prompt file

Any prompts/*.md file across any agent.

## No reload needed

Prompt files are lazy-read per turn. Changes take effect on the next turn.

## Still commit

Audit trail and rollback:

    config_git_commit(message="update <role>/<file>.md prompt")
    emit_completion(status="ok", text="Updated <role>'s <file>.md. No reload required - next turn. Commit <sha>.")
    # no reload call

## Only consideration

If the file is referenced by a prompt_file:/card_file: pointer in a YAML, verify it still exists at boot time (editing in place is fine).
