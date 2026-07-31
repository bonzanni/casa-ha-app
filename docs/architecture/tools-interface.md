---
last_reviewed: 2026-07-31
---

# The tool interface

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The mutation interface Casa exposes to its agents: how the tool surface is assembled, what
a tool result promises, how engagement and plugin mutations sequence their side effects,
and which failures roll back versus which merely report. It covers families and contracts,
not a per-tool catalog — the registry tuple is the authority on what exists. Dispatch and
authorization live in `architecture/mcp-and-tools.md`; this file is about what the tools
themselves guarantee.

## Mental model

**One registry is the whole surface.** A single module-level tuple of handlers drives both
the SDK server an agent sees and the bridge's dispatch map. Adding a tool there exposes it
on both transports; grant filtering is a separate, later cut against fully-qualified names.

**A result has two layers, and the outer one is inferred.** A tool returns a payload
serialized into one text content block; the wrapper marks the outer result as an error only
when the payload says `status: "error"` or `ok: false`. Statuses like *unavailable*,
*pending* and *acknowledged* deliberately ride as successes — they are outcomes, not
failures. And the wrapper is a convention, not a law: at least one tool returns raw
envelopes without it, so "every error becomes `is_error`" is not a property of the surface.

**Argument-schema validation depends on the transport.** The SDK path registers the
decorated tools with their schemas, and MCP validation there rejects a missing required
argument or a bad enum before the handler runs — code and tests rely on it. The internal
bridge route checks only that the name is known and passes the arguments through, so a
tool reachable both ways must carry its own validation for the bridge side.

**Engagement mutation is a funnel, not parallel paths.** Completion and cancellation
converge on one finalize path whose strict registry transition picks a single winner
(INV-ENG-001); everything observable — permits, brokers, topics, notifications, retention —
happens after, best-effort, and is not transactional.

**Plugin mutation is persist-then-converge.** Identity, source and requirement guards run
before the registry is touched; after the registry commits, reload and verification try to
make the runtime match. A failure *before* the commit leaves the registry unchanged. A
failure *after* it does not roll anything back — the honest outcome is
committed-but-not-ready, and the envelope says so.

## Contracts & invariants

**INV-TOOL-001**: The result wrapper marks an outer error only for a payload with status "error" or ok false; every other status is a successful outcome.

Enforced in the wrapper itself, which serializes the payload into exactly one content
block.

What it does not cover: tools that do not call the wrapper. A handler returning raw
envelopes carries its own error semantics.

**INV-TOOL-002**: Internal tool calls bind engagement authority only for an active record; completion alone may bind a terminal record, so a duplicate completion gets its truthful already-terminal answer.

Enforced by the internal handler's binding check against an explicit terminal-binding
allowlist containing exactly the completion tool.

What it does not cover: it does not authorize any other tool after termination, and it is
a binding rule, not an argument or schema check.

**INV-TOOL-003**: Plugin mutations serialize under one lock, and a failure before registry activation leaves the registry unchanged, reported in a pinned envelope shape.

Enforced by the shared mutation lock across all five ordinary plugin tools and by the
guard-resolve-publish-then-save ordering in the synchronous cores. The pinned fields —
kind, activation-committed, runtime-ready, verify — make the failure phase machine-readable.

What it does not cover: published store artifacts and installed system requirements are not
unwound by a later refusal; only the registry is untouched.

**INV-TOOL-004**: A reload or verification failure after the registry commit yields committed-but-not-ready; nothing rolls the registry back.

Enforced by the converge step reporting `activation_committed: true, runtime_ready: false`
rather than compensating. The next reload — or an explicit verify — is the repair path.

What it does not cover: it makes no promise about *when* the runtime converges, only that
the registry's word is already given.

## Failure behavior

**A malformed request.** Invalid JSON, a missing or unknown name, and a non-object request
come back as typed JSON-RPC-style error objects from the route, before any tool runs; a
tool that raises becomes an error object rather than a transport failure. The shape
checking is exactly that list — a request whose `params` is a truthy non-object slips past
it and raises instead of earning a typed error.

**A completion is invalid or refused.** Bad arguments, the plugin-developer release guard,
and the unread-inbound veto all leave the engagement active; a duplicate completion is
acknowledged as already terminal; a failed strict persist reports retryable.

**A delivery is uncertain.** The send classifiers separate definitive refusal from
uncertainty, and an uncertain Telegram send is deliberately not retried — a duplicate
message is worse than a missing one that the operator can see is missing.

**Memory cannot answer.** The recall tools report unavailability as its own status and
refuse blank queries outright; neither is ever a fake empty result (INV-MEM-001's
tool-level face).

## Extension points

**A new tool** is a decorated handler added to the registry tuple — that alone puts it on
both transports, which is why the addition is also a security decision; grant filtering and
the coverage ledger pick it up from there.

**A new plugin lifecycle operation** follows the established split: synchronous
disk-and-registry ordering in a core, then the async wrapper that takes the lock, reloads,
verifies and pins the envelope.

**A new terminal side effect** for engagements goes after the winner is decided in the
finalize path, and must tolerate running on a record whose other side effects partially
failed.

**A new delivery medium** touches the media policies, filename validation and the send
classifier together — the classifier is where refusal-versus-uncertainty is decided, and
that distinction is the contract.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::create_casa_tools`
- `casa/rootfs/opt/casa/tools.py::_result`
- `casa/rootfs/opt/casa/tools.py::select_casa_tools`
- `casa/rootfs/opt/casa/tools.py::emit_completion`
- `casa/rootfs/opt/casa/tools.py::plugin_add`
- `casa/rootfs/opt/casa/internal_handlers.py::_make_internal_tools_call_handler`
- `casa/rootfs/opt/casa/mcp_envelope.py::_tool_schema`

**Tests**
- `tests/test_internal_handlers.py`
- `tests/test_plugin_tools.py`
- `tests/test_emit_completion_tool.py`

**Related**
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
