---
last_reviewed: 2026-08-01
---

# Plugins

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a plugin becomes something an agent can use: the registry that assigns it, the store
that holds its bytes, what pins its identity, and what stands between a plugin's tools and
an operator's approval. It does not cover authoring a plugin, nor the MCP protocol itself.

## Mental model

Two things are easily conflated and do different jobs. **The registry is the authority on
what is assigned to whom.** **The store is content-addressed storage for the bytes.** A
plugin is usable at runtime only when a valid registry entry and a valid stored artifact
agree — and, for resident and specialist sessions, when the environment variables its
`.mcp.json` references are resolved in the effective environment (INV-PLUG-008): an
undefined `${VAR}` would otherwise reach the plugin's MCP server as the literal string,
which then runs "successfully" with placeholder credentials.

**The artifact id is not a content hash.** It is computed over source coordinates —
repository, resolved revision, subdirectory, and the registry name — and nothing else. Two
different byte trees fetched for the same coordinates produce the *same* artifact id. Bytes
are pinned separately, by a checksum recorded in the artifact's own metadata and verified
when the artifact is validated. Reasoning about integrity from the artifact id alone is the
most common way to be wrong here: identity answers "which plugin is this", and a separate
checksum answers "are these the expected bytes". Know the checksum's trust boundary, though:
its expected value lives in the artifact's own metadata file — which is excluded from the
hash and recorded nowhere else, not in the registry and not signed. It detects drift of the
tree relative to the artifact's internal metadata, not tampering: a writer able to change the
store can change bytes and recorded checksum together, and nothing recomputes a digest from
the source.

**Resolution is snapshot-based, but not everything is cached.** Registry parsing and deep
artifact validation happen when a snapshot is built; the checksum verdict is cached and never
recomputed by resolution. Resolution does, however, re-read the artifact's `plugin.json` on
every resolve, and the resolved paths hand the live artifact tree to the SDK without
revalidation. So a *registry* edit is invisible until a reload — deliberate, because
validation is expensive and a long-lived agent should not see a half-written registry — while
a mutation *inside a stored artifact* can become visible immediately and evades the cached
checksum until an explicit verification, the next snapshot reload — or an interactive
specialist resume, which deep-validates its recorded artifacts automatically. Executor
resume checks only that the recorded directories still exist.

**Plugin failures degrade; they do not stop the container.** The boot path writes health data
and exits successfully whatever it finds. One broken plugin costs that plugin. The health
report's operator-DM dedup (fingerprints already notified) is a read-merge-write over one
file from both the event loop and worker threads; a process-wide lock serializes it, and the
regeneration reads the previous report inside that critical section, so a regeneration
racing a just-delivered notification cannot erase its marker and re-alert.

**Approval is per call, not per install, and it does not survive a restart.** A protected
tool call by a resident or specialist consumes a single-use grant bound to a specific
operator, chat, role, artifact, tool name and exact arguments. The grant store is in process
memory only.

**Tiers are not gated alike, and this is the asymmetry to carry away.** Residents and
specialists get plugin grants merged into their allowed tools, a fail-closed tool gate, and
the protected-tool approval hook. **Executors get none of those** — they receive plugin
paths, and their declared tool list is passed to the SDK as *auto-approved* tools, which is
a convenience rather than an enforcement boundary: sub-agent spawning bypasses an
allowed-tools list, and only the disallowed list is CLI-enforced. What actually constrains an
executor is the code-mandatory clamps merged into every options build — sub-agent spawn tools
are hard-denied, Bash is hard-denied unless the declaration allows it, and guard hooks
protect managed components and agent-home settings. A plugin declaring protected tools
protects resident and specialist calls; it creates no equivalent gate on the executor path.

## Contracts & invariants

**INV-PLUG-001**: A registry entry is usable only when its recorded artifact id equals the id computed from its own source coordinates.

Enforced during registry validation, which rejects a mismatching entry and excludes it from
the document rather than failing the whole registry.

What it does not cover: this is an identity check over coordinates. It attests nothing about
the bytes in the store.

**INV-PLUG-002**: A resolved artifact must match its recorded content checksum, and the artifact path and its parent must not be symlinks.

Enforced by the artifact verdict computed when a snapshot is built; a failing verdict means
the plugin is not resolved and the reason is recorded.

What it does not cover: the verdict is not recomputed after the snapshot is published.
Resolution meanwhile re-reads the artifact's manifest and hands out live paths, so a change
underneath a live snapshot is detected only by an explicit verification or the next snapshot
reload — nothing catches it on its own. The recorded checksum itself lives in the artifact's
metadata (see the mental model): it pins the tree against that metadata, not against an
independently authenticated content identity. Internal non-escaping symlinks inside an
artifact are permitted.

**INV-PLUG-003**: Archive extraction refuses traversal, absolute paths, links out of the tree, and special files.

Enforced by an explicit per-member loop. The standard library's extraction filter is applied
as well where the runtime supports it, but it is defence in depth — the per-member loop is
what actually carries the guarantee, and the fallback exists because the shipped interpreter
predates the filter.

**INV-PLUG-004**: A protected tool call from a resident or specialist proceeds only by consuming an exact, single-use grant.

Enforced by the authorization hook, with consumption made atomic in the grant store. The
grant binds operator, chat, tier-stripped role, artifact, full tool name, and a hash of the
canonical arguments — so an approval authorises one action with one argument set, not a
capability.

What it does not cover: unprotected plugin tools, and the executor path entirely. And
"proceeds by consuming" applies to direct resident calls and ephemerally delegated
specialists — a call whose provenance is an interactive *engagement* is denied outright,
before any grant lookup: protected tools are not available inside engagements at all.

Who may approve is a separate guarantee, INV-PLUG-007: read this invariant as "one
approval authorises one action" and that one as "the approver is the configured operator".

**INV-PLUG-005**: Grants exist only in process memory; a restart revokes every one of them.

There is no persistence path. Revocation additionally happens on consumption, on TTL expiry
plus a periodic sweep, on a chat reset, on role reload or removal, and on plugin update or
removal.

Trigger consent is the deliberate exception: a webhook-trigger acknowledgement is persisted
and re-validated from disk at startup, because it authorises a route rather than a call.

**INV-PLUG-006**: Executor options receive plugin paths without a grant merge and without a tool gate.

Stated as an invariant because it is a security-relevant asymmetry that reads like an
oversight and is not one — the executor path is constrained by its code-mandatory disallow
clamps, guard hooks and relay instead; its declared tool list is auto-approval, not a gate.
Verification will report an executor whose declaration lacks a needed authorisation, but
nothing merges it automatically.

**INV-PLUG-007**: An authorization challenge is posted, and its grant minted, only for a turn whose sender is the configured operator; any other sender's protected call is denied outright, before any grant lookup and with no challenge.

Enforced in the authorization hook through the Telegram channel's single operator rule —
the same sender-id match that decides attribution and clearance — so the person who taps
Approve is the person the deployment names as operator, not whoever asked. Denying without
a challenge is the point: posting one would hand the requester their own approval button.

What it does not cover: with `telegram_chat_id` empty ("accept all chats") there is no
configured operator, so protected tools are denied for every sender — deliberate, and
announced by a warning at channel construction. It also does not cover the in-engagement
permission relay, whose keyboard is answerable by the engagement's creator rather than the
configured operator (tracked as #374); and sender identity itself is Telegram's
authentication of its user ids, not an additional Casa-side proof.

**INV-PLUG-008**: A plugin whose parseable `.mcp.json` references an environment variable that is unresolved in the effective environment is withheld from resident and specialist session builds, and its automatic setup episode does not dispatch until the secrets resolve and the executing agent can load it.

References are collected from every string value of each declared server's *launch
fields* — `command`, `args`, `url`, `headers` and `env`, the positions the CLI expands
`${VAR}` in; tolerated unknown extension fields are not scanned — and unresolved means
absent, empty, or still an `op://` reference (a failed secret resolution falls back to
the raw value). Enforced at three points: both session builders — the resident/specialist
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

Three runtime integration paths sit beside the install model and are easy to miss. **A
plugin's declared setup tool is dispatched automatically — but only after its entire
trigger-consent round approves, its trigger routes are live, its required environment
resolves, and the executing agent can load it**: the dispatch is a durable, retrying,
crash-recovered episode; a single denied trigger withholds it, so consent is not merely
route authorization; an episode whose plugin still has unresolved environment variables
stays pending rather than running the setup tool against a placeholder-credentialed
server — a consent round can settle while the installing engagement is still wiring
secrets, and every successful reload re-kicks the dispatch worker; and for a resident
execution target it stays pending while that agent's published binding predates those
secrets, until an agent reload makes the plugin loadable there (specialists resolve
fresh per delegation and need no such hold). **Plugin environment values live in a mode-0600 conf
file** re-sourced into the process only by the plugin-env reload scope — deleting an entry
from the file changes nothing until that reload runs. **Plugin media flows through a shared outbox directory**
(operator-relocatable by environment variable) with atomic claim semantics, size and type
gates, and periodic orphan reaping — consumption is destructive by design.

## Failure behavior

**The registry document is malformed.** It loads as invalid, no plugins resolve, and the
condition is recorded as a health issue. Boot still succeeds.

**One entry is malformed, or two entries collide on name.** That entry is excluded — both, on
a collision — and the rest continue.

**An artifact is missing, corrupt, or fails its identity or checksum check.** That plugin is
not resolved and the reason is recorded. Other plugins are unaffected.

**An archive is unsafe.** Extraction raises, staging is cleaned up, and the failure is
reported. This happens *before* any registry mutation, so a refused install leaves no
half-state.

**A protected tool is called without an approval.** The hook denies the call and posts or
reuses an approval challenge to the operator. The retry must present identical canonical
arguments — a changed argument is a different grant.

**The authorization hook itself fails.** Any unexpected exception becomes an explicit deny;
only cancellation is re-raised. The hook fails closed.

**The plugin's MCP declaration is missing or malformed.** Grants degrade to none. A missing
declaration is not an error — a plugin with no tools is valid — but a malformed one is
reported.

**A required environment variable is unresolved.** The plugin is withheld from resident and
specialist session builds — excluded from the SDK plugin list, its server grants, and the
recorded binding, and surfaced as an `env_unresolved` resolution issue — and any pending
setup episode holds. Wiring the value and running the plugin-env reload makes the plugin
loadable; the agents that should carry it still need their own reload to rebuild sessions.

## Extension points

**Adding a plugin** means publishing it and registering it, then reloading. Editing the
registry file directly is not sufficient: nothing takes effect until a snapshot reload runs.

**Adding MCP tools** means shipping the declaration; grants are derived per server. If the
plugin targets an executor, that executor's own declared tool list must be updated separately
— verification will tell you it is missing, but no merge happens for you.

**Adding a protected tool** is a manifest declaration. Validation checks its shape and name
uniqueness, **not that the named tool exists**; a typo produces a declaration that protects
nothing. And it applies only to the resident and specialist paths.

**Declaring system requirements** is strict the same way the other manifest extensions
are: an absent declaration means none, but a present-but-malformed one (a non-list value,
a non-object member, or a requirement without a safe executable `verify_bin` name)
refuses the install or update rather than silently reading as "no requirements" — or
running an install that can never succeed — and activating a plugin whose binary was
never installed. Package-manager types keep their own dedicated refusal. On the
verification surface the same malformation reports as a visible missing-requirement
status instead of crashing the verify.

**Adding a webhook trigger** requires intrinsic validation plus a durable operator consent
bound to the exact trigger identity. This approval outlives a restart — one of the durable
approval ledgers (specialist and persona install acknowledgements are others), in contrast
to the in-memory tool-call grants above.

**Adding an authorization callback** is a sibling manifest extractor: `casa.callbacks` is a
peer of `casa.triggers`, parsed and intrinsically validated the same way, and gated by a
durable operator consent that is another of those restart-surviving ledgers. Its consent
is deliberately asymmetric to a trigger's, and the asymmetry is the thing to carry away: a
trigger ack binds the plugin *artifact*, so any update re-consents; a callback ack binds the
*declaration digest* (the declared name only) and excludes the artifact, so a routine
upgrade that leaves the declaration unchanged keeps consent — because a callback grants no
role turn and no memory access, only a spool deposit. The mechanism lives in
`architecture/callbacks.md`.

**Reloading** refreshes the snapshot before agents and executors are rebuilt, and purges
role-scoped grants and pending challenges before a role is replaced or removed.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_registry.py::compute_artifact_id`
- `casa/rootfs/opt/casa/plugin_registry.py::reload_snapshot`
- `casa/rootfs/opt/casa/plugin_store.py::safe_extract_tar`
- `casa/rootfs/opt/casa/plugin_store.py::artifact_verdict`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_protected_tools`
- `casa/rootfs/opt/casa/plugin_grants.py::protected_map`
- `casa/rootfs/opt/casa/authz_grants.py::GrantKey`
- `casa/rootfs/opt/casa/authz_grants.py::GrantStore`
- `casa/rootfs/opt/casa/plugin_boot.py::main`

**Tests**
- `tests/test_plugin_registry.py`
- `tests/test_plugin_store_publish.py`
- `tests/test_plugin_grants.py`
- `tests/test_authz_grants.py`
- `tests/test_authz_hook.py`
- `tests/test_plugin_boot.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
<!-- END SOURCEMAP -->
