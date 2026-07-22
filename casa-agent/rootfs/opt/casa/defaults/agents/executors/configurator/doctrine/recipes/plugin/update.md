# Recipe: update a registered plugin to a new ref

Use `plugin_update(name, new_ref, expected_revision)` when a plugin repo has
a new release you want Casa to run. It re-publishes the artifact from
`new_ref` (same source repo/subdir), installs any new system requirements
before moving the registry pointer, then reloads + verifies. The **version
is derived from the fetched manifest** — you never pass it.

## What `new_ref` must be (v0.74.0)

- `new_ref` is the **producer's handed-off `ref` verbatim** — the `vX.Y.Z`
  release tag from the plugin-developer completion. **Never** infer a tag
  from the version, **never** reuse the registry entry's existing ref,
  **never** substitute `master`/`main` or a bare sha when a tag was handed
  off.
- **No ref handed off ⇒ producer error.** Stop and surface it — do not
  guess. The producer must re-run its release ritual and hand off
  `ref` + `revision` + `version`.
- **Always pass `expected_revision`** = the producer's handed-off `revision`
  (40-hex sha). A tag that moved between the build and this pin aborts hard
  (`revision_mismatch`) before anything is installed or activated. A
  `tag_version_mismatch` means the tag doesn't match the remote
  `plugin.json.version` — also a producer error; surface it.

## Do it

1. Confirm the plugin is registered: `plugin_list()` (or
   `verify_plugin_state`).
2. Call `plugin_update(name, new_ref=<handed-off tag>,
   expected_revision=<handed-off sha>)`.
3. Assert the returned `revision` and `version` equal the handoff
   (`revision` compares as lowercase 40-hex; the registry stores
   `git:<sha>`).
4. Read the **phase fields** — they say what actually happened and what (if
   anything) to retry:
   - `ok:true` (`activation_committed:true, runtime_ready:true`) — done.
     Every target runs the new artifact; report and `emit_completion(...)`.
   - `activation_committed:false` — nothing changed (resolve/guard/publish
     failed; `kind` says why: `ref_not_found`, `revision_mismatch`,
     `tag_version_mismatch`, `resolve_auth_failed`, `source_empty`,
     `resolve_unavailable` — the last carries `retry_after_s` when GitHub
     asked to wait). Fix the cause; retrying the same call is safe.
   - `activation_committed:true, runtime_ready:false` — **the pin already
     moved.** Do NOT repeat `plugin_update` as if nothing happened. The
     remedy is a reload/verify retry: `casa_reload(scope="agent",
     role=<affected role>)` then `verify_plugin_state(name)`. A persisting
     `reload_required` on a target means that agent is still bound to the
     previous artifact — surface it, do not mask it.
   - `ok:true` with a non-empty `pending_targets` — success: those
     specialist targets are not installed yet (the documented
     plugin-before-specialist order). Nothing to retry; the specialist
     install activates them.

## Why this is safe

Artifacts are content-addressed by provenance, so a new commit is always a
NEW `artifact_id` even if the manifest `version` field didn't change — the
stale-version-cache bug is structurally impossible. Old artifacts are
retained (GC handles them later). Any mid-flight executor engagement keeps
running its RECORDED artifact; verify lists those under
`sessions_on_previous_artifact` as informational (not a failure) — they pick
up the new code on their next launch.

**No separate casa_reload is needed on the happy path** — `plugin_update`
reloads + verifies internally. Report the outcome and `emit_completion(...)`.
