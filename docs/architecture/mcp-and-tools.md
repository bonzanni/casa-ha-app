---
last_reviewed: 2026-07-30
---

# The MCP surface and the tool boundary

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How tools reach an agent and what stands between a tool call and its execution. It covers the
separate bridge service, the internal dispatch path, and where authorization actually
happens. It does not cover individual tools, nor the MCP protocol itself.

## Mental model

**There are two surfaces onto the same tools, and they filter differently.** The SDK surface
presents an agent a role-filtered set. The HTTP bridge advertises the full static set. Which
tools an agent *sees* therefore depends on which surface it came through.

**The dispatch layer does not enforce the agent-level allowlist.** This is the single most
important thing on this page. The internal tool-call handler looks a name up in the full
dispatch map and invokes it; it never consults an agent's declared tool list. That list is
enforced *upstream*, by the SDK's own permission machinery and by hooks, before a call is
ever dispatched — and that upstream enforcement has modes: an executor engagement running in
an autonomous permission mode short-circuits the relay to allow *before* the declared list is
consulted, so of the two declared lists only the disallowed one still prohibits there. The
code-mandatory guard hooks and tool-local gates are separate from both lists and keep
denying regardless of mode.

The consequence is worth stating plainly: **the allowlist is a constraint on the agent, not a
boundary at the tool.** Anything able to reach full-map dispatch directly is not constrained
by it. There are two such reaching points, not one: the internal endpoint (a
permission-restricted Unix socket), and fallback MCP and hook-resolution routes on the main
loopback application for in-container workspace subprocesses. The second is where "the
boundary is the container" needs care: the external nginx listener refuses those paths, but
the Home Assistant ingress listener proxies them — so an HA-authenticated ingress caller
outside the container can reach full-map dispatch. The boundary around it is HA's own
authentication, not the container wall.

Individual tools may still refuse individual operations. Those are tool-local gates, not a
universal authorization check.

**The bridge runs as its own supervised service** so that the bridge *connection* survives a
restart of the main application. Its own client is a thin shell shim, and the failure
semantics are two-layered and opposite: the shim **fails open** — when its own HTTP call to
the bridge fails, it returns an allow decision rather than blocking (an unreachable Casa
should not wedge a running engagement) — but the bridge itself **fails closed**: when it is
up and the main application's socket is not, it answers with an explicit deny, and the shim
relays that deny. So hook policy is unenforced only when the *bridge* is unreachable; a
main-application restart denies rather than allows. Anything that must hold regardless
belongs in a tool, not in a hook.

## Contracts & invariants

**INV-MCP-001**: The internal tool dispatch path resolves a call by name against the full tool map and does not consult any per-agent allowlist.

Stated as an invariant because its absence is load-bearing. Enforcement of what an agent may
call happens before dispatch, not at it.

What it does not cover: tool-local checks still apply, and a terminal-binding subset is
treated specially.

**INV-MCP-002**: The internal endpoint is reachable only from inside the container, over a Unix socket with restricted permissions.

This is the boundary that actually contains the property above. If reasoning about who can
call a tool, reason about who can reach that socket.

What it does not cover: the *fallback* full-map routes on the main loopback application.
Those are refused by the external nginx listener but proxied by the HA ingress listener, so
they are contained by Home Assistant's authentication rather than by this socket boundary
(see the mental model).

**INV-MCP-003**: The two surfaces expose different tool sets — role-filtered on the SDK side, the full static set over HTTP.

What it does not cover: being advertised is not being permitted. The HTTP advertisement
describes what the bridge can route, not what any particular agent may invoke.

## Failure behavior

**An unknown tool name.** Resolution fails and the call is refused; nothing is invoked.

**The bridge service is unreachable.** The shim returns an allow decision. A hook that would
have denied the call does not run, so the call proceeds. This is why a hook is not the right
place for a constraint that must never be bypassed.

**The bridge is up but the main application is not.** The opposite of the case above: the
bridge answers hook resolution with an explicit deny, and the shim relays it. Hook-gated
calls fail closed for the duration of a main-application restart.

**A tool raises.** The failure is returned in the response envelope rather than propagating
as a transport error, so a failing tool is a result, not a broken connection.

## Extension points

**A new tool** is added to the tool table, which is what both surfaces are built from. Adding
it there makes it dispatchable; making it *reachable* by a given agent is a separate question
of that agent's declared tools.

**A new constraint on tool use** should be placed deliberately. A check in the tool runs for
every caller; a check in the agent's declared list runs only for agents that go through the
SDK path. If the intent is "nothing may do this", the tool is the place.

**Anything that assumes the allowlist is a security boundary** needs re-examining against the
dispatch path first.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/internal_handlers.py::_make_internal_tools_call_handler`
- `casa/rootfs/opt/casa/svc_casa_mcp.py`
- `casa/rootfs/opt/casa/tools.py::init_tools`

**Tests**
- `tests/test_internal_handlers.py`
- `tests/test_svc_casa_mcp.py`
- `tests/test_mcp_envelope.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
<!-- END SOURCEMAP -->
