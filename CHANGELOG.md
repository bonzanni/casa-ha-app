# Changelog

This file tracks repo-level changes across the Casa project. Add-on
version history lives in [`casa-agent/CHANGELOG.md`](casa-agent/CHANGELOG.md).

## 2026-04-28 — Memory M5 deriver-trust resolution (documentation only)

- M5 (`remember_fact` MCP tool) closed without code. The Honcho deriver
  covers the recall need via the `summary.content` path on the regular
  `add_messages` flow Casa already runs; empirical N150 probes (sessions
  `m5-probe-2026-04-28-1777392473` and
  `m5-probe-pcfg-2026-04-28-1777396895`, workspace `casa`,
  `honcho-ai==2.1.1`) showed ~460-char summary content within ~90s of
  22 biographical messages. Three alternatives explicitly rejected:
  RMW `set_card`, tagged-message variant, override-only carve-out.
- New spec
  `docs/superpowers/specs/2026-04-28-memory-m5-deriver-trust-design.md`
  records the decision, the live evidence, and the explicit "not built"
  list (no `remember_fact` tool, no `set_card` calls, no tagged-message
  shim, no SQLite-side writer, no `peer_card.create=True` session-config
  knob until a future honcho-ai upgrade exposes it).
- Doc-cleanup edits:
  `docs/superpowers/specs/2026-04-26-memory-architecture.md` (§ 12,
  § 14.4, § 15.4); `docs/MEMORY-ROADMAP.md` (roadmap table, § M5 phase
  entry rewritten with archaeology blockquote, M3/M4b forward-refs,
  session-start prompt); `docs/ROADMAP.md` (line 325–326).
- No version bump. Active version stays `v0.17.2`.

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
