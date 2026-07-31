---
last_reviewed: 2026-07-31
---

# Triggers and scheduling

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What makes an agent act without a person speaking: scheduled triggers, webhook triggers,
and the plugin-declared triggers that need an operator's approval before they route. It does
not cover what the resulting turn does, nor webhook authentication mechanics, which belong to
the HTTP surface.

## Mental model

**Three trigger types exist for residents — interval, cron and webhook — and plugins may
declare webhooks only.**

**Every webhook trigger arrives on one wildcard route.** There is no route per trigger. The
name in the path is looked up against a registry, and an unknown name is refused before any
authentication happens.

**Plugin trigger routing is an overlay, replaced atomically.** It is a data structure, not a
set of registered routes, and reconciliation swaps the whole thing at once rather than
mutating entries. Resident registrations are untouched by that swap.

**Scheduled jobs do not survive a restart.** The scheduler is configured with no persistent
job store, so jobs live in process memory. Definitions are rebuilt from configuration at
boot, which makes it *look* durable — but next-run times and any occurrence missed while the
process was down are simply gone. The grace-period setting bounds lateness for a running
process; it cannot resurrect what was never recorded.

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
so a failure partway can leave that role with fewer triggers than it started with.

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
<!-- END SOURCEMAP -->
