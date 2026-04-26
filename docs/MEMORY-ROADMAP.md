# Memory Roadmap

Living tracker for the memory subsystem (Honcho + SQLite + scope-routing
+ disclosure). Multi-session, gitignored like `ROADMAP.md`. Read at the
start of every memory-touching session; update at the end.

Last updated: 2026-04-27 — M3 shipped as v0.15.4 (Honcho contract coverage + `memory_call` telemetry).

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
| M4 | Engagement memory (executors get continuity; meta scope readable) | 📋 Brainstorm needed | — |
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
  in pre-3.2 shape (`voice:{scope_id}:{role}` — 3 segments). Agent uses
  4 segments (`{channel}:{chat_id}:{scope}:{role}`). Result: prewarm
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

## M4 — Engagement memory

**Why.** Three audit findings collapse into one design problem:
- G5 — `meta` and `executor:<type>` are session-id segments not declared
  in `scopes.yaml`. Never readable.
- G9 — Tier-3 Executors get NO memory injection. Configurator starts
  blind every engagement.
- B4 — engagement summaries are write-only in normal recall.

Fix: make engagement history actually flow back into agent context.
Three approaches, design-pick required:

1. **Minimal.** Declare `meta` as readable-by-assistant scope. Engagement
   summaries reach Ellen's normal turn. ~50 LOC.
2. **Right.** Executors get `MemoryConfig` on `definition.yaml`
   (scopes_owned, scopes_readable). Configurator remembers what it
   changed last week.
3. **Both.** Combined.

Real argument for "executors stateless" exists ("fresh slate per
engagement, reasoned from artifacts"). **This needs a brainstorm
session before plan-writing.**

**Status:** 📋 Brainstorm needed. M2/M3 prerequisite.

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
