---
last_reviewed: 2026-07-30
---

# Personality: roles, personas and bindings

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The separation between what an agent *is* and how it *presents*: role artifacts, personas,
the binding that ties one to the other, and what prompt is actually served at turn time. It
does not cover agent declaration and loading, which belong to the taxonomy document.

## Mental model

**A role is identity; a persona is presentation.** The role decides what an agent *is* —
identity, model, doctrine, and the role-based checks. What a resident or specialist *may do*
— its tool lists, permission mode and MCP servers — comes from its own runtime
configuration, not from the role artifact; only the executor path applies a role-artifact
capability ceiling. The persona decides how the agent sounds. Roles and personas are
versioned and validated separately, and a binding is what associates them.

**The compiled bundle replaces the composed prompt — it does not layer onto it.** This is the
easiest thing to get wrong here. The composed prompt is built from the agent's own
configuration — character prompt, voice, response shape, delegates, disclosure policy; it
reads no role artifact. Role artifacts feed the *compiled bundle* path instead, and separately a
bundle is compiled when a binding activates. At turn time, if a bundle exists, its projection
*is* the base prompt; the composed one is used only when there is no bundle. So text present
in the composed prompt is not automatically carried into an activated compiled prompt — it is
there only if the compilation put it there.

**A persona can misdescribe its role, but cannot expand it.** Persona validation checks
structure, required markers, sections and size against the manifest. It does not check that
what the persona *says* about its capabilities is true. A persona cannot grant a tool; it can
claim one it does not have. Capability comes from the role and the tool layer, never from
prose.

**The binding digest does not cover everything about a binding.** It detects the drift it was
designed to detect, and some binding attributes can change without moving it. Before relying
on the digest to notice a change, check whether that change is one it covers.

**Restricted contexts compile a different prompt.** The persona section is removed entirely
for the restricted webhook path — an untrusted-origin turn does not get a personality.

## Contracts & invariants

**INV-PERS-001**: When a resident has an activated compiled bundle, that bundle's projection is the base prompt; the composed prompt is a fallback for when there is none.

Enforced where turn options are built, and mirrored on the specialist path.

What it does not cover: it does not merge them. Anything expected in the served prompt must
be present in the compiled bundle.

**INV-PERS-002**: Persona validation is structural; it does not verify that a persona's claims about capability are true.

What it does not cover: nothing prevents a persona describing a tool the agent lacks. Treat
persona text as presentation, never as a source of truth about what an agent can do.

**INV-PERS-003**: A resident's binding reconciliation runs as part of loading and is not isolated from it.

The consequence is the important part: a failure there propagates into resident loading,
which is boot-fatal. Persona problems on a resident are not a degraded mode.

**INV-PERS-004**: The restricted-origin prompt omits the persona section.

## Failure behavior

**A persona fails validation on a resident.** It depends on what exists already. On a fresh
install — no active binding tuple — loading fails, and because resident loading is
boot-fatal, so does boot. With an existing active binding, a failing *candidate* is
discarded with a diagnostic and boot proceeds on the retained last-known-good binding;
reconciliation raises only when there is nothing good to retain.

**A persona fails validation on a specialist.** Absorbed by that tier's isolated loading; the
specialist is unavailable and the system continues.

**A binding cannot be activated.** Folded into the loading error for that agent, so it is
reported as a load failure rather than a separate class.

**No bundle exists.** The composed prompt is served. This is a working state, not an error —
which means a silently missing bundle presents as an agent that behaves correctly but sounds
wrong.

## Extension points

**A new persona** must satisfy the structural contract — markers, sections, size — and be
bound to a role. Getting it *accurate* is not something validation will help with.

**A new role artifact** changes what the compilation must carry — and only that: the
composed fallback prompt never reads role artifacts (see the mental model), so an artifact
change lands solely on the compiled-bundle path.

**Changing what the binding digest covers** changes what counts as drift, and therefore what
forces a re-activation. Widening it is safe; narrowing it silently stops detecting something.

**Anything that must appear in every prompt** belongs in the compilation, not only in the
composed prompt — otherwise it appears exactly for the agents that have no bundle.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/personality_binding.py`
- `casa/rootfs/opt/casa/personality_types.py`
- `casa/rootfs/opt/casa/agent_loader.py::_compose_prompt`
- `casa/rootfs/opt/casa/role_slot.py`

**Tests**
- `tests/test_personality_binding.py`
- `tests/test_persona_install.py`
- `tests/test_personality_admin_handlers.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
