"""Tests for role_artifact.py — the canonical role-artifact loader seam
(Personality Phase A, Task 5).

``load_role_artifact(role_dir)`` reads one image-owned canonical role
directory (``defaults/roles/<kind>/<slot>/``: exactly ``role.yaml`` +
``doctrine.md``), canonicalizes both files' text (NFC + LF via
``canonical_bytes.canonical_text``), schema-validates ``role.yaml``
against ``defaults/schema/role.v1.json``, and returns an immutable
``RoleArtifactSource``. It fails closed on: a missing directory, an
incomplete or extra file set, a schema-invalid role, and an empty
doctrine body. It does NOT perform model resolution, kind/slot
cross-validation against the directory it was loaded from, or checksum
computation — those are agent_loader.py's (Task 5) and role_slot.py's
(Task 6) jobs respectively.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import MappingProxyType

import jsonschema
import pytest
import yaml

from role_artifact import RoleArtifactSource, load_role_artifact


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / (
    "casa-agent/rootfs/opt/casa/defaults/schema/role.v1.json"
)
_REAL_ROLES_DIR = Path(__file__).resolve().parent.parent / (
    "casa-agent/rootfs/opt/casa/defaults/roles"
)

_REAL_ROLE_DIRS = [
    ("resident", "assistant"),
    ("resident", "butler"),
    ("specialist", "finance"),
    ("executor", "configurator"),
    ("executor", "plugin-developer"),
]


def valid_role(**overrides: object) -> dict:
    """A minimal role.yaml payload that satisfies role.v1.json exactly."""
    role = {
        "api_version": "casa.role/v1",
        "id": "resident:testrole",
        "kind": "resident",
        "slot": "testrole",
        "mission": "A test-fixture role.",
        "enabled": True,
        "model": {"source": "fixed", "value": "sonnet"},
        "tools": {
            "allowed": ["Read"],
            "disallowed": [],
            "permission_mode": "acceptEdits",
            "max_turns": 10,
            "skills": "all",
            "voice_guard": "none",
        },
        "mcp_servers": [],
        "channels": [],
        "memory": {"token_budget": 0, "read_strategy": "per_turn"},
        "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
        "disclosure": {"policy": "standard", "overrides": {}},
        "delegates": [],
        "executors": [],
        "triggers": [],
        "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "none", "error_phrases": {}},
        "response": {
            "text": {"register": "plain"},
            "voice": {"register": "plain"},
            "restricted_webhook": {"register": "plain"},
        },
        "persona": {"policy": "forbidden"},
        "requires": {"plugins": [], "tools": []},
        "doctrine_file": "doctrine.md",
    }
    role.update(overrides)
    return role


def write_role_dir(
    base: Path,
    *,
    role: dict | None = None,
    role_yaml_text: str | None = None,
    doctrine_text: str | None = "# Core doctrine\n\nBody.\n",
    extra_files: dict[str, str] | None = None,
    omit_role_yaml: bool = False,
    omit_doctrine: bool = False,
) -> Path:
    """Write a role_dir under *base* with configurable contents/defects."""
    d = base / "role_dir"
    d.mkdir(parents=True, exist_ok=True)
    if not omit_role_yaml:
        text = role_yaml_text if role_yaml_text is not None else yaml.safe_dump(
            role if role is not None else valid_role()
        )
        (d / "role.yaml").write_text(text, encoding="utf-8")
    if not omit_doctrine and doctrine_text is not None:
        (d / "doctrine.md").write_text(doctrine_text, encoding="utf-8")
    for name, content in (extra_files or {}).items():
        (d / name).write_text(content, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_loads_valid_role_artifact(self, tmp_path):
        role = valid_role()
        d = write_role_dir(tmp_path, role=role)

        artifact = load_role_artifact(d)

        assert isinstance(artifact, RoleArtifactSource)
        assert artifact.role["id"] == "resident:testrole"
        assert artifact.role["kind"] == "resident"
        assert artifact.role["slot"] == "testrole"
        assert artifact.role["model"] == {"source": "fixed", "value": "sonnet"}
        assert artifact.doctrine == "# Core doctrine\n\nBody.\n"
        assert artifact.role_path == d / "role.yaml"
        assert artifact.doctrine_path == d / "doctrine.md"

    def test_role_mapping_is_immutable(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role())
        artifact = load_role_artifact(d)

        assert isinstance(artifact.role, MappingProxyType)
        with pytest.raises(TypeError):
            artifact.role["kind"] = "specialist"  # type: ignore[index]

    def test_dataclass_is_frozen(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role())
        artifact = load_role_artifact(d)

        with pytest.raises(dataclasses.FrozenInstanceError):
            artifact.doctrine = "mutated"  # type: ignore[misc]

    def test_hidden_dotfile_in_role_dir_is_tolerated(self, tmp_path):
        """A dotfile (e.g. editor swap state) sitting alongside role.yaml
        and doctrine.md must not trip the exact-file-set check."""
        d = write_role_dir(tmp_path, role=valid_role())
        (d / ".DS_Store").write_text("junk", encoding="utf-8")

        artifact = load_role_artifact(d)
        assert artifact.role["slot"] == "testrole"


# ---------------------------------------------------------------------------
# File-set rejection: missing directory / missing file / extra file
# ---------------------------------------------------------------------------


class TestFileSetRejection:
    def test_missing_directory_raises(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        with pytest.raises(OSError):
            load_role_artifact(missing)

    def test_missing_role_yaml_raises(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role(), omit_role_yaml=True)
        with pytest.raises(ValueError, match="role artifact must contain exactly"):
            load_role_artifact(d)

    def test_missing_doctrine_raises(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role(), omit_doctrine=True)
        with pytest.raises(ValueError, match="role artifact must contain exactly"):
            load_role_artifact(d)

    def test_extra_file_raises(self, tmp_path):
        d = write_role_dir(
            tmp_path, role=valid_role(),
            extra_files={"notes.md": "stray file\n"},
        )
        with pytest.raises(ValueError, match="role artifact must contain exactly"):
            load_role_artifact(d)

    def test_extra_subdirectory_raises(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role())
        (d / "prompts").mkdir()
        (d / "prompts" / "system.md").write_text("nope\n", encoding="utf-8")

        with pytest.raises(ValueError, match="role artifact must contain exactly"):
            load_role_artifact(d)

    def test_empty_directory_raises(self, tmp_path):
        d = tmp_path / "role_dir"
        d.mkdir()
        with pytest.raises(ValueError, match="role artifact must contain exactly"):
            load_role_artifact(d)


# ---------------------------------------------------------------------------
# Doctrine body rejection
# ---------------------------------------------------------------------------


class TestDoctrineRejection:
    def test_empty_doctrine_raises(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role(), doctrine_text="")
        with pytest.raises(ValueError, match="role doctrine is empty"):
            load_role_artifact(d)

    def test_whitespace_only_doctrine_raises(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role(), doctrine_text="   \n\n\t \n")
        with pytest.raises(ValueError, match="role doctrine is empty"):
            load_role_artifact(d)

    def test_doctrine_is_canonicalized(self, tmp_path):
        """CRLF + trailing whitespace + missing final newline in doctrine.md
        must come back through canonical_text (NFC, LF-only, no trailing
        whitespace, exactly one terminal newline)."""
        d = write_role_dir(
            tmp_path, role=valid_role(),
            doctrine_text="# Core doctrine\r\n\r\nBody with trailing space.   \r\n",
        )
        artifact = load_role_artifact(d)
        assert artifact.doctrine == "# Core doctrine\n\nBody with trailing space.\n"
        assert "\r" not in artifact.doctrine


# ---------------------------------------------------------------------------
# Schema-invalid role.yaml
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_missing_required_field_raises(self, tmp_path):
        role = valid_role()
        del role["mission"]
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(jsonschema.exceptions.ValidationError):
            load_role_artifact(d)

    def test_unknown_top_level_field_raises(self, tmp_path):
        role = valid_role()
        role["bogus_field"] = "surprise"
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(jsonschema.exceptions.ValidationError):
            load_role_artifact(d)

    def test_invalid_kind_enum_raises(self, tmp_path):
        role = valid_role(kind="butler")  # not resident/specialist/executor
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(jsonschema.exceptions.ValidationError):
            load_role_artifact(d)

    def test_invalid_id_pattern_raises(self, tmp_path):
        role = valid_role(id="Resident:TestRole")  # pattern requires lowercase
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(jsonschema.exceptions.ValidationError):
            load_role_artifact(d)

    def test_wrong_doctrine_file_const_raises(self, tmp_path):
        role = valid_role(doctrine_file="doctrine.txt")  # schema pins the const
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(jsonschema.exceptions.ValidationError):
            load_role_artifact(d)

    def test_persona_required_without_compatibility_raises(self, tmp_path):
        role = valid_role(persona={"policy": "required"})  # missing compatibility
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(jsonschema.exceptions.ValidationError):
            load_role_artifact(d)

    def test_ha_option_model_missing_allowed_raises(self, tmp_path):
        role = valid_role(model={
            "source": "ha_option", "option": "primary_agent_model",
            "default": "opus",
        })  # missing "allowed"
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(jsonschema.exceptions.ValidationError):
            load_role_artifact(d)

    def test_malformed_yaml_raises(self, tmp_path):
        d = write_role_dir(
            tmp_path, role_yaml_text="api_version: [unterminated\n",
        )
        with pytest.raises(yaml.YAMLError):
            load_role_artifact(d)


# ---------------------------------------------------------------------------
# Canonicalization of role.yaml text before parsing
# ---------------------------------------------------------------------------


class TestRoleYamlCanonicalization:
    def test_crlf_role_yaml_still_parses(self, tmp_path):
        role = valid_role()
        text = yaml.safe_dump(role).replace("\n", "\r\n")
        d = write_role_dir(tmp_path, role_yaml_text=text)

        artifact = load_role_artifact(d)
        assert artifact.role["slot"] == "testrole"


# ---------------------------------------------------------------------------
# Real shipped canonical role artifacts (integration)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,slot", _REAL_ROLE_DIRS)
def test_real_shipped_role_artifact_loads(kind, slot):
    """Every one of the five image-owned canonical role artifacts must
    itself satisfy role.v1.json and load_role_artifact's own contract —
    this is the tie between the schema/loader and the actual shipped
    defaults/roles/<kind>/<slot>/ content."""
    role_dir = _REAL_ROLES_DIR / kind / slot
    artifact = load_role_artifact(role_dir)

    assert artifact.role["kind"] == kind
    assert artifact.role["slot"] == slot
    assert artifact.role["id"] == f"{kind}:{slot}"
    assert artifact.role["doctrine_file"] == "doctrine.md"
    assert artifact.doctrine.strip() != ""
    # Every shipped doctrine starts with the same top-level heading.
    assert artifact.doctrine.startswith("# Core doctrine\n")


def test_real_shipped_executor_roles_forbid_persona():
    for kind, slot in _REAL_ROLE_DIRS:
        if kind != "executor":
            continue
        artifact = load_role_artifact(_REAL_ROLES_DIR / kind / slot)
        assert artifact.role["persona"] == {"policy": "forbidden"}


def test_real_shipped_resident_roles_require_persona():
    for kind, slot in _REAL_ROLE_DIRS:
        if kind != "resident":
            continue
        artifact = load_role_artifact(_REAL_ROLES_DIR / kind / slot)
        assert artifact.role["persona"]["policy"] == "required"
        assert artifact.role["persona"]["compatibility"]


def test_shipped_schema_file_matches_module_lookup():
    """The schema file load_role_artifact reads relative to its own
    __file__ resolves to the same file this test loads directly — guards
    against schema-path drift if role_artifact.py ever moves."""
    import role_artifact as role_artifact_module

    module_schema_path = Path(role_artifact_module.__file__).parent / (
        "defaults/schema/role.v1.json"
    )
    assert module_schema_path.resolve() == _SCHEMA_PATH.resolve()
    # Both must parse as the same schema document.
    assert json.loads(module_schema_path.read_text(encoding="utf-8")) == json.loads(
        _SCHEMA_PATH.read_text(encoding="utf-8")
    )
