# Completion order

## The canonical order

Every completion path is the same:

1. **Commit.** Call config_git_commit(message="<imperative sentence>"). Skip only if you made zero file edits.
2. **Emit completion.** Call emit_completion(...) with a structured summary.
3. **Reload (if needed).** Call casa_reload() for hard or casa_reload_triggers(role) for soft. Skip entirely for none-reload changes.

**Never invert the order. Never call reload before emit_completion. Never commit after completion.**

Why: reload tools terminate your session (hard) or at minimum disrupt the driver. The bus (carrying emit_completion to Ellen's queue) persists across addon restart; the reload tool call itself does not.

## emit_completion payload

    emit_completion(
        status="ok" | "partial" | "failed" | "cancelled",
        text="Free-form narrative - one paragraph.",
        artifacts=[
            {
                "kind": "commit",
                "repo": "/addon_configs/casa-agent",
                "sha": "<from config_git_commit>",
                "files_changed": ["agents/specialists/fitness/character.yaml", ...],
            },
        ],
        next_steps=[],
    )

`text` is what Ellen READS. Make it:

- Factually complete.
- Not rhetorical. No "I have successfully completed your request" fluff.
- Terse. One paragraph, 3-6 sentences.

`next_steps` is almost always empty for the configurator.

## Hard-reload verification

When you call casa_reload(), your session is terminated within seconds. You cannot verify the reload worked from your own turn - Ellen does that on her resumed turn by reading the bus registry. So your `text` summary should describe what you did (e.g., "committed SHA abc123, called casa_reload"), not claim success of the reload itself.

## Cancellation

If the user says /cancel or you decide to abort, emit completion with status="cancelled". Do NOT call config_git_commit if you made zero edits; DO call it if you made edits before the abort.
