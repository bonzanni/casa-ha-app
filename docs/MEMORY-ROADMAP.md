# Memory Roadmap

Living tracker for the memory subsystem (Honcho + SQLite + scope-routing
+ disclosure). Multi-session, gitignored like `ROADMAP.md`. Read at the
start of every memory-touching session; update at the end.

Last updated: 2026-04-28 — F2 stale-citation sweep shipped (master `466e585`, doc-only, no version bump); 19 cites flipped across in-code docstrings, configurator doctrine, the live arch spec, and one user-memory example. M4b remains the most recent feature ship as v0.17.0 (Finance still operator-disabled in prod — paths exercise on enable).

## Doctrine

**Honcho is the target.** Everything important — semantic retrieval,
peer cards, summarisation, cross-channel `nicola` continuity — runs on
Honcho v3.

**SQLite is graceful degradation.** When `HONCHO_API_KEY` is unset, Casa
must keep working with last-N exchange replay and nothing more. SQLite is
explicitly NOT a feature-equivalent local replacement. Performance,
summarisation, peer cards, retention — all "good enough, no more".

This framing rules out a chunk of work that would otherwise feel
required (SQLite summariser, SQLite peer-card writer, backend
migration tooling). Anything that's only meaningful when SQLite is the
primary store is out of scope until that doctrine changes.

## Phases at a glance

| Phase | Focus | Status | Plan |
|---|---|---|---|
| M1 | Spec consolidation + dead-code removal | ✅ Shipped (v0.15.3) | `plans/2026-04-26-memory-m1-spec-recovery-and-cleanup.md` |
| M2 | Honcho-side breakage fixes (voice prewarm, cancel paths, origin scope) | ✅ Shipped (v0.15.3) | `plans/2026-04-26-memory-m2-honcho-fixes.md` |
| M3 | Honcho contract coverage (real summary/peer_repr test + latency telemetry) | ✅ Shipped (v0.15.4) | `plans/2026-04-27-memory-m3-honcho-contract-coverage.md` |
| M4 | Engagement memory (meta scope + executor archive read; specialists deferred) | ✅ Shipped (v0.16.0) | `plans/2026-04-27-memory-m4-engagement-memory.md` (spec: `specs/2026-04-27-memory-m4-engagement-memory-design.md`) |
| M4b | Specialists become memory-bearing (per-`(role, user_peer)` 2-segment Honcho session; opt-in via `cfg.memory.token_budget > 0`) | ✅ Shipped (v0.17.0) | `plans/2026-04-27-memory-m4b-specialist-memory-design.md` (spec: `specs/2026-04-27-memory-m4b-specialist-memory-design.md`) |
| M5 | `remember_fact` tool (the deferred-since-0.4.0 feature) | 📋 Planned | — |
| M6 | Cross-role recall (consult-other-agent-memory) | 📋 Optional, large | — |

Status legend: 📋 Planned · 🚧 In flight · ✅ Shipped · ⏸ Deferred

## M1 — Spec consolidation + cleanup

**Why first.** Three separate dev-side specs (`2026-04-17-honcho-v3-memory-design.md`,
`2026-04-17-sqlite-memory-2.2b.md`, `2026-04-20-3.2-domain-scope-runtime.md`)
and a series of follow-up tunings live in `docs/superpowers/specs/`. None
is the "current contract" — they're design history. Future memory work
needs a single living doc to reason against.

While we're touching memory files: remove dead branches that exist
because the SQLite "could compete with Honcho" hypothesis was on the
table at one point. Doing this before adding features means the next
phase reasons about a smaller surface.

**Deliverables:**
- `docs/superpowers/specs/2026-04-26-memory-architecture.md` — current
  contract, supersedes the 2.2a/2.2b/3.2 specs for "what's true today"
  purposes (originals stay for "why").
- Drop `card_only` from `_VALID_READ_STRATEGIES` + schema enum + the
  branch in `_wrap_memory_for_strategy`.
- Drop SQLite-side `peer_cards` table + reader + tests. Honcho's
  `peer_card` rendering stays untouched.
- Drop `archive_session_full` (dead config, never read).

**Status:** ✅ Shipped 2026-04-26 as v0.15.3 (folded into the M2 release because M1 was internal-only — no version bump at the time).

## M2 — Honcho-side breakage fixes

**Why.** Three small bugs in Honcho-touching code paths. Each is a
1–10 line repair that restores intended behaviour. Doing them before
M3/M4/M5 means we're adding features on top of a working baseline, not a
30%-broken one.

**Deliverables:**
- **G1 voice prewarm.** `channels/voice/channel.py:445` builds session_id
  in pre-3.2 shape (`voice-{scope_id}-{role}` — 3 segments). Agent uses
  4 segments (`{channel}-{chat_id}-{scope}-{role}`). Result: prewarm
  cache key never read on real turn. Restore intended voice latency
  budget. Loop over `scopes_readable`; warm one entry per scope.
- **G4 cancel-path memory.** `cancel_engagement` (`tools.py:1427`) and
  `delete_engagement_workspace(force=True)` (`tools.py:1614`) pass
  `memory_provider=None` to `_finalize_engagement`. Resolve from
  `agent_mod.active_memory_provider` like `emit_completion` does.
- **G6 origin.scope plumbed.** `tools.py:1357` reads
  `engagement.origin.get("scope", "meta")`; `agent.py:280` never sets
  `"scope"`. Either set it from active scopes at engage-time, or remove
  the `.get` fallback and accept the constant.

**Status:** ✅ Shipped 2026-04-26 as v0.15.3. Voice prewarm now uses 4-segment session ids; cancel + force-delete write meta summaries to Honcho; origin_var carries argmax scope so query_engager retrieves from the engager's rooted scope.

## M3 — Honcho contract coverage

**Why.** `_render` (`memory.py:117-124`) emits `## Summary so far` and
`## My perspective` sections when Honcho returns them. Existing tests
mock the SDK with `summary=None, peer_representation=None`. We don't
actually know if v3 returns these in production. If Honcho is the
target, this is a confidence problem.

Also: zero memory-side observability. `scope_route` log line is
classifier metrics. Nothing on Honcho call latency, cache hit ratios,
SQLite write counts.

**Deliverables:**
- Integration test against a recorded (or live, behind a flag) Honcho v3
  response with `summary` + `peer_representation` populated, asserting
  `_render` produces both sections.
- Single per-turn telemetry line `memory_call` with: backend, latency
  ms, peer count, summary present (bool), peer_repr present (bool),
  cache hit. Closes B8 enough.

**Status:** ✅ Shipped 2026-04-27 as v0.15.4. M3a closed by the
populated-response integration test in
`tests/test_memory_honcho.py`; M3b closed by `memory_call` info-level
emissions in Honcho/SQLite providers + cache-hit branch of
CachedMemoryProvider. NoOp intentionally silent. New spec § 13
documents the telemetry contract. Live `HONCHO_LIVE_TEST=1`-gated
test deferred as M3a.1 follow-up.

## M4 — Engagement memory (Medium scope: L1 + L3 + L4)

**Why.** Three audit findings (G5, G9, B4) collapse into one design;
brainstorm 2026-04-27 reframed roadmap A/B/C as a layered L1/L2/L3/L4
model. M4 ships the **Medium** subset: residents + executors. L2
(specialists) deferred to M4b.

**Layered architecture:**
- **L1** — `meta` declared as a system scope (`kind: system`,
  `minimum_trust: authenticated`) in a v2 `scopes.yaml`. Always-on
  after trust filter; no classifier routing; no embedding. Resident
  `scopes_readable` adds `meta`. Voice (Tina) excluded by trust.
- **L3** — Executor archive read at engage-start. New
  `ExecutorMemoryConfig(enabled, token_budget)` on `ExecutorDefinition`
  (`config.py:189` — note: the M4 design spec mis-cites this as
  `ExecutorEntry`; `ExecutorEntry` at `config.py:141` is the
  resident-side delegate ref, a different class — corrected in the
  plan's Decisions log D5); Configurator opts in. New
  `{executor_memory}` prompt-template slot in `engage_executor`
  (`tools.py:858`).
- **L4** — Free benefit: `_finalize_engagement` already writes
  engagement summaries to the `meta` session for both specialist and
  executor engagements; L1 makes them readable on Ellen's normal
  turn. No new write code.

**Deliverables:**
- `policy-scopes.v2.json` schema with `kind: topical | system`.
- `defaults/policies/scopes.yaml` v2 declaring `meta`.
- `assistant/runtime.yaml::scopes_readable` adds `meta`.
- `agent.py::_process` partitions readable into system (always-on) +
  topical (classifier-routed).
- `ExecutorMemoryConfig` dataclass; `ExecutorDefinition.memory` field
  (corrected from spec's `ExecutorEntry`); `executor.v1.json` schema
  additive update (no const bump per plan D2).
- `tools.py::engage_executor` `{executor_memory}` slot interpolation
  via new `_fetch_executor_archive` helper.
- Configurator doctrine sync: `architecture.md`, `recipes/scopes/edit.md`,
  `recipes/executor/scaffold.md`, `recipes/resident/{create,update}.md`.
- Architecture-spec updates (§§ 5, 6, 11, new § 14) ship in same
  commit set per spec-doc-rot prevention.

**Pre-1.0.0 license invoked:** scopes.yaml schema bump v1→v2 with
no migration shim. Existing v1 fixtures regenerated; loader rejects
v1 overlays at boot.

**Version:** v0.16.0. **Ship-gate:** ineligible for low-risk fast
path (changes runtime read semantics + new schema + production-critical
paths); all 9 gates apply.

**Status:** ✅ Shipped 2026-04-27 as v0.16.0. All 21 plan tasks landed
+ 4 fix-up commits (test-convention align, redundant-import drops,
guard simplification). Master tip `93b442e`. Workflow_dispatch CI:
all 4 jobs (tier1/tier2/tier3/baseline-runtime) green. N150 deployed
via `/ha-prod-console:update`; smoke 3/3 PASS (healthz, turn-assistant,
voice-sse). Pre-1.0.0 schema bump scopes.yaml v1→v2 with no migration
shim landed cleanly. Configurator doctrine sync + arch-spec § 5/6/11/12
+ new § 14 in same commit set. Plan caught & corrected 7 stale plan
citations during implementation (per spec-doc-rot prevention). M4b
(specialists) remains separate brainstorm; M5 `remember_fact`, M6
cross-role recall queued.

## M4b — Specialists become memory-bearing (✅ Shipped v0.17.0, 2026-04-28)

**What shipped.** Specialists (Tier 2 — Finance today; future
Health/Personal/Business) gain per-`(role, user_peer)` Honcho memory.
Each enabled specialist becomes a first-class Honcho peer whose session
id is `f"{role}-{user_peer}"` (e.g. `finance-nicola`; built via
`honcho_session_id` since v0.17.1, originally `:`-joined at v0.17.0
ship and rotated by F1),
**channel-agnostic** and **scope-agnostic**. Honcho's
`observe_others=True`-on-agent-peer setup populates `peer_representation`
automatically over time, giving each specialist a domain-narrow
theory-of-mind of the user.

**Architecture chosen:** Option A — one session per `(specialist,
user_peer)`, mixed-domain. Brainstorm 2026-04-27 collapsed the original
three candidates into a fourth, cleaner shape based on user behavioral
elicitation: scenario-1 ruled out per-channel partition; scenario-2
ruled out domain-scope partition. Spec: `2026-04-27-memory-m4b-specialist-memory-design.md`.

**Code surface:**
- `tools.py:_run_delegated_agent` (now at `:399`) reads via
  `ensure_session` + `get_context` before SDK invocation; injects
  `<memory_context agent="{role}">…</memory_context>` block between
  `<delegation_context>` and `Task:`. Writes via background
  `_specialist_add_turn_bg` after SDK return. Module-level
  `_specialist_bg_tasks: set[asyncio.Task]` GC anchor.
- `_specialist_meta_write_bg` writes 200-char-truncated summaries to
  the parent's meta session for coordinator visibility, so Ellen sees
  specialist activity independent of which scope her per-turn argmax
  went to.
- **Validator drop**: `specialist_registry._validate_tier2_shape` no
  longer rejects `token_budget > 0`. Plan-time gap: M4b spec § 4.3
  acknowledged only `agent_loader.py:562-573`; the duplicate validator
  in `specialist_registry.py:133-138` was caught by the Task 13
  implementer subagent in flight and dropped in commit `58fa70b`.
- **Defaults flip**: `defaults/agents/specialists/finance/runtime.yaml`
  bumps `memory.token_budget` 0 → 4000.

**Trust posture.** No per-call filter at the memory layer. Trust
enforcement stays one level up at the resident's `delegates`
decision. If voice-Tina is ever permitted to delegate to Finance,
Finance's full unified memory is in scope — operator must
structurally split into separate specialist roles
(e.g. `finance_personal` vs `finance_business`) if per-channel
memory partition is desired.

**Pre-existing concern surfaced during deploy verify**: live N150
log shows `Memory add_turn failed in background: ... pattern '^[a-zA-Z0-9_-]+$' ...`
on the resident write path. Honcho server-side validation rejects
colons in session IDs that Casa has used since v0.2.2. Fail-soft
absorbs it (background-only, no user regression). NOT M4b-introduced
but M4b's own 2-segment session ID would have hit the same wall
once Finance was operator-enabled, but F1 (v0.17.1, this branch)
closed the regex bug and rotated the M4b session shape to
`finance-nicola` before any specialist write hit the wire. See
`reference_honcho_session_id_format` memory.

**Deferred to M5/M6:** specialist `peer_card` writes / `remember_fact`
MCP tool → M5. Cross-specialist recall via `peer_perspective` → M6.
`read_strategy: cached` for specialists. Multi-user
(`user_peer != "nicola"`) — out of scope.

## M5 — `remember_fact` tool

**Why.** The single biggest UX gap. Without it, Casa cannot durable-store
anything the user explicitly asks it to remember. Deferred since v0.4.0
(2026-04-17). With Honcho as primary, `peer_card` is the natural backing
store and the implementation is small.

**Deliverables:**
- `remember_fact(text, category)` MCP tool. On Honcho: appends to the
  current user_peer's `peer_card` via the v3 SDK. On SQLite: log-and-skip
  (one INFO line: "remember_fact requires Honcho"). Don't attempt
  degraded SQLite write — the doctrine says SQLite is degraded mode.
- Surface in agent prompts so the user can say "remember that…".
- Visible feature parity gap is the right place to nudge users toward
  setting up Honcho.

**Status:** 📋 Planned. M4 prerequisite (might want a "fact" scope).

## M6 — Cross-role recall (optional, large)

**Why.** Tina cannot see what Ellen said earlier today — each role is
its own peer, `observe_others=True` only inside its own session. Real
users will say "Tina, what did Ellen tell me about X" and get nothing.

Two implementation paths:
- Topology change (role-peers in shared sessions). Invalidates pre-existing
  session continuity. Big.
- `consult_other_agent_memory(role, query)` tool. Additive, safer.
  Reads from another role's session perspective via Honcho's
  `peer_perspective` parameter.

The tool path wins for size. **Defer to after M3-M5 ship.**

**Status:** 📋 Optional. M5 prerequisite.

## Open follow-ups (pick up next session)

> **Session-start prompt:** "F1 (v0.17.1, 2026-04-28) and F2 (master `466e585`,
> 2026-04-28) both shipped. No memory follow-ups currently open. Next memory
> work is M5 (`remember_fact` tool); read `MEMORY.md` + the M5 phase entry
> above before starting."

### F1 — Honcho session-id colon-pattern rejection (✅ Shipped v0.17.1, 2026-04-28)

**Status: SHIPPED v0.17.1 (2026-04-28).**

Implementation plan: `docs/superpowers/plans/2026-04-28-honcho-session-id-format-fix.md`.
Spec: `docs/superpowers/specs/2026-04-28-honcho-session-id-format-design.md`.
Memory entry rotated: `memory/reference_honcho_session_id_pattern_drift.md` →
`memory/reference_honcho_session_id_format.md`.

**Original symptom (pre-fix).** Live N150 logs (since some unknown date
pre-2026-04-28) showed:

```
{"level":"WARNING","logger":"agent","msg":"Memory add_turn failed in background:
  [{'type': 'string_pattern_mismatch',
    'loc': ['body', 'id'],
    'msg': \"String should match pattern '^[a-zA-Z0-9_-]+$'\",
    'input': 'voice:probe-scope:house:butler',
    'ctx': {'pattern': '^[a-zA-Z0-9_-]+$'}}]"}
```

The Honcho server-side regex `^[a-zA-Z0-9_-]+$` does NOT permit colons.
Casa had used 4-segment colon-separated session IDs
(`{channel}:{chat_id}:{scope}:{role}`) since v0.2.2. Resident write
path was silently failing — fail-soft (background-only, WARNING-only,
no user regression visible) but **all new resident turns were NOT being
persisted to Honcho** for ~11 days. M4b's specialist write path
(`f"{role}:{user_peer}"`, e.g. `finance:nicola`) had the same pattern
violation, latent until Finance opted in.

**Fix shipped.** New `casa-agent/rootfs/opt/casa/honcho_ids.py::honcho_session_id`
canonical builder joins segments with `-` and validates each segment
against `^[A-Za-z0-9_-]+$` before join. All build sites
(`build_session_key`, `_one_scope`, voice prewarm, specialist M4b
write, executor archive read/write, finalize meta write,
`query_engager` engager-scope rebuild) routed through the builder.
`session_sweeper` partition flipped from `:` to `-` to match.
Pre-v0.17.1 colon-shaped Honcho sessions are abandoned in place (per
the §5 "pre-3.2 IDs are orphaned" doctrine — same precedent).

**Memory references:**
- `reference_honcho_session_id_format.md` (rotated post-ship from
  `reference_honcho_session_id_pattern_drift.md`)
- `project_memory_m4b_shipped.md` (M4b ship summary; flagged this as
  follow-up)
- `docs/superpowers/specs/2026-04-26-memory-architecture.md` § 5
  (session-id topology)

### F2 — Stale citation sweep (✅ Shipped master `466e585`, 2026-04-28)

**Status: SHIPPED master `466e585` (2026-04-28).** Doc-only PR #14 ff-merged
to master; no version bump (Casa convention for doc-only). Low-risk fast
path: feature-branch CI ran by GitHub default but step 4 was officially
skipped per `feedback_ship_gate_doctrine`.

**Headline.** The original F2 trigger — `casa-agent/rootfs/opt/casa/tools.py:1003`
docstring citing the wrong write-site line — was already closed as a side
effect of F1 Task 7 (the docstring is now at line 1005 and cites
`tools.py:1383`, the current `honcho_session_id` build site for the executor
archive write). The real F2 work was the broader sweep.

**What shipped (19 cites flipped, 0 demoted):**

- **In-code docstrings** under `casa-agent/rootfs/opt/casa/*.py` — 3 cites
  in `tools.py` (M4b helper docstrings: `Agent._bg_tasks` cite
  `agent.py:592-614 → :133`; `Agent._add_turn_bg` cite `:592-614 → :595-617`;
  `_finalize_engagement` meta-write cite `tools.py:1325-1334 → :1326-1338`).
- **Configurator doctrine markdown** — 1 cite in `defaults/policies/scopes.yaml`
  (`meta` scope's pointer at the tool-write site, `tools.py:1131 → :1326-1338`).
- **Live arch spec** at `docs/superpowers/specs/2026-04-26-memory-architecture.md`
  — 14 cites refreshed: 3 class-def lines in `memory.py` (Honcho/Sqlite/Cached
  providers), 2 `tokens // 40` cites, 2 `query_engager` cites, 3
  `_run_delegated_agent` cites, 2 ScopeRegistry cites, 1 `engage_executor`
  cite, 1 meta-write block, 1 executor archive write block, 1 origin-stamp
  range, 1 read-path span, 1 write-path block.
- **Sibling test files** under `tests/` (extended-scope per M4b ship-summary
  discipline that test docstrings drift independently) — 5 cites in
  `test_agent_origin_scope_stamp.py`, `test_agent_process_scope.py`,
  `test_run_delegated_agent_memory.py`.
- **User-memory entry** `feedback_spec_doc_rot_prevention.md` — 1 cite
  (example kept live for instructional value: `memory.py:464 → :564`).

**Out-of-budget (frozen historical records, not retroactively rewritten):**

- `project_*_shipped.md`, `project_*_design_state.md` (M2/M3/M4/M4b) —
  describe state at ship/design time; rewriting would corrupt the historical
  record.
- `casa-agent/CHANGELOG.md` — per-version ship descriptions, frozen.
- `scripts/eval_scope_dist.py` docstring — pinned to "as of v0.8.4", broader
  rewrite needed beyond a citation flip.
- `honcho_ids.py:4` upstream `src/schemas/api.py:37` — navigational hint to
  Honcho's repo; the regex itself is independently verifiable from server
  error messages.

**Memory references:**
- `feedback_spec_doc_rot_prevention.md` (rationale for the discipline)
- `project_memory_m4b_shipped.md` (final-review section originally flagged
  this as out-of-scope-for-M4b)

---

## Explicitly NOT doing

These were in the audit but ruled out by the "Honcho primary, SQLite
graceful" framing:

- ⏸ **B3 SQLite summariser.** Degraded mode is allowed to be dumb. Last
  N exchanges is fine.
- ⏸ **B2 SQLite retention/pruning.** Light touch only — log a warning
  above N MB, never auto-prune.
- ⏸ **G10 backend migration.** Switching SQLite ↔ Honcho silently
  abandons turns; fresh start is the answer. If Honcho is the target,
  "set HONCHO_API_KEY" is the migration path.
- ⏸ **B7 disclosure-as-enforcement.** Separate epic, touches output
  filtering not memory. Memory v3 already moved memory enforcement to
  the read-side (channel_trust × scope filter); the output side is a
  different problem.
- ⏸ **G2 SQLite peer_cards writer.** Drop the table on the SQLite side
  entirely (M1). Honcho is where peer_cards live.

## Session-end checklist

When a memory-touching session wraps:
1. Update this roadmap — status markers, "what shipped this session"
   note under the relevant phase.
2. Commit code artifacts (this file + plan files live under gitignored
   `docs/`, not committed).
3. Update memory files for non-trivial findings.
4. Update `ROADMAP.md` (top-level project tracker) if a memory phase
   shipped.
