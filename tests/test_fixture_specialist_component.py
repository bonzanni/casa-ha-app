"""Rot-check the Task-16 e2e static fixtures against the production loaders.

The committed trees under ``test-local/fixtures/specialist-components/mtg-test``
and ``test-local/fixtures/personas/alt-butler-tina`` carry REAL checksums
produced by ``gen_fixtures.py`` using the same canonical_bytes/
specialist_component helpers the container uses. If a schema or
canonicalization change ever drifts the code away from those committed bytes,
the Docker e2e (test_specialist_install_from_repo.sh) would fail opaquely
inside a container; this fast unit test fails FIRST, with a clear message, in
the opt-out gate. Regenerate with::

    venv_test/bin/python test-local/fixtures/specialist-components/gen_fixtures.py
"""
from __future__ import annotations

from pathlib import Path

from persona_pack import load_persona_pack
from personality_binding import check_persona_requirements
from role_artifact import load_role_artifact
from role_slot import materialize_role
from specialist_component import load_specialist_component
from specialist_install import resolve_dependency_closure

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _REPO_ROOT / "test-local" / "fixtures"
_COMPONENT = _FIXTURES / "specialist-components" / "mtg-test"
_PERSONA = _FIXTURES / "personas" / "alt-butler-tina"
_BUTLER_ROLE_DIR = (
    _REPO_ROOT / "casa-agent/rootfs/opt/casa/defaults/roles/resident/butler"
)


def test_mtg_test_component_loads_with_available_dependencies() -> None:
    component = load_specialist_component(_COMPONENT, _COMPONENT / "manifest.json")
    assert component.slug == "mtg-test"
    assert component.component_id == "casa-test/mtg-test"
    assert component.version == "0.1.0"
    deps = resolve_dependency_closure(component, _COMPONENT)
    assert deps, "fixture must declare its bundled persona dependency"
    assert all(d.available for d in deps), [
        (d.kind, d.identifier, d.detail) for d in deps if not d.available
    ]


def test_alt_butler_persona_repo_is_installable_and_butler_compatible() -> None:
    pack = load_persona_pack(_PERSONA / "pack", _PERSONA / "manifest.json")
    assert pack.persona_id == "casa/tina"
    # A DIFFERENT version than the image default (casa/tina@0.1.0), so applying
    # it as an override genuinely changes butler's binding_digest.
    assert pack.version == "0.2.0"

    butler_role = materialize_role(source=load_role_artifact(_BUTLER_ROLE_DIR), options={})
    # Raises if the fixture persona would be rejected by the butler role's
    # persona compatibility range — the exact precondition persona_apply
    # enforces in the e2e.
    check_persona_requirements(butler_role.normalized, pack)
