---
last_reviewed: 2026-07-31
---

# The specialist install lifecycle

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist gets installed, upgraded, rolled back and uninstalled: the instance
tuples, the content-addressed component store, consent receipts, operational-file
materialization, and the crash journal that makes a bundle transaction recoverable. It does
not cover how a loaded specialist runs (taxonomy and turn loop) nor persona binding
mechanics (`architecture/personality.md`).

## Mental model

**The active tuple is the installed specialist; operational files are a projection.** An
install commits `active.yaml` first, and the loader-facing files under the agents tree are
*derived* from it — materialization can fail after a successful commit without rolling
anything back, and the per-slug reconcile pass rebuilds the projection later. Two separate
trees exist: the tuple-and-store tree, scanned by the installed index, and the operational
tree the registry loads.

**Identity is the closure, not the component.** The install root digest hashes the
component checksum, the raw manifest checksum, and every resolved dependency digest — so
"same component, different dependency" is a different install identity, and consent is
taken against exactly that identity plus the receipt digest.

**Consent precedes persistence; verification precedes publication.** The acknowledgement
is checked before any durable component-store write, and publication into the store is
verify-then-publish from a fresh staging copy, tolerant of a same-digest concurrent winner.
The store is append-only in practice: roots are pinned, unreferenced blobs are not
collected.

**A sourced-plugin install is a journalled bundle transaction.** The pre-state of the
owned registry entries, the six tuple/sidecar files and the slug's acknowledgements is
journalled before the visible swap; a sync-phase failure rolls that recorded state back,
and boot reconciliation replays or quarantines whatever a crash left. The journal's reach
is exactly what it records — component-store and plugin-store artifacts published earlier
stay put as inert residue.

**One lock serializes every instance mutation.** Install, upgrade, rollback, uninstall and
the reconcile pass all run under the materialize lock, and mutations re-read the active
tuple inside it — a pre-lock read that went stale refuses as a concurrent mutation rather
than overwriting.

## Contracts & invariants

**INV-SPEC-001**: No durable component-store write happens without a recorded consent for the exact install identity.

Enforced in the commit path, which constructs the identity (root digest plus receipt
digest) and refuses before staging when no acknowledgement matches.

What it does not cover: crash residue. A crash between store publication and journal
creation leaves verified-but-unreferenced content in the store, deliberately outside
rollback.

**INV-SPEC-002**: An install whose required configuration is missing becomes a pending instance — a desired tuple only, never an active one.

Enforced in the commit and upgrade cores.

What it does not cover: full invisibility. The component-role overlay considers active *or*
desired, so a pending instance's role can appear there while its operational files are
deliberately not materialized.

**INV-SPEC-003**: An upgrade failure retains the complete prior active tuple; a rollback restores it.

Enforced by the upgrade core recording an error result without touching the running tuple,
and by the rollback core's restoration from the retained prior.

**INV-SPEC-004**: Operational materialization writes a fresh content directory and atomically retargets the slug symlink, with deletion containment-gated.

Enforced in the materializer. The one-time migration of a legacy real directory has a
momentary absent-path window; steady-state swaps do not.

**INV-SPEC-005**: A receipt is integrity-checked on load — a malformed or tampered receipt reads as absent, never as attested.

Enforced by digest recomputation in the receipt loader.

What it does not cover: freshness of the fetched bytes. A valid receipt attests what was
inspected; the commit separately re-checks that what it fetched still matches.

## Failure behavior

**Resolution, fetch, manifest or dependency problems.** Typed refusals before anything
durable — reference not found, fetch failure, invalid manifest, slug collision, dependency
unavailable. Sourced plugin dependencies are additionally refused categorically when they
declare system requirements or triggers of their own, or when a required environment name
collides with another installed plugin's — otherwise-valid bundles fail with dedicated
error kinds the dependency model alone would not predict.

**A component declares system requirements.** Each installs by its declared strategy —
verified tarball, virtualenv or npm, processed in declaration order; OS packages are
refused — and the winning strategy is recorded durably. Boot reconciliation then only *reports* a missing binary as
degraded; nothing reinstalls tooling automatically.

**Consent missing or the inspection disagrees with the receipt.** Refused before tuple
activation; a changed closure means a changed identity means new consent.

**Materialization fails after commit.** Logged and reported as the instance's last
activation error; the reconcile pass retries per slug, and one slug's failure does not
block another's.

**A bundle sync phase fails.** The journal rolls the recorded pre-state back; if rollback
itself fails, the journal stays in progress for boot to finish.

**Boot finds journals.** Complete ones are pruned, valid in-progress ones rolled back,
corrupt or unrollbackable ones quarantined — a filename that cannot be parsed quarantines
every owned entry rather than guessing.

**Two mutations race.** Where an active tuple exists, the loser refuses as a concurrent
mutation; nothing is overwritten or resurrected. The refusal checks the *active* tuple
specifically — a second stale fresh-install racing a pending (desired-only) instance sees
no active tuple and restages the desired one rather than refusing.

## Extension points

**A new checksum-covered component file or manifest field** belongs in the component
loader and its checksum computation — changing either changes every install identity, so
it is a migration, not a tweak.

**A new dependency kind** goes through closure resolution and enters the root digest only
via its resolved digest.

**A new consent-relevant surface** must be attested in the receipt rows and populated at
inspection time, or consent will not cover it.

**A new durable mutation in the bundle transaction** must record its before-state in the
journal and be restorable by rollback, or a crash leaves it outside recovery.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/specialist_install.py::commit_specialist_install`
- `casa/rootfs/opt/casa/specialist_install.py::compute_install_root_digest`
- `casa/rootfs/opt/casa/specialist_materialize.py::current_specialist_roles_dir`
- `casa/rootfs/opt/casa/specialist_materialize.py::materialize_specialist_operational_files`
- `casa/rootfs/opt/casa/specialist_receipt.py::compute_receipt_digest`
- `casa/rootfs/opt/casa/specialist_bundle_journal.py::reconcile_boot`
- `casa/rootfs/opt/casa/specialist_registry.py::InstalledSpecialistIndex`

**Tests**
- `tests/test_specialist_install.py`
- `tests/test_specialist_lifecycle_matrix.py`
- `tests/test_specialist_materialize.py`
- `tests/test_specialist_bundle_journal.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
