---
last_reviewed: 2026-07-30
---

# Configuration, reload and secrets

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Where configuration comes from, what is version-controlled, what a running system can pick up
without restarting, and how secrets are resolved. It does not cover what any individual
option means — the app manifest and its translations are the authority on that.

## Mental model

**There are two configuration worlds and they behave completely differently.**

The first is the **app manifest**: options an operator sets in Home Assistant. These are read
by the supervisor at service start, exported into the environment, and consumed once during
startup. **Changing one requires a restart.** Nothing reloads them.

The second is the **config tree on disk** — agents, policies, bindings, specialists — which
is reconciled against image defaults at boot and can be reloaded in-process afterwards.

**Reload is scope-specific, and it is not a restart.** Eight scopes are registered, each
rebuilding a defined slice. There is no scope that rereads manifest options, reconstructs
channels, or re-reads arbitrary files. If your change is an operator option, reload will not
help you, and this is the single most common wrong expectation in this area.

**A full reload is exclusive but not atomic.** It takes a writer lock that excludes every
other reload, then runs its steps in order — and there is no rollback across them. A failure
partway leaves earlier steps applied. The lock prevents interleaving, not partial
application. It also omits the on-disk reconciliation entirely, and omits plugin environment
unless explicitly asked.

**The config tree is a git repository, but only a whitelist is tracked.** Agents, policies,
bindings, schema, and specific registry files are versioned; plugin stores, staging areas,
the environment file and general working state are not. The whitelist is the authority, and
it is duplicated in the boot script — both must agree.

**Some identity changes cannot be hot-swapped at all.** If a resident's identity changes, the
reload path returns a restart-required outcome *before* mutating live state rather than
attempting a swap.

**Secret indirection is narrower than the option types suggest.** A small, explicit set of
environment variables is resolved through the external secret reference at startup. Options
typed as passwords that are not in that set are used verbatim.

## Contracts & invariants

**INV-CFG-001**: Exactly eight reload scopes exist, and none of them rereads the app manifest options.

Enforced by the registration calls in the reload module — the set is closed and explicit.

What it does not cover, and it is the point of stating it: no scope reloads operator options,
global channel setup, process environment generally, or arbitrary files in the config tree.

**INV-CFG-002**: A full reload excludes every other dispatched reload for its duration.

Enforced by a reader/writer lock, with non-full scopes serialized per scope key.

What it does not cover: the sequence is not transactional. There is no rollback across its
steps, so a mid-sequence failure leaves earlier steps in effect. Handlers called directly,
outside the dispatcher, take no lock at all.

**INV-CFG-003**: A resident identity change is refused as restart-required rather than hot-swapped.

Enforced in the agent and trigger reload paths, checked before any live runtime mutation.

What it does not cover: the policies cascade skips such a resident quietly rather than
surfacing the same refusal, so the outcome depends on which scope you asked for.

**INV-CFG-004**: Only an explicit whitelist of the config tree is version-controlled.

Enforced by the ignore file the repository is initialised with, reconciled on every boot, and
mirrored by the boot script.

What it does not cover: the version-controlled set and the set the reconciler owns are
*different*. A path can be tracked without being reconciled, and vice versa.

**INV-CFG-005**: Reconciliation of the config tree is never boot-fatal.

Enforced by catching everything and returning success, and again by the caller tolerating
failure. Problems are recorded rather than raised.

What it does not cover: a recorded residual problem can still cause a later failure when
something tries to load what was left broken.

## Failure behavior

**The required credential option is missing.** Boot stops at validation. This is the one
configuration failure that is fatal, and every service is gated behind it.

**Reconciliation fails.** Absorbed and logged. A recovery artifact is preserved — a
pre-reconciliation commit where the repository is usable, and a backup copy otherwise — so an
overwritten local edit is recoverable.

**Repository initialisation fails.** Degraded, not fatal. Versioning is unavailable;
everything else proceeds.

**A secret reference fails to resolve.** Absorbed, and the behaviour differs by path in a way
worth knowing: on the startup path the raw reference is retained, while the plugin
environment leaves the variable unset at boot but installs the literal reference on reload.
Same failure, three outcomes.

**A reload handler raises.** The dispatcher returns an error envelope rather than propagating
— a failed reload is a reported outcome, not an exception at the caller.

## Extension points

**A new option** means the manifest options block, its schema entry, the translations entry,
and an explicit export or read wherever it is consumed. Nothing picks up an option
automatically.

**Removing an option** additionally requires appending its key to the boot script's
deprecated-key list, or a stored value lingers and the host keeps warning about it.

**Making an option hot-reloadable** is not a small change: it means a new scope and rebuilding
every consumer, because no generic mechanism exists.

**A new default tree** must be added to the reconciler's list *and* to the version-control
whitelist separately. Neither implies the other.

**A new reload scope** needs its handler, a lock key, a decision about whether the full scope
composes it, whether it participates in trigger reconciliation, and what its failure means.
None of those are inferred.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/reload.py::dispatch`
- `casa/rootfs/opt/casa/reload.py::reload_full`
- `casa/rootfs/opt/casa/reload.py::_resident_identity_changed`
- `casa/rootfs/opt/casa/config_git.py::init_repo`
- `casa/rootfs/opt/casa/config_sync.py::reconcile`
- `casa/rootfs/opt/casa/secrets_resolver.py::resolve`
- `casa/config.yaml::schema`
- `casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh`

**Tests**
- `tests/test_casa_reload_tool.py`
- `tests/test_config_git.py`
- `tests/test_config_sync_backstop.py`
- `tests/test_admin_reload_route.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
<!-- END SOURCEMAP -->
