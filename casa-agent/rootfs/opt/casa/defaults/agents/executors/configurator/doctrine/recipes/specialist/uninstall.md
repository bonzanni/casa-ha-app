# Recipe: uninstall an installed specialist

1. Find all delegate references (Grep tool: pattern `<slug>` across
   `/config/agents/*/delegates.yaml`) and unwire them first
   (`recipes/delegate/unwire.md`) — an uninstall does NOT auto-unwire delegates.
2. `specialist_uninstall(slug=...)`.
3. `config_git_commit(message="uninstall specialist <slug>")`.
4. `casa_reload(scope="agents")` — evicts the removed agent from the live
   registry (canonical commit → reload → emit order, see `completion.md`).
5. `emit_completion(...)`.

CAS blobs are NOT deleted by uninstall (retained for a possible future re-install at the same
digest, and GC sweep execution is out of this plan's scope — see Task N1d's CAS pin/reference
model).

## Common mistakes

- Uninstalling before unwiring delegates — a stale `delegates.yaml` entry left pointing at a
  removed specialist will fail to resolve on the next delegation attempt.
- Skipping `casa_reload(scope="agents")` — the on-disk instance is gone but the live registry still
  holds the (now orphaned) loaded agent until reload runs.
