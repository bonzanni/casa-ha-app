---
last_reviewed: 2026-08-01
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

**An engagement identity is authenticated, not merely claimed.** A tool call that names an
engagement id binds that engagement's record — and with it the record-derived role that
tool-local gates authorize against — only when it also presents the per-engagement secret
token minted at record creation and provisioned into that engagement's own workspace. The id
alone is deliberately treated as public information (it appears in the workspace MCP
configuration, in logs, and on shared loopback endpoints): a known id with a missing or
mismatched token is rejected outright rather than downgraded to an unauthenticated call, on
the paths that resolve the record for tool authority — the internal socket handler, the
in-process fallback twin, and the engagement-channel routes that act on a record's topic and
questions. An id the registry does not know still dispatches unbound, so a stale workspace
gets an honest `not_in_engagement` from the tool rather than an authentication error.

**What the token does not contain, stated plainly.** It raises the bar from "know an id" to
"hold a secret", and it is not process isolation. Engagement subprocesses run as root in one
container, so a shell-capable engagement can still read a sibling workspace's credential
file directly; the credential files are `0600` as defense in depth, which is not a boundary
against a co-resident root process. The inspection tool refuses to return the credential
file's contents precisely because that surface *is* reachable without any identity at all.
Hook resolution presents the same credential: the shim reads the pair from its own
workspace `.mcp.json` and the resolver authenticates any engagement-identity claim against
the record before selecting executor hook parameters or invoking an identity-consuming
policy — the payload's working directory is never an identity source (INV-MCP-006). Treat
the token as removing identity forgery from *knowing an id*, not as containment of a
hostile in-container process.

**The bridge runs as its own supervised service** so that the bridge *connection* survives a
restart of the main application. Its own client is a thin shell shim, and the failure
semantics are two-layered and opposite: the shim **fails open** — when its own HTTP call to
the bridge fails, it returns an allow decision rather than blocking (an unreachable Casa
should not wedge a running engagement) — but the bridge itself **fails closed**: when it is
up and the main application's socket is not, it answers with an explicit deny, and the shim
relays that deny. So hook policy is unenforced only when the *bridge* is unreachable; a
main-application restart denies rather than allows. Anything that must hold regardless
belongs in a tool, not in a hook.

The hooks document an executor carries is a mutable trust surface, and its translation to
workspace settings treats malformed shapes as absent rather than fatal: a non-mapping
document root, a non-list hook section, a non-mapping list member, or an unparseable
per-hook timeout is skipped instead of crashing engagement provisioning, and the
code-mandatory guard entry is emitted regardless of what the document declares.

**Some guards are advisory by construction, and one of them is deliberately imperfect.**
The pre-push self-containment guard inspects the shell command an agent is about to run,
works out which repository the push targets, and scans that tree for anti-patterns. Working
out where a shell command ends up is not decidable from the command text, so the guard
over-approximates: every `cd` token counts, every word of that command is treated as a
possible destination, and the scan covers the union. Extra scanned directories are the
intended cost. What it cannot see are destinations that are not statically resolvable —
parameter and command substitution, `eval`, aliases — and paths adversarially named after
shell syntax. That residual is accepted rather than pursued: the guard advises an
already-trusted in-container channel, it carries a logged `CASA_ALLOW_ANTI_PATTERN=1`
override, and `scripts/gate.sh` is the authoritative check on the real push path. Attempts
to close the residual by adding parser rules have a measured history of generating findings
without reducing risk (eleven review rounds in v0.145.0), so the scope note in the code is
binding: change it in response to an incident, not to a scan.

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

**INV-MCP-004**: An engagement-id claim binds an engagement record only together with that record's per-engagement auth token; a known id with a missing or mismatched token is rejected without invoking the tool.

The terminal-binding allowlist is inside this rule, not an exception to it: a terminal
record still binds for a completion retry only when the token matches.

What it does not cover: an id the registry does not know — that call dispatches with no
engagement bound (unchanged), and the tool answers for itself. It also does not cover a
co-resident root process reading another workspace's credential file (see the mental
model; tracked as #365). The hook-resolution path carries its own statement of the same
rule, INV-MCP-006.

**INV-MCP-005**: The workspace-inspection tool never returns the contents of a credential-bearing workspace file.

The inspection tool needs no engagement identity to run, so returning `.mcp.json` would hand
any caller the credential that INV-MCP-004 exists to require. The refusal is on the resolved
path's basename, so a symlink or a copy in a subdirectory is refused too; directory listings
still show the name.

What it does not cover: a caller with shell access reads the file directly — this closes the
*tool* surface, not the filesystem.

**INV-MCP-006**: Hook resolution binds an engagement only via the per-engagement credential — a known id with a missing or mismatched token is refused, an unauthenticated request selects no executor hook parameters and reaches no identity-consuming hook policy, and an authenticated identity contradicting the payload's working-directory claim is refused.

The same verification function as INV-MCP-004, on the hook route. The shim sends the
credential pair from its own workspace `.mcp.json` as headers; the bridge rebuilds the
forwarded body from those headers alone, so a body-borne identity claim cannot bypass
it. The resolver threads the authenticated identity to the policy
callback in-process; the permission relay and the buttons reminder act only on that
identity, which is what stops a forged working directory from posting a permission
keyboard into another engagement's topic or borrowing another executor's hook parameters.

What it does not cover: an id the registry does not know proceeds unauthenticated under
the default-configured policies (mirroring INV-MCP-004's unknown-id clause); the shim's
fail-open transport contract is unchanged (an unreachable bridge still allows); and a
co-resident root process can read a sibling's credential file (see the mental model;
tracked as #365).

**INV-MCP-007**: A hook policy parameter of the wrong type fails the build of that policy, and an authenticated hook resolution naming an executor the per-executor policy map does not represent is refused rather than answered from the default-configured policies.

The hooks schema leaves per-policy parameters open, so a mistyped value is schema-valid and
reaches the policy factory intact. The scope parameters are lists of path prefixes and every
consumer iterates them — and a string is iterable. A `writable: /config` written without the
list dash therefore expanded character by character into a prefix set containing a bare `/`,
which prefix-matches every absolute path: the scope guard admitted precisely the writes it
was configured to refuse. A wrong type now raises out of the factory, where an unrecognised
parameter *name* already did, and the executor fails closed at load rather than enforcing a
widened scope at runtime. The rule is deliberately uniform across every parameter in the
registry, including where coercion would have been available and harmless-looking: the
boolean gating resident deletion is required to be a boolean, because truthiness is not a
type check in either direction — a falsy non-boolean would silently disable the guard and a
truthy one silently enable it — and the commit-size limit is required to be a whole number
rather than coerced, because a coercion is a guess at what the author meant.

The second clause is what makes the first one worth having, and it is the harder half.
Refusing to build is not by itself a refusal to run: the HTTP hook path builds its
per-executor policy map separately, and the resolver falls back **per policy** to the
default-configured map, whose `casa_config_guard` forbids no write path at all. So an
executor simply *missing* from that map enforced less than the operator wrote — the same
fail-open shape, one layer out.

The rule is stated at the point of use rather than as a list of failure modes, because
that list turned out not to be enumerable. Three attempts at it each closed one arm and
left another: the file fails to build on the second read; the file fails to *load*, so the
registry publishes no definition and nothing iterating loaded executors names it at all;
the whole executor directory fails to scan, so no type name survives to be marked as
having failed. Each is a different way to be absent, and the fallback treated absence as
consent. So absence is now the refusal: an authenticated request naming an executor the
map does not represent is denied, whatever made it missing. The corollary is that an
executor which legitimately declares no parameters must be represented **positively** — it
is, by an explicit marker — since otherwise "known, and needs nothing" and "never loaded"
are the same silence. The separately-wired permission relay is not in the per-executor
map, so a broken configuration still leaves the executor able to ask rather than only to
fail.

Denial is the answer only when there is nothing better. Two cases have something better
and take it: an executor whose *document* failed to build gets a deny-all map naming that
reason, and a *reload* keeps the known-good pre-reload policy set — built from the last
file that did load — in preference to both the defaults and deny-all, because evicting it
would take live engagements down over an edit that was never accepted. So the guarantee is
about what is *never* answered from the defaults, not that every failure ends in a denial.

The reload preference is per executor type, and it is driven by *evidence* that a type
failed, which is a narrower thing than the type having failed. An executor root that
raises on being read aborts the reload before any rebuild, and the whole known-good map
survives. But a root that is merely absent scans successfully and reports nothing: no
definitions, and no failures either. Every previously known type then looks genuinely
removed rather than unproven, its entry is dropped, and its guarded calls are refused from
then on. The pre-reload callbacks still existed and were still good — what was missing was
any evidence to tell "gone" apart from "unproven". Refusing is the fail-closed side of
that ambiguity and is the deliberate choice, but it is a real cost: a configuration
directory that goes missing under a running Casa stops guarded work for live engagements
until it comes back.

What it does not cover: a well-typed but wrong value. A list of prefixes that is simply too
broad builds and enforces exactly what it says — this rule constrains the shape of a
parameter, never its meaning. Nor does it cover an *unauthenticated* resolution, which
selects no executor parameters at all and is governed by INV-MCP-006.

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

**A tool runs long.** Bridge tool forwarding carries a hard three-minute timeout and
answers temporarily-unavailable past it — the server side may still be executing. Hook
forwarding is deliberately unbounded at the transport, governed by per-policy timeouts
instead.

A wholly optional MCP server rides on the environment too: setting `N8N_URL` registers an
n8n workflow server (bearer-authenticated when `N8N_API_KEY` is set); unset, nothing is
registered. No manifest option exposes it — these variables are its only switch.

Two environment variables move pieces of this topology, unevenly:
`CASA_FRAMEWORK_MCP_URL` redirects newly provisioned engagement workspaces to a different
framework endpoint, and `CASA_INTERNAL_SOCKET` relocates the socket for the
engagement-channel client *only* — the main application, the bridge and generated
production workspaces hard-code the standard path, so treating it as a system-wide knob
splits the topology.

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
