---
last_reviewed: 2026-07-30
---

# Agent taxonomy and the registry

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an agent is declared, validated and looked up: the tiers, the artifacts a tier requires,
and the registry that answers "who is this?" at runtime. It does not cover what happens
inside a turn once an agent is chosen, nor how personas dress an agent up.

## Mental model

An agent is a **directory of configuration**, not a class: the loader checks a per-tier file
set rather than importing anything. That decision explains most of the shape here.

**The role registry models residents and specialists only** — its tier type admits exactly
those two, and executors load on their own isolated path. Every registry claim below is
scoped to that boundary, and reasoning about executors through it is the likeliest mistake
in this area.

Two names exist for a registered agent and they are not interchangeable. The **role** is the
stable identifier the system routes on. The **name** is what a person sees. Code resolves by
role and translates to a name at the edge, because a name can change without anything
breaking while a role cannot.

Failure is **not uniform across tiers**, and that is the second thing to internalise.
Residents are gating: a bad one stops boot. Specialists and executors load on isolated paths
whose failures are deliberately boot-non-fatal — one broken specialist does not take the
system down, and the code says so at the loader's walk. A document claiming "a validation
failure is a boot failure" would be wrong for two of the three tiers.

The registry is the read side: a small, already-validated index answering what tier a role
belongs to, what it is called, and whether it exists at all.

## Contracts & invariants

**INV-AGENT-001**: A role claimed by both a resident and a specialist is refused when the role registry is built.

Enforced in `_build_role_registry`, which raises naming the duplicated role. This check is
load-bearing precisely because `AgentRegistry.build` is not: it assigns residents and then
specialists into one mapping, so a collision that got past the check would silently resolve
to the specialist rather than raise.

**INV-AGENT-002**: Every tier declares an exact file set, and `_check_file_set` refuses a missing required file, a forbidden file, or an unrecognised one.

All three directions, not just the missing case: an unexpected file in an agent directory is
as much a failure as an absent one. Directories, dotfiles and recognised editor backups are
skipped, so a stray save does not become a half-parsed agent.

**INV-AGENT-003**: Specialist and executor loading is isolated per agent and boot-non-fatal; resident loading is not.

Stated as the asymmetry it is. `validate_config_repo` additionally skips the
pipeline-managed specialists subtree, so it is not a whole-repository gate either.

Two qualifiers, both of which invert the naive reading:

The loader's own docstring says collection-level errors still raise — but the registries
above it catch those, so nothing reaches boot. Reading the loader alone tells you the
opposite of what happens. Follow the call up before concluding a raise is fatal.

The resident path fails closed on a bad directory, but an *absent* agents directory is
not a bad one: the walk returns empty before any drift check runs, and a system with no
residents boots clean. "Fails closed both ways" holds for what is there, not for what is
missing entirely.

**INV-AGENT-004**: The registry performs no filesystem access; it is an index built from already-loaded configuration.

Nothing resolves a role by touching disk at request time.

## Failure behavior

**A required file is missing, forbidden, or unrecognised.** `_check_file_set` raises naming
the role and the files. For a resident this stops boot; for a specialist or executor the
isolated path absorbs it and the rest of the system continues.

**A resident and a specialist claim one role.** Registry construction raises, naming the
role. The check is cross-tier because each directory looks fine on its own.

**A role is unknown at runtime.** The registry does not raise: `name_to_role` returns `None`,
`is_known` returns `False`, and `role_to_name` returns the role unchanged. Callers decide
what that means — read the call site rather than assuming it errors.

## Extension points

A new agent: a directory under its tier, the file set that tier requires, and an unclaimed
role.

A new tier is a much larger change than a new agent: the file set, the loading path and the
registry's tier type all move together. Note that the registry's type currently admits two
tiers, so anything registry-visible is a change to that type and everything reading it.

A new required artifact means updating the tier's file set and every agent of that tier in
the same change, since the check is exact and a missing file is a boot failure.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent_registry.py::AgentRegistry`
- `casa/rootfs/opt/casa/agent_registry.py::KnownAgent`
- `casa/rootfs/opt/casa/agent_registry.py::AgentRegistry.tier_for_role`
- `casa/rootfs/opt/casa/agent_loader.py::validate_config_repo`
- `casa/rootfs/opt/casa/agent_loader.py::LoadError`
- `casa/rootfs/opt/casa/agent_loader.py::_check_file_set`
- `casa/rootfs/opt/casa/casa_core.py::_build_role_registry`

**Tests**
- `tests/test_agent_registry.py::test_role_to_name_basic`
- `tests/test_agent_registry.py::test_name_to_role_unknown_returns_none`
- `tests/test_agent_loader.py::test_duplicate_role_across_residents_and_specialists_fails`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
