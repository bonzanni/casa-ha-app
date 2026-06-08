# Recipe: reconcile overwritten config after a default sync

**When:** the operator says "reconcile config" / "carry over my changes" / asks about a
heads-up that an update overwrote customizations, OR you are asked to review config drift
after a deploy.

## Background

On every boot the default-sync reconciler makes image-default-owned files under
`/config/agents/**` and `/config/policies/**` track the shipped defaults. On a true
conflict (both the image and the live file changed the same file since the last sync), or
when a kept-live file is invalid against a newly tightened schema, **the image wins** so
casa always boots — but the operator's previous version is preserved first:

- Normal path: committed to the `/config` git repo **before** the overwrite. The report
  records the `pre_sync_sha` of that commit.
- Degraded path (git unavailable): saved next to the file as `<file>.casabak`.

## Steps

1. **Read the report:** `/data/config-sync-report.json`. The fields that mean "a
   customization was overwritten" are `conflicts[]`, `schema_forced[]`, and `casabak[]`.
   (`updated[]`/`deleted[]` are routine default-tracking — nothing to reconcile.)

2. **For each overwritten `path` with a `pre_sync_sha`:** show the operator what was lost:

   ```
   git -C /config diff <pre_sync_sha> -- <path>
   ```

   This is "their previous content" → "the new default". For a `casabak` entry instead,
   diff the sidecar:

   ```
   diff /config/<path>.casabak /config/<path>
   ```

3. **Ask the operator** whether to keep the new default as-is, or carry their change
   forward on top of it. Do not assume — present the diff and let them decide.

4. **If carrying forward:** re-apply the operator's intent on top of the new default using
   the normal per-artifact edit recipes (e.g. `voice/edit`, `prompt/edit`,
   `executor/edit-definition`). Re-applying *intent* on the new base is safer than blindly
   restoring the old file, which may reintroduce the very field a schema-tightening
   removed.

5. **Validate + reload:** after edits, the standard config-commit gate runs schema
   validation; then `casa_reload(scope=...)` (or `scope=config_sync` to re-run the full
   sync + cascade). Never restore content that fails validation — that is exactly what the
   backstop overwrote to keep casa booting.

6. **Clean up:** once handled, delete any `<file>.casabak` sidecars you reconciled. The
   git history remains as the durable archive.

## Notes

- Carry-over is **always operator-initiated and post-boot.** Never block boot on it.
- If the operator wants nothing carried over, just confirm — the new defaults already
  apply and `.casabak` files can be removed.
