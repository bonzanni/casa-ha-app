---
last_reviewed: 2026-07-31
---

# Triggers and scheduling

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What makes an agent act without a person speaking: scheduled triggers, webhook triggers,
and the plugin-declared triggers that need an operator's approval before they route. It does
not cover what the resulting turn does, nor webhook authentication mechanics, which belong to
the HTTP surface. Plugin-declared *authorization callbacks* share this document's
wildcard-route, overlay and durable-consent shape but produce no turn and grant no access —
they are `architecture/callbacks.md`.

## Mental model

**Four trigger types exist for residents — interval, cron, date and webhook — and plugins may
declare webhooks only.**

**A reminder is a trigger, not a separate thing.** A resident creates one through a narrow
writer that may only touch entries it owns; everything downstream — registration, firing,
listing — is the ordinary trigger path. One-off reminders use the point-in-time `date`
type, because cron has no year field and a dated one-shot written as cron is an *annual*
trigger in disguise.

**One file, ownership per entry.** A reminder is an ordinary entry in the role's
`triggers.yaml`, marked `managed_by: agent`. Reminders once had a file of their own,
because reconciliation resolved an edited image-owned file against a changed shipped
default as "image wins" and would have deleted every pending reminder on such an update.
That file is gone: `triggers.yaml` is now reconciled *per entry*, so an entry the image has
never shipped is preserved rather than dying with the file
([`architecture/configuration.md`](configuration.md)).

**Ownership is data, and is never inferred from an entry's content.** The schema permits an
operator to write a `reminder-`-prefixed dated one-shot of their own, so neither the
reserved name prefix, nor the `date` type, nor the `one_shot` flag distinguishes an agent's
reminder from an operator's trigger — only the explicit field does. Everything that may
sweep, re-register, or delete an entry keys off it. Inferring ownership from any of those
shapes instead produced a fresh way to delete a live operator trigger every time it was
attempted.

**The file is machine-maintained, and only meaning is preserved across a rewrite.**
Operators change triggers by asking the configurator agent, not by hand-editing; both that
agent and per-entry reconciliation already reconstruct the file through a plain YAML dump.
The reminder writer is a third writer of the same kind, so comments, quote styles and key
order are not preserved — an entry's *meaning* is. One consequence has teeth: environment
interpolation is substituted into the file's **text** before it is parsed, so a rewritten
`${VAR}` reference can resolve differently afterwards. The writer refuses to add a reminder
to a file containing one rather than risk it, and no shipped configuration uses them.

**A reminder still present with a past fire time is one that is owed.** The entry is the
record and delivery removes it, so there is no second store to keep in sync. This is what
lets a sweep recover occurrences the process was down for — something the scheduler itself
cannot do. Ownership is exclusive: while a live job exists the scheduler owns delivery and
the sweep leaves it alone.

**Removing a delivered reminder must never be blocked by something the sweep tolerates.**
Delivery and cleanup are two steps over the same file, so any check present in the second
and absent from the first lets the sweep deliver an entry it cannot then remove — and
presence being the ledger, it redelivers on every pass indefinitely. Both steps therefore
read through one function, and cleanup deliberately skips the whole-document schema check
that creation performs: a validation failure elsewhere in the file is unrelated to the entry
being removed. Reading a file and re-writing it can still fail *independently* — a document
nested deeply enough parses but cannot be re-emitted — so what is actually guaranteed is
narrower and worth stating precisely: every such failure lands inside the handled error
contract, making the worst case the ordinary at-least-once one rather than an aborted pass.

**A pre-existing schema defect in another entry never blocks either operation.** A running
Casa holds the configuration it booted with, so the file on disk can already be invalid while
everything works — the configurator refuses a commit that fails validation but leaves its
edits in the working tree. Refusing to touch such a file protects nothing, since it already
fails to load, while making reminders unavailable and naming an entry nobody asked about. So
creation validates *the entry it is adding*, on its own; cleanup validates nothing. Judging
the new entry alone is exact rather than approximate — the schema has no constraints that
span entries, and the one real cross-entry hazard, a duplicate name, is refused separately.
The entry is still judged under the file's real top level, since the schema version decides
what is legal there, so a defect in the top level itself does refuse creation — a deliberate
boundary, not a leak. And only the *judgment* is scoped this way: a sibling entry that cannot
be read, or cannot be written back out, blocks both operations, because there is then no way
to rewrite the file at all.

**A recurring reminder keeps its first occurrence as an anchor.** The derived cron fields
drive the recurrence — evaluated in the scheduler's timezone, which is what keeps a series
firing at the same local time across a DST boundary — while the anchor becomes the
scheduler's start date, so "every Thursday from the 20th" cannot fire on the 6th.

**What a cron cannot express exactly is refused, not approximated.** A repeating reminder must
land on a whole minute, and a monthly one on the 28th or earlier — cron has minute resolution
and skips months a literal 29th, 30th or 31st is missing from, so "monthly on the 31st" would
fire seven times a year rather than twelve. Every approximation tried here made the time the
user was told differ from the time that fires, so the request is rejected and the agent asks
for something expressible instead.

**Wall-clock fields are read in the scheduler's timezone**, not the caller's offset. The
offset pins which instant is meant; the cron is evaluated in the scheduler's zone, so deriving
the fields from a caller's offset would misschedule by the difference whenever the two
disagree, and drift across a DST boundary.

**The file is the truth; the scheduler is a cache of it.** The sweep reconciles in both
directions — registering any agent-owned reminder with no live job, and dropping any
agent-owned job with no entry left — which heals a divergence without needing a lock. Both
directions are bounded by recorded ownership, never by the name: an operator's own trigger is
neither registered nor dropped here, and matching on the reserved prefix would drop the one
they are allowed to author. A reload re-registering a role from a snapshot taken before a
reminder was written would otherwise drop a *recurring* reminder for good, since only
one-shots are recoverable by delivery; the same race in the other direction would leave a
cancelled reminder firing forever.

Sharing the operator's file makes one previously incidental property load-bearing: a
`triggers.yaml` that cannot be read *or written back* must be contained to its own role
rather than aborting the pass, or one bad file would strand every later role's overdue
reminders. An unreadable file suspends *both* directions for that role — reporting it as
empty would authorise dropping every one of its reminder jobs.

**Every webhook trigger arrives on one wildcard route.** There is no route per trigger. The
name in the path is looked up against a registry, and an unknown name is refused before any
authentication happens.

**Plugin trigger routing is an overlay, replaced atomically.** It is a data structure, not a
set of registered routes, and reconciliation swaps the whole thing at once rather than
mutating entries. Resident registrations are untouched by that swap.

**Cron fields follow the crontab convention, including day-of-week numbering.** A numeric
day-of-week uses cron's 0/7 = Sunday; registration translates it into day names before it
reaches the scheduler, whose own 3.x numbering starts the week on Monday — passing the
number through verbatim is exactly the silent Sunday-fires-Monday misschedule this
translation exists to prevent.

**Scheduled jobs do not survive a restart.** The scheduler is configured with no persistent
job store, so jobs live in process memory. Definitions are rebuilt from configuration at
boot, which makes it *look* durable — but next-run times and any occurrence missed while the
process was down are simply gone. The grace-period setting bounds lateness for a running
process; it cannot resurrect what was never recorded. Reminders are the one exception, and
only because they do not rely on the scheduler to remember: their entry on disk is the record
that delivery is owed, so a sweep can redeem an occurrence the scheduler never saw.

**A plugin's declared webhook does not route because the plugin is installed.** Declaration
is only intrinsic validity. Routing additionally requires the target to exist and accept
webhooks, the plugin to be assigned to that target, a secret to back the chosen
authentication mode, and a durable operator approval bound to the exact trigger identity.
Until all of those hold, the name is not in the overlay and the route returns not-found.

**Approval is all-or-nothing per plugin.** If one declared trigger fails any check, none of
that plugin's triggers route. Partial routing is deliberately not offered.

## Contracts & invariants

**INV-TRIG-001**: A resident's scheduled trigger registers only if the resident declares the channel it names.

Enforced at registration, which raises rather than registering a trigger that would fire into
a channel the agent does not have.

What it does not cover: it does not establish that the channel is working, only that it is
declared. And it is genuinely *scheduled-only*: a resident webhook trigger registers and
dispatches without any channel-declaration check at all.

**INV-TRIG-002**: Webhook trigger names are unique, and the user and plugin namespaces cannot collide.

Enforced by rejecting a name already owned by another role, and structurally by the schema
reserving the plugin prefix so a user trigger can never take a plugin-shaped name.

**INV-TRIG-003**: A plugin's triggers route only as a complete set, and only when target, assignment, secret backing and a persisted operator approval all hold.

Enforced during reconciliation, which computes the desired set and refuses with a specific
reason for each missing precondition.

What it does not cover: intrinsic validation happens earlier and separately, and passing it
means only that the declaration is well-formed.

**INV-TRIG-004**: A trigger approval is persisted and bound to an exact identity, and an unreadable or mismatched approval store yields no approvals.

Enforced by an atomic write, and by a load path that treats anything malformed or
identity-mismatched as zero approvals rather than trusting it. This approval outlives a
restart — as do the specialist and persona install acknowledgements, which keep their own
stores — so it fails closed on read.

"Exact identity" is a specific tuple: plugin, artifact id, effective name, target, and the
normalized auth policy. **Clearance is not in it** — a clearance change on a trigger installs
under the old approval without renewed consent. Everything in the tuple, including any auth
mode, header or tolerance change, does invalidate the approval.

**INV-TRIG-005**: Reconciliation replaces the entire plugin overlay in a single rebind.

There is no window in which the overlay is half-updated. Names absent from the new set stop
routing immediately.

**INV-TRIG-009**: Firing a one-shot trigger unconditionally drops its scheduler job, and removes its `triggers.yaml` entry only when the agent owns that entry.

Both steps run after the dispatch, in process. Dropping the job is unconditional — a
one-shot that kept its job could fire again, and the id must be freed so the same name can be
registered later. "Drops" means the removal is attempted and the trigger is forgotten either
way: a scheduler that refuses to remove the job is treated as already-gone, so the id is
still freed. That is deliberate, since the alternative is a name that can never be
re-registered, but it means the guarantee is about Casa's own bookkeeping rather than about
the scheduler's internal state. Deleting the *entry* is gated on ownership, because an operator's dated
one-shot lives in the same file and removing their line is not the registry's business. The
red case is either half inverted: a `one_shot` job that survives its own firing, or an
operator's entry deleted because it fired.

An earlier form of this rule promised the entry was always removed. That is no longer true,
and the difference is a deliberate outcome rather than a gap: **an operator's unmarked
one-shot lingers inert after firing** — never re-registered, because a past-dated trigger is
not registered at boot, and never delivered by the sweep, because it carries no ownership
marker.

What it does not cover: it does not promise the entry removal *succeeded*. Cleanup is
deliberately outside the delivery path, so a failure leaves the entry for the sweep rather
than raising back into the scheduled job.

**INV-TRIG-010**: The reminder writer may only create, and the canceller only remove, entries marked as agent-owned in the calling role's own file.

This is the whole boundary between a resident managing its own reminders and a resident
editing operator configuration. The red case is either tool touching an entry that carries no
ownership marker — the heartbeat, the morning briefing, or a `reminder-`-prefixed dated
one-shot the operator wrote themselves — or reaching another role's file. Creation also
refuses a name already present under any owner, because a duplicate name is refused at
registration and that is uncaught at boot.

An earlier form of this rule bounded both tools by the reserved *name prefix* instead. The
prefix was never sound as an authorization predicate: the schema permits an operator to
author a name carrying it, so it identifies a naming convention and not an owner. It survives
only as the shape of a generated name.

The privileged config-commit path keeps its own separate configurator-only guard; this is a
narrower door beside it, not a widening of that one.

**INV-TRIG-008**: A one-shot reminder whose time passed while the process was down is delivered by the next sweep rather than dropped.

Presence is the record: an entry still on disk with a past fire time *is* the evidence that
delivery is owed, which is why removal happens only after a successful send. The red case is
an overdue entry that no sweep ever delivers — and the sharpest form of it is the sweep
reading the wrong file, since a past-dated trigger is deliberately left unregistered *for*
the sweep, so nothing else would ever deliver it. Delivery is consequently at-least-once — a
failed removal redelivers — because a duplicate reminder is a better failure than a missing
one. The scheduler and the sweep never both deliver: the sweep skips any reminder that still
has a live job, so the two never race for one whose time has just passed.

What it does not cover — **"delivered" means placed on the bus, not received by the human.**
The entry is removed once the turn is dispatched, so a reminder lost further down the channel
(a Telegram send that fails while the transport is reconnecting) is not retried. This is the
same contract every other trigger has had since the beginning, and closing it would need an
end-to-end receipt through the whole turn pipeline rather than anything reminder-specific. It
is a known residual, not an oversight.

## Failure behavior

**A webhook body is too large.** Requests are hard-capped at 64 KiB — chunked or not — and
refused with 413 *before* authentication or dispatch, so an oversized producer never
reaches its trigger.

**An unknown webhook name.** Not-found, with no turn dispatched — and the name check happens
*after* the body has been read and size-capped, so an unknown name still consumes the
request.

**Authentication fails, the body is too large, or rate limiting applies.** Refused with the
corresponding status. A malformed body that is not valid JSON is *not* refused — it is
absorbed as text and dispatched, once authenticated.

**Reconciliation raises.** The overlay is replaced with an empty one before the exception
propagates, so a failure removes plugin routing rather than leaving a stale set. Note that
some comments elsewhere describe the opposite; the swap is what happens.

**A resident's trigger registration fails at boot.** It is not caught, so it stops boot.
The pre-commit config gate replays the same registration into a throwaway registry, so a
trigger set that passes the schema but cannot register — duplicate names, an undeclared
channel, an out-of-range cron field — is refused at commit time rather than discovered as
a boot loop. Re-registration later behaves differently: the old entries are removed first,
and a failure partway unwinds the partially-installed replacements too, leaving the role
with *no* triggers — the fail-closed state the reload error reports. The one exception is a
scheduler that refuses to *remove* an existing job: re-registration then refuses, the stuck
job stays live and tracked while the role's webhook entries are already unregistered, and
the error names exactly the jobs that remain.

**The approval store is missing or corrupt.** Treated as no approvals. Pending routes stay
absent rather than opening.

**A role's `triggers.yaml` cannot be parsed while the process is running.** The sweep skips
that role entirely — no delivery, no reconciliation in either direction — and continues with
the others. Setting a reminder fails with the parse error rather than rewriting a file it
cannot read.

**A reminder is delivered but its entry cannot be removed.** This state is reachable and is
not treated as an error to be prevented: a document nested deeply enough to parse but not to
be re-emitted is the clearest case, and a full disk is the ordinary one. The guarantee is
*containment*, not removal — the failure is reported, the entry stays, the remaining roles are
still swept, and the reminder is delivered again on the next pass. That is the at-least-once
contract working as intended, because a duplicate nudge is a better failure than a missed
reminder. What is ruled out is the failure escaping and aborting the pass, which would strand
every later role's overdue reminders too.

**The file already contains `${VAR}` interpolation.** Setting a reminder is refused, because
re-emitting the file could change what an existing entry resolves to. Cancelling or sweeping
one is *not* refused — it warns and proceeds, since blocking cleanup is what strands a
delivered reminder into redelivering forever.

## Extension points

**A new resident trigger type** touches the schema, the loader, registration and dispatch —
the current set is four.

**A new resident webhook** needs the trigger declaration and a name outside the reserved
plugin prefix. Declaring the webhook channel on the resident is *not* checked for webhooks —
the channel gate is scheduled-only (see INV-TRIG-001).

**A new plugin trigger** needs the manifest declaration, an assigned target that accepts
webhooks, secret backing, and operator consent — and reconciliation must then run. The
declaration itself has hard rails a plugin author cannot discover from the routing model:
at most eight triggers per plugin, effective names capped at 64 characters, and
provider-owned secrets rejected outright. Secret backing is mode-specific: static-header
and timestamped modes get a per-trigger secret minted eagerly after consent into the
webhook-secrets state directory, while body-HMAC rides the one global webhook secret —
provisioning the wrong kind leaves the plugin unroutable. Resident trigger files have
their own schema rails: v2 forbids a webhook `path` (the wildcard route provides it),
while legacy v1 required one, and a scheduled trigger takes exactly one of an inline
prompt or a prompt file.
Reconciliation is hooked at boot, at plugin lifecycle changes, at consent and revocation,
and at exactly four reload scopes: triggers, agent, agents, and full. The policies and
config-sync reloads refresh agent configuration without reconciling the plugin overlay, so
a routing-relevant change arriving through those leaves the old overlay live until a
covered scope runs.

**Anything relying on a missed schedule being caught up** needs a persistent job store first;
there is none today.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/trigger_registry.py::TriggerRegistry`
- `casa/rootfs/opt/casa/trigger_registry.py::TriggerRegistry.replace_plugin_overlay`
- `casa/rootfs/opt/casa/trigger_reconcile.py::compute_desired`
- `casa/rootfs/opt/casa/trigger_acks.py::TriggerAckStore`
- `casa/rootfs/opt/casa/plugin_triggers.py::parse_and_validate`
- `casa/rootfs/opt/casa/casa_core.py::_make_webhook_handler`

**Tests**
- `tests/test_config_triggers_schema.py`
- `tests/test_trigger_consent.py`
- `tests/test_agent_loader_trigger_auth.py`
- `tests/test_casa_reload_triggers_resident.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
<!-- END SOURCEMAP -->
