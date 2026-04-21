# Changelog

## 0.8.4 — 2026-04-21 — Scope-routing evaluation harness

### Added
- `casa_eval/` framework — pluggable `Tester` ABC +
  `Suite`/`Case`/`Report`/`Failure`/`Recommendation` dataclasses, all
  JSON-round-trippable. Designed so a future Builder MCP tool can call
  the same `Tester.run()` / `Tester.sweep()` / `recommend_from_sweep()`
  surface with a thin JSON wrapper.
- `ScopeRoutingTester` — evaluates scope-routing accuracy on a labelled
  probe suite with a tunable threshold. Emits `accuracy`,
  `top2_accuracy`, `fallback_rate`, `mean_winner_score`, `mean_margin`,
  `p50_latency_ms`, `p95_latency_ms`. `optimization_axes = ["threshold"]`;
  `optimization_bounds = {"threshold": (0.20, 0.50)}`. Model is frozen
  (see CHANGELOG 0.8.2 rationale).
- `tests/fixtures/eval/scope_routing/default.yaml` — 35-case probe
  suite across the four shipped scopes. Grows by hand when Nicola spots
  a misroute in prod (`metadata.source='real-misroute'`).
- Three pytest run modes: fast (mocked `_FakeEmbedder`, always-on in
  CI); full (`CASA_REAL_EMBED=1`, asserts `accuracy >= 0.85`,
  `fallback_rate <= 0.20`); sweep (`CASA_EVAL_SWEEP=1
  CASA_REAL_EMBED=1`, informational table + recommendation).
- `scripts/eval_scope_dist.py` — audits live `scope_route` log lines,
  emits per-channel winner-score histograms (text or `--json`), flags
  channels whose winners cluster within ±0.05 of the threshold.

### Changed
- `scope_threshold` promoted from a silent env-var fallback (the
  `CASA_SCOPE_THRESHOLD` default `0.35` at `casa_core.py:427`) to a
  first-class HA addon option in `config.yaml`. Default unchanged;
  users can now tune it via the HA UI and Builder will be able to tune
  it via `supervisor.addon_options_set` in 3.5. Runtime read semantics
  at `casa_core.py:427` are untouched — the env var is now sourced
  from `bashio::config 'scope_threshold'` in
  `etc/s6-overlay/s6-rc.d/svc-casa/run`. Restart-required, matching
  every other addon option (restart cost on N150 ≈ 3 sec).

### Notes — post-deploy recipe
- Full-mode pytest on the live N150:
  `sudo docker exec addon_c071ea9c_casa-agent sh -c \
   'cd /opt/casa && CASA_REAL_EMBED=1 python3 -m pytest \
    /opt/casa/tests/test_scope_routing_eval.py::TestScopeRoutingTesterFull -v'`
  (run via `/ha-prod-console:exec` after each deploy that touches
  `scopes.yaml` descriptions or the threshold).

## 0.8.3 — 2026-04-21 — Voice-latency optimizations

### Added
- Per-process LRU cache for query embeddings in `ScopeRegistry` (256
  entries, keyed on `text.strip().lower()`). Voice retriggers and
  repeat commands are frequent — hits skip the ~90 ms ONNX forward
  pass and drop `score()` cost from ~90 ms to ~1 ms (just the cosine
  dot-products).
- `scope_route` telemetry now includes `embed_cache=N/M` where `M` is
  total calls this process has seen. Use to verify the cache is
  actually paying off after a few hours of real use.
- `ScopeRegistry.cache_stats()` returns `(hits, misses)` for tests
  and telemetry.

### Changed
- Write-path classifier now short-circuits when `owned_and_readable`
  contains exactly one scope — argmax over a single candidate is
  trivially that scope. Saves ~90 ms on every butler (voice) turn,
  since Tina only owns `house`. Assistant (3 owned scopes) still
  classifies.

### Latency impact (measured on N150 with e5-large)
- Butler voice critical path: ~90 ms → ~1 ms on cache hit
- Butler voice total per-turn overhead: ~180 ms → ~0-90 ms
  (write-path classifier removed unconditionally, read-path when
  cached)
- Assistant telegram: unchanged on first call, ~90 ms saved on any
  repeat of the same user text

## 0.8.2 — 2026-04-21 — Post-deploy hotfixes (model + trust bypass)

### Fixed
- Embedding model name — `intfloat/multilingual-e5-small` is not in
  fastembed 0.4's supported-model catalog (only `-large` ships). v0.8.1
  was silently booting in degraded mode with the "model not supported"
  error on first init. Switched `_DEFAULT_MODEL_NAME` (and the
  setup-configs pre-warm invocation) to `intfloat/multilingual-e5-large`
  so the classifier comes up non-degraded. The large variant is ~500 MB
  (vs ~200 MB for small) — still well within N150 capacity.
- Write-path trust bypass — when the channel's trust tier filters out
  every scope the agent owns (`scopes_owned ∩ readable == []`), the
  write path was falling back to `default_scope` and persisting the
  exchange into a scope the channel cannot see. Now skips the write
  entirely. Regression test
  `TestWritePath::test_write_skipped_when_owned_and_readable_empty`
  covers this. Observed in v0.8.1: webhook → assistant turn was logging
  `scope_route ... active=[house] write=personal`.

## 0.8.1 — 2026-04-21 — Debian base image (onnxruntime compatibility)

### Changed
- Base image migrated from `amd64-base-python:3.12-alpine3.22` to
  `amd64-base-debian:bookworm`. Alpine ships no `musllinux` wheels for
  `onnxruntime` (a transitive dep of `fastembed>=0.4`), forcing a
  from-source build that failed under the addon's build constraints.
  Debian/glibc pulls the prebuilt `manylinux_2_17_x86_64` wheel.
- Container Python is now 3.11 (Debian bookworm default), down from
  3.12. Casa's code uses only 3.9+ features; dev-host test suite runs
  on 3.11.9 so container Python now matches.
- Python deps installed into a virtualenv at `/opt/casa/venv` (PEP 668
  "externally managed" environment on Debian prevents direct `pip
  install` to system site-packages). The venv's `bin/` is prepended to
  `PATH` so all `python3` invocations in s6 service + setup scripts
  resolve to the venv interpreter without script changes.

### Dependencies
- Node.js 18 (Debian bookworm apt) replaces Alpine's nodejs (identical
  major version; `@anthropic-ai/claude-code` engine constraint
  `>=18.0.0` still satisfied).

### Image size
- Uncompressed image grows by ~200-350 MB (Debian base + Python stack
  larger than Alpine). No impact on the N150's 120 GB storage.

## 0.8.0 — 2026-04-20 — Phase 3.2: Domain scope runtime

### Added
- Domain scope as the authoritative memory visibility layer. Four scopes
  ship by default (`personal`, `business`, `finance`, `house`) declared in
  `/addon_configs/casa-agent/policies/scopes.yaml` with editable
  natural-language descriptions and `minimum_trust` tiers.
- `ScopeRegistry` with a local `fastembed` embedding model
  (`intfloat/multilingual-e5-small`, ~200 MB, downloaded to `/data/fastembed/`
  on first boot). Scores user text per readable scope; fan-out reads above
  threshold; end-of-turn classifies the full exchange for the write target.
- Per-scope Honcho session topology: `{channel}:{chat_id}:{scope}:{role}`.
  Per-turn telemetry line `scope_route role=... channel=... active=[...]
  write=... (t=Nms)`.
- `memory.default_scope` field in resident `runtime.yaml` (required for
  residents with `scopes_readable`; forbidden on executors).
- `channel_trust()` now returns a canonical token; `channel_trust_display()`
  preserves the human-readable form for the `<channel_context>` prompt block.

### Changed
- **Breaking (internal): memory session topology.** Pre-v0.8.0 Honcho /
  SQLite sessions (keyed `{channel}:{chat_id}:{role}`) are orphaned. Fresh
  scoped sessions accumulate from turn 1 after upgrade; prior transcripts
  remain visible in the Honcho dashboard but Casa does not read from them.
- Butler `disclosure.yaml` override shortened — `categories: {}`,
  `safe_on_any_channel` and `deflection_patterns` inherit from the shared
  `standard` policy. Scope-at-retrieval enforcement makes the confidential
  category listing redundant for Tina.
- `Agent` constructor now takes `scope_registry` as a required argument.

### Environment
- New: `CASA_SCOPE_THRESHOLD` (default `0.35`). Raise to make routing
  stricter (fewer scopes pulled per turn); lower to be more inclusive.

### Dependencies
- `fastembed>=0.4,<0.5`.

### Non-goals carried forward
- No scope-aware tool gating — 3.x follow-up.
- No legacy memory migration — cold start on upgrade.
- No remote embedding provider — local only in v0.8.x.
- No `/finance ...` user-prefix syntax.

### Deployment note
- First boot downloads the embedding model (~200 MB, ~30 s). Subsequent boots
  reuse `/data/fastembed/`. Offline first-boot degrades gracefully (fan-out
  to every readable scope) with a WARNING log.

## 0.7.0 — 2026-04-20 — Agent-definition refactor (Spec X / Phase 4.x)

### Added

- **Per-agent directory format.** Each resident and executor lives in its
  own directory under `/addon_configs/casa-agent/agents/<role>/` with
  one file per concern: `character.yaml`, `voice.yaml`,
  `response_shape.yaml`, `runtime.yaml`, and optionally
  `disclosure.yaml`, `delegates.yaml`, `triggers.yaml`, `hooks.yaml`.
  Flat `agents/<role>.yaml` files are no longer loaded.
- **Strict-mode loader** (`agent_loader.py`) with JSON Schema
  validation: unknown field / unknown file / missing required
  `schema_version` / unknown `disclosure.policy` all fail-fast at boot.
- **Shared policy library** (`policies.py`) resolves
  `disclosure.policy: <name>` references against
  `/addon_configs/casa-agent/policies/disclosure.yaml`.
- **Per-agent trigger registry** (`trigger_registry.py`) replacing the
  global heartbeat block. Residents declare their own
  `interval` / `cron` / `webhook` triggers in `triggers.yaml`.
- **`HOOK_POLICIES` registry + `resolve_hooks`** in `hooks.py`; per-agent
  hook wiring via `hooks.yaml`, resolved at `Agent.__init__`. Default
  bundle (`block_dangerous_bash` + `path_scope`) applies when the file
  is absent or empty.
- **`config_git`** module — initialises a local git repo under
  `/addon_configs/casa-agent/` and snapshots manual edits on every boot
  for free history / rollback.

### Removed (breaking)

- Flat agent YAMLs: `defaults/agents/{assistant,butler,subagents}.yaml`
  and `defaults/agents/executors/finance.yaml`.
- Global schedules/webhooks files: `defaults/schedules.yaml`,
  `defaults/webhooks.yaml`.
- All one-shot migrations from `setup-configs.sh` (`migrate_rename`,
  `migrate_memory_fields`, `migrate_voice_fields`,
  `migrate_disclosure_clause`, `migrate_scope_metadata`,
  `migrate_channels`, `migrate_executor_rename`, `migrate_mcp_allowed`)
  and their six regression test modules. Migrations are no longer
  needed — the new file format is the only format the loader
  understands.
- `config.ROLE_ALIASES`, `config._normalize_role`,
  `config.load_agent_config`, the `_build_*` helpers, and the legacy
  `name` / `personality` / `description` fields on `AgentConfig`.
- `casa_core._log_subagents_deprecation_if_present`,
  `casa_core._load_agents_by_role`, `casa_core.init_heartbeat_defaults`,
  `casa_core.build_heartbeat_message`, and the inline global heartbeat
  scheduler block.
- `hooks.AGENT_PATH_RULES`, `hooks._check_path_scope`,
  `hooks.make_path_scope_hook` (replaced by the parameterized
  `make_path_scope_hook_v2`).

### Migration

No production users — this is a hard cut. Existing installations will
find their old flat YAMLs unread; seed the new tree by deleting
`/addon_configs/casa-agent/agents/*.yaml` and letting
`setup-configs.sh` copy the bundled directory defaults on next boot.

## 0.6.2 — 2026-04-20 — Phase 3.4: disabled-executor pattern (plumbing)

### Added

- **Glob-based executor seeding.** `setup-configs.sh` now discovers
  `defaults/agents/executors/*.yaml` at first boot, seeding each to the
  user's config directory if absent. Adding a new bundled-disabled
  executor is now a single-file drop — no Casa code edit. Residents
  and top-level config files stay hand-enumerated (they are individually
  required by startup). Mirrored into
  `test-local/init-overrides/01-setup-configs.sh`.

- **`n8n-workflows` MCP server registration.** New
  `_maybe_register_n8n(mcp_registry, env=None)` helper in
  `casa_core.py`, wired into `main()` after the existing
  `homeassistant` block. When `N8N_URL` is set, registers the
  `n8n-workflows` HTTP MCP server (with `Authorization: Bearer ...`
  header if `N8N_API_KEY` is also set). Generic shared infrastructure:
  any agent (resident or executor) that declares `n8n-workflows` in
  `mcp_server_names` can reach it; per-agent tool whitelisting via
  `tools.allowed` governs which workflows each agent may actually
  invoke.

- **Executor enabled/disabled summary log.**
  `ExecutorRegistry.load()` now emits one INFO line at the tail of
  loading: `Executors: enabled=[...] disabled=[...]`. Operator
  visibility into the executor landscape and a stable grep target for
  future automation.

- **User-facing docs.** New "Enabling a bundled-disabled executor"
  section in `DOCS.md` walks users through flipping
  `enabled: false` → `true` on `finance.yaml` (or any future
  disabled-by-default executor YAML) and restarting the addon.

### Tests

- 4 new unit tests in `tests/test_n8n_registration.py` covering the
  helper's env-gated behavior (URL unset, URL set with/without API
  key, whitespace-only URL).
- 2 new unit tests in `tests/test_executor_registry.py::TestSummaryLog`
  for the summary log output (mixed state + empty state).
- New `test-local/e2e/test_delegation.sh` with three scenarios
  (D-1/D-2/D-3) proving bundled-disabled, flip-to-enabled, and
  config-not-code discovery contracts. All assertions are log-line +
  file-presence; tool-behavior contracts remain at the unit level
  (`test_delegate_to_agent.py`) because the offline mock SDK doesn't
  dispatch tool calls.

### Non-breaking

- Default-env startup is unchanged for users who don't set `N8N_URL`
  and don't edit `finance.yaml`. `finance` continues to be bundled as
  `enabled: false`.

### Deferred

- `finance`'s tool whitelist, prompt polish, and n8n workflow bindings
  ship in a separate capabilities session. Plumbing only.

## 0.6.1 — 2026-04-20 — Phase 3.1 follow-ups: role-over-name + 3.4 prerequisites

### Fixed

- **Executor role/name cleanup.** v0.6.0 shipped the Alex executor with
  `role: alex`, conflating human-facing name and functional role. Every
  other resident uses `role=<function>` (assistant, butler) with
  `name=<persona>` (Ellen, Tina). Renamed the bundled default file
  `defaults/agents/executors/alex.yaml` → `finance.yaml`, set
  `role: finance` + `name: Alex`. New one-shot `migrate_executor_rename`
  in `setup-configs.sh` moves any existing
  `/addon_configs/casa-agent/agents/executors/alex.yaml` → `finance.yaml`
  and patches the `role:` + `name:` lines. Idempotent by file existence.
  Delegation API change: `delegate_to_agent(agent="finance", ...)` is
  now the canonical invocation; `agent="alex"` returns `unknown_agent`.

### Added

- **Phase 3.4 prerequisite: MCP registry wiring in
  `_build_executor_options`.** v0.6.0 hardcoded `mcp_servers={}` for
  executor invocations, which would have left a future-enabled Alex
  with zero MCP tools. `init_tools()` now accepts an optional
  `mcp_registry: McpServerRegistry`; when passed, `_build_executor_options`
  resolves `cfg.mcp_server_names` through it. `casa_core.main()`
  passes the registry. Legacy 3-arg `init_tools` still works (degrades
  to empty mcp_servers) for test harnesses.

- **Phase 3.4 prerequisite: Ellen can now call `delegate_to_agent`.**
  The Claude Agent SDK blocks MCP tools unless explicitly whitelisted
  by their `mcp__<server>__<tool>` name. v0.6.0's bundled
  `assistant.yaml::tools.allowed` didn't list
  `mcp__casa-framework__delegate_to_agent`, so Ellen refused to invoke
  it even though the tool was registered. Added both
  `mcp__casa-framework__delegate_to_agent` and
  `mcp__casa-framework__send_message` to the bundled default. New
  `migrate_mcp_allowed` one-shot in `setup-configs.sh` backfills both
  entries into existing users' `assistant.yaml::tools.allowed`, gated
  by `# casa: mcp-tools v1` marker. Handles inline list (`allowed: [...]`)
  and block list (`allowed:\n  - ...`) forms; preserves existing entries.

### Notes

- No deployment steps beyond `ha apps update`. Both new migrations
  (`migrate_executor_rename`, `migrate_mcp_allowed`) are idempotent.
  The rename migration only fires if you upgraded to v0.6.0 and had
  the Alex executor seeded at `/addon_configs/casa-agent/agents/
  executors/alex.yaml` (which is the case for anyone who ran v0.6.0).
- Updated Ellen's personality (`Delegation:` section) to reference
  `delegate_to_agent(agent="finance")` instead of the deprecated
  "spawn Alex subagent" / "spawn automation-builder subagent" wording.
  Non-functional prose — Ellen's behaviour is governed by the tool
  surface, not the prose.

---

## 0.6.0 — 2026-04-20 — Phase 3.1: Residents, Executors, delegate_to_agent

### Added

- **Phase 3.1: Residents, Executors, `delegate_to_agent`**
  (spec `docs/superpowers/specs/2026-04-20-3.1-residents-executors-delegation.md`,
  taxonomy foundation `2026-04-20-agent-taxonomy.md`).
  - Tier 1 resident loader relaxed: any `agents/<role>.yaml` with
    non-empty `channels:` loads as a resident. No code change required
    to add new user-defined residents.
  - Tier 2 executor loader + `ExecutorRegistry` at
    `casa-agent/rootfs/opt/casa/executor_registry.py`. Scans
    `agents/executors/*.yaml`; rejects Tier-1-shaped YAMLs; honours
    `enabled: false` gating.
  - New framework tool `delegate_to_agent(agent, task, context, mode)`
    in `casa-framework` MCP. Sync-with-degradation (60s
    `asyncio.wait` — never `asyncio.wait_for`) + explicit async. Late
    completions post a bus NOTIFICATION to the delegating resident;
    Ellen's NOTIFICATION branch synthesizes a fresh turn and replies
    via the origin channel.
  - In-flight delegations persisted to `/data/delegations.json`;
    orphaned records on restart fire a synthetic "lost on restart"
    NOTIFICATION exactly once.
  - Alex ships bundled at `defaults/agents/executors/alex.yaml` with
    `enabled: false`. Becomes functional when 3.4 registers the n8n MCP.
  - YAML metadata migration: `scopes_owned` + `scopes_readable` on
    Ellen + Tina, gated by `# casa: scopes v1` marker. Fields parse at
    runtime but are **unread in v0.6.0** — scope-aware retrieval ships
    in 3.2.

### Changed

- `subagents.yaml` entries (`automation-builder`, `plugin-builder`,
  inline `alex`) are no longer loaded. Re-classified as Tier 3
  Builders (deferred spec). One-time deprecation log on startup if
  the file is present; no auto-migration.

### Fixed

- Upgrade-path regression: pre-2.1 YAMLs that went through
  `migrate_rename` (ellen.yaml → assistant.yaml / tina.yaml →
  butler.yaml) lacked a `channels:` key. Task 3's tightened Tier 1
  loader would have skipped them at startup. New idempotent
  `migrate_channels` one-shot in `setup-configs.sh` backfills
  sensible defaults (`[telegram, webhook]` for assistant;
  `[ha_voice]` for butler) on upgrade, gated by `# casa: channels v1`
  marker. Non-destructive: existing channels are preserved.

### Notes

- No deployment steps for existing N150 users beyond `ha apps update`.
  The scope-metadata and channels-backfill migrations are idempotent;
  running an older Casa after upgrading YAMLs is a no-op (extra
  fields ignored by pre-3.1 loaders).

---

## 0.5.10 — 2026-04-20 — Phase 5.7: Close public dashboard

### Fixed
- On the public hostname (`agent.oudekamp.bonzanni.casa`, addon-nginx
  `:18065`), `GET /` no longer proxies to casa-core and returns
  the dashboard HTML. It now returns a static **nginx 404** via an
  exact-match `location = /` rule placed immediately before the
  existing catch-all. No Python hop, no aiohttp handler invocation.
  The dashboard remains reachable via HA ingress on the separate
  `listen $INGRESS_PORT` server block.
- Test-infra gap from v0.5.9: `test-local/mock-claude-sdk/` was
  missing `ProcessError`. v0.5.9's resume-resilience code added a
  `ProcessError` import in `agent.py`, which unit tests tolerated
  (they resolved the real pip-installed SDK on the host) but the
  Docker e2e image crashed at container start with `ImportError:
  cannot import name 'ProcessError'`. Mock now mirrors the real
  SDK's 3-arg `(message, exit_code, stderr)` signature so tests
  that construct `ProcessError(..., exit_code=N)` work unchanged.
  Unblocks all local e2e on this release; no production impact.

### Tests
- New e2e: `test-local/e2e/test_external_surface.sh` — maps both
  ingress (`:8080`) and external (`:18065`) ports, asserts three
  outcomes:
  1. Ingress `GET /` → 200 (dashboard still alive internally).
  2. External `GET /` → 404 (new contract).
  3. External `GET /healthz` → 200 (uptime contract pinned).
- Wired into `.github/workflows/qa.yml` e2e-fast as the final step.
- `test-local/e2e/common.sh::start_container()` now accepts an opt-in
  `EXT_PORT` env var that maps a second host port to container `:18065`.
  Default behaviour unchanged — all eight pre-existing e2e scripts
  still map only `:8080`.

### Not changed
- `casa-core` aiohttp routes — `app.router.add_get("/", dashboard)`
  stays intact.
- Ingress server block — the `listen $INGRESS_PORT` block is
  untouched; HA-authenticated dashboard access works as before.
- All other external routes — `/invoke/*`, `/webhook/*`,
  `/api/converse`, `/api/converse/ws`, `/telegram/update` continue to
  match the catch-all `location /` and hit their existing gates
  (HMAC, secret token, anonymous `/healthz`).
- Nginx `/terminal/` rule on the external block — pre-existing 404
  unchanged.

---

## 0.5.9 — 2026-04-19 — Phase 5.8: SDK session resume resilience

### Added
- **`SessionRegistry.clear_sdk_session(channel_key)`** — drops only
  the `sdk_session_id` field from a registry entry; keeps
  `last_active` and `agent` intact so the session sweeper and
  downstream consumers still see the scope. Idempotent and no-op on
  missing keys.
- **`Agent._process` resume fallback.** When
  `claude_agent_sdk.ProcessError` fires on a turn that attempted to
  resume a prior SDK session (`resume_session_id` was set), Casa now:
  1. Logs a `WARNING` — `SDK resume failed (key=<k> sid=<sid>); clearing and retrying fresh`.
  2. Clears the stale `sdk_session_id` via `clear_sdk_session`.
  3. Rebuilds `ClaudeAgentOptions` with `resume=None` via
     `dataclasses.replace`.
  4. Re-runs `retry_sdk_call(_attempt_sdk_turn)` once. On success the
     fresh `sdk_session_id` is persisted via the existing `register`
     path.
  If `resume_session_id` was `None` or the fresh retry also raises
  `ProcessError`, the exception propagates to the caller — no
  infinite loop.

### Fixed
- `/data/sessions.json` persists across `ha apps rebuild` (bind-
  mounted), but the claude CLI's own session state under
  `/root/.claude/` does NOT (container-local). Every rebuild
  therefore orphaned every `sdk_session_id` recorded in
  `sessions.json`. Subsequent resume attempts crashed claude CLI
  with exit 1 + `No conversation found with session ID: <uuid>`,
  manifesting in Casa as `ProcessError` and a user-facing `sdk_error`
  persona line. Tripped by the v0.5.8 post-deploy `voice-sse` smoke
  probe (`voice:probe-scope` → butler agent Tina). The fix recovers
  transparently: the first post-rebuild turn on any stale scope
  logs one `SDK resume failed` warning and proceeds on a fresh
  session. Agent memory is unaffected (Honcho / SQLite memory is
  keyed on user peer + channel key, not `sdk_session_id`).

### Tests
- New: `tests/test_session_registry.py::TestClearSdkSession` — 4
  tests (field removal, metadata preservation, missing-key no-op,
  disk persistence).
- New: `tests/test_agent_process.py::TestResumeResilience` — 5
  tests (stale resume cleared and retried, second attempt sees
  `resume=None`, no-resume re-raises, double-ProcessError re-raises
  with cleared stale id, fallback logs a single prefixed WARNING).
- Count: 426 → 435 unit tests green.

### Not changed
- `retry.py` — stays a pure policy module. `ProcessError` remains
  classified as `ErrorKind.UNKNOWN` (not in `RETRY_KINDS`). The
  fallback runs at the outer `Agent._process` layer.
- `error_kinds.py` — no classification changes.
- `SessionSweeper` — TTL-based eviction unchanged.
- `sessions.json` persistence — unchanged shape. The fallback
  mutates entries only when it fires.
- Memory providers — unchanged. Honcho / SQLite remain orthogonal
  to `sdk_session_id`.

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-19-5.8-session-resume-resilience.md`
- Plan: `docs/superpowers/plans/2026-04-19-5.8-session-resume-resilience.md`

## 0.5.8 — 2026-04-19 — Phase 5.5: log hygiene

### Added
- **aiohttp cid middleware** in new `casa_core_middleware.py`
  (`cid_middleware`). Every inbound HTTP request now gets an 8-char
  lowercase-hex correlation id at ingress. Operators may override
  via `X-Request-Cid` header (accepts 8–32 hex chars,
  case-insensitive; uppercase normalised to lowercase; invalid shape
  silently ignored in favour of a fresh allocation). The middleware
  binds `log_cid.cid_var` with a scoped ContextVar token for the
  handler's task. `asyncio.create_task` snapshots contextvars, so
  any task the handler spawns (notably `bus.request`'s inner
  dispatch task) inherits the same cid — access-log lines, ingress
  INFO lines, and the `turn_done` budget summary all share one cid.
- **Custom aiohttp `AccessLogger`** (`CasaAccessLogger`) in the same
  module. Emits one `logger.info(...)` on the `casa.access` logger,
  which picks up Casa's installed root handler (5.2-H) — so access
  lines share the active formatter (human/JSON), carry the current
  cid, and run through `RedactingFilter`. Line format:
  `access method=<M> path=<P> status=<S> duration_ms=<D> bytes=<B>`.
  Replaces aiohttp's default CLF output (which double-stamped the
  timestamp and always logged `cid=-`). Wired into `AppRunner` via
  `access_log_class=CasaAccessLogger,
  access_log=logging.getLogger("casa.access")`.
- **Telegram `_handle` inherit-or-allocate cid** — unifies webhook
  and polling transport. When the aiohttp middleware has bound
  `cid_var` (webhook mode via `/telegram/update`), PTB's `_handle`
  inherits it via contextvars. When running in polling mode (no HTTP
  ingress), `_handle` allocates a fresh cid via `new_cid()` as
  before. Pattern:
  `cid = cid_var.get(); cid = cid if cid != "-" else new_cid()`.

### Changed
- **[BREAKING] `LOG_FORMAT` default flipped from human to JSON.**
  Unset `LOG_FORMAT` now yields JSON; any value other than `"human"`
  (case-insensitive — includes typos like `LOG_FORMAT=JSON` or
  `LOG_FORMAT=true`) also resolves to JSON. **For prior behaviour,
  set `LOG_FORMAT=human`** in the addon options. Casa does not
  consume its own logs, so no internal callers break; only operators
  reading raw `docker logs` by eye are affected. Motivation:
  operators running a log aggregator (Loki+Promtail, Vector, Fluent
  Bit) no longer need to opt-in to JSON — the default is now
  parseable out of the box.
- **Addon nginx `error_log` level `info` → `warn`** in
  `casa-agent/rootfs/etc/s6-overlay/scripts/setup-nginx.sh`. The
  "closed keepalive connection" info-spam (one line per external
  request at idle) disappears; real config errors and upstream
  timeouts still surface. This is the fix the dropped-as-YAGNI 5.6
  (NPM upstream connection reuse) was reaching for — at zero
  operational debt versus a template fork or migration-off-NPM.
- **ttyd `-d 0`** in `svc-ttyd/run`. Routine connection/session
  chatter silenced; fatal errors stay visible. Only affects
  deployments that enable the `web_terminal` addon option.
- **HTTP handler cid reads** — `webhook_handler` (`casa_core.py`),
  `invoke_handler` (`casa_core.py`), voice SSE handler
  (`channels/voice/channel.py:_sse_handler`), Telegram webhook POST
  `telegram_update_handler` — all now read `request["cid"]` instead
  of calling `new_cid()` inline. `invoke_handler` additionally
  primes `payload["context"]["cid"]` from `request["cid"]` before
  calling `build_invoke_message`, so the builder's defensive
  fallback is a no-op on the normal HTTP path.

### Infra
- **Bashio ANSI strip (defensive).** Every s6 `run`/`finish` script
  plus `setup-configs.sh`, `setup-nginx.sh`, `validate-config.sh`,
  and `sync-repos.sh` now exports `BASHIO_LOG_NO_COLORS=true` and
  `NO_COLOR=1` at the top (after the shebang, before any `bashio::*`
  call). Idempotent — re-sourcing on s6 respawn costs nothing.
  Baseline ANSI count on v0.5.7 prod was not captured this session
  (SSH-agent auth failure); defensive exports ship regardless so
  future bashio/s6 TTY-detection changes cannot reintroduce ANSI.

### Not changed
- **`bus._dispatch` cid binding** — the defensive
  `cid_var.set(msg.context["cid"])` from 5.2-H stays. It's a no-op
  when the middleware already bound the same cid; authoritative
  when a non-HTTP caller sets a different cid in `context` (e.g.
  scheduler heartbeat). Removing it would silently break non-HTTP
  cid paths.
- **`CidFilter` utility class** in `log_cid.py` remains available
  for manual LogRecord construction. Not auto-wired — the
  LogRecord factory in `install_logging` handles cid tagging at
  creation time.
- **Voice WS per-utterance cid allocation** stays manual. One WS
  connection, many utterances, one cid per utterance per the 5.2-H
  contract. The middleware allocates a connection-level cid for the
  WS upgrade request; the utterance loop then overrides per frame
  via `new_cid()`.
- **Scheduler heartbeat cid** stays. It runs in the scheduler
  task, no HTTP request, so the middleware never sees it.
  `bus._dispatch` picks up `context["cid"]` as today.
- **`build_invoke_message` defensive fallback** stays. On the
  normal HTTP path `invoke_handler` primes `payload.context.cid`
  from the middleware, so the fallback is a no-op. Non-HTTP
  callers (future) can still rely on it.

### Tests
- New: `tests/test_cid_middleware.py` (12 tests across
  `TestDefaultAllocation`, `TestHeaderOverride`,
  `TestContextVarBinding`, `TestExceptionSafety`,
  `TestSpawnedTaskInherits`) — uses Casa's existing
  `TestClient(TestServer(app))` pattern, no `pytest-aiohttp`
  dependency.
- New: `tests/test_casa_access_logger.py` (6 tests across
  `TestFormat`, `TestCidInRecord`, `TestLoggerWiring`,
  `TestJsonMode`).
- Extended: `tests/test_telegram_split.py::TestInheritOrAllocateCid`
  (2 tests — inherits pre-bound cid, allocates when default).
- Extended: `tests/test_log_cid.py::TestFormatDefaultIsJson`
  (2 tests — unset env yields JSON, `LOG_FORMAT=human` yields
  human). Existing `test_human_format_default` updated to set
  `LOG_FORMAT=human` explicitly (preserves human-format
  coverage).
- Updated: `tests/test_voice_channel_sse.py` — 3 fixture
  `web.Application()` calls now register `cid_middleware` so
  handlers see `request["cid"]`.
- Count: 403 → 425 (+22 new/extended tests).

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-19-5.5-log-hygiene.md`
- Plan: `docs/superpowers/plans/2026-04-19-5.5-log-hygiene.md`

## 0.5.7 — 2026-04-18 — Phase 5.3 infra hygiene (partial: items A + K)

### Added
- `.dockerignore` at the repo root. Excludes `**/build/`,
  `**/*.egg-info/`, `**/.eggs/`, `**/dist/`, `**/__pycache__/`,
  `**/*.pyc`, `**/.pytest_cache/`, `**/.mypy_cache/`,
  `**/.ruff_cache/`, `.spike-venv/`, `.venv/`, `venv/`, `.git/`,
  `.worktrees/`, `docs/`, `.claude/`, `.env`, `.env.*`, and
  `test-local/options.json`. Docker does NOT honor `.gitignore`, so
  host-side pip-install artifacts (seen in 5.2 item F: stale
  `test-local/mock-claude-sdk/build/lib/` COPYd into the test image
  and masked product changes) now can't poison Docker builds. This
  closes the backlog item filed during 5.2-F. Verified with
  `docker buildx build --progress=plain` on a tiny `COPY . /tmp`
  probe: **context transfer 360.33 MB → 12.22 kB** (the bulk was
  the `.spike-venv` virtualenv under `.gitignore` that Docker was
  shipping to the daemon on every build).

### Changed
- `test-local/Dockerfile.test` — `ARG BUILD_FROM` now pins the HA
  base image by sha256 digest
  (`ghcr.io/home-assistant/amd64-base-python@sha256:cb37b54…`)
  instead of the floating `3.12-alpine3.22` tag. Pinned 2026-04-18
  against HA base 2026.04.0 (`org.opencontainers.image.created
  2026-04-13`). Refresh process documented in-file via a block
  comment (`docker buildx imagetools inspect … | jq
  '.manifest.digest'`). Rationale: pre-pin, a silent HA base
  republish under the same tag could change test behaviour with no
  record in our repo; CI results stop being reproducible across
  time. Spec §4 / decision H4.
  - Scope note: production `casa-agent/Dockerfile` stays unpinned
    per spec §2 non-goal / H3. It inherits `BUILD_FROM` from the HA
    builder pipeline, which pins its own base per release.
    Re-pinning would fight the HA release machinery.

### Deferred
- **Item J — narrow AppArmor `file,` rule** (spec §3). Requires the
  complain-mode discovery loop on real Linux hardware with AppArmor
  enabled (the N150 production box). Casa's dev machine is Windows
  / Docker Desktop; kernel AppArmor is unreachable. Spec decision
  H1 explicitly warns against shipping a theoretical path list
  without the `aa-logprof` / kernel-audit capture. Left on the 5.3
  roadmap entry; no code change on this release.

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-18-infra-hygiene-5.3.md`
- No separate plan file (mechanical sweep; two edits).

## 0.5.6 — 2026-04-18 — Phase 5.2 item I: inbound rate limiting

### Added
- `rate_limit.py` — pure policy module. `TokenBucket`: single-key
  refill-on-check bucket with an injectable clock; `capacity<=0`
  short-circuits every `check()` to allowed. `RateLimiter`:
  `dict[str, TokenBucket]` with lazy bucket creation AND a
  disabled-state short-circuit so disabled limiters never grow the
  per-key dict. `RateDecision` (frozen dataclass): `allowed`,
  `should_notify` (fires on the FIRST reject after any allow — the
  signal Telegram uses for its reply-once-per-streak semantic),
  `retry_after_s`. `rate_limit_response(limiter, key)` — aiohttp 429
  helper returning `None` (allowed) or a `web.Response` with
  `Retry-After` integer-seconds header rounded up from the underlying
  bucket's `retry_after_s`.
- Three `RateLimiter` instances constructed in `casa_core.main()`:
  `TELEGRAM_RATE_PER_MIN` (default 30) keyed on `chat_id`,
  `VOICE_RATE_PER_MIN` (default 20) keyed on `scope_id`,
  `WEBHOOK_RATE_PER_MIN` (default 60) on a single shared `"global"`
  key across `/webhook/{name}` + `/invoke/{agent}` per spec §8.2.
  All three env vars via the `_env_int_or` helper from item G with
  `min_value=0`; setting the value to 0 disables the limit.
- Startup log line `Rate limits: telegram=30/min, voice=20/min,
  webhook=60/min` (values rendered as `off` when the channel's limit
  is disabled).
- Centralised `telegram.*` stub install in `tests/conftest.py` with
  canonical `_FakeNetworkError` / `_FakeTimedOut` / `_FakeTelegramError`
  classes. Previously `tests/test_telegram_reconnect.py` and
  `tests/test_telegram_split.py` each installed their own stubs with
  locally-defined exception classes — pytest's alphabetical discovery
  could let one file's classes "win" and diverge from what production
  code would catch. Now all Telegram-adjacent test files share the
  same class identities regardless of load order.

### Changed
- `TelegramChannel.__init__` gains an optional
  `rate_limiter: RateLimiter | None = None` kwarg. In `_handle`,
  immediately after deriving `chat_id` (and before `_start_typing`),
  the channel consults the limiter. On reject it drops the message
  and — only on `should_notify=True` — sends one
  `"Slow down — try again in a minute."` reply via
  `bot.send_message` (wrapped in a try/except that logs at DEBUG and
  does not raise). Pre-existing callers that don't pass a limiter
  keep unlimited behaviour.
- `VoiceChannel.__init__` gains the same `rate_limiter` kwarg. On
  SSE the handler opens a 200 SSE stream and writes one
  `event: error` with `kind=rate_limit` + persona line from
  `voice_errors["rate_limit"]` (falls back to `_DEFAULT_ERROR_LINES`);
  no `event: done` is emitted. On WS `_run_ws_utterance` sends one
  `{type:"error", utterance_id, kind:"rate_limit", spoken:…}` and
  returns — no `bus.request`, no stream open.
- `casa_core.webhook_handler` and `casa_core.invoke_handler` each
  call `rate_limit_response(webhook_rate_limiter, "global")` as the
  FIRST step (before HMAC verification). On reject returns 429 with
  JSON body `{"error": "rate_limited"}` + `Retry-After` integer
  seconds. Rationale for before-HMAC: an unauthenticated flood still
  burns zero Claude quota (the real protection) and gets throttled
  cheaply without the HMAC hash.
- `tests/test_telegram_reconnect.py` aliases its local
  `_FakeNetworkError` / `_FakeTimedOut` / `_FakeTelegramError` names
  from `sys.modules["telegram.error"]` so the exceptions it raises
  via AsyncMock `side_effect=` match the class `channels.telegram`'s
  `except NetworkError:` catches, regardless of whether its own stub
  install ran first or conftest's did.

### Tests
- `tests/test_rate_limit.py` — 16 unit tests across
  `TestTokenBucket` (8), `TestRateLimiter` (5), `TestRateLimitResponse` (3).
- `tests/test_telegram_rate_limit.py` — 6 integration tests driving
  `TelegramChannel._handle` against a fake Update: burst under cap
  reaches bus; reject emits exactly ONE reply then drops silently;
  per-`chat_id` isolation; `capacity=0` disables; pre-existing
  no-limiter callers unaffected; rejected messages don't start the
  typing indicator.
- `tests/test_voice_channel_sse.py::TestRateLimit` — 3 tests
  (exhaust+reject emits `event: error kind=rate_limit` with no
  `event: done`; `capacity=0` is unlimited; per-`scope_id` isolation).
- `tests/test_voice_channel_ws.py::TestRateLimit` — 2 tests
  (exhaust+reject emits `type:error kind=rate_limit` on the socket
  with no `type:done`; `capacity=0` is unlimited).
- `tests/test_casa_core_helpers.py::TestWebhookRateLimit` — 4 tests
  (burst-then-429 with integer `Retry-After`, global bucket shared
  across `/webhook/*` and `/invoke/*`, `capacity=0` disables, 429
  body shape).
- 403 unit/integration tests green. E2E smoke, invoke-sessions, and
  concurrency scenarios still green; "Rate limits: …" startup line
  verified in a standalone container for both the default and
  all-off paths.

### Not changed
- Bus, agent, retry, memory, session_registry, session_sweeper,
  log_cid, log_redact, mcp_registry, config, tools, channel_trust,
  telegram_supervisor, voice/{session,prosodic,tts_adapter} — all
  untouched. Rate limiting is a pre-filter at each ingress; nothing
  downstream of the bus sees the reject path.
- No dashboard row, no `/metrics` endpoint (spec §5.3 precedent
  carries to §8). Logs + HTTP status codes + the Telegram reply
  text are the operator-facing surface.
- No per-agent-role override on the webhook bucket (spec §8.2 is
  explicit: "all names and agents share one bucket").
- No persistence of bucket state across restarts. A restart resets
  all three buckets to full capacity; this is intentional —
  webhook also requires valid HMAC as primary authN; rate limit is
  defense-in-depth against accidental self-DoS + a flooded
  leaked-secret.
- No eviction of idle buckets from the per-key dict. On a
  single-user Casa the set of unique keys is bounded by real
  Telegram chats + voice devices + 1 (global webhook). Add an
  idle-bucket sweep only if the dict footprint becomes a concern.
- No E2E shell scenario. The webhook 429 path is trivially
  reproducible (`for i in $(seq 1 61); do curl -X POST …/webhook/t; done`
  → last response is 429) but faithfully replaying per-chat_id
  Telegram rate limits or per-scope_id voice rate limits from a
  shell harness is out of proportion to value. Matches item D/E/G
  precedent.

## 0.5.5 — 2026-04-18 — Phase 5.2 item G: session rotation + cleanup

### Added
- `session_sweeper.py` — `SessionSweeper`: pure async policy module
  that runs a periodic TTL sweep over `SessionRegistry`. Every 6 h
  (hard-coded per spec R5) it iterates `_data` under the 5.1 lock,
  drops entries whose `last_active` is older than
  `SESSION_TTL_DAYS` (default 30), and — for `webhook:*` entries
  whose scope_id parses as a UUID (the one-shot pattern fabricated
  by `build_invoke_message`) — applies the shorter
  `WEBHOOK_SESSION_TTL_DAYS` (default 1). Non-UUID webhook scopes
  (e.g. deliberately-pinned `webhook:ha-automation-daily`) keep the
  standard TTL. Unparseable / missing `last_active` is treated as
  garbage and evicted.
- `_prune_sdk_session()` helper — forward-compat seam: `getattr`
  lookup of `claude_agent_sdk.delete_session`; no-op when absent
  (today), one-line flip when Anthropic's SDK grows it. Exceptions
  swallowed at DEBUG — the local eviction is source of truth.
- `casa_core._env_int_or` — clamping int-from-env helper matching
  `retry._env_int`'s shape; kept local until a second caller
  appears (item I will reuse it — §9.3), then promote to `env.py`.

### Changed
- `casa_core.main()` constructs a `SessionSweeper` immediately after
  the `SessionRegistry`, using env vars `SESSION_TTL_DAYS` and
  `WEBHOOK_SESSION_TTL_DAYS`. Sweeper starts alongside the
  APScheduler and stops during the shutdown sequence — before
  `channel_manager.stop_all()` — so any in-flight sweep completes
  before the registry quiesces.

### Tests
- `tests/test_session_sweeper.py` — 18 async tests across four
  classes. `TestEvictionPolicy` (9): active survive, expired
  evicted, inclusive-keep boundary, webhook UUID → short TTL,
  webhook non-UUID → standard TTL, non-webhook ignores webhook TTL,
  unparseable `last_active` evicted, no-evictions = no-save,
  one-info-log-per-pass-with-count. `TestConcurrency` (2):
  sweep + concurrent register preserves both; lock is genuinely
  held during the eviction critical section. `TestSdkSessionPrune`
  (3): forward-compat seam called when method present, no-op when
  absent, resilient to SDK exceptions. `TestLifecycle` (4): start
  schedules recurring sweeps, stop-before-start is safe, double
  start is idempotent, stop cleanly cancels the task.

### Not changed
- `SessionRegistry` public API is untouched. The sweeper uses
  underscore-prefixed attributes (`_lock`, `_data`, `_save_locked`)
  by design — the 5.1 internal-consumer seams.
- Sweep cadence is not on the env-var surface. Spec §9.3 lists
  only the two TTL knobs; the 6-h interval is hard-coded (R5: one
  pass over < 100 entries is cheap; adding a knob expands the
  support matrix for no operator benefit).
- No E2E shell scenario — a real TTL pass is days-scale; faking
  wall-clock from the harness is out of proportion. Matches item
  E / item H precedent.

## 0.5.4 — 2026-04-18 — Phase 5.2 item E: Telegram reconnect with backoff

### Added
- `channels/telegram_supervisor.py` — `ReconnectSupervisor`: pure async
  policy module that wraps a rebuild callback with 1s → 60s jittered
  exponential backoff (reuses `retry.compute_backoff_ms`). Retries
  forever per spec §4.2. Logs exactly one `ERROR` per outage and one
  `INFO` on recovery — not one line per attempt. Coalesces concurrent
  triggers (single-task design); idempotent `start()`; clean `stop()`.
- `TelegramChannel._rebuild()` — idempotent build-and-handshake: tears
  down any existing `Application` (best-effort; exceptions swallowed)
  then constructs, initializes, starts, and registers webhook or
  polling. Replaces the inline block that used to live in `start()`.
- `TelegramChannel._health_probe_loop()` — periodic `bot.get_me()`
  probe (`_PROBE_INTERVAL = 45s`, `_PROBE_TIMEOUT = 10s`). On
  `NetworkError` / `TimedOut` / `asyncio.TimeoutError`, triggers the
  supervisor. Non-transport exceptions are logged at DEBUG and the
  probe continues.
- `TelegramChannel._on_ptb_error` — registered via
  `Application.add_error_handler`. Routes `NetworkError` and
  `TimedOut` to the supervisor; other handler errors are logged at
  WARNING without triggering a rebuild.

### Changed
- `TelegramChannel.start()` no longer silently falls back from webhook
  to polling on `set_webhook` failure (that path was dead once the
  supervisor retries forever; it also downgraded a user who explicitly
  configured webhook). On `NetworkError` / `TimedOut` during initial
  bring-up, the supervisor takes over.
- `TelegramChannel.stop()` cancels the probe task and stops the
  supervisor in addition to the existing cleanup.

### Removed
- `_POLL_STALL_THRESHOLD` constant and `_poll_stall_watchdog` method —
  the old "watchdog" only refreshed its own timestamp and performed no
  actual detection. Replaced by `_health_probe_loop`.

### Tests
- `tests/test_telegram_supervisor.py` — 11 pure-asyncio tests for
  `ReconnectSupervisor` covering trigger/no-trigger, backoff on
  failure, unbounded retry, single error log per outage, single info
  log on recovery, state reset between outages, clean stop before and
  after start, idempotent start.
- `tests/test_telegram_reconnect.py` — 6 integration tests using the
  same `telegram.*` stub pattern as `test_telegram_split.py`. Covers
  initial `set_webhook` failure, probe failure, PTB error handler
  routing, non-transport errors ignored, full-cycle teardown, and
  log-once semantics at channel level.
- `tests/test_telegram_split.py` — stub module extended with
  `NetworkError` / `TimedOut` symbols (required by the new imports in
  `channels/telegram.py`).

### Not changed
- `_TYPING_BACKOFF_*` and `_TYPING_CIRCUIT_BREAK` remain as-is —
  orthogonal to reconnect (spec §4.3).
- No new env vars — reconnect schedule is hard-coded per spec §9.3.

## 0.5.3 — 2026-04-18 — Phase 5.2 item F: token budget monitoring (descoped — no cost estimate under Max)

### Added
- `tokens.py` — pure accounting module. Exports `estimate_tokens(text)`
  (`len(text) // 4`, treats `None`/`""` as 0), `extract_usage(result_msg)`
  (defensive read of `input_tokens / output_tokens / cache_read_input_tokens /
  cache_creation_input_tokens` off the SDK `ResultMessage`; missing or
  non-numeric values default to 0), `BudgetTracker` (per-`session_id`
  consecutive-overrun streak; emits one WARNING per session_id per
  process lifetime when the digest exceeds `token_budget * 1.1` for
  three turns in a row; under-budget turns reset the streak;
  `budget <= 0` short-circuits), and `format_turn_summary(role, channel,
  usage)` (renders `turn_done role=… channel=… input=… output=…
  cache_read=… cache_write=…`; cache fields kept separate so a
  `cache_write > 0` per-turn pattern surfaces as a stable-prefix
  regression).
- `Agent` instantiates a per-instance `BudgetTracker` in `__init__` so
  assistant (4000-budget) and butler (800-budget) keep independent
  warning state. After `memory.get_context` returns successfully,
  `Agent._process` records the digest size; the broken-memory branch is
  silent (no digest to measure).
- `Agent._process._attempt_sdk_turn` now captures `ResultMessage.usage`
  via `extract_usage` (resets per attempt — partial usage from a failed
  attempt cannot leak into the summary); after `retry_sdk_call`
  returns, emits one `turn_done` INFO line carrying the role, channel
  (or `-` when missing), and input/output/cache_read/cache_write token
  counts.
- `test-local/mock-claude-sdk` — `ResultMessage` gains an optional
  `usage: dict[str, int]` field populated from `MOCK_SDK_USAGE_INPUT`,
  `MOCK_SDK_USAGE_OUTPUT`, `MOCK_SDK_USAGE_CACHE_READ`,
  `MOCK_SDK_USAGE_CACHE_WRITE` (each defaults to 0). The `build/lib`
  copy is gitignored and regenerates from `setup.py`; only the
  source-tree mock is tracked.

### Descoped from spec
- **No `cost_estimate` and no `MODEL_PRICES` table.** Casa runs on a
  Claude Max subscription — Anthropic does not bill per token, so a
  USD `cost_est` log line would be theatre against list prices we
  don't pay. Operators wanting spend modelling can do it out-of-band
  against the same `turn_done` line. Spec §5.2 wording around cost is
  therefore not implemented.

### Changed
- (none — purely additive instrumentation; no env vars new per spec
  §9.3, no dashboard surface per spec §5.3.)

### Tests
- `test_tokens.py` — 23 unit tests across 4 classes (`TestEstimateTokens`,
  `TestExtractUsage`, `TestBudgetTracker`, `TestFormatTurnSummary`).
- `test_agent_process.py::TestTokenBudgetMonitoring` — 5 integration
  tests (memory recorder per turn, broken-memory skip, three-turn
  warning fires once, turn_done line carries usage, usage resets across
  retries).
- Full unit suite: 335 passed.

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
