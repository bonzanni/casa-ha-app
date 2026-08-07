---
last_reviewed: 2026-08-07
---

# Plugin runtime attachment

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What stands between a validly installed plugin and a plugin an agent can actually use: the
environment its MCP servers need, the setup tool that provisions that environment, and the
two side channels a plugin reaches the rest of Casa through. Installation, artifact
identity and per-call authorization are [`plugins.md`](plugins.md); trigger and callback
consent themselves are [`triggers.md`](triggers.md) and [`callbacks.md`](callbacks.md).

## Mental model

**A valid install is not a usable plugin.** Two things stand between them, and they fail in
opposite directions. An MCP server launched with an unresolved `${VAR}` does not fail — it
runs, on the literal string, and reports success against placeholder credentials. So an
unresolved reference *withholds* the plugin (INV-PLUG-008). But a plugin whose setup tool
exists to *create* those credentials would then be withheld for exactly the variables its
tool would produce, so one manifest declaration converts the withhold into a loud
not-ready state instead (INV-PLUG-009).

**Setup has exactly one runner, and "not yet" is an answer it can give.** Casa runs a
declared `casa.setupTool` itself; nothing hands that work to an agent. The runner is a
durable per-artifact *obligation* released by a **positively sealed** consent verdict — and
an obligation with no verdict holds rather than guessing (INV-PLUG-010). That third state
is the whole design: the alternative, deciding at mutation time which of two runners owns
the job, has no correct answer for a plugin whose consent the operator has not yet decided.

## Contracts & invariants

**INV-PLUG-008**: A plugin whose parseable `.mcp.json` references an environment variable that is unresolved in the effective environment, and that the plugin's manifest does not declare as setup-provisioned or optional, is withheld from resident and specialist session builds, and its automatic setup episode does not dispatch until those secrets resolve and the executing agent can load it.

References are collected from every string value of each declared server's *launch
fields* — `command`, `args`, `url`, `headers` and `env`, the positions the CLI expands
`${VAR}` in; tolerated unknown extension fields are not scanned — and unresolved means
absent, empty, or still an `op://` reference (a failed secret resolution falls back to
the raw value). The two manifest declarations that carve out of it are INV-PLUG-009 below.
Enforced at three points: both session builders — the resident/specialist
Agent's resolution and the delegated-specialist options builder — filter the resolved
plugin set before anything derives from it, so SDK plugins, grants, the protected map and
(for residents) the recorded binding all reflect what the session actually loads; the
setup-episode worker holds a settled episode until the secrets resolve, because a
trigger-consent round can settle while the installing engagement is still wiring them; and
for a *resident* execution target the worker additionally holds until that agent's next
session build will carry the episode's exact artifact — a binding published while the
plugin was withheld keeps excluding it until an agent reload, and a dispatch into that
session would consume the one automatic setup against a session without the tool. A
specialist execution target needs no such hold: specialists build their options fresh per
delegation against the current environment. Every successful reload — plugin-env landing
the secrets, or any agent-reconstructing scope — kicks the episode worker.

What it does not cover: the executor path, whose options builder hands out plugin paths
without this gate (the same asymmetry as INV-PLUG-006 — a configurator or plugin-developer
executor must be able to work on a plugin whose secrets are not wired yet). A *malformed*
declaration yields no requirements and passes this gate deliberately: the shared parser
gives the CLI nothing to spawn a server from, so no placeholder-credential path exists,
and malformed-ness is reported on the verification surface instead. The withhold decision
is evaluated when an agent publishes its binding snapshot and refreshed by the reload
seams, not continuously — and the check is admission control, not a fence: an environment
mutation between an agent's check and its MCP process spawn can still produce a stale
server, and a credential *rotation* leaves a warm session's already-spawned MCP process
on the old value even though the binding passes the gate. Both heal through the reload
seams, and neither is something verification can fully see: a plain-value rotation shows
as reload-pending only until the plugin-env reload lands, and an unchanged `op://`
reference cannot be compared at all — a warm session on a rotated credential reports
ready. An interactive specialist engagement records the plugin set *admitted when the
delegation was validated* (one filter feeding the requires gate where declared, the
record, and the launch), and every later build — including resume — re-applies
current-environment admission control; an environment change after that admission
point — even one that RESOLVES a variable moments later — is not re-admitted into this
engagement, and a change during the engagement can still make a build differ from the
record. Wiring a
secret mid-engagement does not make the plugin appear on resume when it was withheld at
creation — a new engagement picks it up.

**INV-PLUG-009**: An environment variable a plugin's manifest declares in `casa.setupProvides` does not withhold that plugin, and while it is unresolved the session build passes it to the CLI as an explicit empty string rather than letting the reference expand to a literal placeholder.

Without this, INV-PLUG-008 is a deadlock for an entire class of plugin: one whose setup
tool exists to *create* its credentials — forging a private key into the vault, registering
an application and learning its id — can never run that tool, because the plugin is
withheld for exactly the variables the tool would produce, and nothing re-kicks it (the
gate retries on plugin-env and agent reloads, neither of which can supply a value only
setup makes). The specialist that requires such a plugin is gated off with it.

`casa.setupProvides` is the ONLY such declaration, and its value is **readiness**, not the
withhold exemption. It says *my setup tool provisions this*: the plugin loads so setup can
run, but still verifies **not ready** with reason `setup_env_unprovisioned` until the value
actually lands, so a setup run that never happened stays loud rather than passing as
configured on empty credentials. Declaring it without a `casa.setupTool` is refused —
there would be nothing to be unprovisioned by.

A merely *optional* variable needs no declaration at all, and Casa deliberately offers none
(#431): `${VAR:-}` is documented Claude Code syntax, the CLI substitutes the default, and
the requirement extractor does not match that form — so it neither withholds nor leaks a
placeholder, with no manifest field and no reserved name. It is also strictly more
expressive, since a default may be a real value rather than only empty. What a default
cannot express is readiness, which is precisely why `setupProvides` survives as the one
declaration. Both are read
strictly on both artifact-verification paths (install-time validation and resolution-time
verdict), because a declaration that relaxes a gate must never be guessed at; a malformed
one excludes the artifact from resolution, and the runtime readers fail closed to "no
declaration", which leaves the plugin withheld exactly as before.

A declared name must live in a **reserved declaration namespace**, `CASA_PLUGIN_<NAME>`.
The binding is process-wide for the session's CLI subprocess rather than scoped to the
declaring plugin, so declaring a name is the difference between "absent" and "empty" for
everything in that session — including Casa's own reads, the CLI's knobs, and every other
attached plugin. A deny-list of what a plugin may *not* declare cannot be finished over an
open namespace, so the rule is inverted: everything outside the reserved prefix is
excluded by construction. Only *declared* names are fenced — a plugin may still reference
any `${VAR}` in `.mcp.json`, and an undeclared one withholds the plugin exactly as before,
binding nothing. This is a distinct rule from the reserved-key check on a server's own
`env` block, which is about shadowing a value the CLI injects per plugin.

The pinning is driven by the **declaration**, not by the `.mcp.json` reference set: a
server that reads its provisioned credential from the inherited environment rather than
naming it in its launch config still gets a binding. Without that it would see whatever
the environment happens to hold — including a leftover unresolvable `op://` reference,
which an idempotent setup tool can easily read as "already provisioned" and skip the
creation over.

It covers every path that attaches a plugin: the three in-process options builders
(resident/specialist Agent, delegated specialist, executor — including its by-path resume
branch) and the engagement run script, which hands recorded artifacts to a *supervised*
CLI via `--plugin-dir` and so sits outside the option builders entirely. The run script's
overlay is derived inside the renderer from the plugin directories being attached rather
than assembled by each caller — the driver's start path and boot reconciliation both
render that same service pair, and a per-caller contract is how one of them gets
forgotten.

What it does not cover: the exemption is not phase-scoped. An exempt plugin loads in
*ordinary* sessions too, on empty credentials, not only in the session that runs its
setup tool — Casa has no per-session plugin phase, and a resident's session is long-lived.
The compensating control is visibility, not exclusion: the unprovisioned row and the
health issue it generates persist until setup lands the value. An empty string is also
not the same as an unset variable to every server implementation; the contract Casa
offers is "never a literal `${VAR}`", and a plugin that declares these fields owns
failing clearly on an empty credential.

**INV-PLUG-010**: A plugin's declared setup tool is dispatched by Casa alone — no tool result, completion or prompt routes it to an agent — and an artifact's setup obligation dispatches only after a consent verdict has been positively sealed for that exact artifact and settled with no denial; the absence of a sealed verdict never permits a dispatch, and a verdict asserting that an artifact needs no consent is sealed only when the pending-consent computes for both trigger and callback consents succeeded.

**A plugin's declared setup tool is run by Casa and by nothing else — released only by a
positively sealed consent verdict for that exact artifact, and then only once its trigger
**and callback** routes are live — the gate rejects any outstanding issue of either kind,
per plugin and all-or-nothing — its required environment resolves, and the executing agent
can load it**. The obligation is durable, retrying and crash-recovered; a single denial withholds
it, so consent is not merely route authorization; an obligation whose plugin still has
unresolved environment variables stays pending rather than running the setup tool against
a placeholder-credentialed server — a consent round can settle while the installing
engagement is still wiring secrets, and every successful reload re-kicks the dispatch
worker; and for a resident execution target it stays pending while that agent's published
binding predates those secrets, until an agent reload makes the plugin loadable there
(specialists resolve fresh per delegation and need no such hold).

The single-runner rule is load-bearing rather than tidy. Until v0.161.0 an agent could
also run setup, acting on a `run_plugin_setup_tool` hand-back in the configurator's
completion, and *which* runner acted was classified when the registry mutated. Two
attempts to make that classification total failed adversarial review, for one reason:
at mutation time there is no third answer. A runner must be named then and there, and
every hole the attempts found was a case whose correct answer was **"not yet"** — a
future operator decision, or a question about what an updated setup tool needs that
nothing in the manifest answers. So the second runner is gone, and the remaining one
expresses "not yet" as *hold*: the obligation stays pending, stays visible in plugin
health (where `pending` never decays), and is re-checked on every reconcile.

What releases it is a **positive** statement, never an absence. The reconciler — the only
component that computes the consent requirement, and one that runs at every lifecycle
site — seals one round per `(plugin, artifact_id)` whose membership is the union of the
plugin's pending *trigger* and *callback* consents, so neither kind alone describes it.
That membership may be **empty**, which asserts that this artifact needs no consent and
releases the obligation; that is deliberately distinct from no round at all, which means
no verdict yet. Reading absence as permission is the concrete defect the first attempt
shipped: it would dispatch before the reconcile had opened the round.

An empty membership is sealed only where the consent position is genuinely *knowable*. A
declared trigger or callback carrying a **non-consent** gap — an unassigned target, a role
without the `webhook` channel, a missing global secret, an invalid public base URL — is
omitted from the pending rows altogether, so reading that omission as "needs none" would
assert precisely what the plugin contradicts. Such a plugin's obligation is recorded and
holds, unsealed, until the gap clears. The route gate would also stop the dispatch, but a
verdict is the one thing this design requires to be true rather than merely harmless.

For the same reason
a zero-member verdict is sealed only when the pending computes for *both* consent kinds
succeeded — a compute that degrades a failure to "nothing pending" cannot be
distinguished from one that means it — and sealing happens before the
operator-reachability gate, so an unreachable DM yields a members-bearing verdict that
correctly holds instead of no verdict at all.

The obligation is created level-triggered by that same sweep, for every resolved plugin
declaring `casa.setupTool`, keyed by the current `artifact_id`. That covers all three
artifact-publishing paths — `plugin_add`, `plugin_update`, and a specialist's bundled
plugins — without a hook at any of them. The setup tool itself is resolved at dispatch
time from the current manifest, so an update that changes `casa.setupTool` while leaving
`casa.callbacks` byte-identical still runs the new tool without binding the setup
contract into a consent identity. A denial marks the obligation refused rather than
dispatching; a later re-prompt for the same artifact re-arms it, which is also how a
re-consent that re-mints a secret gets setup re-run on an unchanged artifact. A plugin
that names a setup tool only in a producer handoff or a README, with no `casa.setupTool`,
has no supported automatic path before v1.0 — nobody runs it, and the configurator says
so rather than guessing a tool name.

Two more attachment paths are easy to miss. **Plugin environment values live in a
mode-0600 conf file** re-sourced into the process only by the plugin-env reload scope —
deleting an entry from the file changes nothing until that reload runs. **Plugin media
flows through a shared outbox directory** (operator-relocatable by environment variable)
with atomic claim semantics, size and type gates, and periodic orphan reaping —
consumption is destructive by design.

## Failure behavior

**A required environment variable is unresolved.** The plugin is withheld from resident and
specialist session builds — excluded from the SDK plugin list, its server grants, and the
recorded binding, and surfaced as an `env_unresolved` resolution issue — and any pending
setup obligation holds. Wiring the value and running the plugin-env reload makes the plugin
loadable; the agents that should carry it still need their own reload to rebuild sessions.

**A `casa.setupProvides` variable is unresolved.** The plugin loads anyway, with the
variable passed to the CLI as an explicit empty string rather than a literal placeholder,
and verification reports not ready with reason `setup_env_unprovisioned` until the value
lands. A setup run that never happened stays loud rather than passing as configured.

**No consent verdict has been sealed for an artifact.** The obligation holds, indefinitely
and visibly: `pending` never decays out of plugin health. This is the state when no operator
DM is reachable to prompt with, and when a pending-consent compute failed — neither is a
licence to dispatch.

**A consent round settles with any denial.** The obligation is refused and nothing is
dispatched; the operator gets one note naming re-consent as the way forward, not a manual
run they have no tool call for. A later re-prompt for the same artifact re-arms it.

**The registry cannot be resolved at dispatch time.** The obligation stays released and
retries on later kicks, bounded; past that bound it goes stale with an operator note, since
a plugin that never resolves is a plugin that is gone. Settlement itself never resolves the
registry, so a release can never be lost this way.

**The plugin's server binding is ambiguous.** An obligation whose plugin does not resolve
to exactly one server grant fails with that reason rather than guessing a namespace;
verification blocks such plugins upstream.

**The dispatch is accepted but the tool fails.** Delivery is what the obligation
guarantees, not execution: `dispatched` means the bus accepted the turn, and the executing
agent reports the tool's own outcome to the operator. Casa makes no claim of its own about
whether the integration works — it cannot see the external side (INV-TOOL-005).

## Extension points

**Declaring a setup tool** means adding `casa.setupTool` to the manifest. It must be
argument-free and idempotent, `setup_`-prefixed, and its plugin must target at least one
resident or specialist — an executor-only target has no invocation path and is refused at
verification. Nothing else is needed: the reconciler sweep finds it and Casa owes the run.

**Declaring that setup provisions a variable** means listing it in `casa.setupProvides`.
The name is then fenced for the whole session, so the declaration namespace is reserved;
declaring it without a `casa.setupTool` is refused, because the field means "my setup tool
provisions these". For a genuinely optional variable use `${VAR:-}` in `.mcp.json` instead
and let the CLI's own default expansion cover it.

**Changing what releases an obligation** means changing what the reconciler seals, not what
the worker infers. The worker deliberately holds on anything it cannot read as a positive
verdict; adding an inference there would reintroduce the defect this design removed.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_setup_episodes.py::ensure_obligation`
- `casa/rootfs/opt/casa/plugin_setup_episodes.py::open_round`
- `casa/rootfs/opt/casa/trigger_reconcile.py::seal_setup_state`
- `casa/rootfs/opt/casa/trigger_reconcile.py::setup_candidates`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_setup_provides`
- `casa/rootfs/opt/casa/plugin_env_conf.py`
- `casa/rootfs/opt/casa/plugin_outbox.py`

**Tests**
- `tests/test_plugin_setup_single_runner.py`
- `tests/test_plugin_setup_episodes.py`
- `tests/test_plugin_store_setup_env.py`
- `tests/test_plugin_env_conf.py`
- `tests/test_plugin_outbox.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
- [`architecture/configuration.md`](../architecture/configuration.md)
<!-- END SOURCEMAP -->
