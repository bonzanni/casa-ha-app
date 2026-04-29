# Changelog

## [0.20.0] - 2026-04-29

### Fixed
- **E-6 / E-10**: `_effective_caller_role()` priority flip — `engagement_var`
  now checked before `origin_var`. Configurator engagements can again call
  `config_git_commit` and `casa_reload` without falling back to raw `git
  commit` or "manual addon restart" workarounds.
- **E-7**: `_deliver_turn` binds `engagement_var` for the duration of the
  SDK loop. `emit_completion` now resolves the active engagement instead
  of returning `not_in_engagement`.
- **Bug 2 (sdk_session_id checkpoint timing)**: `InCasaDriver` now eagerly
  persists `sdk_session_id` to the registry the first time
  `client.session_id` becomes non-null, instead of waiting for the 24h
  idle sweeper. An unclean shutdown mid-turn can no longer orphan an
  engagement with `sdk_session_id: null`.

### Changed
- `InCasaDriver.__init__` gains a new keyword arg
  `persist_session_id: Callable[[str, str], Awaitable[None]] | None`
  (default `None`). The single caller (`casa_core.main`) is updated.
  Pre-1.0 minor bump to signal the API change.

### Internal
- New `tests/test_role_gate_priority.py` (3 tests).
- `tests/test_in_casa_driver.py` extended (4 tests).
- `tests/test_engage_executor_tool.py` extended (1 leak-detection test).

## [0.19.0] - 2026-04-29 — Phase 0 / E-11: persistent addon-config mount

**BREAKING — first boot of v0.19.0 wipes and reseeds the entire
`/addon_configs/casa-agent/` tree.**

The previous map declaration paired `addon_config:rw` with `config:ro`,
both of which target `/config` inside the container. HA Supervisor
silently dropped `addon_config:rw` (the conflict loser), so
`/addon_configs/casa-agent/` was never a real bind mount — it was a
rootfs-overlay path that got wiped on every container rebuild. Every
configurator commit, every manual edit under `/addon_configs/casa-agent/`,
every plugin-marketplace install state, and every git history entry
in the addon-config tree vanished on the next `ha apps restart`. See
`docs/bug-review-2026-04-29-exploration.md` § E-11 for the full
forensic write-up + live evidence (mount-table dump, boot-log seed
trail, git-history collapse).

### Changed (BREAKING)

- **`casa-agent/config.yaml::map`** — replaced
  `addon_config:rw` + `config:ro` with a single
  `all_addon_configs:rw` directive. The container now sees
  `/addon_configs/` as a real bind mount of the supervisor's
  addon-configs root (`/mnt/data/supervisor/addon_configs/`), which
  means `/addon_configs/casa-agent/` is finally a persistent
  per-addon subdir surviving container rebuilds.
- **First-boot reseed:** because the underlying mount source changes
  from rootfs-overlay to bind mount, the existing `/addon_configs/casa-agent/`
  contents are NOT migrated. `setup-configs.sh` re-seeds defaults on
  first boot of v0.19.0 (per its existing `[ ! -d "$dst" ]`
  idempotency gate). User-edited configs from prior versions (such as
  `runtime.yaml::enabled: true` flags, custom `character.yaml` traits,
  custom plugin installs, custom marketplace overlays, and the entire
  in-tree git history under `/addon_configs/casa-agent/.git/`) WILL
  be lost. Any post-v0.19.0 customizations made through the
  configurator engagement path or by manual SSH edits will persist.

### Removed

- **`casa-agent/apparmor.txt`** — removed the dead `/config/** r,`
  rule. Casa code has zero references to `/config/` (verified by
  grep across `casa-agent/rootfs/`). The rule existed only to
  service the dropped `config:ro` mount.

### Added

- **`casa-agent/apparmor.txt`** — added `/addon_configs/ r,` rule.
  Defensive: under the new bind mount, the parent dir
  `/addon_configs/` is a real mount point and `setup-configs.sh`'s
  `mkdir -p` calls need read access to stat it. The existing
  `/addon_configs/casa-agent/** rwk,` rule does not cover the parent.

### Verification

Live-N150 smoke (post-deploy):

1. `mount | grep addon_config` → expect a line of the shape
   `/dev/<X> on /addon_configs type <fstype>` (or a `bind` flag if
   docker-info verbose). Pre-fix this returned nothing.
2. Boot logs immediately after first `ha apps update` to v0.19.0 →
   expect six `Seeded agent dir: <name>` lines (assistant, butler,
   finance, configurator, hello-driver, plugin-developer) plus
   `Initialized config git repo at /addon_configs/casa-agent` — proof
   the seed path fired against an empty mount.
3. Restart twice (`ha apps restart`); on the second boot, the seed
   lines must NOT reappear (`[ ! -d "$dst" ]` is now true → no-op).
   Pre-fix every restart re-seeded; post-fix only the first does.
4. The user-edited `runtime.yaml::enabled: true` flag for finance
   that was set on 2026-04-29 morning is GONE — must be re-set via
   the configurator engagement path (or manual edit) post-deploy.
   This is expected per the BREAKING note above.

### Out of scope

This is Phase 0 of the bugfix roadmap. Phases 1-6 are tracked in
`docs/bug-review-2026-04-29-exploration.md` § "Suggested bugfix-roadmap shape".

### Memory hooks

After verification, add a memory entry summarizing the fix-shape
choice (Option B `all_addon_configs:rw` over Option A `/config`
repoint — see plan-doc rationale) and the live-deploy result. The
memory entry `reference_v0_18_1_addon_config_fixes` is now stale
(it referenced SHAs not present in master tip `04037d0`); revisit
it post-ship to either correct or remove.

## [0.18.2] - 2026-04-29 — Engagement setup_engagement_features() ordering fix

**Latent bug since v0.11.0 surfaced by v0.18.1.** Once `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID` started actually reaching `TelegramChannel.__init__` (v0.18.1 fix), `setup_engagement_features()` ran the bot-permission check at startup — but `self._app` was still `None` because `channel_manager.start_all()` hadn't fired yet. The probe failed with `'NoneType' object has no attribute 'get_me'`, leaving `engagement_permission_ok = False` permanently. Every `engage_executor` / `delegate_to_agent(mode="interactive")` then returned the misleading "set telegram_engagement_supergroup_id in addon" error.

### Fixed

- **`casa_core.py`** — `telegram_channel.setup_engagement_features()` is now called AFTER `channel_manager.start_all()`, not immediately after `register()`. The bot isn't built until `_rebuild()` runs inside `start_all()`. The deferred call is wrapped in try/except + ERROR-log to avoid blocking startup if the supergroup probe fails for an unrelated reason (e.g., Telegram API outage).

This was latent for ~7 months because v0.18.0 and earlier never actually exported `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID` to the env (v0.11.0 schema-write regression that v0.18.1 fixed). Operators who set the option still hit the no-op early-return at `setup_engagement_features` line 634; the bug only manifests once the env var actually reaches `TelegramChannel.__init__`.

## [0.18.1] - 2026-04-29 — Engagement supergroup env-export fix + log_level option

**Two operator-facing fixes discovered during M6 (v0.18.0) deploy verification.**

### Fixed

- **`telegram_engagement_supergroup_id` no longer ignored at runtime.**
  The `s6-overlay/s6-rc.d/svc-casa/run` script exported 4 of the 5
  `telegram_*` config options to env vars but missed
  `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID`. This caused
  `casa_core.py:1028` to read the empty env (default `"0"`),
  parse to `0`, and pass `engagement_supergroup_id=None` to
  `TelegramChannel`. Every engagement-tool call (`engage_executor`,
  `delegate_to_agent` with `mode="interactive"`) returned the
  "set telegram_engagement_supergroup_id in addon" error even when
  the option was correctly set in the addon configuration.
  Regression dates from v0.11.0 (engagement primitive ship); silent
  for ~7 months because the manual Telegram smoke probe in v0.11.0
  was performed by an operator who had also not yet set the option.
  Regression test in `tests/test_run_script_env.py` parameterizes
  every TELEGRAM_* env var the run script must export.

### Added

- **Operator-facing `log_level` addon option.** `list(debug|info|warning|error)?`
  with INFO default. Wired through `svc-casa/run` (null-normalized
  like `casa_tz` / `scope_threshold`) → `LOG_LEVEL` env var →
  `casa_core.py::install_logging(level=...)`. Operators can now flip
  to DEBUG via the HA UI without rebuilding the image.

## [0.18.0] - 2026-04-29 — Memory M6: cross-role recall

**Adds `consult_other_agent_memory(role, query)` — a read-only MCP
tool that lets a resident query another agent's accumulated
theory-of-mind of the user without delegating a full agent turn.**

### Added

- **`MemoryProvider.cross_peer_context`** — 4th method on the ABC
  at `memory.py`. `HonchoMemoryProvider` wraps Honcho v3's
  `peer.context(target=user_peer, search_query=query)` primitive;
  `NoOpMemory` and `SqliteMemoryProvider` return `""` per the
  graceful-degradation contract; `CachedMemoryProvider` is
  passthrough.
- **`_render_peer_context` helper** — renders `peer.context()`'s
  `peer_card` + `representation` shape under
  `## What {Observer} knows about you (cross-role)`.
- **`consult_other_agent_memory` MCP tool** — registered in
  `tools.py` and exposed via `CASA_TOOLS`. Validates role against
  the resident/specialist registry; structured-error strings on
  bad input.
- **`memory.cross_peer_token_budget`** — new field on resident
  `runtime.yaml::memory` (default 2000 when unset). JSON-schema
  additive update on `runtime.v1.json`.
- **System prompt — Ellen's `prompts/system.md`** — new
  "Cross-role memory recall" section teaches Case 1 (recall →
  this tool) vs Case 2 (factual lookup → `delegate_to_agent`).
- **Configurator doctrine** — `recipes/resident/create.md` and
  `recipes/resident/update.md` updated to teach the new tool +
  `cross_peer_token_budget` field.
- **`memory_call` telemetry** — new `call_type: "self" | "cross_peer"`
  field across all emission sites. New tool-side
  `consult_other_agent_memory_call` info line with role / query_len /
  result_len / t_ms.

### Changed

- **`MemoryProvider` ABC** — bumped from 3 methods to 4. Pre-1.0.0
  license invoked, no backward-compat shim. Out-of-tree providers
  (none today) would break loudly at import.
- **Ellen (`assistant/runtime.yaml::tools.allowed`)** — gains
  `mcp__casa-framework__consult_other_agent_memory`.
- **`memory_call` log line** — adds `call_type` field across 5
  emission sites in the same commit per arch-spec § 13 drift-risk
  warning.

### Trust posture

- Tina (`butler`) and Finance (specialist) ship WITHOUT the tool —
  guards Tina's guest-accessible voice channel and keeps
  specialist-to-specialist consultation as an operator-opt-in
  decision via Configurator. Regression tests at
  `tests/test_agent_loader.py` guard the omissions structurally.

### Spec / plan

- `docs/superpowers/specs/2026-04-29-memory-m6-cross-role-recall-design.md`
- `docs/superpowers/plans/2026-04-29-memory-m6-cross-role-recall.md`
- Live arch spec § 16: `docs/superpowers/specs/2026-04-26-memory-architecture.md`

## [0.17.2] - 2026-04-28 — Scheduled trigger silence (F1 follow-up)

**Fixes the v0.17.1 regression where every scheduled trigger fire
raised `ValueError` at session-id construction, plus the
longer-standing leak where Ellen's heartbeat emitted
acknowledgement-style first tokens into Telegram before her
silence-check completed.**

### Fixed

- **`trigger_registry.py:117`** — scheduled-trigger `chat_id` now
  hyphenates `{trig.type}-{trig.name}` instead of colon-joining, so
  `build_session_key` + `honcho_session_id` accept it. Eliminates the
  hourly `ERROR Agent 'Ellen' error [unknown]: part 1='interval:heartbeat'
  contains characters outside [A-Za-z0-9_-]` log line.

### Changed

- **`agent.py` `handle_message`** — `MessageType.SCHEDULED` turns no
  longer receive a `create_on_token` streaming callback. The agent
  thinks privately; only the final text is delivered. Other message
  types (`REQUEST`, `NOTIFICATION`, `RESPONSE`, `CHANNEL_IN`) are
  untouched.
- **`agent.py` `handle_message`** — sentinel-based silence gate for
  `SCHEDULED`: when the model returns `<silent/>` (exact match after
  `strip()`) or whitespace-only output, the send path is skipped and
  no `RESPONSE` BusMessage is emitted.
- **`defaults/agents/assistant/triggers.yaml`** — heartbeat prompt
  replaces the obsolete streaming warning with the
  `<silent/>` sentinel contract. Override rules and closing
  instructions unchanged.

### Tests

- `tests/test_trigger_registry.py::TestInterval::test_interval_chat_id_is_honcho_compliant`
  — roundtrip assertion that producer (trigger_registry) and validator
  (`honcho_session_id`) agree on shape.
- `tests/test_agent_process.py::TestScheduledSilence` (5 tests) —
  `create_on_token` count for SCHEDULED vs REQUEST, sentinel
  suppression, whitespace suppression, real-text passthrough.

### Not changed

- No deprecation shim for the colon-shaped `chat_id` (pre-1.0 license,
  per `feedback_pre_1_0_0_license`).
- No silent server-side sanitization in `honcho_session_id` — the
  v0.17.1 fail-fast doctrine stands.
- `morning-briefing.md` — sentinel is opt-in; prompts that always
  send simply never emit `<silent/>`.
- Voice channel user-supplied `scope_id` validation is followup, not
  blocker (see spec §7).

## [0.17.1] - 2026-04-28 — Honcho session-id format fix (F1)

**Fixes the 11-day silent Honcho-write bug discovered post-M4b deploy.**
Every Casa Honcho session-create has 422'd since v0.2.2 (2026-04-17)
because session ids contained `:`, which Honcho's server-side
`^[A-Za-z0-9_-]+$` regex rejects. Reads returned empty digests; writes
were dropped. Failures landed in `try/except → WARNING` so the bug
remained invisible until M4b's `peer_count: 0` telemetry pattern was
finally read as "writes never landed" rather than "fresh sessions".

### Added

- **`casa-agent/rootfs/opt/casa/honcho_ids.py`** — single canonical
  builder `honcho_session_id(*parts)`. Joins parts with `-` (hyphen),
  fail-fasts (`ValueError`) on inputs containing characters outside
  `[A-Za-z0-9_-]`. Strict-reject by design — silent sanitization is
  what blinded us for 11 days.
- **Regression integration test** in `tests/test_honcho_ids.py`
  asserting that the pre-fix colon shape WOULD have tripped Honcho's
  `string_pattern_mismatch` validator.

### Changed

- **All 11 Honcho session-id construction sites** flipped to call
  `honcho_session_id` instead of f-string concatenation:
  - `agent.py:332,552` (resident read/write)
  - `channels/voice/channel.py:454` (voice prewarm)
  - `tools.py:377` (coordinator meta write)
  - `tools.py:439` (M4b specialist)
  - `tools.py:1009` (executor archive read)
  - `tools.py:1326,1330` (engagement-finalize meta)
  - `tools.py:1379` (executor archive write)
  - `tools.py:1553` (query_engager)
- **`session_registry.build_session_key`** rewired through
  `honcho_session_id`. Output shape flipped from `{channel}:{scope_id}`
  to `{channel}-{scope_id}`. Now also accepts `int` `scope_id`
  (Telegram `chat_id`).
- **`session_sweeper`** partitions registry keys on `-` (was `:`).
  Pre-existing colon-shaped JSON entries fall through to the 30-day
  session TTL and age out — no migration shim per pre-1.0.0 license
  (zero data was ever persisted under the old shape; every server
  create 422'd since v0.2.2).

### Breaking

- **Channel-key on-disk format** (`{DATA_DIR}/sessions.json`) flipped
  from `{channel}:{scope_id}` to `{channel}-{scope_id}`. Pre-v0.17.1
  entries become orphans and age out via TTL — no operator action.
- **`build_session_key`** now rejects `scope_id` containing `:`,
  whitespace, or any character outside `[A-Za-z0-9_-]`. Previously
  preserved colons verbatim.

### Spec / doctrine

- `docs/superpowers/specs/2026-04-28-honcho-session-id-format-design.md`
  (new — design rationale, decision log, migration table)
- `docs/superpowers/plans/2026-04-28-honcho-session-id-format-fix.md`
  (new — task-by-task implementation plan)
- `docs/superpowers/specs/2026-04-26-memory-architecture.md` § 5/§ 14/§ 15
  swept to hyphen shape
- Configurator doctrine (`architecture.md`,
  `recipes/specialist/create.md`) swept

## [0.17.0] - 2026-04-28 — Memory M4b: Specialists become memory-bearing

Specialists (Tier 2 — Finance today; future Health/Personal/Business)
gain per-`(role, user_peer)` Honcho memory. One channel-agnostic,
scope-agnostic session per specialist accumulates messages, summary,
and `peer_representation` across all delegate-call channels.

### Added

- **Specialist memory read+write in `_run_delegated_agent`.**
  When `cfg.memory.token_budget > 0` and a memory provider is bound,
  `tools.py:_run_delegated_agent` opens a Honcho session keyed
  `f"{role}:{user_peer}"` (e.g. `finance:nicola`), fetches a digest
  via `get_context(search_query=task_text, tokens=…)`, and prepends
  a `<memory_context agent="{role}">…</memory_context>` block between
  `<delegation_context>` and `Task:`. After the SDK returns text, a
  background task writes the turn back via `add_turn(user_text=task_text,
  assistant_text=…)`. Failures fail-soft (WARNING log, no propagation).
- **`_specialist_meta_write_bg` — coordinator visibility.** Each
  `delegate_to_agent` call also writes a one-line summary to the
  parent's meta session (`{channel}:{chat_id}:meta:{parent_role}`),
  giving Ellen a unified view of specialist activity independent of
  which scope her own per-turn argmax write went to. Task and reply
  truncated to 200 chars per side.
- **Finance opted in by default.** `defaults/agents/specialists/finance/runtime.yaml`
  bumps `memory.token_budget` from 0 to 4000.

### Breaking

Pre-1.0.0 license per `feedback_pre_1_0_0_license.md`:

- `specialist_registry._validate_tier2_shape` no longer rejects
  specialists with `token_budget > 0`. Operators with stateless
  specialists (`token_budget: 0`) are unaffected; operators who set
  `token_budget > 0` will now see Honcho memory engaged.

### Internal additive (non-breaking)

- New module-level helpers in `tools.py`:
  - `_specialist_bg_tasks: set[asyncio.Task]` — GC anchor.
  - `_specialist_add_turn_bg(...)` — fail-soft background writer.
  - `_specialist_meta_write_bg(...)` — fail-soft meta-summary writer.
- `_build_specialist_options` docstring updated; SDK `resume=None`
  unchanged (memory enters via prompt injection, not SDK continuity).

### Architecture

- New 2-segment session id shape `f"{role}:{user_peer}"` joins the
  existing 4-segment `{channel}:{chat_id}:{scope}:{role}` topology.
  Specialists are channel-agnostic and scope-agnostic; both shapes
  are first-class to Honcho (sessions are id-opaque).
- Trust gating stays one level up at the resident's `delegates`
  decision — no per-call channel filter at the memory layer.

### Doctrine + spec

- **Configurator doctrine sync** (per
  `feedback_configurator_doctrine_sync.md`):
  - `recipes/specialist/create.md` — memory-bearing specialist example.
  - `recipes/specialist/update.md` — enable-memory recipe for an
    existing stateless specialist.
  - `architecture.md` — specialist memory subsection + correction to
    the v0.16.0 "stateless specialists" claim.
- **Live arch spec.** `docs/superpowers/specs/2026-04-26-memory-architecture.md`
  § 5 gains a 2-segment specialist-sessions paragraph, plus new § 15
  documenting the read path, write path, meta-scope coordinator
  visibility, and what's deferred to M5/M6.

### Deferred

- Specialist `peer_card` writes / `remember_fact` MCP tool → **M5**.
- Cross-specialist recall via `peer_perspective` → **M6**.
- `read_strategy: cached` for specialists.
- Multi-user (`user_peer != "nicola"`).

## [0.16.0] - 2026-04-27 — Memory M4: Engagement memory

Three layers, one user-visible behavior: engagement summaries flow back
into Ellen's per-turn memory and Configurator engages with prior
context.

### Added

- **L1 — `meta` declared as a system scope.** `policies/scopes.yaml`
  bumps to schema v2 with a new `kind: topical | system` field. System
  scopes are always-on after the trust filter — no embedding, no
  classifier routing. `meta` is the first system scope; assistant adds
  it to `scopes_readable`. Voice (Tina) is excluded by the
  `authenticated` trust gate.
- **L3 — Per-executor archive read at engage-start.** New
  `ExecutorMemoryConfig(enabled, token_budget)` on
  `ExecutorDefinition`; Configurator opts in. `engage_executor`
  interpolates a new `{executor_memory}` prompt slot from the
  per-(channel, chat, executor_type) Honcho session. `claude_code`
  driver-side `workspace.py` slot supported for forward-compat with
  future memory-enabled claude_code executors.
- **L4 — Free benefit.** `_finalize_engagement` already writes
  engagement summaries to the meta session for both specialist and
  executor engagements (since M2.G4, v0.15.3). L1 makes them readable
  on Ellen's normal turn. No new write code.

### Breaking

Pre-1.0.0 license per `feedback_pre_1_0_0_license.md`:

- `policies/scopes.yaml` schema bumped v1 → v2. No migration shim.
  Tenant overlays at `/addon_configs/casa-agent/policies/scopes.yaml`
  must be updated by the operator on upgrade.
- `defaults/schema/policy-scopes.v1.json` removed; replaced by
  `policy-scopes.v2.json`.

### Internal additive (non-breaking)

- `executor.v1.json` gains optional `memory` property; existing
  executor `definition.yaml` files without a `memory:` block remain
  valid (default disabled).

### Doctrine + spec

- **Configurator doctrine sync** (per
  `feedback_configurator_doctrine_sync.md`): `architecture.md`,
  `recipes/scopes/edit.md` (also fixes pre-existing list-style format
  drift), `recipes/executor/scaffold.md`,
  `recipes/resident/{create,update}.md` updated in the same commit
  set.
- **Architecture spec**
  (`docs/superpowers/specs/2026-04-26-memory-architecture.md`): § 5,
  § 6, § 11 updated; new § 14 "Engagement memory"; § 12 M4 entry
  flipped to "Shipped v0.16.0".

### Deferred to future ships

- L2 — Specialists become memory-bearing → M4b (separate brainstorm).
- Synthesized "lessons learned" archive content → future tweak after
  real archive usage patterns emerge.
- `remember_fact` via directional `peer_card` → M5.
- Cross-role recall (`consult_other_agent_memory`) → M6.
- `HONCHO_LIVE_TEST=1`-gated integration test → M3a.1 follow-up
  bundle.

## [0.15.4] - 2026-04-27 — Memory M3: Honcho contract coverage + `memory_call` telemetry

Observability + confidence-coverage release. No runtime-behaviour
changes; closes the M2-era spec § 9 "real-Honcho-response coverage
not in tests today" gap and adds per-memory-call telemetry.

### Added
- **M3a — Honcho populated-response integration test.**
  `tests/test_memory_honcho.py::test_get_context_renders_summary_and_peer_repr_when_honcho_returns_them`
  primes the SDK stub with populated `summary.content` +
  `peer_representation` + `peer_card` + recent `messages` and asserts
  all four `_render` sections appear in canonical order. Closes the
  spec § 9 wiring-coverage gap. Live `HONCHO_LIVE_TEST=1`-gated test
  deferred as M3a.1 follow-up.
- **M3b — `memory_call` info-level log line.** Emitted from each
  concrete provider's `get_context` (Honcho + SQLite) and from
  `CachedMemoryProvider`'s cache-hit branch. Fields: `backend`,
  `session_id`, `agent_role`, `t_ms`, `peer_count`,
  `summary_present`, `peer_repr_present`, `cache_hit`. NoOp provider
  intentionally silent — see new spec § 13 for the full contract.
- **Spec § 13** (`docs/superpowers/specs/2026-04-26-memory-architecture.md`)
  documents the `memory_call` field set and emission rules.

### Migration
- None. M3 adds log lines and tests; no schema, config, or runtime
  contract change. Operators relying on a regex-style log scrape that
  asserted "no `memory_call` lines exist" would need to update —
  vanishingly unlikely.

## [0.15.3] - 2026-04-26 — Memory M1+M2: spec consolidation + Honcho-side fixes

First user-visible memory ship since v0.8.4. Folds the internal-only M1
cleanup (no version bump at the time) into the same release as M2's
three Honcho-touching bug fixes.

### Added (M1)
- `docs/superpowers/specs/2026-04-26-memory-architecture.md` —
  consolidated current-state spec for the memory subsystem. Supersedes
  2.2a/2.2b/3.2/3.2.1/3.2.2 design specs for "what is true today"
  purposes.

### Removed (M1)
- `card_only` read strategy. Reserved in 2.2a, never implemented; the
  branch in `_wrap_memory_for_strategy` warned and fell back to
  `per_turn`. No default YAML used it.
- SQLite-side `peer_cards` table + reader. No code ever wrote to it;
  the deferred `remember_fact` tool stays a Honcho-only feature per the
  graceful-degradation doctrine.
- `archive_session_full` executor field. Parsed and stored but no
  reader. Plan 4a transcript archival fires unconditionally on
  `kind=executor`.

### Fixed (M2)
- **Voice prewarm cache key restored.**
  `channels/voice/channel.py::_prewarm` was building the pre-3.2
  3-segment session id `voice:{scope_id}:{role}`. The agent's read
  path uses 4 segments `{channel}:{chat_id}:{scope}:{role}`, so the
  prewarm cache key never matched the real-turn key — every wake-word
  paid the full cold-read latency. Now loops over the agent's
  `scopes_readable` and warms one entry per scope using the 4-segment
  shape with budget // len(scopes) tokens each.
- **Cancel + force-delete now write engagement summaries.**
  `cancel_engagement` and `delete_engagement_workspace(force=True)`
  passed `memory_provider=None` to `_finalize_engagement`, silently
  skipping the meta-scope summary write and the per-executor-type
  Honcho archival. Both sites now resolve `active_memory_provider`
  from the `agent` module the same way `emit_completion` does.
  Cancellations and force-deletes leave the same Honcho trace as
  normal completions.
- **`query_engager` reads from the engager's actual scope.**
  `tools.py:1357` reads `engagement.origin.get("scope", "meta")`;
  `agent.py` never set `"scope"`, so Tina's `query_engager("what did
  the user say…")` always retrieved from Ellen's meta scope — which
  only contains engagement summaries, never user conversation. The
  agent now stamps `argmax_scope(scores, default_scope)` onto
  `origin_var` after the read-path classifier runs, so engagements
  spawned during a turn carry the scope the turn was rooted in.

### Migration
- M1 migration notes still apply: existing SQLite databases keep their
  now-orphan `peer_cards` table (harmless, no longer read); existing
  `definition.yaml` files with `archive_session_full: ...` will fail
  schema validation — delete the line.
- M2: no migration. Voice prewarm change is transparent (cache hits
  start working again). Cancel-path memory writes are additive (Honcho
  gets entries it was missing). G6 stamps a new `scope` key onto
  `engagement.origin` — code reading `origin` with `.get(..., default)`
  is unaffected; any code doing exact-equality dict comparison would
  need updating but no such site exists.

## [0.15.2] - 2026-04-26 — Heartbeat noise + sweeper crash

Two production bugs visible in `addon_c071ea9c_casa-agent` logs.

`engagement_idle_sweep` (cron 08:00 daily) and `workspace_sweep`
(interval 6h) were registered as `lambda: asyncio.create_task(...)`
in `casa_core.py`. APScheduler's `AsyncIOExecutor` runs sync callables
in a worker thread, so `asyncio.create_task` raised
`RuntimeError: no running event loop` on every fire — silently no-op
since v0.13.0. Fix: pass the coroutine functions directly with
`kwargs={...}`; AsyncIOExecutor schedules them on the loop natively
(same pattern `trigger_registry._register_scheduled` already uses).

Ellen's `heartbeat` trigger fires every 60min and was producing
chatty "checking in" messages despite the prompt's "stay quiet"
instruction. The Telegram channel runs in `stream` mode — the *first
token* posts a new chat message, so any preamble Ellen drafts before
deciding to stay silent has already gone out. Rewrite the prompt:
silence is now framed as the default action, the bar for sending is
explicit and narrow, and a "no preamble, no reflection text" rule
forbids the first-token leak.

### Fixed

- `casa_core.py:1506,1519` — `engagement_idle_sweep` and
  `workspace_sweep` jobs now register the coroutine function
  directly. Adds `tests/test_scheduled_sweeper_jobs.py` to lock
  the wiring (would have caught this since v0.13.0).
- `defaults/agents/assistant/triggers.yaml` heartbeat prompt
  rewritten — silence-first framing, explicit "what NOT to send"
  list, no-preamble rule.

## [0.15.1] - 2026-04-26 — Tina HA control

Tina (butler) becomes the universal Home Assistant operator. Server-level
grant to the homeassistant MCP gives her every Assist tool the user has
exposed; new prompt sections teach her how to use them; Ellen's
delegates.yaml gains a butler entry so the Telegram-via-Ellen path
("ask Tina to turn off the lights") works end-to-end. Closes the
v0.15.0 deferred manual smoke.

### New

- `mcp__homeassistant` server-level grant in `defaults/agents/butler/runtime.yaml` —
  every HA Assist tool callable from Tina, present and future, no
  enumeration required.
- Three new prompt sections in `defaults/agents/butler/prompts/system.md`:
  `## Home Assistant tools`, `## Intent patterns`, `## Error recovery`.
- `butler` entry in `defaults/agents/assistant/delegates.yaml` so Ellen's
  `<delegates>` block advertises Tina and `delegate_to_agent("butler", ...)`
  passes the role-map gate.
- `CASA_HA_MCP_URL` env override on `casa_core.py` — defaults to
  `http://supervisor/core/api/mcp`. Used by e2e to point HA traffic at
  the mock.
- Mock HA MCP server at `test-local/e2e/mock_ha_mcp/server.py` — minimal
  JSON-RPC 2.0 implementation with `HassTurnOn`/`HassTurnOff`/
  `GetLiveContext` and `/_calls`/`/_reset` test side-channels; rejects
  unknown tool names with `-32602`.
- Mock SDK file-driven HTTP MCP tool-invoke hook
  (`MOCK_SDK_TOOL_INVOKE_FILE`) — lets tier-2 e2e exercise the
  resident-options → SDK → HTTP MCP transport chain without a live model.
- Tier-2 e2e `test-local/e2e/test_ha_delegation.sh` — H-0..H-3 covering
  the CASA_HA_MCP_URL flow, the voice-direct path, and the
  agent_loader → SDK options chain.
- Configurator doctrine recipe `recipes/resident/grant_ha_tools.md`.

### Notes

- HA integration must be enabled and entities exposed to default Assist
  pipeline by the user — Casa cannot configure these.
- "Trust the model fully" decision recorded in spec §6 — no per-tool /
  per-domain restrictions. Safety guardrails (irreversible actions
  behind confirmation read-back) tracked as future roadmap item.
- Tier-2 e2e exercises butler→HA directly via the mock-SDK hook;
  the Ellen→delegate_to_agent→butler two-hop chain stays covered by the
  J.5 manual smoke (live SDK on N150).

## [0.15.0] - 2026-04-25 — Resident-to-resident delegation

Residents can now delegate to other residents and specialists by role
via the new `delegate_to_agent` MCP tool. Lifts the previous "Ellen is
the only delegator" architectural constraint.

### New

- `delegate_to_agent(agent=<role>, task=, context=, mode={sync,async,interactive})` —
  unified delegation tool. Resolves `agent` against a merged role map of
  residents + specialists. `mode=interactive` is rejected for residents.
- `<delegates>` and `<executors>` system-prompt blocks rendered at turn
  time from each resident's `delegates.yaml` / `executors.yaml`. Closes
  the long-standing dead-data bug where `cfg.delegates` was loaded but
  never reached the model.
- `<delegation_context>` block prepended to delegated calls so target
  agents can adapt voice/text register.
- New `executors.yaml` (assistant-only) — `configurator`,
  `plugin-developer`, and `engagement` entries moved out of
  `delegates.yaml`.
- `agent_registry` module: name↔role bidirectional map for prompt
  rendering and future code paths.

### Breaking (no back-compat alias; pre-1.0.0)

- `delegate_to_specialist` removed; replace with `delegate_to_agent`.
- `mcp__casa-framework__delegate_to_specialist` removed from
  `runtime.yaml::tools.allowed` allowlists; replace with
  `…delegate_to_agent`.
- Configurator doctrine updated; recipes wire/unwire generalized.

### Behavioral

- Single-hop depth cap: a delegated agent cannot itself call
  `delegate_to_agent` (returns `delegation_depth_exceeded`). Trivially
  relaxable via the `_MAX_DELEGATION_DEPTH` constant in `tools.py`.

### Out of scope (separate specs)

- HA-control plugin / Tina's tool inventory.
- Cross-channel sending.
- Multi-hop chaining.

## [0.14.12] - 2026-04-25

Log-noise sweep — four fixes surfaced by a live N150 log audit
(2026-04-25). All changes target log signal/noise; no behavior shifts
beyond the heartbeat-delivery one called out below.

### Fixed

- **Telegram channel**: `chat_id` validation. The `context["chat_id"]`
  slot is overloaded — user-initiated messages carry a numeric Telegram
  chat id, but scheduled triggers carry session-keying labels like
  `"interval:heartbeat"`. The Telegram API rejects non-numeric values
  with `BadRequest: Chat not found`, which used to bubble through
  `finalize_stream → send` and surface as a full traceback at the bus
  dispatcher. New `_resolve_chat_id` helper falls back to the channel's
  registered default when the value isn't numeric. **Behavioral note:**
  hourly heartbeats now actually deliver to the registered chat instead
  of silently failing — if the agent prompt's "stay quiet" instruction
  isn't honored, the user will see hourly pings. Tune the prompt if so.
- **CC CLI transcript persistence**: `setup-configs.sh` now symlinks
  `/root/.claude/projects` to `/addon_configs/casa-agent/cc-home/.claude/projects`
  on boot. The bundled CC CLI uses `$HOME=/root → ~/.claude/projects/`,
  but `/root/` is wiped on every container rebuild, so the SDK's
  `--resume <sid>` path failed on every first turn after a deploy
  (visible as `claude_agent_sdk._internal.query: Fatal error in message
  reader` for `voice:probe-scope` and `telegram:interval:heartbeat`).
  One-time migration on first boot copies any pre-existing transcripts
  into the persistent location before replacing the dir with a symlink.
- **Empty `s6-rc-compile` at boot**: `replay_undergoing_engagements`
  used to call `_compile_and_update_locked()` unconditionally, which
  printed `source /data/casa-s6-services is empty` to stderr at every
  boot when no claude_code engagements were active. Now early-returns
  when both `undergoing` and `removed_orphans` are empty — the
  engagement sources dir is unchanged, so a compile would be wasted.
- **`svc-nginx/finish` and `svc-ttyd/finish`**: gate the `bashio::log.warning`
  on exit codes 0 and 256, mirroring the existing pattern in
  `svc-casa-mcp/finish`. Code 0 = clean stop (s6 told it to); code 256
  = s6 "do-not-restart" sentinel. Anything else still surfaces.

### Files

- `casa-agent/rootfs/opt/casa/channels/telegram.py` — `_resolve_chat_id`
  helper + 3 call-site updates (`send`, `create_on_token`, `finalize_stream`).
- `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh` — projects
  dir symlink with first-boot migration.
- `casa-agent/rootfs/opt/casa/casa_core.py` — `replay_undergoing_engagements`
  fast path.
- `casa-agent/rootfs/etc/s6-overlay/s6-rc.d/svc-nginx/finish` and
  `.../svc-ttyd/finish` — exit-code gate.

## [0.14.11] - 2026-04-25

Test tiering — Half 1. Re-groups existing CI tests into a three-tier
structure so trivial PRs get sub-2-minute "is the system on fire"
feedback while hardening (timing/chaos) tests run nightly + on-demand.
No runtime / addon code changes; CI plumbing + test-file rearrangement
only.

### CI

- **`.github/workflows/qa.yml`** rewritten as `tier1-smoke` (every push
  + PR + nightly + manual, ~7-8 min cold-cache) / `tier2-functional`
  (push + PR + manual, ~12 min, parallel with tier 1) /
  `baseline-runtime` (unchanged, parallel with tier 2) /
  `tier3-hardening` (nightly + manual only). Tier 1 has no `needs:`
  gating against tier 2 — contributors get fail-fast smoke signal in
  parallel with the full functional sweep.
- **Nightly cron** at 04:00 UTC. Nightly skips tier 2 (already verified
  on the master push that landed the changes); runs tier 1 + tier 3.
- **Manual `workflow_dispatch`** runs all three tiers from any branch.
- **D-block + P-block CI steps** stay commented out, deferred to the
  pre-existing v0.14.10 D/P-block sweep follow-up. Their split scripts
  exist (so the sweep can re-enable them by uncommenting one block) but
  do not run in CI today.

### Tests

- **`test-local/e2e/test_engagement.sh` split into 3 files**
  (1859 lines → 3 self-contained scripts):
  - `test_engagement_E.sh` (~944 lines): E-0..E-10 Tier-2 specialist +
    Configurator. Tier 2.
  - `test_engagement_D.sh`: D-1..D-12 claude_code driver lifecycle.
    Tier 3. Requires `CASA_USE_MOCK_CLAUDE=1`; skips cleanly otherwise.
  - `test_engagement_P.sh`: P-1..P-9 plugin-developer harness. Tier 2.
    Requires `CASA_USE_MOCK_CLAUDE=1` + `CASA_PLAN_4B=1`; skips
    cleanly otherwise.
- **`start_mock_telegram_server` helper** added to
  `test-local/e2e/common.sh`; replaces the inline E-0 spawn block.
- **Checkpoint count** preserved across the split (sum of `^pass "`
  lines in the 3 new files = original + 1; the +1 is the new
  `pass "P-block container healthy"` boot line because P now boots its
  own container, where it previously reused D's).

### Removed

- **`test-local/e2e/test_migration.sh`** (57 lines) — asserted seeded
  YAML markers for behavior the v0.7.0+ pre-1.0 wipe-on-update doctrine
  explicitly does NOT do. Same fate v0.9.1 gave `test_heartbeat.sh`.
  `git log -- test-local/e2e/test_migration.sh` recovers it post-1.0
  if migrations are reintroduced.
- **`test-local/Makefile::test-migration`** target dropped.

### Build

- **`test-local/Makefile`** gains `test-tier1`, `test-tier2`,
  `test-tier3`, `test-all`; legacy `test`/`test-smoke`/`test-runtime`/
  `test-voice` targets retained.

## [0.14.10] - 2026-04-25

v0.14.9 follow-up: enable seeded plugins after seed-copy. The v0.14.9
seed-copy populates cc-home with 5 default plugins but they all carry
`enabled: false` (CC CLI's `--cache-dir`-mode install at build doesn't
auto-enable). The binding layer (`plugins_binding.py::build_sdk_plugins`)
filters out `enabled: false` entries — so engagements were getting
`plugins=[]` even though all 5 plugins were structurally present.
Live verification on N150 v0.14.9 caught this: `claude plugin list
--json` showed 5/5 plugins, all `enabled=False`.

### Fixed

- **`setup-configs.sh` seed-copy block** now runs `claude plugin enable
  <ref>` for each of the 5 default plugins after the cc-home seed-copy
  completes. Runtime `--scope user` enable persists the flag in
  cc-home's `installed_plugins.json` and is idempotent (returns clean
  on a no-op re-run because of the `|| true` and `>/dev/null 2>&1`).

- **`test-local/init-overrides/01-setup-configs.sh`** mirrors the same
  enable loop so the local e2e build matches production.

### Tests

- **`test_invoke_sessions.sh::C-4`** strengthened to assert `5/5`
  (total/enabled), not just `5` (total). Plain count-check would have
  passed on v0.14.9 even with all-disabled plugins; this catches the
  binding-layer's enabled-filter contract explicitly.

## [0.14.9] - 2026-04-25

Unified github access. Replaces the five distinct mechanisms across
build / boot / Configurator runtime / plugin-developer engagement with
one path: a system-level `/etc/gitconfig` (SSH→HTTPS rewrite + a
credential helper) plus `$GITHUB_TOKEN` propagated at addon-wide scope
via `/run/s6/container_environment/GITHUB_TOKEN`.

### Added

- **`/etc/gitconfig`** ships in the image. Contains an SSH→HTTPS
  insteadOf rewrite for github.com (no token) and a `credential.helper`
  pointing at `/opt/casa/scripts/git-credential-casa.sh`. Applies
  system-wide regardless of which user or HOME the process runs under.

- **`/opt/casa/scripts/git-credential-casa.sh`** — stateless POSIX shell
  helper. Reads `$GITHUB_TOKEN` from process env at request time, emits
  `username=x-access-token\npassword=...` on stdout. Token never
  written to any config file. Includes CR/LF strip on token to prevent
  malformed credential responses if `op read` emits trailing newlines.

### Changed

- **`setup-configs.sh`** resolves `op://${ONEPASSWORD_DEFAULT_VAULT}/GitHub/credential`
  at boot via `bashio::config` + `op read`, then writes the token to
  `/run/s6/container_environment/GITHUB_TOKEN` (mode 0600). s6-overlay
  merges this into every supervised service's environment, so casa-main,
  svc-casa-mcp, every engagement subprocess, and every `git`/`gh`/`claude
  plugin install` invocation inherits the same token automatically.

- **`setup-configs.sh`** seed-copies cc-home plugin state from the
  image-baked `/opt/claude-seed/` on first boot (idempotent — sentinel
  is `installed_plugins.json` in cc-home). Replaces the v0.14.8 boot
  install loop. Symlink-based — CC CLI tolerates `installPath` via
  symlink (verified by spike D.1 on N150). No network access required
  at boot for the 5 default plugins.

- **`Dockerfile`** pairs each `claude plugin install` with a
  `claude plugin enable` so the image-baked seed has `enabled: true`
  for all 5 default plugins. Without this, the seed-copy would
  preserve `enabled: false` from the build (CC CLI's `install` does
  not auto-enable).

### Removed

- **v0.14.8 boot install loop** in `setup-configs.sh` (the
  `claude plugin install <ref>` loop with `flock`-serialised stderr
  capture). Default-plugin install state now comes from the seed-copy.

- **`_resolve_plugin_developer_github_token`** in
  `drivers/claude_code_driver.py` and the per-engagement
  `extra_env["GITHUB_TOKEN"]` injection. The token is in the
  addon-wide environment and inherited automatically.

- **`Dockerfile`'s per-USER `git config --global url.X.insteadOf`**
  for the `casa` build user. The same rewrite ships in `/etc/gitconfig`,
  which applies to every USER.

### Tokenless mode

If `onepassword_service_account_token` or `onepassword_default_vault`
is unset/null, OR if `op read` fails for any reason, `GITHUB_TOKEN`
stays unset. Casa runs in **public-only mode**: all anonymous github
clones still work via `/etc/gitconfig`'s SSH→HTTPS rewrite; private-repo
clones return 404/403; plugin-developer's `gh repo create` fails (logged
at engagement scope). No secret material leaks anywhere.

### Notes

- Pre-1.0.0 wipe-on-update: no migration. On addon update,
  `/opt/claude-seed/` is rebuilt fresh in the new image; cc-home is
  refilled by the seed-copy on next boot.

- The unified path is verified by the v0.14.9 spike findings on N150
  (2026-04-25): all four spikes passed against the production
  `op://Casa/GitHub/credential` with the proposed credential-helper
  pattern.

## [0.14.8] - 2026-04-25

Boot-time fix — register the seed marketplace alongside the user
marketplace so default plugins actually install. Caught from the
N150 boot log: every default plugin in
`defaults/agents/**/plugins.yaml` was logging
`WARNING: plugin install skipped: <name>@casa-plugins-defaults`
and `claude plugin list --json` returned `[]`, so the binding
layer (`/opt/casa/plugins_binding.py::build_sdk_plugins`) handed
no plugins to engagements at all.

### Fixed

- **Seed marketplace was never registered with the CC CLI.**
  `setup-configs.sh` only ran
  `claude plugin marketplace add /addon_configs/casa-agent/marketplace/`
  (the user-writable overlay). The read-only seed at
  `/opt/casa/defaults/marketplace-defaults/` — which is where every
  `<name>@casa-plugins-defaults` install ref resolves — was missing
  from the install loop's environment, so all five default plugins
  (`document-skills`, `mcp-server-dev`, `plugin-dev`, `skill-creator`,
  `superpowers`) failed to install with
  `Plugin "<name>" not found in marketplace "casa-plugins-defaults"`.
  Added a second `claude plugin marketplace add` for the seed dir
  immediately before the install loop. Idempotent (`|| true` on
  re-register).

### Diagnostic

- **Surface the CC CLI's stderr in the install warning.** Replaced
  `>/dev/null 2>&1 || bashio::log.warning "plugin install skipped: $ref"`
  with `install_err=$(... 2>&1 >/dev/null) || bashio::log.warning
  "plugin install skipped: $ref — $install_err"`. Future install
  failures stay diagnosable instead of cryptic.

### CI clean-up surfaced by this ship

v0.14.1 was the first Plan 4b commit to enable CI jobs that hadn't
run before (`CASA_USE_MOCK_CLAUDE=1` D-block, `CASA_PLAN_4B=1` P-block).
Every Plan 4b master push since has been red in ways that were not
being tracked, because the unit job failed first and hid everything
downstream. Unblocking CI here surfaced four pre-existing bugs that
also ship fixed in this bump:

- **`drivers/s6_rc.py::service_pid` used the wrong `s6-svstat` flag.**
  `-u` prints the literal `true`/`false` up status; `-p` prints the
  supervised PID. The code asked for `-u` and parsed as `int()`, so
  `service_pid()` always returned `None` and
  `ClaudeCodeDriver.is_alive_async()` always reported every engagement
  as dead. Flipped to `-p`. Shipped since v0.13.0 (2026-04-23).

- **Mock SDK `ClaudeAgentOptions` missing `plugins=` field.**
  v0.14.1's binding-layer wiring in `agent.py` and `tools.py` passes
  `plugins=build_sdk_plugins(...)` into every SDK construction. The
  test-only mock dataclass had no such field, so every resident /
  specialist / executor turn raised `TypeError` on the mock, the SDK
  session id was never captured, and `/data/sessions.json` stayed
  empty — breaking the Invoke-sessions E2E. Added `plugins` to the
  mock dataclass. (Matches `reference_mock_sdk_drift` memory: v0.5.9
  precedent — new kwargs MUST be mirrored into the mock same commit.)

- **Py 3.11+ tarfile raises `AbsoluteLinkError` not "symlink".**
  `tests/test_system_requirements_installer_tarball.py::test_symlink_
  member_rejected` used `pytest.raises(UnsafeArchiveError,
  match="symlink")` but the message is wrapped from
  `tarfile.data_filter`'s "link to an absolute path". Broadened the
  regex to accept either phrasing. This is what turned master CI red
  on every Plan 4b commit; this is the fix.

- **D-block `s6-svstat -u` parse bugs in `test_engagement.sh`.** D-1,
  D-4 cancel, and D-13 restart survival all invoked `s6-svstat -u`
  and parsed stdout as `int`. D-1 / D-13 fixed (D-13 switched to
  `-p`; D-1 parses `"true"` as boolean). D-4 parses `"false"` for
  down.

### Known limitation — CI D/P block disabled for v0.14.8

Plan 4a D-block (`CASA_USE_MOCK_CLAUDE=1`) and Plan 4b P-block
(`CASA_PLAN_4B=1`) are **intentionally disabled** in `.github/workflows/
qa.yml` for this ship. They were authored without ever running on
Linux CI — D-2 alone surfaces a further JSONL-glob mismatch, and
D-3..D-8 / P-1..P-9 are unverified. Sweeping them properly exceeds
this ship's scope. Tracked for **v0.14.9 follow-up**: run D/P block
locally against real Linux s6/mock CLI behaviour, fix each harness,
re-enable the CI env vars in one go.

Plan 2 E-block (E-0..E-10) still runs in every qa.yml e2e-fast run,
which continues to verify engagement primitives end-to-end.

## [0.14.7] - 2026-04-25

Bug-review v0.14.6 follow-up — closes Bug 10, the only finding from
`docs/bug-review-2026-04-24.md` deferred from v0.14.6 because it
needed a locking design rather than a surgical patch.

### Reliability

- **Telegram `handle_update` topic-status race (Bug 10).** aiohttp
  dispatched each Telegram update as its own task, so a `/cancel`
  arriving alongside a regular turn could race: the regular turn
  passed `rec.status` while the cancel was mid-finalize, then routed
  to a driver that `_finalize_engagement` had just torn down (driver
  raised `DriverNotAliveError` or the turn landed on a closed topic).
  Fixed with a per-topic `asyncio.Lock` keyed by `message_thread_id`
  on `TelegramChannel._engagement_handler_locks`, mirroring the
  `in_casa_driver._locks: dict[id, Lock]` idiom. Updates landing on
  the same topic now serialise; different topics still run in
  parallel. Three new tests in
  `tests/test_telegram_engagement_routing.py::TestHandleUpdateConcurrencyRace`
  exercise the cancel-vs-turn race, the two-regular-turns drop-
  resistance case, and cross-topic parallelism (deadlock-detection).

### CHANGELOG cleanup

- Collapsed the inadvertent duplicate `## [0.14.6]` heading from
  commit `615eac1` into a single section; moved the `Removed` /
  `Migration` blocks below `Tests` so they sit in the same v0.14.6
  body as the other notes.

## [0.14.6] - 2026-04-25

Bug-review v0.14.6 — security and correctness sweep against findings
from `docs/bug-review-2026-04-24.md`. No new features; all changes
are surgical fixes with regression tests.

### Security

- **block_dangerous_bash regex bypass.** Replaced flat regex matcher
  with an argv-aware checker that splits on shell separators
  (`;`, `&&`, `||`, `|`, `&`), shlex-parses each piece, and recurses
  into `bash -c "..."` / `sh -c "..."`. Variants that previously
  bypassed the safety hook (`rm -r -f`, `rm --recursive --force`,
  `rm -rfv`, `rm -fR`, `/usr/bin/rm -rf`, `bash -c "rm -rf /"`,
  `; rm -rf /`) are now all blocked. Verified live on N150 v0.14.5
  before the fix.
- **Tarball zip-slip / symlink-escape (`system_requirements/tarball.py`).**
  `tarfile.extractall` and `zipfile.extractall` now validate every
  member up front: symlinks/hardlinks/devices/fifos refused, and any
  member whose resolved path leaves the extract dir is rejected. Uses
  the `data` filter on Python 3.11.4+, falls back to manual member
  validation on the production 3.11.2 runtime. Also: the `extract:`
  field is resolved-path-checked, and unsafe URL schemes (`file://`,
  `ftp://`, `jar:`) are refused before download.
- **Tarball `install_cmd` shell injection.** `install_cmd` is now an
  argv list (`list[str]`) only — `subprocess.run(..., shell=True)` is
  gone. Backwards-incompatible with any marketplace entry that supplied
  a shell string (the first-party manifest does not).
- **Workspace `extra_dirs` shell injection.** Each entry must be an
  absolute path with no shell-special characters
  (`; | & ` ` $ < > ' "` newline / null). Values are still
  `shlex.quote`'d at render time.
- **Workspace `extra_env` key injection.** Keys must match
  `^[A-Z_][A-Z0-9_]*$` (the same convention used by
  `plugin_env_conf.py`); a newline or `$(...)` in the key would
  otherwise escape the rendered `export` line.
- **`casa_reload` / `config_git_commit` defense in depth.** Both tools
  now verify the calling agent's role (`origin_var` for SDK path,
  `engagement_var.role_or_type` for engagement-bridge path) and refuse
  unless it's `configurator`. Pre-fix they relied solely on each
  agent's `runtime.yaml::tools.allowed`, which is a single point of
  failure if a permissive default sneaks into a new role.
- **Telegram `/cancel` and `/complete` are originator-only.** Pre-fix
  any user in the engagement supergroup could fire either command and
  terminate someone else's engagement. Bus context now carries
  `user_id`, propagates through `origin_var` → `engagement.origin`,
  and the slash-command handler refuses unless `from_user.id` matches.
  `/silent` stays open (local to the topic). Legacy engagements with
  no `user_id` in origin still work.

### Reliability

- **`emit_completion` idempotency.** Re-emitting completion (SDK retry
  / hook misfire) is a recognised no-op now: the second call returns
  `{"status": "acknowledged", "kind": "already_terminal"}` without
  re-NOTIFYing Ellen, re-closing the topic, or re-writing the
  meta-scope summary into Honcho.
- **`delete_engagement_workspace` covers `idle`.** The live-state
  guard previously checked only `"active"`; an idle engagement
  (SDK-suspended after 24h) had its s6 service still running, but a
  non-`force` delete still tore down the workspace under it. Now both
  `active` and `idle` require `force=true`.
- **`_tail_file` follows log rotation.** The s6-log 1 MB rotation of
  `/var/log/casa-engagement-<id>/current` no longer drops the new
  file's content. Tracks `st_ino`; on inode change resets `pos = 0`.
  Also resets if the file shrinks below `pos` (truncate-in-place).
- **`ClaudeCodeDriver.start` rolls back on failure.** If
  `provision_workspace`, `write_service_dir`, `_compile_and_update`,
  or `start_service` raises, the partial workspace + s6 service dir +
  s6-rc compile are best-effort cleaned up before the original
  exception is re-raised. No more orphan UNDERGOING ghosts that the
  sweeper skips forever and that boot replay tries to resurrect.
- **Invalid `casa_tz` no longer crashes every turn.** `resolve_tz()`
  catches `ZoneInfoNotFoundError`, logs a warning naming the bad
  value, falls back to `Europe/Amsterdam`. `lru_cache` does not cache
  exceptions, which pre-fix meant every single turn re-raised.

### Tests

- 37 new regression tests covering all of the above. The live N150
  bypass for `block_dangerous_bash` is captured directly in
  `test_rm_recursive_force_all_blocked`; the rest mirror their bug
  preconditions one-for-one.

### Removed

- **`github_token` addon option.** Plugin-developer now resolves
  `op://${onepassword_default_vault}/GitHub/credential` directly at
  engagement-spawn time. Vault is configurable via
  `onepassword_default_vault`; item title (`GitHub`) and field label
  (`credential`) are conventional. One fewer addon option to configure;
  1P is the single source of truth.

### Migration

- Users with `github_token` set in addon options: remove the entry,
  then ensure your 1P vault contains a `GitHub` item with a
  `credential` field holding a GitHub PAT (`repo` scope).

## [0.14.5] - 2026-04-24

### Fixed
- **N150 turn-assistant failure after plan 4b:** `assistant/runtime.yaml`
  had `cwd: /addon_configs/casa-agent/workspace` (legacy), but F.2 dropped
  the `mkdir -p workspace/...` block from setup-configs.sh. SDK spawn
  failed with `CLIConnectionError: Working directory does not exist`.
  Change cwd to empty so B.4 agent-home fallback takes effect.

## [0.14.4] - 2026-04-24

### Removed
- **Partial `ellen/` and `tina/` default agent dirs** (created by Plan 4b B.7
  with only `plugins.yaml`). These are plan-hypothetical agents not yet
  implemented; their partial dirs failed `agent_loader` required-file check
  in CI (`missing required file runtime.yaml`). Delete cleanly — can be
  re-added when the agents are fully specified.

## [0.14.3] - 2026-04-24

### Fixed
- **N150 boot crash on v0.14.2:** `assistant/delegates.yaml` plugin-developer
  entry used `{executor_type, description, typical_task, engagement_mode}`
  shape but `delegates.v1.json` schema only accepts `{agent, purpose, when}`.
  Rewrote the entry in the correct shape. All 4 delegate entries now match
  the schema.

## [0.14.2] - 2026-04-24

### Fixed
- **N150 boot crash on v0.14.1:** `agent_loader._check_file_set` rejected
  `plugins.yaml` (added by Plan 4b B.7) as "unknown file", crashing
  casa-main with exit 1 before SDK clients spawned. Added `plugins.yaml`
  to the `optional` file set for resident, specialist, and executor tiers.

## [0.14.1] - 2026-04-24

### Added
- **plugin-developer executor** (Tier 3 claude_code driver) that authors
  Claude Code plugins in dedicated per-plugin GitHub repos. Default plugin
  pack: superpowers + plugin-dev + skill-creator + mcp-server-dev +
  document-skills. Produces 100% CC-native plugins — installable into Casa
  agents via Configurator OR into any regular CC session.
- **Two-marketplace model** — `casa-plugins-defaults` (seed-managed, read-only,
  ships with the image) + `casa-plugins` (user-writable via Configurator).
- **Binding layer** at `/opt/casa/plugins_binding.py` — resolves
  `enabledPlugins` → `plugins=[{type:"local",path:...}]` for in_casa agents
  via `claude plugin list --json::installPath`. SDK does not auto-consume
  plugins; this closes the gap.
- **Workspace-template** pattern for claude_code executors
  (`defaults/agents/executors/<type>/workspace-template/` rendered into
  every engagement workspace).
- **Seven Configurator MCP tools** — `marketplace_add_plugin` /
  `marketplace_remove_plugin` / `marketplace_update_plugin` /
  `marketplace_list_plugins` / `install_casa_plugin` (two-stage commit) /
  `uninstall_casa_plugin` / `verify_plugin_state`.
- **`casa.systemRequirements`** — tarball/venv/npm install strategies
  into `/addon_configs/casa-agent/tools/`. apt/dpkg declarations rejected
  at add-time (§4.3.2).
- **Boot-time reconciler** — idempotent, non-blocking; records status to
  `system-requirements.status.yaml`.
- **self_containment_guard** pre-push hook policy — greps for hardcoded
  non-baseline paths, "please install X manually" README strings,
  `apt install` in shell scripts.
- **Universal 1P resolver** — all password-typed addon options accept
  `op://vault/item/field`. `op` CLI installed at image build.
- **`github_token` addon option** (required for plugin-developer).
- **Self-containment axiom** (§2.0) codified — plugins fully operational
  on fresh Casa install solely by marketplace-add + install_casa_plugin.

### Removed

- `repos:` addon option + `sync-repos.sh` script. This was a half-built
  scratch-sync mechanism with no runtime consumer (§9 of Plan 4b spec).
  **Migration:** users with non-empty `repos:` entries must remove them
  from the addon config before upgrading. No data migration needed.
- `/opt/casa/claude-plugins/` symlink tree (Tier 1/2 bundled plugins).
  Replaced by seed-managed `casa-plugins-defaults` marketplace.

### Changed
- Resident SDK construction now sets `cwd=/addon_configs/casa-agent/agent-home/<role>/`
  and injects `plugins=[...]` from the binding layer. `"Skill"` added to
  `allowed_tools` automatically.
- Claude_code engagement subprocesses inherit `CLAUDE_CODE_PLUGIN_SEED_DIR=
  /opt/claude-seed` + `CLAUDE_CODE_PLUGIN_CACHE_DIR=
  /addon_configs/casa-agent/cc-home/.claude/plugins`.
- Casa-main HOME moved from `/root` to `/addon_configs/casa-agent/cc-home/`.

### Notes
- Pre-1.0 wipe-on-update doctrine — no migration code shipped.
- Plan 4b spec: `docs/superpowers/specs/2026-04-24-3.5-plan4b-plugin-developer.md`.
- Plan: `docs/superpowers/plans/2026-04-24-3.5-plan4b-plugin-developer.md`.

## [0.14.0] — Phase 3.6 — `casa-framework` MCP extraction

### Added
- `svc-casa-mcp` — new s6-rc-supervised standalone service (s6 service
  files at `etc/s6-overlay/s6-rc.d/svc-casa-mcp/`, Python entry at
  `rootfs/opt/casa/svc_casa_mcp.py`). Listens on `127.0.0.1:8100`,
  serves `POST /mcp/casa-framework` (JSON-RPC 2.0) and `POST /hooks/resolve`,
  forwards every request to casa-main over a Unix domain socket at
  `/run/casa/internal.sock`.
- Casa-main second `aiohttp.AppRunner` on the Unix socket exposing
  `POST /internal/tools/call` and `POST /internal/hooks/resolve`. New
  helper `start_internal_unix_runner()` in `casa_core.py`.
- New module `mcp_envelope.py` — JSON-RPC envelope helpers + tool schema
  translation, shared between svc-casa-mcp and the public-port fallback.
- New module `internal_handlers.py` — pure aiohttp handler factories
  bound to the Unix socket and consumed in-process by the public-8099
  fallback.
- `CASA_FRAMEWORK_MCP_URL` and `CASA_HOOK_RESOLVE_URL` env-var overrides
  for ops-time port redirection.
- E2E coverage: `test-local/e2e/test_mcp_restart_survival.sh` (D-13)
  proves bouncing casa-main does not drop engagement-subprocess MCP
  connections; new D-11 + D-12 blocks in `test_engagement.sh` exercise
  svc-casa-mcp on port 8100.

### Changed
- `drivers/workspace.py` `.mcp.json` writer now points at
  `127.0.0.1:8100/mcp/casa-framework` for newly-provisioned workspaces
  (was 8099). Existing pre-v0.14.0 workspaces unaffected.
- `scripts/hook_proxy.sh` default URL bumped from 8099 → 8100 with
  `CASA_HOOK_RESOLVE_URL` env override.
- Casa-main public port 8099 continues to serve `/mcp/casa-framework`
  and `/hooks/resolve` as a back-compat fallback for pre-v0.14.0
  workspaces. Removed in v0.14.2 or later (one-release migration).

### Removed
- `casa-agent/rootfs/opt/casa/mcp_bridge.py` — logic split between
  `mcp_envelope.py`, `internal_handlers.py`, `svc_casa_mcp.py`, and
  `casa_core.py`'s public-fallback wrappers. Net coverage unchanged.
- `tests/test_mcp_bridge.py` — coverage migrated to
  `tests/test_mcp_envelope.py`, `test_internal_handlers.py`,
  `test_svc_casa_mcp.py`, and `test_public_fallback_routes.py`.

### Notes
- Restart-survival semantics are Level 1 only: mid-restart tool calls
  return `casa_temporarily_unavailable`; the model handles retry. No
  buffering, no replay, no idempotency guarantees beyond what individual
  tool handlers already provide.
- The pre-existing v0.13.1 known limitation (per-executor hook params
  on the HTTP path use factory defaults) is unchanged in v0.14.0 — that
  wiring is a later item.

## [0.13.1] — 2026-04-23

### Added
- **MCP JSON-RPC 2.0 HTTP bridge at `POST /mcp/casa-framework`.** Real
  `claude` CLI subprocesses can now reach Casa MCP tools via the in-process
  bridge. `X-Casa-Engagement-Id` request header binds `engagement_var` for
  the tool call's duration; missing/unknown id binds `None` and tools that
  guard on engagement context return `not_in_engagement`. GET returns 405.
  Stateless (no session, no SSE). New module: `mcp_bridge.py` (244 LoC).
- **`X-Casa-Engagement-Id` header** written into per-engagement `.mcp.json`
  by `provision_workspace` so the CC CLI forwards it on every `tools/call`.
- **Workspace sweeper.** APScheduler job every 6 hours removes
  `/data/engagements/<id>/` for COMPLETED/CANCELLED engagements past
  `retention_until` (default 7 days from terminal transition).
  `_finalize_engagement` writes `.casa-meta.json` with terminal status +
  retention at terminal-transition time for claude_code driver engagements.
- **Three new MCP tools** exposed on both the SDK path and the HTTP bridge:
  - `list_engagement_workspaces(status?)` — enumerate workspaces with
    status + size, truncated at 100 entries.
  - `delete_engagement_workspace(engagement_id, force=false)` — delete
    a workspace; refuses UNDERGOING without `force=true`.
  - `peek_engagement_workspace(engagement_id, path?, max_bytes?)` —
    read-only tree listing or file read with path-traversal guard.
- **Boot-replay heal path.** When an UNDERGOING engagement's s6 service
  dir is missing and the executor type is in the registry,
  `replay_undergoing_engagements` re-renders the run + log/run scripts
  and re-plants the dir (workspace must still exist — missing workspace
  stays warn-and-skip per §7.3 of the 4a.1 spec). Missing executor →
  warn-and-skip. Takes new optional `executor_registry` kwarg.
- **MCP-blip spike harness** at `test-local/spike/mcp_blip/` — throwaway
  aiohttp server + driver script that simulates mid-`tools/call` connection
  loss to empirically classify CC's MCP client as pessimistic (retries) or
  optimistic (no retry). Runs on N150, not CI. Result feeds the
  Plan 4b / 3.6 decision.

### Changed
- **`HOOK_POLICIES` refactored** from `{name: factory_returning_HookMatcher}`
  to two-tier `{name: {"matcher": regex, "factory":
  factory_returning_HookCallback}}`. The SDK path builds the `HookMatcher`
  once, at `resolve_hooks` time; the new HTTP path reuses the same raw
  `HookCallback`. Four `_policy_*` thin-wrapper helpers dropped; four
  slimmer `_*_factory` helpers replace them.
- **`/hooks/resolve`** replaces the v0.13.0 pass-through stub with real
  policy enforcement: `block_dangerous_bash`, `path_scope`,
  `casa_config_guard`, `commit_size_guard` all produce real deny/allow
  decisions for `claude_code` engagements. Returns CC-native
  `{"hookSpecificOutput": {...}}` shape. Defensive matcher regex re-check
  before dispatch. Callback exceptions return deny, not fail-open.
- **`CASA_TOOLS`** extracted to a module-level tuple in `tools.py` for
  iteration by both the SDK server and the HTTP bridge. Adding a tool to
  the tuple exposes it on both transports automatically.

### Fixed
- **`scripts/hook_proxy.sh` port 8080 → 8099.** Casa binds on 8099; the
  stale shim would have always failed open to "casa unreachable". The
  v0.13.0 stub handler hid this bug; flipping to real enforcement without
  the port fix would have wedged all engagements behind the fail-open
  path.

### Spike findings
- (Fill in from `test-local/spike/mcp_blip/README.md` Result section
  after running the spike on the N150. Include the 1-line verdict:
  `retry_observed=yes|no`, and the ROADMAP implication for 3.6 /
  Plan 4b.)

### Known limitations
- Per-executor hook parameters (e.g. `casa_config_guard.forbid_write_paths`)
  on the HTTP path use factory defaults — the Configurator's defaults
  happen to match what that executor wants. Wiring per-executor YAML
  params into the HTTP path is a later item.

## [0.13.0] — 2026-04-23

### Added
- **Plan 4a — `claude_code` driver.** Replaces the v0.11.0 stub.
  Per-engagement s6-rc-supervised `claude` CLI process (instead of
  Casa-main child) — engagement subprocesses outlive Casa-main restarts.
  New modules: `drivers/s6_rc.py`, `drivers/workspace.py`,
  `drivers/hook_bridge.py`, `scripts/hook_proxy.sh`,
  `scripts/engagement_run_template.sh`.
- **Remote control infrastructure.** Each engagement posts its
  `--remote-control` URL to the Telegram topic when it becomes available;
  users can attach via Claude iOS app or claude.ai/code and drive the
  engagement from anywhere.
- **Tier 1 baseline plugin pack.** `superpowers@v5.0.7` bundled at
  `/opt/casa/claude-plugins/base/superpowers/`. Symlinked into every
  `claude_code`-driver engagement's isolated `$HOME` at provisioning.
- **`hello-driver` test harness executor type.** `enabled: false`;
  validates the driver lifecycle in CI via mock CLI.
- **Boot replay for UNDERGOING engagements.** `replay_undergoing_engagements`
  in `casa_core.py` sweeps orphan service dirs, recompiles the s6 db,
  starts each UNDERGOING engagement's service, and spawns URL-capture +
  respawn-poller tasks.
- **Transcript archival to Honcho keyed by executor type.** Retrofits
  the already-shipped v0.12.0 Configurator — every engagement's completion
  summary lands under peer `executor:<type>` for future "Ellen primes a
  new engagement with past lessons" (Plan 4b+).
- **`/hooks/resolve` loopback endpoint.** Routes CC hook decisions through
  Casa's `HOOK_POLICIES` registry via `hook_proxy.sh` — same policy code
  governs `in_casa` and `claude_code` executors.
- **Sensitive-env blocklist.** The per-engagement `run` script unsets
  `TELEGRAM_BOT_TOKEN` / `HONCHO_API_KEY` / `WEBHOOK_SECRET` /
  `SUPERVISOR_TOKEN` / `HASSIO_TOKEN` before spawning the CLI.
  `CLAUDE_CODE_OAUTH_TOKEN` is preserved (CLI needs it). Future sensitive
  vars must be added to this list in the same commit.

### Changed
- `ExecutorDefinition` gains four optional fields: `extra_dirs`,
  `mirror_chat_to_topic`, `archive_session_full`, `plugins_dir`.
- `engage_executor` now dispatches to the `claude_code` driver for
  `driver: claude_code` executor types instead of raising NotImplementedError.
- `_finalize_engagement` now routes `driver.cancel()` to the per-engagement
  driver based on `engagement.driver`.

### Infrastructure
- Dockerfile clones superpowers at build time (adds ~30 MB to image).
- `setup-configs.sh` pre-creates `/data/casa-s6-services/` and
  `/data/engagements/`.

### Notes
- §10.2 of the design spec — `emit_completion` landing during a Casa-main
  restart's ~30s MCP blip is a known sharp edge in v0.13.0. If Plan 4a.1
  spike-milestone-3 discovers the CLI's MCP client is optimistic (silently
  drops the call on connection loss), ROADMAP 3.6 (`casa-framework` MCP
  extraction to its own s6 service) is re-prioritized as a co-requisite
  before Plan 4b's `plugin-developer` ships. Until then: accept-the-gap.
- `/hooks/resolve` endpoint routes policies through a pass-through stub
  at the HTTP boundary; HOOK_POLICIES values are SDK HookMatcher factories
  not directly HTTP-callable. Real enforcement via in-process hook
  callbacks still works. Future iteration will ship an HTTP-native policy
  layer.
- TelegramChannel now skips InCasaDriver's resume/orphan logic for
  `claude_code` engagements (which have no `sdk_session_id`).
- **MCP HTTP bridge deferred to Plan 4a.1.** The `casa-framework` MCP server
  is currently an in-process SDK server (via `create_sdk_mcp_server`) with
  no HTTP surface. `ClaudeCodeDriver` writes `.mcp.json` pointing at
  `http://127.0.0.1:8099/mcp/casa-framework`, but that route is not yet
  implemented. The v0.13.0 infrastructure (s6-rc, workspace, boot replay,
  hook bridge, hello-driver) is fully reviewed and green in CI via the
  mock CLI, but a real `claude` CLI subprocess cannot yet reach the Casa
  MCP tools. Plan 4a.1 will add an aiohttp MCP JSON-RPC bridge at
  `/mcp/casa-framework` and propagate engagement context via an
  `X-Casa-Engagement-Id` request header so `emit_completion` /
  `query_engager` can resolve the calling engagement.

## 0.12.0 — 2026-04-??

### Added — Phase 3.5 Plan 3: UC1 Configurator

- First Tier 3 Executor type: configurator - knows Casa's configuration surface and CRUDs it via engagement topic.
- ExecutorRegistry + ExecutorDefinition + agent_loader.load_all_executors.
- executor.v1.json JSON schema.
- Three new MCP tools: config_git_commit, casa_reload (Supervisor addon restart), casa_reload_triggers(role) (in-process).
- TriggerRegistry.reregister_for(role, triggers, channels) - soft-reload primitive.
- Two new hook policies: casa_config_guard (blocks /data/, /schema/, /opt/casa/, resident deletion) and commit_size_guard (ask above N files).
- engage_executor real implementation (was stub in v0.11.0).
- TELEGRAM_BOT_API_BASE env override in channels/telegram.py - retires Plan 2's deferred e2e coverage.
- Configurator defaults at defaults/agents/executors/configurator/: definition.yaml, prompt.md, hooks.yaml, observer.yaml + 20 doctrine markdown files (~3000 lines).
- Ellen prompt updates: runtime.yaml (engage_executor allowlisted), delegates.yaml (configurator entry), prompts/system.md (Configuration requests section).
- Setup-configs.sh + test override: seed agents/executors/ subtree.
- DOCS.md: "Configurator (v0.12.0)" section.
- E2E: test_engagement.sh E-1..E-8 fleshed (Plan 2 deferred cleared) + E-9 happy path + E-10 hook-blocked.
- Manual smoke: test-local/smoke/test_configurator_engagement.sh.
- Addon option: telegram_bot_api_base (default empty).

## 0.11.0 — 2026-04-22 — Engagement primitive + Tier 2 Specialist interactive mode

### Added

- **Engagements — bounded conversational threads in a Telegram forum supergroup.**
  New addon option `telegram_engagement_supergroup_id` binds Casa to a
  dedicated supergroup; each engagement spawns a forum topic via
  `createForumTopic`. See DOCS.md "Engagements" section for setup.
- **`delegate_to_specialist(mode="interactive")`**. New branch: instead of
  one-shot sync/async invocation, opens an engagement topic where the
  specialist (e.g. Alex) works with the user turn-by-turn. Completion is
  agent-driven via the new `emit_completion` tool; the user can end early
  via `/complete` or `/cancel` in the topic.
- **`engage_executor` MCP tool** (stub — returns `kind=no_executor_types`
  until Tier 3 types land in Plan 3+). Wires Ellen for the future
  engage flow; Plan 3 fleshes out with the configurator executor type.
- **`query_engager` MCP tool** — specialist-side retrieval. Bounded LLM
  synthesis over the engager's scope-filtered memory; returns `unknown`
  when context is insufficient.
- **`emit_completion` MCP tool** — specialist-side completion funnel.
  Publishes a structured summary (`text`, `artifacts`, `next_steps`),
  closes the topic (✅ icon), writes the summary to Ellen's meta-scope
  memory, and NOTIFIES Ellen for in-main-chat narration.
- **`cancel_engagement` MCP tool** — Ellen-callable. Tears down the
  driver and finalizes the record.
- **Observer module.** Static classifier + rate limiter (3 per engagement)
  + `/silent` per-engagement override. Trigger events (errors, warnings,
  idle-detected, unknown query_engager) run a bounded haiku-class LLM
  pass that may NOTIFY Ellen to interject in the main 1:1 chat.
  Per-type YAML override arrives with Plan 3.
- **Idle + suspension scheduler.** New APScheduler job
  (`engagement_idle_sweep`, daily 08:00) emits `idle_detected` bus
  events after 3 days of no user turn (specialists; 7 days for
  executors — Plan 3+); weekly re-fire. Live SDK clients torn down
  after 24h idle with `sdk_session_id` persisted for seamless resume
  on next user turn.
- **`in_casa` driver** (full impl) and **`claude_code` driver stub**
  (raises `NotImplementedError`, Plan 5 fills in).
- **Slash commands** `/cancel`, `/complete`, `/silent` registered in the
  engagement supergroup via `setMyCommands` for in-UI discoverability.
- **Addon option** `telegram_engagement_supergroup_id` (int?, 0 = disabled).

### Infrastructure

- New `casa-agent/rootfs/opt/casa/engagement_registry.py` — mirrors
  `specialist_registry.py` pattern. Persists live records to
  `/data/engagements.json`; finished records drop from disk (Ellen's
  meta-scope memory is the durable log).
- New `casa-agent/rootfs/opt/casa/drivers/` subpackage: `driver_protocol.py`,
  `in_casa_driver.py`, `claude_code_driver.py`.
- Ellen's shipped `runtime.yaml` + `delegates.yaml` + `prompts/system.md`
  updated to explain engagements and the new tools.
- Mock Telegram Bot API server at `test-local/e2e/mock_telegram/server.py`
  used by the new `test_engagement.sh` (CI).
- Manual Telegram smoke at `test-local/smoke/test_telegram_engagement.sh`
  exercises the real Bot API; not in CI — run pre-N150 deploy.
- `.github/workflows/qa.yml` adds the engagement e2e step.

### Breaking — acceptable pre-1.0.0

- `init_tools` signature adds a new kwarg `engagement_registry`. Internal
  to Casa; no external consumers.

### Deferred

- Tier 3 executor types (configurator, ha-developer, plugin-developer)
  — Plans 3, 4, 5.
- Per-type `observer.yaml` override — Plan 3.
- `claude_code` driver implementation — Plan 5.
- `next_steps` auto-chain by Ellen — Plan 3 (no Tier 3 types to chain to yet).
- Engagement topic archival/housekeeping — Plan 6+.
- `test_engagement.sh` E-1..E-8 checkpoints — scaffolded but not functional;
  flesh in follow-up commits as `TELEGRAM_BOT_API_BASE` override lands.

### Version

- `casa-agent/config.yaml`: `0.10.0` → `0.11.0`.

## 0.10.0 — 2026-04-22 — Rename: Tier 2 "Executor" → "Specialist"

Preparation for Phase 3.5 engagement primitive + Tier 3 Executors (see
`docs/superpowers/specs/2026-04-22-3.5-engagement-and-executors.md` §10).
The "Executor" term shipped in v0.6.2 is renamed to "Specialist" to
free the name for the ephemeral, task-bounded Tier 3 agents coming in
Plan 2. Zero behavior change — pure terminology refactor.

### Breaking (acceptable pre-1.0.0)

- **Directory:** `/addon_configs/casa-agent/agents/executors/` →
  `/addon_configs/casa-agent/agents/specialists/`. Migration on first
  boot under v0.10.0 is by convention — the overlay is wipe-acceptable
  per the pre-1.0.0 doctrine. An empty `agents/executors/` directory
  is now reserved for Plan 2+ Tier 3 Executor types.
- **MCP tool:** `mcp__casa-framework__delegate_to_agent` →
  `mcp__casa-framework__delegate_to_specialist`. Tool argument key
  `agent=...` → `specialist=...`. Error kind `unknown_agent` →
  `unknown_specialist`. Ellen's shipped `runtime.yaml` tool allow-list
  updated accordingly.
- **Python imports:** `from executor_registry import ExecutorRegistry` →
  `from specialist_registry import SpecialistRegistry`. Internal to
  Casa — affects nobody outside the codebase.

### Code

- `executor_registry.py` → `specialist_registry.py` (class
  `ExecutorRegistry` → `SpecialistRegistry`).
- `agent_loader.py`: `load_all_executors` → `load_all_specialists`;
  `TIER_FILES["executor"]` → `TIER_FILES["specialist"]`; `_DELEGATE_MCP_TOOL`
  constant updated; all error messages updated; `load_all_agents` now
  skips BOTH `specialists/` (Tier 2 home) and `executors/` (reserved
  for Plan 2 Tier 3).
- `tools.py`: `delegate_to_agent` handler → `delegate_to_specialist`;
  `_executor_registry` state var renamed; `_build_executor_options` →
  `_build_specialist_options`; `_run_executor` → `_run_specialist`;
  `init_tools` signature updated.
- `casa_core.py`, `agent.py`: import updates, variable renames,
  comment sweep.
- `defaults/agents/executors/` → `defaults/agents/specialists/`
  (including `finance/`). Finance prompt and character card updated.
  Ellen's character card and `runtime.yaml` tool allow-list updated.
  `defaults/schema/agent.v1.json` meta-doc updated to match the new
  TIER_FILES key.
- `setup-configs.sh` + `test-local/init-overrides/01-setup-configs.sh`:
  seed `agents/specialists/` from defaults; reserve empty
  `agents/executors/` for Plan 2.

### Tests

- `tests/test_executor_registry.py` → `test_specialist_registry.py`.
- `tests/test_delegate_to_agent.py` → `test_delegate_to_specialist.py`.
- `tests/test_agent_loader.py`, `test_agent_process.py`, `test_config.py`,
  `test_get_schedule_tool.py`, `test_notification_handling.py`,
  `test_casa_core_agent_loading.py`: reference updates.
- `test-local/mock-claude-sdk/claude_agent_sdk/__init__.py`: comment update.
- `test-local/e2e/test_delegation.sh` → `test_specialist_delegation.sh`;
  fixture dir `test-local/fixtures/delegation-enabled/agents/executors/`
  → `agents/specialists/`. `.github/workflows/qa.yml` updated to the
  new script name.

### Freed

- `agents/executors/` is now reserved for Tier 3 Executor types, arriving
  in Plan 2 (engagement primitive). Empty in v0.10.0.

## 0.9.1 — 2026-04-22 — Drop dead pre-v0.7.0 heartbeat config

### Removed

- **`heartbeat_enabled` / `heartbeat_interval_minutes` addon options.**
  Zero runtime consumers — the global heartbeat block was removed in
  v0.7.0 (Phase 4.x refactor, replaced by per-agent
  `agents/<role>/triggers.yaml`). Since then the options have been
  visible in the HA UI but had no effect. Removed from `config.yaml`
  (both `options:` and `schema:` blocks), `DOCS.md` Features table,
  `translations/en.yaml`, `test-local/options.json.example`, and the
  `test-local/init-overrides/03-export-env.sh` export loop.
- **`e2e-slow` nightly CI job + `test-local/e2e/test_heartbeat.sh`.**
  Same v0.7.0 rot — the test referenced `defaults/webhooks.yaml` and a
  top-level `schedules.yaml`/`heartbeat:` block that no longer exist.
  Also dropped the `schedule: cron "0 4 * * *"` workflow trigger and
  the `test-slow` Makefile target. (Landed earlier today on master in
  commit `2ffa4a6`; called out here for completeness.)

### Changed

- **DOCS.md "How it works" bullet 5** rewritten from the
  global-heartbeat narrative to the current per-agent trigger
  architecture.

### Migration

- **Pre-1.0.0, no migration block.** `/addon_configs/casa-agent/` is
  wipe-acceptable; if a user had explicit `heartbeat_enabled: ...` in
  their options YAML, the HA UI will surface it as "unused option" on
  next restart and they can delete it. Nothing in the runtime depended
  on the value.

## 0.9.0 — 2026-04-21 — Phase 3.3: Scheduling v2 + builder-first config ergonomics

### Added

- **`get_schedule` framework tool** on `casa-framework` MCP server.
  Returns the caller's own upcoming interval + cron triggers as a
  markdown bullet list within a configurable `within_hours` window
  (default 24, clamped to [1, 720]). Own-role visibility only.
- **Unified `<field>_file:` prose externalization idiom.** New shared
  `_resolve_prose` helper in `agent_loader.py` reads either an inline
  YAML field or a relative markdown file under the agent dir. Applies
  `_substitute_env` so external prompts see the same env-var
  substitutions as inline strings. Path traversal + non-`.md`
  extension rejected at load time.
- **Schema support** for `prompt_file` and `card_file` alternatives
  via `oneOf` branches in `character.v1.json` and `triggers.v1.json`.
- **APScheduler hardening**: explicit timezone (`resolve_tz()`:
  `CASA_TZ` → `TZ` → `Europe/Amsterdam`), `misfire_grace_time=600`,
  `coalesce=True`, `max_instances=1`. Restart-safe and wall-clock
  correct.
- **`<current_time>` system-prompt block** — every agent turn gets an
  ISO-8601 timestamp with weekday, time-of-day, and ISO week number
  injected into the composed system prompt. Same timezone source as
  the scheduler.
- **`casa_tz` addon option** in `config.yaml`. Default
  `Europe/Amsterdam`. Propagated to Python via `CASA_TZ` env var.
- **`TriggerRegistry.list_jobs_for(role, within_hours)`** public method
  backing the tool.
- **Seeded defaults**: `assistant/prompts/system.md`,
  `butler/prompts/system.md`, `executors/finance/prompts/system.md`
  — system prompts extracted from inline. `assistant/triggers.yaml`
  gains `morning-briefing` cron at `"0 8 * * 1-5"` Europe/Amsterdam
  using `prompt_file: prompts/morning-briefing.md`.

### Changed

- `_build_character` and `_build_triggers` take `agent_dir` kwarg for
  relative-path resolution.
- `init_tools` in `tools.py` takes a new optional
  `trigger_registry` kwarg.
- Scheduler + trigger registry construction moved ahead of `init_tools`
  call in `casa_core.py` so the tool has a live registry reference.
- `_check_file_set` now skips subdirectories inside an agent dir (so
  `prompts/` doesn't trigger the unknown-file guard).

### Migration

Pre-1.0.0 doctrine: no migration script. Existing
`/addon_configs/casa-agent/agents/*/character.yaml` files using
inline `prompt:` still validate and load. Users who want to benefit
from markdown-editable system prompts can either delete the overlay
(next boot re-seeds the updated defaults) or hand-move their prompt
to `<agent>/prompts/system.md` and switch `character.yaml` to
`prompt_file:`.

## 0.8.6 — 2026-04-21 — Pre-1.0.0 migration cleanup

Codebase slimming pass. Removes every version-migration block in
`setup-configs.sh` + the matching test-mode override + the v0.8.5
existing-instance e2e scenario + a pre-2.2a lazy-migration `.pop` in
`SessionRegistry`. Net -303 lines across the branch.

Driver: **pre-1.0.0 doctrine.** Casa is in full development mode
until v1.0.0. `/addon_configs/casa-agent/` is expected to be wiped
between addon updates; breaking changes ship by updating the shipped
defaults, not by migrating user state. Migration blocks + `.applied`
markers + `.pre-vX.Y.Z.bak` backups are over-engineering at this
stage — v0.8.5 proved it: the scope-corpus migration block shipped
with v0.8.5 never fired on the N150 deploy because the overlay was
fresh on update; seed-if-missing produced an identical outcome.

Removed:
- `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh` —
  the v0.8.5 `SCOPE_MIGRATION_MARKER` block (lines 62-76),
  `migrate_default_scope()` + its two invocations (lines 83-128),
  `migrate_butler_disclosure_v2()` + invocation (lines 130-153).
  Seed-if-missing blocks retained — those are idempotent seeding,
  not migrations.
- `test-local/init-overrides/01-setup-configs.sh` — same blocks
  mirrored from prod.
- `test-local/e2e/test_migration.sh` — M-7 (v0.8.5 marker absent),
  M-9 (backup absent). Reworked M-8 → M-6 as a generic seed-content
  check (`scopes.yaml == shipped defaults` on fresh install).
- `test-local/e2e/test_migration_v085_existing.sh` — whole 68-line
  script deleted (the existing-overlay → migrate → backup scenario
  is dead code).
- `casa-agent/rootfs/opt/casa/session_registry.py` — the
  `.pop("memory_session_id", None)` in `touch()` + the matching
  docstring notes about lazy migration from pre-2.2a entries.
- `tests/test_session_registry.py::TestMigration` class.

Ship-gate doctrine saved to
`memory/feedback_ship_gate_doctrine.md` (new this session):
9-gate sequence per version bump; Monitor as the default for tests
and long-running tasks; `/ha-prod-console:*` as the first choice for
N150 interaction; pre-1.0.0 = no migrations.

Unchanged (NOT migrations):
- `executor_registry.py` orphan-recovery tombstone — runtime
  crash-recovery, not version migration.
- `log_cid.py` boot-time filter cleanup — idempotence, not
  version migration.

## 0.8.5 — 2026-04-21 — Phase 3.2.2: scope-routing hardening

Scope-routing accuracy hardening + structured `scope_route` emission.
Spec at `docs/superpowers/specs/2026-04-21-3.2.2-scope-routing-hardening.md`.

- **scopes.yaml description hardening** — Replaced the v0.8.0 prose
  corpora with comma-separated keyword phrase clusters targeting the
  7 cross-cutting probe failures the v0.8.4 sweep exposed. Generic
  only — no personal names, organizations, or place names — so the
  addon stays shippable to other households. Tenant-specific signals
  belong in the per-instance overlay at
  `/addon_configs/casa-agent/policies/scopes.yaml`, which Builder
  (Phase 3.5) is authorized to edit. The new authoring contract is
  documented as a top-of-file comment block in the defaults file
  itself.
- **`ACCURACY_BASELINE` 0.80 → 0.85** in `tests/test_scope_routing_eval.py`.
  The flat-curve finding from v0.8.4 still holds — threshold tuning is
  a no-op at this fixture scale; the gain comes entirely from the
  description corpus change.
- **Structured `scope_route` log emission.** `agent.py:455` now emits
  via `logger.info("scope_route", extra={"channel": ..., "winner": ...,
  "winner_score": ..., "second_score": ..., "threshold": ...})`. New
  `_winner_pair()` helper computes the read-side winner from the
  `scores` dict.
- **Generic `extra={...}` flow in `log_cid.py`.** `JsonFormatter` now
  flattens non-standard `LogRecord` attributes into the JSON payload;
  new `HumanFormatter` appends them as `key=val` suffix. Benefits any
  future structured log call, not just `scope_route`. New
  `STANDARD_LOGRECORD_ATTRS` constant + `_record_extras()` helper.
- **`scripts/eval_scope_dist.py` works against live N150 logs** —
  the parser was always ready for this shape; the upstream emission
  is the change that unblocks it.
- **One-shot v0.8.5 migration** in `setup-configs.sh` — refreshes
  the per-instance overlay at `/addon_configs/casa-agent/policies/scopes.yaml`
  on first boot, gated by marker file
  `migrations/scope_corpus_v0.8.5.applied`. Pre-migration overlay is
  preserved as `scopes.yaml.pre-v0.8.5.bak`. Manual edits made AFTER
  the marker is written are preserved across all later boots.
- **`ScopeRegistry.threshold` exposed as a public read-only property.**
  Was `_threshold` private; agent.py needed read access for the new
  emission. Constructor signature unchanged.
- **Tests.** New `TestExtrasFlatten` (4 cases) in `tests/test_log_cid.py`;
  new `TestScopeRouteEmission` in `tests/test_agent_process_scope.py`;
  new `TestThresholdProperty` in `tests/test_scope_registry.py`. New
  e2e scenario `test-local/e2e/test_migration_v085_existing.sh` plus
  M-7..M-9 in `test_migration.sh`. 594 unit tests green; full-mode
  accuracy gate 0.943 (baseline 0.85); all local e2e scripts green
  after Dockerfile.test infra catch-up (see below).
- **Test-infra catch-up — `test-local/Dockerfile.test` migrated to
  Debian bookworm.** The main `casa-agent/Dockerfile` switched to
  `amd64-base-debian:bookworm` in v0.8.1 when fastembed pulled
  onnxruntime (no musllinux wheel) — but the test Dockerfile was
  left on Alpine/musl, breaking the local e2e harness and
  `.github/workflows/qa.yml` CI from v0.8.1 onward. v0.8.5 mirrors
  the v0.8.1 migration recipe into the test image so e2e can run
  again. Also adds the v0.8.5 migration block to
  `test-local/init-overrides/01-setup-configs.sh` (the test-mode
  setup-configs override that replaces the bashio-dependent prod
  script) — without this the test container would skip the
  migration entirely since the prod script never runs there.

Rollback: §10 of the spec. Backup file + marker removal restore v0.8.4
runtime behaviour; reverting the formatter changes and `agent.py:455`
restore prior log shape.

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

### Known limitations
- `scripts/eval_scope_dist.py` expects JSON-structured `scope_route`
  log records with `winner_score`/`second_score`/`threshold` fields.
  The live addon at v0.8.4 emits `scope_route` as a formatted-string
  log line (see `agent.py:441`) without `winner_score`, so the script
  reports "total records: 0" against unmodified production logs. A
  follow-up commit will extend the upstream emission (either JSON
  `extra=` or additional score fields in the format string) to unblock
  the audit tool. Parser logic is fully tested against synthetic logs
  and will work the moment the emission ships the expected fields.
- Measured sweep on the 35-case seed fixture shows accuracy is
  **threshold-invariant over [0.20, 0.45]** — `mean_winner_score ~= 0.787`,
  so every case sits above the entire optimization range and `argmax`
  never falls back. `recommend_from_sweep` picks 0.20 by tiebreak only;
  this is not a real improvement. `scope_threshold` stays at 0.35.
  `ACCURACY_BASELINE` was measured at 0.80 on the seed fixture (not
  0.85 as initially scoped) — raising it requires either dropping
  cross-cutting probes from the default set or hardening
  `scopes.yaml` descriptions to better differentiate
  finance/business/personal. Tracked as a 3.2.2 follow-up.

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
