---
last_reviewed: 2026-07-31
---

# Engagements and delegation

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Work one agent hands to another: ephemeral delegation, durable engagements, how they end,
and what survives a restart. It does not cover the turn loop itself, nor what a driver's
underlying runtime does once started.

## Mental model

**A delegated turn and an engagement are different things.** Delegation in its ordinary form
is a task handed to a specialist that runs and returns — ephemeral. An engagement is a
durable record with its own topic, which outlives the call that created it.

Three launch paths exist and they are not symmetrical. Ordinary specialist delegation runs
ephemerally. *Interactive* specialist delegation creates an engagement. Engaging an executor
always creates one.

**Ending an engagement is a race with exactly one winner, and that is the load-bearing
design.** The terminal transition is attempted against the registry; only the caller that
wins it performs the external effects — closing the topic, tearing down the driver, notifying
the resident. Everything else is a loser that does nothing. This is what stops a completion
racing a cancellation from producing two closures and two notifications.

**The transition is strict about persistence.** If writing the terminal state fails, the
in-memory record is restored and the call raises, so there is no state where the process
believes an engagement finished and the durable record disagrees. The caller is told to
retry.

**Completion is gated on unread input.** A *successful* completion is refused while inbound
messages are unread or reserved — an agent cannot declare victory over a question it has not
read. Failure and cancellation deliberately bypass that gate, because something going wrong
must always be able to end.

**Much less survives a restart than the word "durable" suggests.** The record persists;
concurrency permits, live drivers, output sequencers, inbound reservations and various
in-flight maps do not. A record found `active` at startup is rewritten to `idle`, because no
live driver survived to make `active` true.

**Durable is not indefinite, and engagements can speak up unprompted.** A daily sweep
suspends a live session after a day idle and posts recurring idle reminders (three days for
a specialist, seven for an executor, refiring weekly); terminal tombstones age out after
thirty days, which bounds duplicate-task protection. Separately, an observer watches
engagement events and may post a bounded LLM interjection into the resident chat — capped
at three per engagement and suppressible with `/silent` — so engagement work can surface in
the main conversation through a path outside the lifecycle above. The cap holds under the
bus's concurrent dispatch: a budget slot is reserved before the interjection is evaluated
and handed back if nothing is posted, so simultaneous events can neither overshoot the cap
nor burn budget on declined evaluations.

**The depth cap is narrower than it sounds.** It stops an ephemerally delegated agent —
resident or specialist alike — from delegating onwards. It is read in one place and stamped
in one place, and the executor launch path touches neither — so it is not a general limit on
agents creating long-running work. See the invariant below for exactly what it covers.

## Contracts & invariants

**INV-ENG-001**: A terminal transition has exactly one winner, and only the winner performs the finalization side effects.

Enforced by the registry's terminal transition, which refuses a missing or already-terminal
record and returns failure; the finalize path performs topic closure, driver teardown and
notification only on success.

The direct status mutators honour the same boundary: each re-checks for a prior terminal
state under the registry lock and declines to overwrite one — the idle sweep cannot flip a
concurrently-cancelled engagement back to resumable, and a failed resume that loses the
race to a cancel neither overwrites the status nor runs its duplicate topic cleanup (the
error mutator reports whether it won, and only a winner cleans up).

What it does not cover: exclusivity covers the *post-transition* side effects only: the pre-close
inbound spool drain runs before the win/lose transition, so a caller that goes on to lose the
race may already have flushed pending receipts and eviction notices externally. The drain is
idempotent, which is why running it ahead of the gate is tolerated — but "does nothing" is
not what a losing finalizer does.

**INV-ENG-002**: A strict terminal transition never leaves the persisted and in-memory records disagreeing; on a write failure it restores the prior state and raises.

Record *creation* holds the same strictness: a create whose tombstone write fails rolls the
in-memory insert back and raises, rather than handing the caller a running engagement whose
crash-recovery record never reached disk.

Creation also compensates for a *cancelled* creator: a caller cancelled after the persist
committed never receives the record, so the insert is rolled back and its removal persisted
before the cancellation propagates — no durable active record whose driver never started.

That compensation covers the record, and the launch path compensates the rest of the window
around it, because a cancellation is delivered at whichever await happens to be pending and
ordinary `except Exception` handlers do not see it. Before the record exists, a cancellation
closes the topic that was already opened; after it exists but before the driver is confirmed
live, the compensation additionally marks the record errored and runs the driver's own
terminal teardown. That last step matters because a driver can be *partly* live: the
claude-code driver starts its supervised service before its final awaits, so a cancellation
arriving late would otherwise leave a running process behind a terminal record. Each step is
scheduled rather than awaited — a cancelled task cannot await network round-trips.

What it does not cover: the other non-strict registry mutations (status touches, channel
state, counters) warn and continue if their write fails, so the no-disagreement guarantee
belongs to creation and the finalize path specifically. And the cancellation compensation is
itself best-effort on the disk side — if the compensating write fails, the on-disk ghost row
remains until the boot reconcile and reap TTL retire it.

**INV-ENG-003**: A successful completion is refused while unread inbound messages or inbound reservations exist, when the driver exposes its inbound state.

Enforced both as a pre-check and again as a hook inside the transition itself, so the
condition is re-evaluated at the moment the state changes rather than only before it.

What it does not cover: failed and cancelled outcomes intentionally skip the gate, and so
does the operator's own complete command — only the completion *tool* arms the gate, so an
operator marking an engagement complete finalizes past unread input deliberately. The gate
also exists only where the driver implements the inbound accessors — today that is the
claude-code driver alone, so an interactive in-casa specialist completion has no unread-input
gate. Accessor failures fail open with a warning rather than wedging termination.

**INV-ENG-004**: Ephemeral delegation stops at depth one.

Enforced in the pre-launch check for the delegation tool, against a depth stamped when an
ephemeral delegated child's origin is built — stamped for every delegated target, resident
and specialist alike, and checked without regard to the caller's tier.

What it does not cover, and this is the scope worth reading twice: the executor launch path
neither reads nor stamps the depth, and the interactive branch that creates a specialist
engagement copies the caller's origin without stamping — an interactively-engaged specialist
runs at the caller's depth and can delegate onwards. The guarantee is "an agent reached
through ephemeral delegation cannot delegate again", not "agent-created work cannot chain".

**INV-ENG-005**: Once the output sequencer is terminalized, ordinary narration and unresolved sends cannot post below the completion.

Enforced by the sequencer's terminalization and its writer checks, with a dedicated path
reserved for the completion notice itself.

What it does not cover: ordering depends on a bounded drain. If the drain times out, the
completion is posted anyway with a warning, and if no live sequencer exists the finalize path
falls back to a direct send that bypasses sequencing entirely.

## Failure behavior

**Completion is called with a bad status or arguments.** Rejected before any transition; the
engagement stays live and the caller sees a tool error.

**Completion is refused for unread input.** The transition is vetoed, the record stays live,
and the caller gets a retryable outcome naming the condition. This is a precondition failure,
not an error state.

**The terminal write fails.** The record is rolled back to live and no side effects run.
Both the completion tool and the cancellation tool surface this as the same distinct
retryable outcome — the caller is told the record is still live and to call again, rather
than being handed a success for a transition that did not happen. Distinguishing the
retryable outcome from the precondition failure matters where it is surfaced: one says
"read your messages", the other says "try again".

**Two callers race.** The loser is absorbed as already-terminal. No duplicate topic closure
and no duplicate notification.

**A driver fails to start after the record exists.** The engagement is marked errored, topic
cleanup is attempted, and the caller is told the start failed.

**Topic sends, driver teardown, notification and retention fail after the transition.** All
are caught and logged. **The terminal state stays committed** — so an engagement can be
genuinely finished while no completion message ever reached its topic and no notification
reached the resident. These are best-effort effects *after* the authoritative state change,
by design.

**A restart interrupts an engagement.** Persisted records load with `active` rewritten to
`idle`. Replay is attempted only for the driver kind that supports it. A record whose
workspace or recorded plugin artifact is missing is *refused* with a warning — validated
before the intact-service fast path, so an ordinary restart cannot start a service whose run
script would exit-and-respawn forever — and a missing definition is skipped with a warning.
A failed stdin-FIFO recreation and a failed service start are refusals of the same kind:
the record is marked errored and no background spool/relay machinery attaches, rather than
accepting operator messages into an engagement with no consumer (or starting one that
would crash-loop under its supervisor).

## Extension points

**A new driver** implements the driver protocol: start, send, cancel, resume, liveness.

**A new terminal path** should go through the shared finalize funnel to inherit the
single-winner transition, teardown, notification and retention. Setting a terminal status
directly gets none of that.

**A new durable field** must be added to the record, its load path and its write path
together — otherwise it exists at runtime and silently vanishes across a restart.

**A new origin value that may hold a live object** must be registered as non-persistable, or
serialization will either fail or persist something meaningless.

**A new topic output** should go through the per-engagement sequencer if its ordering
relative to narration matters. Direct sends exist as a fallback and bypass ordering.

**Not enforced anywhere**: nothing caps how deeply agents can create *engagements*. If that
matters for a change you are making, it needs new code — do not expect the delegation depth
cap to cover it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRecord`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.try_transition_terminal`
- `casa/rootfs/opt/casa/engagement_registry.py::TerminalPreconditionFailed`
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement`
- `casa/rootfs/opt/casa/tools.py::FinalizeResult`
- `casa/rootfs/opt/casa/tools.py::cancel_engagement`
- `casa/rootfs/opt/casa/drivers/driver_protocol.py::DriverProtocol`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver`
- `casa/rootfs/opt/casa/channels/output_sequencer.py::OutputSequencer`
- `casa/rootfs/opt/casa/casa_core.py::replay_undergoing_engagements`

**Tests**
- `tests/test_delegate_to_agent.py`
- `tests/test_delegate_to_agent_interactive.py`
- `tests/test_claude_code_driver.py`
- `tests/test_cancel_engagement_tool.py`
- `tests/test_engagement_registry.py`
- `tests/test_observer.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
<!-- END SOURCEMAP -->
