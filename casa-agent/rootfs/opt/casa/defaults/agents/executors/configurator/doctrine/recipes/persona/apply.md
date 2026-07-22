# Recipe: apply an installed persona to a resident or specialist

1. Confirm the target (`resident:assistant`/`resident:butler`/`resident:concierge`, or
   `specialist:<slug>` for an INSTALLED specialist — hand-authored specialists have no binding to
   apply to) and the persona id/version (must already be installed — see `recipes/persona/install.md`).
2. `persona_apply(target_role_id=..., persona_id=..., persona_version=...)`.
3. If `ok: false, kind: "incompatible"`: the persona failed the role's compatibility/ceiling check —
   report the detail verbatim, do not retry with a different persona without asking.
4. If `ok: false, kind: "not_installed"`: the specialist slug given is not an installed component —
   report this and stop; a hand-authored specialist has no binding to override.
5. If `ok: true` and `restart_required: true` (residents): tell the operator the swap is staged and
   takes effect on the resident's next restart (`casa_restart_supervised`) — a resident's binding
   change is NEVER hot-swapped (Plan 1 Task 8's `ReloadError("restart_required", ...)` guard).
6. If `ok: true` and `restart_required: false` (specialists): `casa_reload(scope="agents")` activates
   it immediately.
7. `config_git_commit`, `emit_completion`.

## Common mistakes

- Treating `ok: true` for a specialist target as immediately live without the follow-up
  `casa_reload(scope="agents")` — the binding is committed to disk but the live registry keeps
  running the old compiled bundle until reload runs.
- Forgetting that a resident swap is restart-to-swap, never hot-swapped — do not tell the operator
  the resident's voice changed until AFTER `casa_restart_supervised` actually runs.
