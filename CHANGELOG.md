# Changelog

This file tracks repo-level changes across the Casa project. Add-on
version history lives in [`casa-agent/CHANGELOG.md`](casa-agent/CHANGELOG.md).

## 2026-04-17 — Phase 2.1 E2E suite

- Fixed unintended bus serialisation (`casa-agent/rootfs/opt/casa/bus.py`):
  concurrent messages to the same agent now dispatch in parallel via
  `asyncio.create_task`. Discovered while writing the concurrency E2E
  test; the plan assumed this was already the case.
- Dockerized end-to-end test harness in `test-local/e2e/` covering all
  three v0.2.0 runtime-bug regressions, YAML migration, concurrency.
  Run with `make -C test-local test` (or `bash test-local/e2e/*.sh`
  directly if `make` isn't installed).
- Offline mock `claude_agent_sdk` at `test-local/mock-claude-sdk/`
  makes runtime tests hermetic — no OAuth token required.
- GitHub Actions QA workflow (`.github/workflows/qa.yml`) runs unit +
  fast E2E on every push; slow (heartbeat) tier on a nightly schedule.
- Test harness fixes: `test-local/init-overrides/01-setup-configs.sh`
  now mirrors the production `migrate_rename` function so containers
  actually run the migration in test mode.

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
