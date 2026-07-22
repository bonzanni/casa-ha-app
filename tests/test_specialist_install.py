import json
import os
from pathlib import Path

import pytest
import yaml

from specialist_install import (
    DependencyResolution,
    SpecialistInstallError,
    resolve_dependency_closure,
)
from specialist_component import load_specialist_component
from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes


def _write_component(root: Path, *, slug: str = "mtg-test",
                      dependencies: list[dict] | None = None) -> Path:
    (root / "role").mkdir(parents=True)
    (root / "persona" / "pack").mkdir(parents=True)
    role_yaml = {
        "api_version": "casa.role/v1", "id": f"specialist:{slug}", "kind": "specialist",
        "slot": slug, "mission": "Answer test questions.", "enabled": True,
        "model": {"source": "fixed", "value": "sonnet"},
        "tools": {"allowed": [], "disallowed": ["Bash"], "permission_mode": "dontAsk",
                   "max_turns": 8, "skills": "none", "voice_guard": "none"},
        "mcp_servers": [], "channels": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
        "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
        "disclosure": {"policy": "delegated", "overrides": {}},
        "delegates": [], "executors": [], "triggers": [], "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "none", "error_phrases": {}},
        "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                      "restricted_webhook": {"register": "plain"}},
        "persona": {"policy": "required", "compatibility": ["casa/judge@>=0.1.0 <1.0.0"]},
        "requires": {"plugins": [], "tools": []}, "doctrine_file": "doctrine.md",
    }
    (root / "role" / "role.yaml").write_text(yaml.safe_dump(role_yaml, sort_keys=False), encoding="utf-8")
    (root / "role" / "doctrine.md").write_text("# Core doctrine\n\nAnswer test questions.\n", encoding="utf-8")
    config_schema = {"required": [], "secret_names": []}
    (root / "config-schema.json").write_text(json.dumps(config_schema), encoding="utf-8")

    persona_yaml = {
        "api_version": "casa.persona/v1", "id": "casa/judge", "version": "0.1.0",
        "trait_schema_version": 1,
        "identity": {"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        "relationship_posture": "established", "archetype": "adjudicator",
        "traits": {"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                    "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        "quirks": [],
    }
    (root / "persona" / "pack" / "persona.yaml").write_text(
        yaml.safe_dump(persona_yaml, sort_keys=False), encoding="utf-8")
    core = "X" * 350
    (root / "persona" / "pack" / "persona.md").write_text(
        f"# Core\n\n{core}\n\n## Negative space\n\nNever guesses.\n", encoding="utf-8")
    manifest_rows = []
    # persona_pack._admit_files sorts admitted files by NAME
    # ("persona.md" < "persona.yaml" alphabetically) — the manifest row
    # order must match that sort, not source-declaration order, or
    # load_persona_pack's own recomputed manifest payload (and hence its
    # checksum) will never equal what's written to disk here.
    for name in sorted(os.listdir(root / "persona" / "pack")):
        text = canonical_text((root / "persona" / "pack" / name).read_text(encoding="utf-8"))
        manifest_rows.append({"path": name, "type": "file", "executable": False,
                               "checksum": checksum_bytes(text.encode("utf-8"))})
    persona_manifest_payload = {"api_version": "casa.persona.manifest/v1", "files": manifest_rows}
    persona_checksum = checksum_bytes(canonical_json_bytes(persona_manifest_payload))
    persona_manifest_payload["checksum"] = persona_checksum
    (root / "persona" / "manifest.json").write_text(json.dumps(persona_manifest_payload), encoding="utf-8")

    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    from specialist_component import compute_component_checksum
    component_checksum = compute_component_checksum(files)
    manifest = {
        "api_version": "casa.specialist-component/v1", "component_id": f"casa-test/{slug}",
        "version": "0.1.0",
        "default_persona": {"ref": "casa/judge@0.1.0", "checksum": persona_checksum},
        # Controller resolution B: a dependency row is exactly
        # {kind, identifier, digest} — specialist-component.v1.json sets
        # additionalProperties: false, so any extra key (the brief's stale
        # "checksum_field_unused") would fail schema validation.
        "dependencies": dependencies if dependencies is not None else [
            {"kind": "persona", "identifier": "casa/judge@0.1.0", "digest": persona_checksum},
        ],
        "checksum": component_checksum,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_resolve_dependency_closure_marks_bundled_persona_available(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component")
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    persona_rows = [r for r in resolutions if r.kind == "persona"]
    assert len(persona_rows) == 1
    assert persona_rows[0].available is True


def test_resolve_dependency_closure_reports_missing_corpus(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component", dependencies=[
        {"kind": "corpus/data", "identifier": "mtg-rules-corpus", "digest": "sha256:" + "9" * 64},
    ])
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    assert resolutions[0].available is False
    assert "corpus" in resolutions[0].detail


def test_resolve_dependency_closure_matches_corpus_digest(tmp_path: Path) -> None:
    from plugin_store import content_checksum

    corpus_dir = tmp_path / "component" / "corpus" / "mtg-rules-corpus"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "cr.txt").write_text("702.1 Some rule text.\n", encoding="utf-8")
    # plugin_store.content_checksum returns a BARE hex digest; the
    # specialist-component.v1.json schema constrains a dependency row's
    # `digest` field to `sha256:<hex>` — the brief's test used the bare
    # digest directly, which would fail schema validation at
    # load_specialist_component. Prefix it here, matching the normalization
    # resolve_dependency_closure itself applies to the same bare value.
    digest = "sha256:" + content_checksum(corpus_dir)
    root = _write_component(tmp_path / "component", dependencies=[
        {"kind": "corpus/data", "identifier": "mtg-rules-corpus", "digest": digest},
    ])
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    assert resolutions[0].available is True
