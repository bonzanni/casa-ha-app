# Completion order

## The canonical order — MANDATORY

Every completion path is the same three tool calls, in this order:

1. **Commit.** Call `config_git_commit(message="<imperative sentence>")`. Skip only if you made zero file edits.
2. **Reload (if needed).** Call `casa_reload()` (hard) or `casa_reload_triggers(role)` (soft). Skip ONLY for none-reload changes (prompts, response_shape, doctrine, scopes_readable). Consult `reload.md` to pick the right scope.
3. **Emit completion.** Call `emit_completion(...)` with a structured summary.

**Never invert step 1 and step 2/3. Never call `emit_completion` BEFORE the reload step when a reload is needed.**

`emit_completion` is the terminal action. After it lands, the engagement closes and Ellen reads the summary. If you fire `emit_completion` without first running the prescribed reload, the artifact is **COMMITTED BUT INERT** — the YAML on disk is correct, but the running Casa keeps the prior runtime state. The trigger does not fire; the new agent does not load; the new tool does not surface. From Ellen's point of view (and the operator's), you "succeeded" — but the change has no effect. This is the highest-leverage doctrine violation in this executor.

## Reload-before-completion is safe

`casa_reload_triggers(role)` is in-process and returns immediately — no race.

`casa_reload()` POSTs to Supervisor's `addons/self/restart` and returns ~immediately with `supervisor_status: 200`. Supervisor schedules the actual restart asynchronously. By the time the addon container is killed, your subsequent `emit_completion` call has already written the summary onto the bus, which persists across addon restart. Ellen reads it on her next turn after Casa comes back up.

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

When you have already invoked the reload before this call (i.e. the
canonical order), say so factually in the text — e.g.
`"Committed SHA abc123, soft-reloaded triggers for assistant."` — so
Ellen's narration to the operator is accurate.

`next_steps` is almost always empty for the configurator.

## Hard-reload note

`casa_reload()` returns `supervisor_status: 200` quickly, but the actual
restart kills your subprocess seconds later. Your `emit_completion` call
must still run promptly after the reload returns; do not interpose
extra Read or Bash tool_uses between `casa_reload` and `emit_completion`
or you may be killed before the bus write lands. Soft reload
(`casa_reload_triggers`) has no such constraint.

## Cancellation

If the user says /cancel or you decide to abort, emit completion with `status="cancelled"`. Do NOT call `config_git_commit` if you made zero edits; DO call it if you made edits before the abort. A cancelled engagement does NOT need a reload — the artifact is either uncommitted (no edits) or operator-pending (the operator decides whether to keep the partial commit).
