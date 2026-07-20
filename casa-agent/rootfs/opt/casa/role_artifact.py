from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import jsonschema
import yaml

from canonical_bytes import canonical_text


@dataclass(frozen=True, slots=True)
class RoleArtifactSource:
    role: Mapping[str, object]
    doctrine: str
    role_path: Path
    doctrine_path: Path


def load_role_artifact(role_dir: Path) -> RoleArtifactSource:
    expected = {"role.yaml", "doctrine.md"}
    actual = {path.name for path in role_dir.iterdir() if not path.name.startswith(".")}
    if actual != expected or any(not path.is_file() for path in role_dir.iterdir()):
        raise ValueError(f"role artifact must contain exactly {sorted(expected)}")
    role_path = role_dir / "role.yaml"
    doctrine_path = role_dir / "doctrine.md"
    raw = yaml.safe_load(canonical_text(role_path.read_text(encoding="utf-8")))
    schema_path = Path(__file__).parent / "defaults/schema/role.v1.json"
    jsonschema.validate(raw, json.loads(schema_path.read_text(encoding="utf-8")))
    doctrine = canonical_text(doctrine_path.read_text(encoding="utf-8"))
    if not doctrine.strip():
        raise ValueError("role doctrine is empty")
    if raw["doctrine_file"] != doctrine_path.name:
        raise ValueError("doctrine_file must resolve to doctrine.md")
    return RoleArtifactSource(
        role=MappingProxyType(dict(raw)), doctrine=doctrine,
        role_path=role_path, doctrine_path=doctrine_path,
    )
