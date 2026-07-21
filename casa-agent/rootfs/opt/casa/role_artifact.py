from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import jsonschema
import yaml

from authored_markers import contains_forbidden_marker, reject_markers_in_parsed
from canonical_bytes import assert_json_safe, canonical_text, deep_freeze
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


# role.yaml's marker check runs on PARSED string leaves (dict keys AND
# values, via authored_markers.reject_markers_in_parsed), not raw source
# text. role.v1.json intentionally leaves some fields open
# (``disclosure.overrides``, ``tts.error_phrases`` values) as free-form
# strings — that authored-content surface is exactly what this check must
# cover. Raw-text scanning is deliberately NOT used here (unlike
# doctrine.md/persona content) because role.yaml's own flow-style YAML
# syntax legitimately produces adjacent closing braces (e.g.
# ``disclosure: {policy: standard, overrides: {}}``), which collides
# byte-for-byte with the forbidden ``}}`` template-close marker in every
# shipped role artifact. Scanning only the parsed string leaves catches
# every real injection surface (any string value the schema allows)
# without that false positive, and — per foundation review r3, F-A — is
# also immune to a marker hidden behind a YAML string escape, since it
# only ever sees the DECODED value.


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
    # R1 (foundation review r2): yaml.safe_load can yield non-JSON-native
    # types (set/bytes/datetime/cyclic) via YAML tags/aliases that
    # role.v1.json's schema-open fields admit. Assert the parsed tree is
    # JSON-native BEFORE the marker walk / schema validation / deep_freeze
    # below, so none of them can crash on a cycle or be bypassed by a
    # marker hidden inside a non-dict/list/str container.
    assert_json_safe(raw)
    reject_markers_in_parsed(raw)
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
        role=deep_freeze(raw), doctrine=doctrine,
        role_path=role_path, doctrine_path=doctrine_path,
    )
