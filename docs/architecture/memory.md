---
last_reviewed: 2026-07-31
---

# Memory and recall

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Long-term memory: how a fact is written, who may read it back, and what a caller is told
when the store cannot answer. It covers the memory seam, the sensitivity model, and the
provenance carried on a recalled fact. It does not cover the SDK session transcript, which
is short-term context on a different lifecycle, and it does not describe the memory backend's
own internals — those live outside this repository.

## Mental model

There is **one shared bank**. Roles do not partition memory; separation comes from
sensitivity tiers and read clearance, not from storage.

**Recall has three outcomes, and the third is the reason this subsystem is shaped the way it
is.** A call returns hits, returns a genuine empty result, or fails as *unavailable*. The
distinction between the last two is load-bearing: "I searched and found nothing" and "I could
not search" mean opposite things to a model deciding whether to assert that something never
happened. Collapsing them produces confident false denials, which is the failure this design
exists to prevent.

Two consequences follow, and both are easy to get wrong.

**All hits being unreadable is *unavailable*, not empty.** If every returned hit sits above
the caller's clearance, or carries an unusable tier, the seam raises rather than returning an
empty result. The caller genuinely does not know whether relevant memory exists.

**An empty rendered string does not prove zero hits.** Rendering stops once an entry would
exceed the token budget, and if the *first* entry is already too large, nothing is emitted.
So a rendered `""` may mean no hits, or may mean hits that did not fit. Code that treats an
empty render as evidence of absence is making a claim the render cannot support.

**Auto-recall is not "every turn".** It happens when a turn's options are built, which is a
fresh non-voice session only — a warm reused client skips that path entirely, and voice never
auto-recalls. Both can still recall explicitly through the tool. "The agent remembers
automatically" is true of a narrower set of turns than it sounds.

**Read clearance is per channel and fails closed.** Known channels are mapped explicitly; an
unrecognised one gets the *least* sensitive clearance. The fail direction is the security
control here: an unknown surface must read less, never more, and a test pins the function's
docstring to that direction so prose and behaviour cannot drift apart again.

**Writing is narrower than reading.** Only write-trusted channels retain to the shared bank.
A channel that can recall is not thereby able to store.

**When a session goes cold is tunable, and retention deduplicates.** The freshness windows
that decide when a session stops being resumable and becomes save-eligible are
environment-tunable (`FRESHNESS_VOICE_MINUTES`, default 30; `FRESHNESS_TELEGRAM_HOURS`,
default 12). Retained facts are content-addressed, so the same speaker saying the same
thing across sessions collapses to one stored document — and agent-side deduplication
ignores persona version, so a persona upgrade does not mint duplicate memories.

**Mental-model overlays cannot be tier-filtered at all**, because they are bank-wide
summaries rather than individually tagged facts. That is why they are exposed only at the
highest clearance — there is no way to redact part of one.

## Contracts & invariants

**INV-MEM-001**: Recall reports unavailability by raising; it never represents a failure as a successful empty result.

Enforced in the seam's implementations — the backend implementation raises `RecallUnavailable`
(or its `RecallProtocolError` subclass) for timeout, HTTP failure, transport failure and
malformed envelopes, and the no-op implementation raises rather than returning empty when no
backend is configured.

What it does not cover: **individual call sites may still collapse the distinction after the
fact**, and at least one does. `query_engager()` reports an empty rendered digest as "searched
and found nothing relevant" — and because rendering emits nothing when the first hit exceeds
the token budget, hits that exist but did not fit are reported as absence. The invariant holds
at the seam, for typed recall, not at every consumer. Check the call site you care about
rather than assuming it propagates.

**INV-MEM-002**: A typed hit is readable only when its tags carry exactly one recognised tier at or below the caller's clearance; if every hit is dropped, the result is unavailable rather than empty.

Enforced in the backend implementation's typed recall path, which decodes each hit's tier and
drops what it cannot read, then raises when nothing readable survives.

What it does not cover: the legacy string recall path, mental-model overlays, and the SDK
transcript are not tier-filtered by this check. Filtering is also applied locally to what the
backend returned — the request's own tag filter is not treated as the access control.

**INV-MEM-003**: An unrecognised channel reads at the least-sensitive tier.

Enforced by the channel-clearance lookup's default. The direction matters: an unknown surface
sees less, not more.

What it does not cover: origin-aware resolution is narrower than it sounds. Resident
auto-recall and the recall tool resolve clearance from the stamped origin (`clearance_for_origin()`),
so a webhook turn's clearance there depends on its declared origin. Delegated recall
(`delegated_recall()`) resolves from the origin *channel* alone and discards any
origin-stamped route or clearance override.

**INV-MEM-004**: A caller cannot inject a sensitivity tier or a provenance tag through ordinary application tags.

Enforced in the retain-item builder, which refuses reserved tag namespaces before doing any
classification or I/O, and validates the speaker provenance it is given.

What it does not cover, and this is worth stating plainly: it protects the *write* path from
its own callers. It does not authenticate what the backend returns. On read, a syntactically
valid provenance tag is trusted and is not cross-checked against the duplicate copy stored in
the item's metadata. A recalled fact can therefore carry a speaker identity that the read path
has not independently established.

**INV-MEM-005**: Only write-trusted channels retain to the shared bank.

Enforced by the channel write-policy check, consulted by every production retain path.

What it does not cover: the seam's retain method itself enforces nothing. A future caller that
skips the builder and the policy check would bypass both.

**INV-MEM-006**: A session save or removal keyed by channel acts only on the session id its caller snapshotted — a registration carrying a different id in that window is released, not retained or deleted; an explicit reset deliberately removes its snapshotted session even when re-registered.

The registry key names a *conversation slot*, not a session — a new turn can re-register the
slot at any suspension point. Every step of the save protocol therefore carries the session
id its caller judged (the reaper's cold snapshot, a reset's own snapshot): the save entry
point releases a claim that landed on a different session, `finish_save` and
`clear_save_claim` decline when the stored id moved, and an explicit reset's trailing
removal declines the same way — as do the reaper's direct removals of unusable and
recall-only entries (a snapshot without a session id guards on that absence).

The guard is deliberately session-scoped, not registration-scoped, and each path has its own
reason that suffices. A *reset* that removes a same-id re-registration is executing its
contract: the id names exactly the conversation the user asked to reset, whoever refreshed
the pointer meanwhile. The *reaper* cannot meet a same-id re-registration from a racing
turn at all: resuming stamps `last_active` before the turn runs and a past-freshness
session is never resumed, so any turn racing a cold sweep registers a *fresh* id — which
this guard catches.

What it does not cover: a caller that passes no expected id gets the unconditional
behavior; and a turn still running on the *same* session when a reset saves it can have its
tail exchanges miss retention — the reset drops the pointer (its contract) and nothing
saves that session again.

## Failure behavior

**The backend is slow, unreachable, or returns an error.** The seam raises `RecallUnavailable`
carrying a reason slug that names the class of failure. There is no HTTP-level retry beyond a
single connection retry.

**The backend returns something malformed** — a bad envelope, a hit with unusable text or
tags, or nothing readable at the caller's clearance. The seam raises `RecallProtocolError`.
This is deliberately not an empty result.

**No backend is configured.** Recall raises with a reason naming that condition, the overlay
comes back empty, and **writes silently succeed without persisting anything**. The write side
fails quietly here while the read side does not.

**Auto-recall fails.** The turn absorbs it: no memory block is injected and the turn proceeds
without memory. Repeated failures open a per-agent breaker that skips the attempt entirely.
The model is not told that recall was skipped, so an agent cannot distinguish "no memory
matched" from "memory was not consulted" — which is precisely why an agent should not assert
absence from silence.

**A recall path fails repeatedly.** A circuit breaker fast-fails subsequent calls with a
dedicated reason rather than calling the backend. Genuine zero-hit results count as successes
and reset it; only unavailability counts as failure.

**Tier classification fails.** Retention classifies each item's sensitivity with a bounded
LLM pass; a failed or unparseable classification retries once and then assigns *private*,
with only a log warning to show for it. The write is not lost — but the fact becomes invisible below the highest
clearance, which reads as absence on voice and friends surfaces.

**Saving a session fails.** The save is abandoned, its claim is released, and the entry stays
for a later sweep to retry. An explicit reset is the exception — it drops the pointer whether
or not the save succeeded, unless a newer session registered meanwhile (INV-MEM-006), in
which case the newer registration stands.

**The session registry file is corrupt at boot.** It is renamed aside and the process starts
with an empty registry. Session pointers are lost; the app comes up.

## Extension points

**A new backend** implements the seam's methods and must preserve the three outcomes —
in particular it must raise, not return empty, when it cannot answer or when nothing readable
survives filtering. If it holds resources, implement the close hook.

**A new channel** needs an explicit clearance entry, or it reads at the least-sensitive tier
by default. Write access is a separate, deliberate addition. If its sessions should be
retained when they go cold, that is a *third* list — none of the three is inferred from the
others, and forgetting one produces a channel that silently never persists.

**A new recall caller** should decide whether it wants its own telemetry and breaker, which
means choosing a distinct recall path rather than inheriting another's. It must also decide,
explicitly, what it does with unavailability — and if its prompt says anything like "no
prior results found", it must not collapse unavailable into silence.

**A new render surface** means extending the surface type and the provenance view together,
since what may be disclosed is decided per surface.

**A new writer** should build its items through the retain-item builder. Calling the seam's
retain directly bypasses tier tagging, provenance validation and the write-trust check at
once.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/semantic_memory.py::SemanticMemory`
- `casa/rootfs/opt/casa/semantic_memory.py::RecallUnavailable`
- `casa/rootfs/opt/casa/semantic_memory.py::RecallProtocolError`
- `casa/rootfs/opt/casa/semantic_memory.py::NoOpSemanticMemory`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.recall_items`
- `casa/rootfs/opt/casa/sensitivity.py::clearance_for_channel`
- `casa/rootfs/opt/casa/sensitivity.py::clearance_for_origin`
- `casa/rootfs/opt/casa/channel_policy.py::writes_to_bank`
- `casa/rootfs/opt/casa/memory_provenance.py::build_retain_items`
- `casa/rootfs/opt/casa/recall_renderer.py::render_recall`
- `casa/rootfs/opt/casa/recall_health.py::observed_recall`
- `casa/rootfs/opt/casa/session_saver.py::save_session`
- `casa/rootfs/opt/casa/session_saver.py::reset_channel`
- `casa/rootfs/opt/casa/freshness_reaper.py::FreshnessReaper.sweep_once`

**Tests**
- `tests/test_recall_absence_invariant.py`
- `tests/test_recall_health.py::test_breaker_opens_after_threshold_failures`
- `tests/test_sensitivity.py::test_readable_tiers_is_clearance_and_below`
- `tests/test_sensitivity.py::test_clearance_docstring_states_the_fail_closed_direction`
- `tests/test_channel_policy.py`
- `tests/test_memory_provenance.py`
- `tests/test_agent_auto_recall_unavailable.py`
- `tests/test_session_saver.py`
- `tests/test_freshness_reaper.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
