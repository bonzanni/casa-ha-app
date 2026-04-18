# Changelog

## 0.5.2 — 2026-04-18 — Phase 5.2 item H: structured logging with correlation IDs

### Added
- `log_cid.py` — pure logging module. `cid_var` (contextvars), `new_cid()`
  (8-char hex), `CidFilter` (standalone utility: injects `record.cid`
  from the current context var — not auto-attached by `install_logging`,
  kept for callers that construct records manually), `JsonFormatter`
  (one-line JSON with `ts/level/logger/cid/msg[/exc]` fields),
  `_human_formatter()` (ISO UTC human format `... cid=X: msg`), and
  `install_logging()` — idempotent root-logger setup that (a) installs
  a `logging.setLogRecordFactory` wrapper which tags every record with
  `record.cid = cid_var.get()` at creation time (works for all
  loggers, including caplog, because the factory runs inside
  `Logger.makeRecord`), (b) attaches a single Casa-owned StreamHandler
  with `RedactingFilter` on the handler (not root — root-level filters
  do not fire for records from descendants). Spec 5.2 §7.
- Every ingress-built `BusMessage` carries a fresh `context["cid"]`:
  Telegram `_handle`, voice SSE + WS, webhook `/webhook/{name}`,
  `/invoke/{agent}` (`build_invoke_message`), and scheduler heartbeat
  (`build_heartbeat_message`). Caller-supplied `context.cid` in
  payloads wins so external systems can thread their own trace ids.
- Env var `LOG_FORMAT` — `json` switches root formatter to one-line
  JSON; anything else (incl. unset) uses the human format. Read at
  `install_logging()` call time.

### Changed
- `MessageBus._dispatch` sets `log_cid.cid_var` from
  `msg.context["cid"]` with a scoped token before invoking the
  handler and resets it in `finally`. Cross-task contamination is
  impossible: each dispatch runs in its own `asyncio.create_task`
  whose context is a snapshot. Messages without a cid in their
  context read as `cid=-` (backward-compat).
- `casa_core.main` logging setup — a single `install_logging()` call
  replaces the prior `logging.basicConfig(...)` +
  `addFilter(RedactingFilter())` +
  `getLogger("httpx").setLevel(WARNING)` sequence. Behaviour parity
  for the single-handler case Casa ships today: same stdout stream,
  same level, same redaction, same httpx quieting. Log format gains a
  `cid=XX` field per record. Note: `RedactingFilter` now lives on
  Casa's StreamHandler rather than the root logger (which was a
  pre-existing no-op for records from descendant loggers); future
  handlers that want redaction must attach it themselves.
- Timestamps are now ISO-UTC with `Z` suffix
  (`2026-04-18T14:32:01Z`), not the previous
  `2026-04-18 14:32:01,123`. Downstream log tooling that parses the
  old format may need an update.

### Not changed
- `Agent._process`, `retry.py`, and the memory path are untouched —
  item H is strictly a logging-layer change.
- `RedactingFilter` logic unchanged; it is re-attached to Casa's
  StreamHandler via `install_logging` alongside the new factory.
- No new dependency: `json`, `uuid`, and `contextvars` are stdlib.

## 0.5.1 — 2026-04-18 — Phase 5.2 item D: SDK retry + backoff

### Added
- `retry.py` — pure policy module. `RETRY_KINDS` (TIMEOUT, RATE_LIMIT,
  SDK_ERROR), `compute_backoff_ms()` jittered exponential backoff,
  `parse_retry_after_ms()` for server-supplied Retry-After hints,
  `retry_sdk_call()` async coroutine runner. Spec 5.2 §3.
- Env vars `SDK_RETRY_MAX_ATTEMPTS` (default 3), `SDK_RETRY_INITIAL_MS`
  (500), `SDK_RETRY_CAP_MS` (8000). Read at import time — adjust via
  add-on options + restart. Malformed or below-minimum values are
  logged and clamped, never crash module import.
- Server-supplied `Retry-After` hints are clamped at `10 * CAP_MS`
  (default 80 s) to prevent a misbehaving upstream from parking the
  worker indefinitely.

### Changed
- `Agent._process` — the `ClaudeSDKClient` turn is now wrapped in
  `retry_sdk_call`. Each attempt builds a fresh client and resets
  the streaming accumulator, so `on_token` replays cumulative text
  from scratch on retry. Cancellation (e.g. voice barge-in) bypasses
  the retry loop. Non-retryable exceptions (MEMORY_ERROR,
  CHANNEL_ERROR, UNKNOWN) surface unchanged. Spec 5.2 §3.2–§3.3.
- One `logger.warning` per retry attempt emitted via the new
  `Agent._log_retry` hook; log line carries role, attempt number,
  kind, delay_ms, exc repr.
- Internal refactor: `ErrorKind`, `_classify_error`, and
  `_USER_MESSAGES` moved from `agent.py` to a new `error_kinds.py`
  module to break an `agent ↔ retry` import cycle. `agent.py`
  re-exports them so `from agent import ErrorKind` continues to
  work unchanged for all existing consumers.

### Not changed
- Memory path is still silent-degrade (spec 2.2a §11 retained — no
  retry wrapper there per spec 5.2 §2).
- Channel modules untouched; retry is strictly at the SDK layer.
- `MAX_CONCURRENT_AGENTS` / `MAX_CONCURRENT_VOICE` seams untouched.

## 0.5.0 — 2026-04-18 — Phase 5.1: Concurrency correctness + disclosure v2

### Fixed
- `SessionRegistry` — mutate+save serialised via a single `asyncio.Lock`.
  Closes the lost-register / torn-touch race reachable since v0.2.1's
  concurrent bus dispatch. Public `save()` acquires; new internal
  `_save_locked()` assumes the lock is held. Spec 5.1 §3.
- `CachedMemoryProvider` — per-key `asyncio.Lock` with double-checked
  cache in the miss path. Concurrent cold reads on the same key now
  collapse to a single backend call; cache hits remain lock-free.
  Spec 5.1 §4.

### Changed
- `butler.yaml` default personality — layer-1 `Disclosure:` clause
  tightened with concrete per-category examples, stronger deflection
  wording aligned to the `<channel_context>` trust prefix, and an
  explicit positive list of topics safe on any channel. Spec 5.1 §5.

### Migration
- `migrate_disclosure_clause` one-shot in `setup-configs.sh` replaces
  the v1 disclosure block in existing `butler.yaml` files on upgrade.
  Gated by the trailing marker comment `# casa: disclosure v2`;
  idempotent. Mirrored into `test-local/init-overrides/01-setup-configs.sh`.
- No code-level migration for 5.1 Items A and B — the asyncio locks
  are in-memory only and take effect on next process start.

### Deferred
- `MAX_CONCURRENT_AGENTS` / `MAX_CONCURRENT_VOICE` caps — seams
  preserved (`VoiceSession.gate: Semaphore(10)`, architecture §3).
  Spec 5.1 §6.
- Layer-2 post-response disclosure backstop — beyond 5.x per spec 5.1
  §9 C7.

## 0.4.0 — 2026-04-17 — Phase 2.2b: SQLite memory drop-in

### Added
- `SqliteMemoryProvider` — durable local-storage backend for the
  3-method `MemoryProvider` ABC. Single `sqlite3` connection, WAL
  journal mode, schema versioned at `1`. Stores a thin log
  (`messages`, `sessions`, `peer_cards`); no summariser, no dialectic
  (spec §3 / S1).
- `_SqliteCtx` duck-typed wrapper so the existing `_render` produces
  `## What I know about you` + `## Recent exchanges` for SQLite without
  a second rendering code path.
- `MEMORY_BACKEND` env var — `honcho` / `sqlite` / `noop`. Resolution:
  explicit value wins; else `HONCHO_API_KEY` → honcho; else sqlite.
  Invalid values fail fast at startup. `MEMORY_BACKEND=honcho` without
  an API key also fails fast.
- `MEMORY_DB_PATH` env var — SQLite file location, default
  `/data/memory.sqlite`. Parent directory is created if missing.
- Dashboard "Memory" row now renders SQLite / Honcho / none.
- `casa_core.resolve_memory_backend_choice()` + `_wrap_memory_for_strategy()` — pure helpers lifted out of `main()` and unit-tested.

### Changed (behaviour change — documented fallout)
- Fresh installs without `HONCHO_API_KEY` now persist memory to
  `/data/memory.sqlite` by default. Previously: no memory at all. Opt
  out with `MEMORY_BACKEND=noop`.
- `CachedMemoryProvider` wrap is skipped when the backend is SQLite
  (native reads are ~1 ms; caching adds staleness and a background
  task for no measurable benefit). Butler YAMLs keep
  `read_strategy: cached` unchanged — the selector silently degrades
  to bare with a one-time INFO log at startup (spec §2 / S5).

### Migration
- None. No schema changes; no YAML changes. SQLite initialises itself
  on first open via `CREATE TABLE IF NOT EXISTS`. Switching backends
  = fresh start in the new backend (spec §7 / S7).

### Deferred
- LLM summariser (2.2c seam reserved: `_SqliteCtx.summary=None`).
- `remember_fact` tool writing to `peer_cards` (4.x).
- Export/import CLI between backends.
- Retention / pruning policy.

### Tests
- New: `tests/test_memory_sqlite.py` (schema, ensure_session, add_turn
  transactional, get_context rendering, peer_card scoping, topology
  visibility), `tests/test_memory_backend_select.py`
  (resolve + wrap policy), `tests/test_agent_process_sqlite.py`
  (`Agent._process` loop integration), `test-local/e2e/test_sqlite_memory.sh`
  (persistence across restart).
- Existing Honcho unit + integration tests still green (regression
  coverage for 2.2a).

## 0.3.0 — 2026-04-17 — Phase 2.3: voice pipeline

### Added
- `VoiceChannel` — dual ingress: generic SSE at `POST /api/converse`
  and HA-optimised WebSocket at `/api/converse/ws`. Both default on.
  Toggle with `VOICE_SSE_ENABLED` / `VOICE_WS_ENABLED`; paths override
  with `VOICE_SSE_PATH` / `VOICE_WS_PATH`. Idle eviction via
  `VOICE_IDLE_TIMEOUT_SECONDS` (defaults to `butler.session.idle_timeout`).
- `ProsodicSplitter` — delta-fed, tag-opaque sentence splitter that
  treats `[…] (…) {…} <…>` as atomic. Flushes on `.`, `!`, `?`, `…`,
  paragraph break. Safety-caps at 1.5 s / 200 chars with rightmost-
  clause-mark fallback (`,`, `;`, em-dash).
- `TagDialectAdapter` — canonical `[tag]` rewriter for three dialects:
  `square_brackets` (identity), `parens` (global `[tag]→(tag)`),
  `none` (strips leading tag atoms). Agents stay in canonical form;
  rewriting happens at the transport edge.
- `VoiceSessionPool` — process-local pool keyed on `scope_id`.
  Background sweeper evicts idle sessions every 30 s at
  `butler.session.idle_timeout`. `MAX_CONCURRENT_VOICE` gate seam
  reserved (defaults to 10 slots; 5.x hardening flips to 1).
- `stt_start` WebSocket prewarm hook — calls `memory.ensure_session`
  + `memory.get_context` on `CachedMemoryProvider` so the first
  utterance lands on a warm cache. Dedup'd against repeated
  `stt_start` frames for the same scope.
- Persona-voice error lines per `ErrorKind` in `butler.yaml`
  (`voice_errors:` block with `timeout`, `rate_limit`, `sdk_error`,
  `memory_error`, `channel_error`, `unknown`). Rendered through
  `TagDialectAdapter`. Empty string = silent degrade.
- Channel-supplied error hook: `channel.emit_error_line(kind, context,
  cfg)` duck-typed method. `Agent.handle_message`'s error branch
  prefers it over plain-text delivery when present. Non-voice
  channels (Telegram) unchanged.
- `TTSConfig` on `AgentConfig` (`tts.tag_dialect`, default
  `square_brackets`).
- Dashboard row for Voice channel status (transports + on/off).

### Changed
- `MessageBus.request()` now propagates caller cancellation to the
  dispatch task (previously: only the caller's future was cancelled,
  and downstream handlers kept running). Required for voice cancel /
  barge-in semantics (spec §10.2). Backward-compatible — all existing
  bus tests still green.
- `MessageBus._dispatch` resolves the handler per-message from
  `self.handlers[name]` rather than capturing it at `run_agent_loop`
  startup. Enables dynamic handler reconfiguration at the cost of a
  dict lookup per dispatch; same-loop asyncio keeps the lookup safe.

### Migration
- `setup-configs.sh` one-shot: injects `tts:` and `voice_errors:`
  blocks into existing `butler.yaml` if absent. Idempotent. Mirrored
  in `test-local/init-overrides/`.

### Deferred to 5.x hardening
- `MAX_CONCURRENT_VOICE=1` enforcement (seam reserved via
  `VoiceSession.gate`).
- Voice-ID promotion (`voice_speaker → nicola` peer when HA voice-ID
  matures).
- Personality hot-reload.
- Concurrent-cold-key dedup in `CachedMemoryProvider`.

### Tests
- 62 new unit/integration tests (config, migration, splitter, adapter,
  pool, SSE, WS) + 2 new E2E scenarios (SSE smoke + WS smoke under
  Docker). Full voice+agent suite: 192 passed at merge.

## 0.2.2 — 2026-04-17 — Phase 2.2a: Honcho v3 memory redesign

### Changed (breaking for pre-release users)
- `MemoryProvider` is now a 3-method ABC: `ensure_session`, `get_context`,
  `add_turn`. The pre-v3 `store_message` / `create_session` /
  `close_session` surface is removed.
- `HonchoMemoryProvider` rewritten for the honcho-ai 2.1.x peer/session
  model. The pre-v3 apps/users API is gone; `.initialize()` is no
  longer needed (v3 is lazy).
- Agent YAML `memory` block: `peer_name` and `exclude_tags` are
  removed; `read_strategy` (`per_turn` | `cached` | `card_only`) is
  added. Existing user YAMLs are migrated on first boot.
- `SessionRegistry.register()` no longer takes `memory_session_id`
  (the Honcho session is derived from `{channel_key}:{role}`).
  Existing `sessions.json` entries are migrated on first write.

### Added
- `CachedMemoryProvider` — warm cache + background refresh wrapper for
  the voice path; default `read_strategy` for `butler`.
- `<channel_context>` block in every system prompt, so agents can
  condition disclosure on the ingress channel's trust level.
- Personality baselines for `assistant` and `butler` include a
  disclosure clause referencing `<channel_context>`.

### Internal
- `voice_speaker` peer for unauthenticated voice ingress; `nicola`
  peer for authenticated channels (Telegram, webhook). Future
  voice-ID can upgrade a recognised speaker's attribution without
  touching agent code.
- Storage is unconditional: write-side filtering is gone (spec §4.3).
  Visibility is enforced by session/peer topology; disclosure is
  enforced by the agent on the output side.

## 0.2.1

- Fix bus serialisation: `MessageBus.run_agent_loop` no longer awaits
  each handler inline. Each message is now dispatched via
  `asyncio.create_task`, so concurrent `/invoke` calls to a single agent
  run in parallel instead of queuing behind one another. Handler
  exceptions are logged and REQUEST callers receive an error response
  instead of hanging until the 300 s timeout.
- Test-only: added offline mock `claude_agent_sdk` package and
  Dockerized E2E suite under `test-local/e2e/`. The mock replaces the
  real SDK inside the test image so runtime tests can run without an
  OAuth token. E2E suite covers smoke, YAML migration scenarios,
  `/invoke/{agent}` session isolation, heartbeat delivery, and
  concurrent dispatch.

## 0.2.0

- Fix heartbeat silent failure: scheduled ticks now use `channel: scheduler`
  and resolve to a valid session key (`build_session_key` rejects empty
  channels, which previously swallowed every tick).
- Fix dashboard startup race: a request landing on `/` between HTTP server
  start and scheduler init no longer raises `UnboundLocalError` on
  `heartbeat_enabled` / `heartbeat_interval`.
- Fix `/invoke/{agent}` session collision: each invocation gets a distinct
  `chat_id` (caller-supplied via `context.chat_id` or a fresh UUID),
  replacing the shared `webhook:default` session key.
- Harden agent-YAML migration: the migration script now force-sets the
  canonical role on rename (no longer assumes the legacy role value) and
  strips CR first so YAMLs saved with CRLF line endings migrate cleanly.
- Pin Python runtime dependencies.

## 0.1.22

- Role-based agent refactor. Agent YAML filenames and internal identifiers
  now use structural roles (`assistant`, `butler`) instead of display names
  (Ellen, Tina). Display names remain configurable via
  `primary_agent_name` / `voice_agent_name` and are used for personality
  text and the dashboard.
- Session keys formalised as `{channel}:{scope_id}` via
  `build_session_key()`.
- One-shot migration on boot: `agents/ellen.yaml` -> `agents/assistant.yaml`
  (with `role: main` -> `role: assistant` and `peer_name` update);
  `agents/tina.yaml` -> `agents/butler.yaml`.

## 0.1.3

- Add Tina (voice agent) wiring in core startup
- Add APScheduler heartbeat with configurable interval
- Add webhook endpoints (`/webhook/{name}`, `/invoke/{agent}`) with HMAC verification
- Add Telegram message splitting for responses over 4096 characters
- Add error classification with structured user-facing messages
- Add log redaction filter for secrets and tokens
- Make SessionRegistry I/O async (non-blocking)
- Add explicit sys.path management for reliable imports
- Add `apparmor: true` and `url` to config.yaml
- Add store assets: DOCS.md, CHANGELOG.md, translations, icons

## 0.1.2

- Add safety hooks: dangerous command blocking and per-agent path scope enforcement
- Add Honcho memory provider with async SDK wrapper
- Add MCP server registry (HTTP and SDK-based servers)
- Add session registry with JSON persistence
- Add unit tests for all core modules

## 0.1.1

- Add asyncio message bus with priority queues and request/response pattern
- Add channel abstraction with Telegram implementation (python-telegram-bot v20+)
- Add agent config loading with YAML, env var substitution, and model resolution
- Add Ellen agent config with personality prompt and tool permissions

## 0.1.0

- Initial add-on scaffold
- Dockerfile with Python 3.12 Alpine base, Node.js, nginx, ttyd
- S6-overlay init scripts: config validation, default setup, nginx generation
- S6 services: casa (Python core), nginx (ingress proxy), ttyd (web terminal)
- AppArmor profile
- Multi-repo workspace sync script
- Local Docker testing setup with mock Supervisor
