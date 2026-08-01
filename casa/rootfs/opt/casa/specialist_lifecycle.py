from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from personality_binding import InstanceTuple

InstanceState = Literal["installed", "pending-configuration", "configured", "active", "error"]


@dataclass(frozen=True, slots=True)
class SpecialistInstance:
    slug: str
    stable_agent_id: str
    state: InstanceState
    active: InstanceTuple | None
    desired: InstanceTuple | None
    last_activation_error: str | None = None


def check_slug_uniqueness(
    *, candidate_slug: str, fixed_role_slots: frozenset[str], installed_specialist_slugs: frozenset[str],
) -> None:
    """Spec §2.4: the canonical collision key is the bare slot/slug string, compared
    across ALL image roles of ANY KIND — resident, executor, AND specialist (the
    image ships a transitional in-image specialist, specialist:finance, whose bare
    slot 'finance' is just as much a collision authority as any resident/executor
    slot) — and every installed specialist, with NO case/Unicode folding. Callers
    pass fixed_role_slots = specialist_registry._discover_image_role_slots()'s
    result (every image role's bare slot, all three kinds), never just
    FIXED_RESIDENT_SLOTS alone and never a hand-picked resident+executor subset."""
    if candidate_slug in fixed_role_slots or candidate_slug in installed_specialist_slugs:
        raise ValueError(
            f"slug {candidate_slug!r} collides with an existing image role slot or an "
            "already-installed specialist; rename to install"
        )


def satisfy_config(
    *, schema: Mapping[str, object], provided_non_secret: Mapping[str, object],
    provided_secret_names: frozenset[str],
) -> tuple[bool, list[str]]:
    required = list(schema.get("required", []))
    missing = [name for name in required if name not in provided_non_secret and name not in provided_secret_names]
    return (not missing, missing)


def secret_config_violations(
    *, schema: Mapping[str, object], provided_non_secret: Mapping[str, object],
    provided_secret_names: frozenset[str],
) -> tuple[list[str], list[str]]:
    """#337: the two channels must respect the schema's secret declaration.

    Returns ``(secret_valued, unknown_secret_names)``:

    - ``secret_valued`` — keys of ``provided_non_secret`` that the schema
      declares in ``secret_names``. A secret VALUE must never arrive in the
      plain config channel: pre-#337 it both satisfied the requirement and was
      persisted verbatim into desired.yaml/active.yaml ``config_snapshot``
      (under /config, which snapshots/backups include).
    - ``unknown_secret_names`` — entries of ``provided_secret_names`` the
      schema does NOT declare secret. Accepting these lets a caller "satisfy"
      a required non-secret key without ever providing a value (#331/#324).

    Callers must refuse (typed error) when either list is non-empty, BEFORE
    :func:`satisfy_config` and before any durable mutation.
    """
    declared = set(schema.get("secret_names", []) or [])
    secret_valued = sorted(k for k in provided_non_secret if k in declared)
    unknown_secret_names = sorted(n for n in provided_secret_names if n not in declared)
    return secret_valued, unknown_secret_names
