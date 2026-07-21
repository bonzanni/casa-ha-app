from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import jsonschema
import yaml

from authored_markers import contains_forbidden_marker, reject_markers_in_parsed
from canonical_bytes import (
    assert_json_safe,
    canonical_json_bytes,
    canonical_text,
    checksum_bytes,
    deep_freeze,
)
from markdown_sections import sections

_REQUIRED = {"persona.yaml", "persona.md"}
_OPTIONAL = {"examples.yaml"}
_AXES = {
    "warmth", "formality", "candor", "attunement",
    "curiosity", "levity", "social_energy", "optimism",
}


class PersonaPackError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PersonaManifest:
    files: tuple[Mapping[str, object], ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class PersonaPack:
    persona_id: str
    version: str
    trait_schema_version: int
    identity: Mapping[str, object]
    relationship_posture: str
    archetype: str
    traits: Mapping[str, int]
    quirks: tuple[Mapping[str, str], ...]
    markdown: str
    examples: tuple[Mapping[str, str], ...]
    manifest: PersonaManifest
    checksum: str


def _admit_files(pack_dir: Path) -> tuple[Path, ...]:
    entries = sorted(pack_dir.iterdir(), key=lambda path: path.name)
    names = {entry.name for entry in entries}
    if not _REQUIRED <= names or names - _REQUIRED - _OPTIONAL:
        raise PersonaPackError("persona pack file set is invalid")
    admitted = []
    for path in entries:
        info = path.lstat()
        if path.name.startswith(".") or not stat.S_ISREG(info.st_mode):
            raise PersonaPackError(f"invalid persona file: {path.name}")
        if info.st_nlink != 1:
            raise PersonaPackError(f"hard-linked persona file: {path.name}")
        if info.st_mode & 0o111:
            raise PersonaPackError(f"executable persona file: {path.name}")
        admitted.append(path)
    return tuple(admitted)


def _reject_markers(text: str) -> None:
    if contains_forbidden_marker(text):
        raise PersonaPackError("template, include, HTML, or delimiter detected")


def _validate_schema(payload: object, schema_path: Path) -> None:
    # jsonschema.validate raises jsonschema.exceptions.ValidationError on
    # its own — the loader's contract is that every rejection surfaces as
    # PersonaPackError, so wrap it here rather than leaking the jsonschema
    # exception type to callers.
    try:
        jsonschema.validate(payload, json.loads(schema_path.read_text()))
    except jsonschema.exceptions.ValidationError as exc:
        raise PersonaPackError(f"schema validation failed: {exc.message}") from exc


def load_persona_pack(pack_dir: Path, manifest_path: Path) -> PersonaPack:
    files = _admit_files(pack_dir)
    canonical_files = {}
    manifest_rows = []
    for path in files:
        text = canonical_text(path.read_text(encoding="utf-8"))
        _reject_markers(text)
        canonical_files[path.name] = text
        manifest_rows.append({
            "path": path.name,
            "type": "file",
            "executable": False,
            "checksum": checksum_bytes(text.encode("utf-8")),
        })

    try:
        persona = yaml.safe_load(canonical_files["persona.yaml"])
    except (yaml.YAMLError, RecursionError) as exc:
        # F-B (foundation review r3, P0): a hostile deeply-nested flow
        # scalar makes yaml's own PARSER recurse, well under any size cap
        # here — this happens INSIDE yaml.safe_load, before
        # assert_json_safe below (which only bounds depth in the
        # ALREADY-PARSED tree) ever runs. Fold both a genuine YAML syntax
        # error and a parser RecursionError into the same generic,
        # fail-closed PersonaPackError.
        raise PersonaPackError(
            "persona.yaml contains invalid or unparseable YAML"
        ) from exc
    # R1 (foundation review r2): guarantees freeze-soundness (deep_freeze
    # below) and rejects any non-JSON type (set/bytes/datetime/cyclic) a
    # YAML tag/alias could otherwise smuggle through yaml.safe_load.
    try:
        assert_json_safe(persona)
    except ValueError as exc:
        raise PersonaPackError(
            f"persona.yaml contains non-JSON-native content: {exc}"
        ) from exc
    # F-A (foundation review r3): persona.yaml is already raw-text
    # marker-scanned above via _reject_markers (kept as defense in depth),
    # but a YAML string escape (e.g. "\x24\x7bOVERRIDE\x7d") has no
    # literal marker bytes in the raw source — yaml.safe_load DECODES it
    # into the live marker. Scan the PARSED string leaves too, which sees
    # the value only after decoding and so cannot be bypassed this way.
    try:
        reject_markers_in_parsed(persona)
    except ValueError as exc:
        raise PersonaPackError(str(exc)) from exc
    schema_path = (
        Path(__file__).parent / "defaults/schema/persona.v1.json"
    )
    _validate_schema(persona, schema_path)
    if set(persona["traits"]) != _AXES:
        raise PersonaPackError("traits must contain exactly the eight v1 axes")

    try:
        parsed_sections = sections(canonical_files["persona.md"])
    except ValueError as exc:
        raise PersonaPackError(f"persona.md failed Markdown validation: {exc}") from exc
    core_bodies = [body for level, name, body in parsed_sections
                   if level == 1 and name == "Core"]
    if len(core_bodies) != 1:
        raise PersonaPackError("exactly one # Core section is required")
    core_length = len(core_bodies[0].strip())
    if not 300 <= core_length <= 500:
        raise PersonaPackError("Core body must contain 300–500 characters")
    if not any(level == 2 and name == "Negative space"
               for level, name, _ in parsed_sections):
        raise PersonaPackError("## Negative space is required")

    examples = ()
    if "examples.yaml" in canonical_files:
        try:
            raw_examples = yaml.safe_load(canonical_files["examples.yaml"])
        except (yaml.YAMLError, RecursionError) as exc:
            # F-B (foundation review r3, P0): same parser-recursion hazard
            # as persona.yaml above.
            raise PersonaPackError(
                "examples.yaml contains invalid or unparseable YAML"
            ) from exc
        try:
            assert_json_safe(raw_examples)
        except ValueError as exc:
            raise PersonaPackError(
                f"examples.yaml contains non-JSON-native content: {exc}"
            ) from exc
        # F-A (foundation review r3): same parsed-leaf marker scan as
        # persona.yaml above — examples.yaml's raw text is already
        # marker-scanned via _reject_markers, but a YAML string escape is
        # only visible after yaml.safe_load decodes it.
        try:
            reject_markers_in_parsed(raw_examples)
        except ValueError as exc:
            raise PersonaPackError(str(exc)) from exc
        example_schema = (
            Path(__file__).parent / "defaults/schema/persona-examples.v1.json"
        )
        _validate_schema(raw_examples, example_schema)
        examples = tuple(
            deep_freeze(value)
            for value in raw_examples.get("examples", [])
        )

    manifest_payload = {
        "api_version": "casa.persona.manifest/v1",
        "files": manifest_rows,
    }
    manifest_checksum = checksum_bytes(canonical_json_bytes(manifest_payload))
    manifest_payload["checksum"] = manifest_checksum
    try:
        expected_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersonaPackError(f"persona manifest could not be read: {exc}") from exc
    if expected_manifest != manifest_payload:
        raise PersonaPackError("persona manifest does not match admitted files")

    return PersonaPack(
        persona_id=persona["id"],
        version=persona["version"],
        trait_schema_version=persona["trait_schema_version"],
        identity=deep_freeze(persona["identity"]),
        relationship_posture=persona["relationship_posture"],
        archetype=persona["archetype"],
        traits=deep_freeze(persona["traits"]),
        quirks=tuple(deep_freeze(q) for q in persona.get("quirks", [])),
        markdown=canonical_files["persona.md"],
        examples=examples,
        manifest=PersonaManifest(
            files=tuple(deep_freeze(row) for row in manifest_rows),
            checksum=manifest_checksum,
        ),
        checksum=manifest_checksum,
    )
