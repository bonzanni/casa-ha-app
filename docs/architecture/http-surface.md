---
last_reviewed: 2026-07-30
---

# The HTTP surface

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The two HTTP servers Casa runs, what each exposes, and how a request from outside is
authenticated before it can reach an agent. It does not cover what an agent does once
reached, nor the voice transport's own protocol beyond the point where a request becomes a
turn.

## Mental model

Routes are registered on **two separate applications**, and which one a route lands on
decides who can call it.

The **public app** carries the externally-reachable routes: a dashboard, a health endpoint,
inbound webhooks, agent invocation, the Telegram update sink, a hook-resolution endpoint, an
MCP endpoint, and conditionally-registered voice and per-agent trigger routes. This document
does not enumerate them; the registration block is the authority and it changes.

The **internal app** carries routes intended for other processes in the container to call —
admin reload, personality and specialist endpoints, and a family of internal channel routes.
Its listener configuration, not its route table, is what makes it internal; check the runner
setup before assuming reachability either way.

nginx restricts ingress on the app's port to the Home Assistant supervisor address, so
"public" here means reachable through the host, not from the internet.

Authentication is **per route**, not ambient. There is no boundary that authenticates
everything arriving on the public app, so the question for any route is which check *it*
performs.

## Contracts & invariants

**INV-HTTP-001**: `verify` authenticates under one of three named modes — a body HMAC, a static header, or a timestamped HMAC — and returns a single boolean.

Not "webhooks use HMAC": a static-header trigger is authenticated by comparing a shared
value, with no HMAC involved. Which mode applies is configuration, so read the trigger's
mode before reasoning about what protects it.

**INV-HTTP-002**: Every secret comparison inside `verify` uses a constant-time primitive, and an absent or empty secret returns false rather than passing.

Fail-closed on a missing secret is the part worth remembering: there is no unauthenticated
path that runs when configuration is incomplete.

**INV-HTTP-003**: Only the timestamped mode has a replay window. The body-HMAC and static-header modes accept a valid credential indefinitely.

This asymmetry is the thing to carry away, and it is easy to read past. A captured
body-HMAC signature replays for as long as the secret lives, and a static header is a
bearer token — no body binding, no nonce, no expiry. Only the timestamped mode compares
against a tolerance and refuses what falls outside it. Choosing a mode is therefore
choosing whether replay is in the threat model, and the default tolerance is a
configuration value, not a constant.

**INV-HTTP-004**: External context arriving on a request cannot set provenance fields; the ingress supplies them.

A payload that could name its own origin could claim any origin, and provenance is what
later decides what a turn may do — see `provenance.py` for what is stripped.

**INV-HTTP-005**: Every external entry point must be able to say who spoke. The ingress-identity table is validated at boot against an independently-written contract, and a route that cannot name its speaker is a boot failure.

The check is deliberately redundant: the table and the contract are separate declarations,
so adding a route in one place without the other fails rather than silently inheriting a
default. Per request the same function raises instead of returning anything a caller could
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

A new public route means choosing its authentication explicitly. Nothing authenticates it
for you, so a route with no check is callable by whatever can reach the app.

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
- `casa/rootfs/opt/casa/ingress_identity.py`
- `casa/rootfs/opt/casa/provenance.py`

**Tests**
- `tests/test_webhook_auth.py::test_hmac_body_valid`
- `tests/test_webhook_auth.py::test_hmac_body_wrong_secret_fails`
- `tests/test_webhook_auth.py::test_hmac_body_missing_header_fails`
- `tests/test_webhook_origin_containment.py`
- `tests/test_setup_nginx_ingress.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
