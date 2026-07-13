# Recipe: update a registered plugin to a new ref

Use `plugin_update(name, new_ref)` when a plugin repo has a new release you
want Casa to run. It re-publishes the artifact from `new_ref` (same source
repo/subdir), installs any new system requirements before moving the registry
pointer, then reloads + verifies. The **version is derived from the fetched
manifest** — you never pass it.

## Do it

1. Confirm the plugin is registered: `plugin_list()` (or `verify_plugin_state`).
2. Call `plugin_update(name, new_ref)`.
3. Read the result's `verify` summary — every target must show the NEW
   `artifact_id` (`ready:true`). A `reload_required` reason means the sequence
   didn't complete; surface it, do not mask it.

## Why this is safe

Artifacts are content-addressed by provenance, so a new commit is always a NEW
`artifact_id` even if the manifest `version` field didn't change — the stale-
version-cache bug is structurally impossible. Old artifacts are retained (GC
handles them later). Any mid-flight executor engagement keeps running its
RECORDED artifact; verify lists those under `sessions_on_previous_artifact` as
informational (not a failure) — they pick up the new code on their next launch.

**No separate casa_reload is needed** — `plugin_update` reloads + verifies
internally. Report the outcome and `emit_completion(...)`.
