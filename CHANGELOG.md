# Changelog

This file tracks repo-level changes across the Casa project. Add-on
version history lives in [`casa-agent/CHANGELOG.md`](casa-agent/CHANGELOG.md).

## 2026-04-17 — Phase 2.1 follow-up

- Extracted `build_heartbeat_message`, `build_invoke_message`, and
  `init_heartbeat_defaults` helpers in `casa_core.py` for regression coverage.
- Fixed heartbeat `channel=""` silent failure; scheduled ticks now use
  `channel: scheduler` and resolve to a valid session key.
- Fixed dashboard `UnboundLocalError` startup race by hoisting heartbeat
  defaults above the HTTP server start.
- Fixed `/invoke/{agent}` session collision: each call gets a distinct
  `chat_id` (caller-supplied or a fresh UUID).
- Hardened `setup-configs.sh` migration: force-set canonical role on
  rename; strip CR so Windows-edited YAMLs migrate cleanly.
- Local-test harness now copies `assistant.yaml`/`butler.yaml` defaults
  and exports heartbeat options from `options.json`.
- Pinned Python runtime dependencies in `casa-agent/requirements.txt`.

## 2026-04-16 — Phase 2.1

- Role-based refactor of the Casa add-on (agents keyed on structural role
  rather than display name). See `casa-agent/CHANGELOG.md` 0.1.22.
