from __future__ import annotations

import json
import textwrap
from pathlib import Path

import jsonschema
import pytest

from canonical_bytes import canonical_json_bytes, checksum_bytes
from specialist_component import (
    ComponentDependency,
    SpecialistComponent,
    compute_component_checksum,
    load_specialist_component,
)

_FAKE_PERSONA_CHECKSUM = "sha256:" + "a" * 64
_FAKE_DEPENDENCY_DIGEST = "sha256:" + "b" * 64


def _role_yaml(*, kind: str, slug: str, persona_policy: str = "optional-but-bound") -> str:
    if persona_policy == "forbidden":
        persona_lines = ["persona:", "  policy: forbidden"]
    else:
        persona_lines = [
            "persona:",
            f"  policy: {persona_policy}",
            '  compatibility: ["casa/alex@>=0.1.0 <1.0.0"]',
        ]
    lines = [
        "api_version: casa.role/v1",
        f"id: {kind}:{slug}",
        f"kind: {kind}",
        f"slot: {slug}",
        f"mission: Handle {slug}-scoped delegations deterministically.",
        "enabled: false",
        "model: {source: fixed, value: sonnet}",
        "tools:",
        "  allowed: [Read, Skill]",
        "  disallowed: [Bash, Write, Edit]",
        "  permission_mode: acceptEdits",
        "  max_turns: 10",
        "  skills: all",
        "  voice_guard: none",
        "mcp_servers: []",
        "channels: []",
        "memory: {token_budget: 4000, read_strategy: per_turn}",
        "session: {strategy: ephemeral, idle_timeout_seconds: 0}",
        "disclosure: {policy: delegated, overrides: {}}",
        "delegates: []",
        "executors: []",
        "triggers: []",
        "hooks: {pre_tool_use: []}",
        "tts: {tag_dialect: none, error_phrases: {}}",
        "response:",
        "  text: {register: precise, max_status_sentences: 3}",
        "  voice: {register: spoken, max_status_sentences: 2}",
        "  restricted_webhook: {register: plain, max_status_sentences: 2}",
        *persona_lines,
        "requires: {plugins: [], tools: []}",
        "doctrine_file: doctrine.md",
    ]
    return "\n".join(lines) + "\n"


_DOCTRINE = textwrap.dedent("""\
    # Core doctrine

    Answer only scoped delegations precisely, citing retrieved evidence.

    ## Text projection

    Use concise prose.

    ## Voice projection

    Lead with the result.

    ## Restricted webhook projection

    Do not expose persona identity.
""")


def _build_component_dir(
    tmp_path: Path, *, slug: str = "mtg", kind: str = "specialist",
    dependencies: list[dict[str, str]] | None = None,
) -> tuple[Path, Path]:
    component_dir = tmp_path / "component"
    role_dir = component_dir / "role"
    role_dir.mkdir(parents=True)
    (role_dir / "role.yaml").write_text(_role_yaml(kind=kind, slug=slug), encoding="utf-8")
    (role_dir / "doctrine.md").write_text(_DOCTRINE, encoding="utf-8")
    config_schema = {"required": ["api_base"], "secret_names": []}
    (component_dir / "config-schema.json").write_text(
        json.dumps(config_schema), encoding="utf-8",
    )

    files = {
        "role/role.yaml": (role_dir / "role.yaml").read_bytes(),
        "role/doctrine.md": (role_dir / "doctrine.md").read_bytes(),
        "config-schema.json": (component_dir / "config-schema.json").read_bytes(),
    }
    checksum = compute_component_checksum(files)
    manifest = {
        "api_version": "casa.specialist-component/v1",
        "component_id": f"casa/{slug}",
        "version": "0.1.0",
        "default_persona": {"ref": "casa/alex@0.1.0", "checksum": _FAKE_PERSONA_CHECKSUM},
        "dependencies": dependencies or [],
        "checksum": checksum,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return component_dir, manifest_path


def test_compute_component_checksum_is_order_independent() -> None:
    files_a = {"a": b"1", "b": b"2"}
    files_b = {"b": b"2", "a": b"1"}
    assert compute_component_checksum(files_a) == compute_component_checksum(files_b)


def test_compute_component_checksum_changes_with_content() -> None:
    original = compute_component_checksum({"a": b"1"})
    changed = compute_component_checksum({"a": b"2"})
    assert original != changed


def test_compute_component_checksum_matches_manual_canonical_hash() -> None:
    files = {"a": b"1"}
    expected = checksum_bytes(canonical_json_bytes({
        "api_version": "casa.specialist-component.manifest/v1",
        "files": [{"path": "a", "checksum": checksum_bytes(b"1")}],
    }))
    assert compute_component_checksum(files) == expected


def test_load_specialist_component_round_trips_a_valid_component(tmp_path: Path) -> None:
    dependency = {"kind": "persona", "identifier": "casa/alex@0.1.0", "digest": _FAKE_DEPENDENCY_DIGEST}
    component_dir, manifest_path = _build_component_dir(tmp_path, slug="mtg", dependencies=[dependency])

    component = load_specialist_component(component_dir, manifest_path)

    assert isinstance(component, SpecialistComponent)
    assert component.component_id == "casa/mtg"
    assert component.version == "0.1.0"
    assert component.slug == "mtg"
    assert component.role.role["kind"] == "specialist"
    assert component.default_persona_ref == "casa/alex@0.1.0"
    assert component.default_persona_checksum == _FAKE_PERSONA_CHECKSUM
    assert component.config_schema == {"required": ["api_base"], "secret_names": []}
    assert component.dependencies == (
        ComponentDependency(kind="persona", identifier="casa/alex@0.1.0", digest=_FAKE_DEPENDENCY_DIGEST),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert component.checksum == manifest["checksum"]


def test_load_specialist_component_rejects_checksum_mismatch(tmp_path: Path) -> None:
    component_dir, manifest_path = _build_component_dir(tmp_path, slug="mtg")
    # Tamper the doctrine AFTER the manifest checksum was computed.
    (component_dir / "role" / "doctrine.md").write_text(_DOCTRINE + "\nExtra tampered line.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum"):
        load_specialist_component(component_dir, manifest_path)


def test_load_specialist_component_rejects_non_specialist_role_kind(tmp_path: Path) -> None:
    component_dir, manifest_path = _build_component_dir(tmp_path, slug="mtg", kind="executor")

    with pytest.raises(ValueError, match="kind: specialist"):
        load_specialist_component(component_dir, manifest_path)


def test_load_specialist_component_rejects_manifest_schema_violation(tmp_path: Path) -> None:
    component_dir, manifest_path = _build_component_dir(tmp_path, slug="mtg")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["checksum"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(jsonschema.exceptions.ValidationError):
        load_specialist_component(component_dir, manifest_path)
