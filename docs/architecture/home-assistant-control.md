---
last_reviewed: 2026-07-31
---

# Home Assistant device control

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an agent reads and changes the state of the house. It does not cover Home Assistant
itself, nor how entities are exposed on the Home Assistant side — which, as this document
explains, is where the actual limits live.

## Mental model

**Casa is not the authorization boundary for what an agent may control.** This is the single
most important thing here, and it is the opposite of what the architecture suggests. No code
in this application reads Home Assistant's entity registry or refuses an action because of
the entity or domain it names. Action arguments are forwarded unchanged for ordinary tools —
the one exception is the live-context tool, whose upstream arguments the facade replaces
outright (a requested domain never leaves the facade) and whose response it domain-filters
only when the upstream answers in the legacy mapping shape — the current curated
success/result shape passes through unfiltered.

Two different limits apply, and they answer different questions. *Which agents can talk to
Home Assistant at all* is Casa configuration: an agent reaches these tools only if its
configuration names the Home Assistant MCP server — in the shipped fleet that is the butler
alone. *What a connected agent may touch* is **Home Assistant's own exposure configuration**
— which entities are exposed to its assistant surface. That is upstream,
operator-controlled, and outside this repository. If you need a connected agent not to be
able to touch something, that is where to do it; adding a per-entity check here would be
adding the first one.

**A facade wraps the underlying tool surface**, curating what is offered and doing some
filtering of what comes *back*. Read filtering and write authorization are different things,
and only the first is happening. The curation is dynamic, not an allowlist: the facade
mirrors every upstream tool that carries a valid object schema, skipping malformed ones
individually and substituting only the live-context tool's schema — there is no manual
mapping to add a tool to.

**Two environment switches shape the integration itself.** `CASA_HA_MCP_URL` redirects the
Home Assistant MCP endpoint that both the raw registration and the facade connect to — the
first place to look when control fails against the wrong upstream. And
`CASA_DISCOVERY_AUTH_ENABLED` governs Supervisor discovery: when on, Casa publishes an
authenticated discovery record — its external-port endpoint *carrying the webhook secret*
— to Home Assistant, persists only the discovery UUID locally, and withdraws the record
when turned off. That publication is how the companion integration finds Casa, and it is
also a credential leaving the container.

**The facade is conditional, and its absence is not a closed door.** It applies only when its
preconditions hold, and it absorbs its own startup failure. The raw tool surface is
registered separately and earlier — so a facade that fails or does not apply leaves the raw
path in place rather than removing access. Degradation here means *less curation*, not less
capability.

**The facade's tool cache has no expiry.** It holds until an explicit refresh or a recovery
after a transport failure. Staleness therefore means newly added tools are invisible and
removed ones are still offered — it is a cache of the *tool surface*, not of house state, so
a stale cache does not mean stale readings.

## Contracts & invariants

**INV-HA-001**: No code in this application restricts a control action by entity or domain.

Stated as an invariant precisely because it is an absence. Action arguments pass through
unchanged; the only filtering applied is to returned content on the read path.

What it does not cover: this says nothing about what Home Assistant will accept. Exposure
settings there are the real constraint.

**INV-HA-002**: A failed or inapplicable facade leaves the raw tool surface registered.

What it does not cover: the raw surface is less curated, so the failure mode is a different
tool surface rather than an unavailable one. Code that assumes "no facade means no HA access"
is wrong — with one precondition: the raw surface itself registers only when the supervisor
token is present, so in a tokenless environment facade and raw path are absent together and
there genuinely is no HA access.

**INV-HA-003**: The facade's cached tool surface has no time-based expiry.

Refresh happens explicitly or on recovery. Nothing ages it out.

## Failure behavior

**The facade cannot start.** Absorbed; the raw path remains. Agents keep HA access with a
different surface.

**A transport failure occurs during a call.** Recovery is attempted, and one scheduled
refresh runs. Note there is no backoff and no retry timer — if that attempt fails, nothing
retries until another call triggers recovery.

**Home Assistant refuses an action.** The refusal comes back from upstream. Casa did not
prevent it and does not distinguish it from any other upstream error, so read the returned
payload rather than assuming a Casa-side check ran.

## Extension points

**Restricting what an agent may control** is currently done in Home Assistant, not here. If a
Casa-side restriction is genuinely wanted, it would be new — there is no existing hook to
extend, and it would need to sit where action arguments are forwarded.

**Adding a tool to the curated surface** means the facade's mapping. Remember the cache: an
addition is invisible until a refresh.

**Anything relying on cache freshness** needs an explicit refresh, since none happens on a
timer.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/ha_mcp_facade.py::HomeAssistantFacade`
- `casa/rootfs/opt/casa/mcp_registry.py::McpServerRegistry`

**Tests**
- `tests/test_ha_mcp_facade.py`
- `tests/test_ha_mcp_url_override.py`
- `tests/test_mock_ha_mcp.py`

**Related**
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
