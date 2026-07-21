from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import jsonschema
import yaml

from authored_markers import contains_forbidden_marker
from canonical_bytes import canonical_text
from markdown_sections import validate_markdown

# Byte-size caps checked BEFORE read_text, so a hostile artifact can't force
# an unbounded allocation. role.yaml is a small structured config file;
# doctrine.md is free-form prose but still bounded generously.
MAX_ROLE_YAML_BYTES = 65536
MAX_DOCTRINE_BYTES = 262144
_EXPECTED_FILES: dict[str, int] = {
    "role.yaml": MAX_ROLE_YAML_BYTES,
    "doctrine.md": MAX_DOCTRINE_BYTES,
}


@dataclass(frozen=True, slots=True)
class RoleArtifactSource:
    role: Mapping[str, object]
    doctrine: str
    role_path: Path
    doctrine_path: Path


def _reject_markers(text: str) -> None:
    if contains_forbidden_marker(text):
        raise ValueError("template, include, HTML, or delimiter detected")


def _iter_string_leaves(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_string_leaves(key)
            yield from _iter_string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_string_leaves(item)


def _reject_markers_in_role_values(raw: object) -> None:
    """Marker-check role.yaml's authored STRING VALUES rather than its raw
    source text. role.v1.json intentionally leaves some fields open
    (``disclosure.overrides``, ``tts.error_phrases`` values) as free-form
    strings — that authored-content surface is exactly what this check must
    cover. Raw-text scanning is deliberately NOT used here (unlike
    doctrine.md/persona content) because role.yaml's own flow-style YAML
    syntax legitimately produces adjacent closing braces (e.g.
    ``disclosure: {policy: standard, overrides: {}}``), which collides
    byte-for-byte with the forbidden ``}}`` template-close marker in every
    shipped role artifact. Scanning only the parsed string leaves catches
    every real injection surface (any string value the schema allows)
    without that false positive, since YAML's own structural punctuation
    never appears inside a parsed string value."""
    for text in _iter_string_leaves(raw):
        if contains_forbidden_marker(text):
            raise ValueError("template, include, HTML, or delimiter detected")


def _admit_files(role_dir: Path) -> dict[str, Path]:
    """Admit exactly {role.yaml, doctrine.md} from *role_dir* as an
    adversarial trust gate — mirrors persona_pack.py's _admit_files.
    Uses lstat (never is_file(), which follows symlinks) so a hostile
    artifact directory can't substitute a symlink, hard link, or
    executable file for either expected entry, and rejects any hidden or
    unexpected entry outright rather than silently ignoring it."""
    entries = sorted(role_dir.iterdir(), key=lambda path: path.name)
    names = {entry.name for entry in entries}
    if names != set(_EXPECTED_FILES):
        raise ValueError(
            f"role artifact must contain exactly {sorted(_EXPECTED_FILES)}"
        )
    admitted: dict[str, Path] = {}
    for path in entries:
        info = path.lstat()
        if path.name.startswith(".") or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"invalid role artifact file: {path.name}")
        if info.st_nlink != 1:
            raise ValueError(f"hard-linked role artifact file: {path.name}")
        if info.st_mode & 0o111:
            raise ValueError(f"executable role artifact file: {path.name}")
        cap = _EXPECTED_FILES[path.name]
        if info.st_size > cap:
            raise ValueError(f"role artifact file too large: {path.name}")
        admitted[path.name] = path
    return admitted


def load_role_artifact(role_dir: Path) -> RoleArtifactSource:
    admitted = _admit_files(role_dir)
    role_path = admitted["role.yaml"]
    doctrine_path = admitted["doctrine.md"]

    role_text = canonical_text(role_path.read_text(encoding="utf-8"))
    raw = yaml.safe_load(role_text)
    _reject_markers_in_role_values(raw)
    schema_path = Path(__file__).parent / "defaults/schema/role.v1.json"
    jsonschema.validate(raw, json.loads(schema_path.read_text(encoding="utf-8")))

    doctrine = canonical_text(doctrine_path.read_text(encoding="utf-8"))
    if not doctrine.strip():
        raise ValueError("role doctrine is empty")
    _reject_markers(doctrine)
    try:
        validate_markdown(doctrine)
    except ValueError as exc:
        raise ValueError(f"role doctrine failed Markdown validation: {exc}") from exc
    if raw["doctrine_file"] != doctrine_path.name:
        raise ValueError("doctrine_file must resolve to doctrine.md")
    return RoleArtifactSource(
        role=MappingProxyType(dict(raw)), doctrine=doctrine,
        role_path=role_path, doctrine_path=doctrine_path,
    )
