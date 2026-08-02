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
writer that may only touch entries carrying a reserved name prefix; everything downstream —
registration, firing, listing — is the ordinary trigger path. One-off reminders use the
point-in-time `date` type, because cron has no year field and a dated one-shot written as
cron is an *annual* trigger in disguise.

**Reminders live in their own file, and that placement is load-bearing.** They are declared
in an agent-owned `reminders.yaml` beside `triggers.yaml`, and the loader merges the two into
one list. The separation exists because configuration reconciliation resolves an edited
image-owned file against a changed shipped default as *image wins* — so reminders kept in
`triggers.yaml` would be deleted wholesale by the first update that touched its default,
which is precisely the durability this feature exists to provide. A file absent from the
defaults tree is adopted and never rewritten.

**A reminder still present with a past fire time is one that is owed.** The entry is the
record and delivery removes it, so there is no second store to keep in sync. This is what
lets a sweep recover occurrences the process was down for — something the scheduler itself
cannot do. Ownership is exclusive: while a live job exists the scheduler owns delivery and
the sweep leaves it alone.

**A recurring reminder keeps its first occurrence as an anchor.** The derived cron fields
drive the recurrence — evaluated in the scheduler's timezone, which is what keeps a series
firing at the same local time across a DST boundary — while the anchor becomes the
scheduler's start date, so "every Thursday from the 20th" cannot fire on the 6th. A cron
expression has minute resolution, so a sub-minute anchor is rounded *up* to the next whole
minute and everything — schedule, anchor, and the time reported back to the user — is derived
from the rounded value. Rounding down would be wrong twice: the truncated minute may already
have passed, delaying the first occurrence by a whole period, and the series would fire
seconds before the time the user was promised. A monthly reminder past the 28th means
end-of-month rather than a literal day number: cron skips months a literal 29th, 30th or 31st
is missing from, so "monthly on the 31st" would fire seven times a year rather than twelve.

**The store is the truth; the scheduler is a cache of it.** The sweep re-registers any
reminder that has no live job, which is what heals a divergence rather than a lock — a reload
re-registering a role from a snapshot taken before a reminder was written would otherwise
drop a *recurring* reminder for good, since only one-shots are recoverable by delivery.

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

**INV-TRIG-006**: A one-shot trigger fires at most once — firing removes both the scheduler job and the `triggers.yaml` entry.

Removal runs after the dispatch, in process, and frees the job id so the same name can be
registered again. The red case is a `one_shot` trigger that survives its own firing: either
the job stays in the scheduler, or the entry stays on disk and a restart resurrects an
already-delivered reminder.

What it does not cover: it does not promise the removal *succeeded*. Cleanup is deliberately
outside the delivery path, so a failure leaves the entry for the sweep rather than raising
back into the scheduled job.

**INV-TRIG-007**: The reminder writer may only create, and the canceller only remove, entries carrying the reserved name prefix in the calling role's own file.

This is the whole boundary between a resident managing its own reminders and a resident
editing operator configuration. The red case is either tool touching a name without the
prefix — the heartbeat, the morning briefing — or reaching another role's file. The
privileged config-commit path keeps its own separate configurator-only guard; this is a
narrower door beside it, not a widening of that one.

**INV-TRIG-008**: A one-shot reminder whose time passed while the process was down is delivered by the next sweep rather than dropped.

Presence is the record: an entry still on disk with a past fire time *is* the evidence that
delivery is owed, which is why removal happens only after a successful send. The red case is
an overdue entry that no sweep ever delivers. Delivery is consequently at-least-once — a
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

## Extension points

**A new resident trigger type** touches the schema, the loader, registration and dispatch —
the current set is exactly three.

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
