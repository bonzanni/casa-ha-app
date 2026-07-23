#!/usr/bin/env python3
"""Static-fixture generator for the Task 16 final-gate e2e
(test-local/e2e/test_specialist_install_from_repo.sh).

Writes TWO fixtures with REAL, production-computed checksums, so the
install/persona pipelines accept them byte-for-byte:

  1. A minimal valid specialist COMPONENT repo (slug ``mtg-test``) under
     ``test-local/fixtures/specialist-components/mtg-test/`` — mirrors the
     shape of ``tests/test_specialist_install.py``'s ``_write_component``
     (role/ + bundled persona/ + config-schema.json + manifest.json), the
     canonical fixture shape that plan's install tests already validate.

  2. A bare-persona repo (``casa/tina@0.2.0``) under
     ``test-local/fixtures/personas/alt-butler-tina/`` — an override persona
     compatible with the butler resident role's persona requirement
     (``casa/tina@>=0.1.0 <1.0.0``, defaults/roles/resident/butler/role.yaml)
     but a DIFFERENT version than the image default (``casa/tina@0.1.0``), so
     applying it as an override forces butler's binding_digest to change.
     Mirrors ``tests/test_persona_install.py``'s ``_write_persona_repo``.

Single source of truth: this generator. The committed trees it produces are
loaded (and thereby rot-checked) by ``tests/test_fixture_specialist_component.py``
in the unit gate, and copied into the running container by the e2e. Checksums
are computed by the SAME production helpers (canonical_bytes,
specialist_component) the container uses, so a committed tree can never
silently drift from what the code will accept — the unit test fails first.

Run (from repo root) to regenerate the committed trees::

    venv_test/bin/python test-local/fixtures/specialist-components/gen_fixtures.py

or pass an explicit output base dir as argv[1] (the e2e / unit test do this to
write into a scratch dir instead of the committed location).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _ensure_code_root_on_path() -> None:
    """Make the casa code root importable whether run on the host
    (casa-agent/rootfs/opt/casa relative to this file) or inside the
    container (/opt/casa)."""
    for candidate in (
        Path(__file__).resolve().parents[3] / "casa-agent" / "rootfs" / "opt" / "casa",
        Path("/opt/casa"),
    ):
        if (candidate / "canonical_bytes.py").is_file():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("could not locate the casa code root (canonical_bytes.py)")


_ensure_code_root_on_path()

from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes  # noqa: E402
from specialist_component import compute_component_checksum  # noqa: E402
from plugin_store import content_checksum  # noqa: E402


def _write_persona_pack(pack_dir: Path, manifest_path: Path, *, persona_id: str,
                        version: str, display_name: str, archetype: str,
                        pronoun_set: str, core_fill: str, negative_space: str,
                        traits: dict) -> str:
    """Write a persona pack (persona.yaml + persona.md) and its manifest.json.
    Returns the pack checksum. Mirrors _write_persona_repo / _write_component's
    persona block exactly — manifest rows sorted by NAME (persona_pack.
    _admit_files' own admitted-file order)."""
    import yaml

    pack_dir.mkdir(parents=True, exist_ok=True)
    pronouns = {
        "he": {"subject": "he", "object": "him", "possessive_adjective": "his",
               "possessive_pronoun": "his", "reflexive": "himself"},
        "she": {"subject": "she", "object": "her", "possessive_adjective": "her",
                "possessive_pronoun": "hers", "reflexive": "herself"},
        "they": {"subject": "they", "object": "them", "possessive_adjective": "their",
                 "possessive_pronoun": "theirs", "reflexive": "themself"},
    }[pronoun_set]
    persona_yaml = {
        "api_version": "casa.persona/v1", "id": persona_id, "version": version,
        "trait_schema_version": 1,
        "identity": {"display_name": display_name, "pronouns": pronouns},
        "relationship_posture": "established", "archetype": archetype,
        "traits": traits, "quirks": [],
    }
    (pack_dir / "persona.yaml").write_text(
        yaml.safe_dump(persona_yaml, sort_keys=False), encoding="utf-8")
    (pack_dir / "persona.md").write_text(
        f"# Core\n\n{core_fill}\n\n## Negative space\n\n{negative_space}\n", encoding="utf-8")

    rows = []
    for name in sorted(os.listdir(pack_dir)):
        text = canonical_text((pack_dir / name).read_text(encoding="utf-8"))
        rows.append({"path": name, "type": "file", "executable": False,
                     "checksum": checksum_bytes(text.encode("utf-8"))})
    payload = {"api_version": "casa.persona.manifest/v1", "files": rows}
    checksum = checksum_bytes(canonical_json_bytes(payload))
    payload["checksum"] = checksum
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return checksum


def write_specialist_component(root: Path, *, slug: str = "mtg-test") -> Path:
    """A minimal valid specialist component (mirrors _write_component)."""
    import yaml

    if root.exists():
        import shutil
        shutil.rmtree(root)
    (root / "role").mkdir(parents=True)
    role_yaml = {
        "api_version": "casa.role/v1", "id": f"specialist:{slug}", "kind": "specialist",
        "slot": slug, "mission": "Answer Magic: the Gathering rules questions.",
        "enabled": True, "model": {"source": "fixed", "value": "sonnet"},
        "tools": {"allowed": [], "disallowed": ["Bash"], "permission_mode": "dontAsk",
                  "max_turns": 8, "skills": "none", "voice_guard": "none"},
        "mcp_servers": [], "channels": [],
        "memory": {"token_budget": 0, "read_strategy": "per_turn"},
        "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
        "disclosure": {"policy": "delegated", "overrides": {}},
        "delegates": [], "executors": [], "triggers": [], "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "none", "error_phrases": {}},
        "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                     "restricted_webhook": {"register": "plain"}},
        "persona": {"policy": "required", "compatibility": ["casa/judge@>=0.1.0 <1.0.0"]},
        "requires": {"plugins": [], "tools": []}, "doctrine_file": "doctrine.md",
    }
    (root / "role" / "role.yaml").write_text(
        yaml.safe_dump(role_yaml, sort_keys=False), encoding="utf-8")
    (root / "role" / "doctrine.md").write_text(
        "# Core doctrine\n\nAnswer Magic: the Gathering rules questions precisely.\n",
        encoding="utf-8")
    (root / "config-schema.json").write_text(
        json.dumps({"required": [], "secret_names": []}), encoding="utf-8")

    persona_checksum = _write_persona_pack(
        root / "persona" / "pack", root / "persona" / "manifest.json",
        persona_id="casa/judge", version="0.1.0", display_name="Judge",
        archetype="adjudicator", pronoun_set="they", core_fill="X" * 350,
        negative_space="Never guesses.",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3})

    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    component_checksum = compute_component_checksum(files)
    manifest = {
        "api_version": "casa.specialist-component/v1",
        "component_id": f"casa-test/{slug}", "version": "0.1.0",
        "default_persona": {"ref": "casa/judge@0.1.0", "checksum": persona_checksum},
        "dependencies": [
            {"kind": "persona", "identifier": "casa/judge@0.1.0", "digest": persona_checksum},
        ],
        "checksum": component_checksum,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def write_persona_repo(root: Path) -> Path:
    """A bare override-persona repo: casa/tina@0.2.0 (butler-compatible,
    differs from the image default casa/tina@0.1.0)."""
    if root.exists():
        import shutil
        shutil.rmtree(root)
    _write_persona_pack(
        root / "pack", root / "manifest.json",
        persona_id="casa/tina", version="0.2.0", display_name="Tina",
        archetype="butler", pronoun_set="she", core_fill="Z" * 350,
        negative_space="Never oversteps a household boundary.",
        # Traits deliberately differ from the image-default tina@0.1.0 so the
        # override materializes a genuinely different binding_digest.
        traits={"warmth": 4, "formality": 5, "candor": 3, "attunement": 5,
                "curiosity": 2, "levity": 2, "social_energy": 3, "optimism": 4})
    return root


def _write_bundled_plugin_tree(plugin_dir: Path, name: str = "bt") -> str:
    """Write a minimal, real-shaped bundled plugin tree at ``plugin_dir``
    (``.claude-plugin/plugin.json`` + one skill file — no ``.mcp.json``,
    no triggers/sysreqs/protectedTools, so it carries no consent-surface
    beyond its own bytes) and return the digest a manifest dependency row's
    ``digest`` field must pin (``"sha256:" + plugin_store.content_checksum
    (plugin_dir)``) for ``resolve_dependency_closure`` to resolve it
    ``available=True``. Mirrors ``tests/specialist_fixtures.write_bundled_
    plugin`` (this generator's committed-fixture counterpart), kept
    self-contained here rather than importing a ``tests/`` helper into a
    committed-fixture generator."""
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "0.1.0"}), encoding="utf-8")
    skills_dir = plugin_dir / "skills" / f"{name}-skill"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}-skill\n"
        "description: Trivial fixture skill for the bundled-specialist e2e "
        "(test_bundled_specialist_install.sh).\n"
        "---\n\n"
        f"# {name} skill\n\n"
        "This is a fixture skill with no real behavior — it exists only so "
        "the bundled plugin dependency has a second file besides its "
        "manifest.\n",
        encoding="utf-8",
    )
    return "sha256:" + content_checksum(plugin_dir)


def write_bundled_specialist_component(root: Path, *, slug: str = "bundletest") -> Path:
    """A minimal valid specialist component (mirrors ``write_specialist_
    component`` above) that ALSO carries one bundled plugin dependency
    (manifest name ``bt``, tree at ``plugins/bt``) — the Task 13 e2e fixture
    for the one-flow bundled-specialist install + uninstall cascade
    (``test_bundled_specialist_install.sh``). The bundled plugin publishes
    as the owned registry entry ``bundletest.bt`` (``plugin_registry.
    scoped_name(slug, "bt")``), owner ``specialist:bundletest``, targeting
    ``specialist:bundletest`` — exactly the shape
    ``tests/test_specialist_bundle_commit.py``'s ``_owned_entry`` helper
    and the real ``commit_specialist_install`` bundle-mode path produce."""
    import yaml

    if root.exists():
        import shutil
        shutil.rmtree(root)
    (root / "role").mkdir(parents=True)
    role_yaml = {
        "api_version": "casa.role/v1", "id": f"specialist:{slug}", "kind": "specialist",
        "slot": slug, "mission": "Answer bundled-plugin e2e fixture questions.",
        "enabled": True, "model": {"source": "fixed", "value": "sonnet"},
        "tools": {"allowed": [], "disallowed": ["Bash"], "permission_mode": "dontAsk",
                  "max_turns": 8, "skills": "none", "voice_guard": "none"},
        "mcp_servers": [], "channels": [],
        "memory": {"token_budget": 0, "read_strategy": "per_turn"},
        "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
        "disclosure": {"policy": "delegated", "overrides": {}},
        "delegates": [], "executors": [], "triggers": [], "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "none", "error_phrases": {}},
        "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                     "restricted_webhook": {"register": "plain"}},
        "persona": {"policy": "required", "compatibility": ["casa/judge@>=0.1.0 <1.0.0"]},
        "requires": {"plugins": [], "tools": []}, "doctrine_file": "doctrine.md",
    }
    (root / "role" / "role.yaml").write_text(
        yaml.safe_dump(role_yaml, sort_keys=False), encoding="utf-8")
    (root / "role" / "doctrine.md").write_text(
        "# Core doctrine\n\nAnswer bundled-plugin e2e fixture questions precisely.\n",
        encoding="utf-8")
    (root / "config-schema.json").write_text(
        json.dumps({"required": [], "secret_names": []}), encoding="utf-8")

    persona_checksum = _write_persona_pack(
        root / "persona" / "pack", root / "persona" / "manifest.json",
        persona_id="casa/judge", version="0.1.0", display_name="Judge",
        archetype="adjudicator", pronoun_set="they", core_fill="X" * 350,
        negative_space="Never guesses.",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3})

    plugin_digest = _write_bundled_plugin_tree(root / "plugins" / "bt", "bt")

    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    component_checksum = compute_component_checksum(files)
    manifest = {
        "api_version": "casa.specialist-component/v1",
        "component_id": f"casa-test/{slug}", "version": "0.1.0",
        "default_persona": {"ref": "casa/judge@0.1.0", "checksum": persona_checksum},
        "dependencies": [
            {"kind": "persona", "identifier": "casa/judge@0.1.0", "digest": persona_checksum},
            {"kind": "plugin/implementation", "identifier": "bt", "digest": plugin_digest,
             "source": {"type": "bundled", "path": "plugins/bt"}},
        ],
        "checksum": component_checksum,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def main() -> None:
    here = Path(__file__).resolve().parent          # test-local/fixtures/specialist-components
    fixtures_root = here.parent                       # test-local/fixtures
    if len(sys.argv) > 1:
        base = Path(sys.argv[1])
        component_root = base / "mtg-test"
        persona_root = base / "alt-butler-tina"
        bundled_root = base / "bundletest"
    else:
        component_root = here / "mtg-test"
        persona_root = fixtures_root / "personas" / "alt-butler-tina"
        bundled_root = here / "bundletest"
    write_specialist_component(component_root)
    write_persona_repo(persona_root)
    write_bundled_specialist_component(bundled_root)
    print(f"wrote specialist component -> {component_root}")
    print(f"wrote override persona repo -> {persona_root}")
    print(f"wrote bundled-plugin specialist component -> {bundled_root}")


if __name__ == "__main__":
    main()
