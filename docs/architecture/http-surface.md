---
last_reviewed: 2026-07-30
---

# The HTTP surface

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The two HTTP applications the main Casa process runs, what each exposes, and how a request
from outside is authenticated before it can reach an agent. It does not cover what an agent
does once reached, nor the voice transport's own protocol beyond the point where a request
becomes a turn. The container also ships a third, separately supervised loopback HTTP
service — the MCP bridge — which belongs to `architecture/mcp-and-tools.md`, not here.

## Mental model

Routes are registered on **two separate applications**, and which one a route lands on
decides who can call it.

The **public app** carries the externally-reachable routes: a dashboard, a health endpoint,
agent invocation, the Telegram update sink, a hook-resolution endpoint, an MCP endpoint,
conditionally-registered voice routes, and inbound webhooks — which are a *single wildcard
route* backed by a dynamically-maintained trigger allowlist, not per-trigger route
registrations. This document does not enumerate the routes; the registration block is the
authority and it changes.

The **internal app** carries routes intended for other processes in the container to call —
admin reload, personality and specialist endpoints, and a family of internal channel routes.
Its listener configuration, not its route table, is what makes it internal; check the runner
setup before assuming reachability either way.

**nginx runs two listeners with different security postures, and conflating them is the
easiest way to be badly wrong here.** The Home Assistant ingress listener carries a
server-scope source restriction to the supervisor address. The second listener is published
by the app manifest as an external API port — declared with host publication *disabled* by
default, so it is host-reachable only where the operator maps it — and carries **no source
restriction at all** —
it proxies to the same backend application. So "reachable through the host" is true of one
listener and not the other, and a route's exposure depends on which listener you arrive on.

The listeners differ in two ways, and both matter: the ingress listener carries the
source restriction above (and sits behind Home Assistant's own authentication), while the
external listener carries neither — and a set of explicit 404s on the external listener
refuses a handful of path prefixes that the ingress listener passes through. For deciding
*which backend routes the external listener proxies*, the 404 set is the boundary. Read the
server blocks before reasoning about who can reach what.

Authentication is **per route**, not ambient. There is no boundary that authenticates
everything arriving on the public app, so the question for any route is which check *it*
performs — and several routes perform none.

**The routes with no application-layer check are the ones to understand first.** An MCP
endpoint and a hook-resolution endpoint are unauthenticated at the backend and are protected
only by being 404'd on the external listener. They remain reachable over ingress and from
loopback. A new route registered near them inherits no protection from that arrangement; it
inherits only its own absence of a check.

## Contracts & invariants

**INV-HTTP-001**: `verify` authenticates webhook-trigger requests under one of three named modes — a body HMAC, a static header, or a timestamped HMAC — and returns a single boolean.

Not "webhooks use HMAC": a static-header trigger is authenticated by comparing a shared
value, with no HMAC involved. Which mode applies is configuration, so read the trigger's
mode before reasoning about what protects it.

Scope matters as much as the modes. **`verify` is the webhook-trigger verifier, not the
application's authentication layer.** Agent invocation, the voice transports and the
Telegram update sink each perform their own route-specific check against the one shared
configured webhook secret. Do not read
this invariant as describing what protects any route other than a webhook trigger.

**INV-HTTP-002**: Every secret comparison inside `verify` uses a constant-time primitive, and an absent or empty secret returns false rather than passing.

Fail-closed on a missing secret is the part worth remembering: within this verifier there
is no path that passes when configuration is incomplete. That is a statement about
`verify`, not about the application — routes that never call it are unaffected by it.

**INV-HTTP-003**: No mode prevents replay. The timestamped mode *bounds* it to a tolerance window; the other two accept a valid credential indefinitely.

This is the correction most worth reading carefully, because the intuitive reading of
"timestamped" is wrong. Nothing tracks nonces or spent signatures, so a captured
timestamped request can be replayed **repeatedly within its window**, and a timestamp
modestly in the future is accepted — the comparison is on absolute difference. Bounded
replay is a materially weaker property than replay prevention.

The other two modes have no bound at all. A captured body-HMAC signature authenticates its
own exact body for as long as the secret is accepted; it cannot authenticate a modified
body, which is a real but narrow protection. A static header is a bearer token: no body
binding, no nonce, no expiry.

The tolerance default is a literal in the code, not an absent value the operator must
supply, and configured values are constrained to a bounded range. Replay is in the threat
model for every mode; choosing a mode chooses how long the window stays open.

**INV-HTTP-004**: External context arriving on a request cannot set provenance fields; the ingress supplies them.

A payload that could name its own origin could claim any origin, and provenance is what
later decides what a turn may do — see `provenance.py` for what is stripped.

**INV-HTTP-005**: The ingress-identity table is validated at boot against an independently-written route contract, and any disagreement between the two is a boot failure.

Read the scope precisely, because the useful-sounding version of this is false. **The check
compares two hand-maintained declarations with each other. It does not inspect the
registered HTTP routes.** Adding a turn-producing route without touching either declaration
therefore produces no boot failure — the guarantee is that the two declarations cannot
drift apart, not that they cover the application. Keeping a new ingress honest is still a
matter of remembering to declare it.

What the check does enforce is worth having: both directions of set equality, so neither
declaration can gain or lose a route alone, plus per-route agreement on surface,
authentication flag and peer strategy.

Per request the identity function raises instead of returning anything a caller could
mistake for "no identity" — there is no quiet fallback to an anonymous or system speaker.
Automation ingresses are additionally prevented from resolving to an operator's identity,
which is what stops an unattended trigger from being recorded as a person.

## Failure behavior

**A signature is absent, malformed, out of tolerance, or wrong.** `verify` returns false in
every case. What the caller sees is the handler's decision, not this function's — read the
handler for the response.

**A secret is missing or empty.** `verify` returns false before doing anything else.

**The health endpoint** returns a fixed ok response without consulting agents or memory. It
reports that the process is serving requests and nothing more; treating it as a system
health signal reads more into it than it carries.

## Extension points

A new public route means choosing its authentication explicitly **and** knowing which
listeners will carry it. Nothing authenticates it for you, so a route with no check is
callable by whatever can reach the app — which, on the external listener, is whatever can
reach the published port. If the route should not be externally reachable, the 404 list on
that listener is what excludes it, and adding the route alone does not add the exclusion.

A route that produces a turn also needs an entry in both ingress-identity declarations.
Nothing detects its absence at boot, so this is a step to remember rather than one the
system enforces.

A new internal route belongs on the internal application, and is worth writing as though it
were reachable from outside — a later change to the listener is all it would take.

`read_secret` is the read path for webhook secret material, with validation and orphan
sweeping alongside it. Whether every secret in the system flows through it is not something
this document establishes; check the call sites for the one you care about.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/webhook_auth.py::verify`
- `casa/rootfs/opt/casa/webhook_auth.py::read_secret`
- `casa/rootfs/opt/casa/casa_core.py::healthz`
- `casa/rootfs/opt/casa/ingress_identity.py::validate_ingress_identity_table`
- `casa/rootfs/opt/casa/ingress_identity.py::ingress_identity`
- `casa/rootfs/opt/casa/provenance.py::sanitize_external_context`
- `casa/rootfs/etc/s6-overlay/scripts/setup-nginx.sh`
- `casa/config.yaml::ports`

**Tests**
- `tests/test_webhook_auth.py::test_hmac_body_valid`
- `tests/test_webhook_auth.py::test_hmac_body_wrong_secret_fails`
- `tests/test_webhook_auth.py::test_hmac_body_missing_header_fails`
- `tests/test_webhook_origin_containment.py`
- `tests/test_setup_nginx_ingress.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
