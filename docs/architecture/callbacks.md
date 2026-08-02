---
last_reviewed: 2026-08-02
---

# Authorization callbacks

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The facility that lets a plugin receive an external provider's browser redirect — the return
leg of an OAuth-style authorization flow — at a public, unauthenticated `GET /callback/<name>`
URL, deposits the result into an on-disk spool, and nudges the plugin's agent to collect it.
It covers the public endpoint, the spool protocol, the consent that opens a route, the
reconciler that routes it, the delivery nudge, and the validated base URL every redirect URI
is built from. It does not cover webhook *triggers* (`architecture/triggers.md`), webhook
authentication (`architecture/http-surface.md`), or what the collecting turn then does with
the code.

## Mental model

**Casa is the untrusted middle, not a party to the flow.** An external provider redirects a
browser to Casa carrying a bearer credential (an authorization code) in the query string; an
ephemeral plugin consumer, which minted the flow's `state`, later picks the result up. Casa
never parses what the consumer minted and never keeps a credential past its own short TTL. The
endpoint produces **no turn**: no ingress-identity row, no clearance, no provenance — a browser
redirect is not an authenticated principal.

**One wildcard route, allowlisted by an overlay.** As with triggers, there is a single
`GET /callback/{name}` route, not a route per callback. The name is looked up in a
plugin-callback overlay that the reconciler swaps atomically; a name absent from the overlay is
refused. `/callback/done` is a separate static route registered first so the terminal redirect
target can never be mistaken for a callback name.

**One response, always.** Success and every refusal cause — unknown name, missing or malformed
`state`, no live pending, replay, an existing result, a write failure, an internal fault —
return the *same* 303 redirect to the query-less `/callback/done`, with the same headers. A
differentiated status, header or target would be an enumeration oracle telling a prober which
names route and which states are live. There is deliberately no 429: flood handling damps
casa's internal log *emission*, never the HTTP response.

**Nothing query-derived is logged.** The query carries the credential. Handler logs carry a
reason enum, a correlation id and the *effective* name only — except an unrouted name, which
logs a fixed sentinel because the path component is attacker-controlled. Casa's access logger
suppresses the query for `/callback/`, and the in-container nginx access logs do the same via a
`map` rule.

**Consent is narrower than a trigger's, and bound to the declaration, not the artifact.** What
the operator approves is only "an unauthenticated GET may deposit a query blob into this
plugin's spool" — no role turn, no memory access, so there is no target, clearance or auth
policy to disclose. The consent identity is `(plugin, effective name, declaration digest)`; the
digest folds in only the declared name. A routine plugin upgrade that leaves `casa.callbacks`
unchanged keeps its ack, so a re-authorization flow never opens a dark window — unlike an
artifact-bound trigger ack.

**The spool is a same-uid, mtime-clocked, publish-once protocol.** Every plugin process runs as
root in one container, so the spool is not an inter-plugin security boundary; its guarantees are
against *itself* racing, crashing, or a swapped symlink. A pending file's mtime is its mint time
and survives the claiming rename; each TTL runs off its own file's mtime. Publication is always
a `link(2)` of an already-complete inode whose `EEXIST` is the atomic arbiter — the claim has
exactly one winner however many processes race, and a replayed redirect can never rewrite a
result. A claim also pins the *identity* of the plugin's spool directory: each directory carries
a random `.dir-id` token minted at creation, and discard/publish refuse when the token no longer
matches the one captured at claim time — so a plugin removal + reinstall mid-flow always fails
closed. The token carries this rather than the directory's `(st_dev, st_ino)` pair alone,
because a filesystem is free to hand a freed inode number straight back to the recreated
directory.

## Contracts & invariants

**INV-CB-001**: The callback route serves only names present in the consented overlay; a request for any other name performs no spool mutation.

Enforced by the wildcard handler, which looks the path component up in the reconciler's overlay
and, on a miss, returns the neutral redirect without touching the spool. The overlay is the sole
authority on what the endpoint serves — the spool's advisory `ready.json` marker cannot open a
route on its own.

What it does not cover: this is about ingress routing, not the credential's fate once deposited;
collection and the consumer's own handling are outside it.

**INV-CB-002**: A pending state is consumed at most once — the claim-by-rename is the consumption point — and a replayed redirect never rewrites or duplicates a result.

Enforced in the spool: a claim renames `pending/<hash>` into `.claims/`, and a result is
`link(2)`-published, so a second arrival for the same state finds no pending to claim and a
result that already exists, and mutates neither. The exactly-once property holds under
concurrent threads and concurrent processes alike.

What it does not cover: it bounds duplication *inside the spool*. A consumer that collects a
result and then acts non-idempotently owns that idempotency itself.

**INV-CB-003**: A callback ack binds `(plugin, effective, declaration_digest)`; the ack store fails closed whole-store on any malformed or key-mismatched record; and plugin removal revokes its acks and unroutes.

Enforced by the ack store's load path — wrong schema, any malformed record, or any key that does
not equal its record's recomputed identity yields *no* acks at all, never a partial store — and
by the reconciler, which prunes an ack no installed declaration can still compute and swaps the
overlay so a removed plugin's route goes dark.

What it does not cover: the identity deliberately excludes the artifact id, so a routine upgrade
that leaves the declaration unchanged keeps consent; only an operator-visible declaration change
(a rename, a later new field) mints a new identity and forces re-consent.

**INV-CB-004**: Casa relays the query opaquely — the raw query string plus an ordered list of decoded key/value pairs — and interprets only `state`; no provider-specific parsing lives in core.

Enforced in the handler: `state` is extracted from the *raw* query against a fixed grammar (so a
percent-encoded or duplicated `state` is a rejection, not a smuggled decode), and everything else
is recorded verbatim alongside an ordered decoded view for the consumer. Duplicate keys and their
order are preserved; undecodable bytes decode with replacement rather than being rejected.

What it does not cover: it makes no claim about the credential's *meaning* — casa neither
validates nor understands the provider's parameters beyond `state`.

**INV-CB-005**: For a syntactically-accepted GET that reaches the app, success and every refusal cause yield the same status, headers and redirect target, at any traffic level — there is no throttle response.

Enforced by routing every outcome through one neutral-redirect builder and by wrapping the whole
request path so no fault escapes as a differentiated 500; the per-key sampler damps only internal
log emission, never the response, and a drained sampler bucket still answers identically.

What it does not cover, by construction: rejections that never reach the app — a proxy-level
refusal or rate limit, request timing, and a non-GET method (answered by the framework's own 405)
— and an over-length request line (the framework's 400). These are documented as outside the
uniformity guarantee.

**INV-CB-006**: The callback query string never reaches the app access logger, the app handler and exception logs, or the in-container nginx access logs; an unrouted name logs a fixed sentinel.

Enforced on three surfaces: the access logger suppresses the query for the `/callback/` prefix;
the handler interpolates no request data into its log lines or its static pages and logs the
sentinel for an unrouted name; and an installed `logging.Filter` on the `aiohttp.server` logger
redacts the whole request target — path and query, message *and* exception traceback — from an
over-length request line that raises below the handler (redacting the entire target, not just the
query, so no inner quote in the query can leave a fragment behind). The in-container nginx access
log applies the same suppression by a `map` rule.

What it does not cover — documented, tested residuals: the in-container nginx *error* path on an
upstream failure, and the outer reverse proxy's own logs, which are operator-configured. The
`/callback/` access line still records the path (only its query is dropped).

## Failure behavior

**An unrouted name.** Neutral redirect, no spool mutation, one log line naming the fixed
sentinel rather than the attacker-controlled component (INV-CB-001, INV-CB-006).

**A missing, malformed, expired, replayed, or never-minted state.** All collapse to the same
neutral redirect; the spool refuses expired, replayed and never-minted claims identically, so the
handler logs `no_pending` for all of them (`expired` is the sweep's vocabulary, not the
handler's).

**A result write fails.** The claim is discarded and the state stays consumed (single-use); no
partial result is published, so the consumer must start a fresh authorization. The response is
still the neutral redirect (INV-CB-002, INV-CB-005).

**An internal fault anywhere on the request path.** Absorbed by the outer guard into the same
neutral redirect — a 500 would itself be a differentiated response (INV-CB-005).

**The ack store is missing, unreadable, or malformed.** Treated as zero acks; callbacks stay
unrouted, and the next successful `record` rewrites a valid store. It never raises into the
reconciler (INV-CB-003).

**`PUBLIC_URL` is unset or not a clean `https://` origin.** No redirect URI can be built; every
otherwise-routable plugin surfaces `callback_base_url_invalid`, and no readiness marker or index
entry is written. A bare IP, a path, userinfo, or an embedded control character is rejected the
same as unset.

**A reconcile compute fails.** The overlay fails closed to empty and the pruning of stale acks is
skipped — a resolution hiccup must never vaporize consent.

**A delivery nudge (`kick`) is lost to a crash.** The recovery pass — boot and periodic —
re-enqueues an episode for any result lacking an episode or tombstone, so the at-least-once flow
converges rather than dropping; a second nudge is idempotent for a consumer whose collection
against an emptied directory finds nothing.

## Extension points

**A new plugin callback** is a `casa.callbacks` entry naming a redirect endpoint — a peer of
`casa.triggers` but carrying only a name (no target, clearance or auth block). It routes only
after intrinsic validation passes, the plugin is assigned to at least one role, and a persisted
operator ack for the exact consent identity exists; the declaration has hard rails a plugin
author cannot infer from the routing model — at most four callbacks per plugin and an effective
name (`plg-<plugin>--<declared>`) capped at 128 characters. Reconciliation runs at the same seams
as the trigger reconciler: boot, plugin lifecycle changes, the trigger-affecting reload scopes,
the consent approve path, and the revoke tool.

**A bundled or sourced specialist dependency** *may* declare `casa.callbacks` — unlike
`casa.triggers`, which such a dependency may not — because a callback grants no turn or memory
access. Its owned registry entry routes under the *scoped* name (`slug.manifest_name`), so an
inspect-time gate refuses a callback whose scoped effective name would overflow the 128-character
cap (`callback_name_too_long`), before the entry can reach the registry.

**A single-callback off-switch** is the `callback_ack_revoke` tool: it drops every ack for one
`(plugin, effective)` callback across any declaration digest and reconciles, darkening that route
until the operator re-consents.

**Registering a redirect URI with a provider** uses `redirect_uri`, which joins the validated
base with `callback/<effective>` — the exact string the provider must be given, matched
byte-for-byte on the return leg. Changing how the base is validated or the URI is composed is the
one place a malformed value could reach a third party.

**Relying on a nudge alone for durability** is the wrong model: the delivery guarantee lives in
the recovery pass, not the request-path `kick`. A new result-delivery path must be reachable by
recovery or it can silently drop.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/callback_http.py::make_callback_handler`
- `casa/rootfs/opt/casa/callback_http.py::make_done_handler`
- `casa/rootfs/opt/casa/callback_http.py::install_callback_log_redaction`
- `casa/rootfs/opt/casa/callback_http.py::OutcomeSampler`
- `casa/rootfs/opt/casa/callback_spool.py::CallbackSpool`
- `casa/rootfs/opt/casa/callback_spool.py::CallbackSpool.claim`
- `casa/rootfs/opt/casa/callback_spool.py::CallbackSpool.publish_result`
- `casa/rootfs/opt/casa/callback_spool.py::mint`
- `casa/rootfs/opt/casa/callback_acks.py::CallbackAckStore`
- `casa/rootfs/opt/casa/callback_consent.py::render_callback_consent_message`
- `casa/rootfs/opt/casa/callback_reconcile.py::reconcile_plugin_callbacks`
- `casa/rootfs/opt/casa/callback_reconcile.py::compute_desired`
- `casa/rootfs/opt/casa/callback_episodes.py::kick`
- `casa/rootfs/opt/casa/callback_episodes.py::recovery`
- `casa/rootfs/opt/casa/callback_urls.py::validated_base`
- `casa/rootfs/opt/casa/callback_urls.py::redirect_uri`
- `casa/rootfs/opt/casa/plugin_callbacks.py::parse_and_validate`
- `casa/rootfs/opt/casa/plugin_callbacks.py::ack_identity`
- `casa/rootfs/opt/casa/plugin_callbacks.py::declaration_digest`

**Tests**
- `tests/test_callback_http.py`
- `tests/test_callback_spool.py`
- `tests/test_callback_acks.py`
- `tests/test_callback_reconcile.py`
- `tests/test_callback_consent.py`
- `tests/test_plugin_callbacks.py`
- `tests/test_callback_urls.py`

**Related**
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
<!-- END SOURCEMAP -->
