from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

import jsonschema

from canonical_bytes import canonical_json_bytes, checksum_bytes
from role_artifact import RoleArtifactSource, load_role_artifact

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class ComponentDependency:
    kind: Literal["persona", "corpus/data", "plugin/implementation"]
    identifier: str
    digest: str


@dataclass(frozen=True, slots=True)
class SpecialistComponent:
    component_id: str
    version: str
    slug: str
    role: RoleArtifactSource
    default_persona_ref: str
    default_persona_checksum: str
    config_schema: Mapping[str, object]
    dependencies: tuple[ComponentDependency, ...]
    checksum: str


def compute_component_checksum(files: Mapping[str, bytes]) -> str:
    rows = [{"path": name, "checksum": checksum_bytes(files[name])} for name in sorted(files)]
    payload = {"api_version": "casa.specialist-component.manifest/v1", "files": rows}
    return checksum_bytes(canonical_json_bytes(payload))


def load_specialist_component(component_dir: Path, manifest_path: Path) -> SpecialistComponent:
    """Load a validated, checksum-verified specialist component from a LOCAL directory
    (already fetched/staged by Plan 2's N1 install pipeline, or a test fixture). This
    function performs no network I/O and trusts nothing about the caller's fetch —
    every byte here is re-validated exactly as image content is (spec §2.5)."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_path = Path(__file__).parent / "defaults/schema/specialist-component.v1.json"
    jsonschema.validate(manifest, json.loads(schema_path.read_text(encoding="utf-8")))

    role_dir = component_dir / "role"
    role_source = load_role_artifact(role_dir)
    if role_source.role["kind"] != "specialist":
        raise ValueError("a specialist component's role artifact must declare kind: specialist")
    slug = str(role_source.role["slot"])
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"invalid specialist slug {slug!r}")

    config_schema_path = component_dir / "config-schema.json"
    config_schema = json.loads(config_schema_path.read_text(encoding="utf-8"))
    dependencies = tuple(
        ComponentDependency(kind=row["kind"], identifier=row["identifier"], digest=row["digest"])
        for row in manifest.get("dependencies", [])
    )
    files = {
        "role/role.yaml": role_dir.joinpath("role.yaml").read_bytes(),
        "role/doctrine.md": role_dir.joinpath("doctrine.md").read_bytes(),
        "config-schema.json": config_schema_path.read_bytes(),
    }
    computed = compute_component_checksum(files)
    if computed != manifest["checksum"]:
        raise ValueError("specialist component checksum does not match its manifest")

    return SpecialistComponent(
        component_id=manifest["component_id"], version=manifest["version"], slug=slug, role=role_source,
        default_persona_ref=manifest["default_persona"]["ref"],
        default_persona_checksum=manifest["default_persona"]["checksum"],
        config_schema=MappingProxyType(dict(config_schema)), dependencies=dependencies, checksum=computed,
    )
