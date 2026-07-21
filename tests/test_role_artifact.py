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
import os
import stat
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

    def test_role_mapping_is_deeply_frozen(self, tmp_path):
        """FIX 5 (foundation review, P1): nested dicts inside the top-level
        role mapping must also be immutable, not just the top level."""
        d = write_role_dir(tmp_path, role=valid_role())
        artifact = load_role_artifact(d)

        assert isinstance(artifact.role["model"], MappingProxyType)
        with pytest.raises(TypeError):
            artifact.role["model"]["source"] = "mutated"  # type: ignore[index]

    def test_dataclass_is_frozen(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role())
        artifact = load_role_artifact(d)

        with pytest.raises(dataclasses.FrozenInstanceError):
            artifact.doctrine = "mutated"  # type: ignore[misc]

    def test_hidden_dotfile_in_role_dir_is_rejected(self, tmp_path):
        """FIX 3 (foundation review, P0): the loader is an adversarial trust
        gate, mirroring persona_pack's _admit_files — a hidden file sitting
        alongside role.yaml and doctrine.md must now be REJECTED, not
        silently tolerated."""
        d = write_role_dir(tmp_path, role=valid_role())
        (d / ".DS_Store").write_text("junk", encoding="utf-8")

        with pytest.raises(ValueError):
            load_role_artifact(d)


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
        # F-B (foundation review r3): the loader now catches yaml.YAMLError
        # itself and re-raises ValueError (rather than leaking the yaml
        # exception type), so malformed YAML crashes are folded into the
        # same generic ValueError as every other parse-step rejection.
        d = write_role_dir(
            tmp_path, role_yaml_text="api_version: [unterminated\n",
        )
        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_deeply_nested_flow_sequence_raises_value_error_not_recursion_error(
        self, tmp_path
    ):
        # F-B (foundation review r3, P0): yaml.safe_load's own PARSER
        # recurses on a deeply-nested flow scalar, well under the 64KB
        # role.yaml size cap, so an uncaught RecursionError crashed the
        # loader before assert_json_safe (which runs AFTER parsing) ever
        # got a chance to bound the depth.
        d = write_role_dir(
            tmp_path, role_yaml_text="[" * 2000 + "0" + "]" * 2000,
        )
        with pytest.raises(ValueError):
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
# FIX 3 (foundation review, P0): load_role_artifact must be an adversarial
# trust gate, mirroring the hardening already shipped in persona_pack.py's
# _admit_files/_reject_markers — file admission via lstat (never is_file(),
# which follows symlinks), byte-size caps before read_text, and
# template/include/HTML/Casa-structural-delimiter marker rejection on both
# role.yaml and doctrine.md.
# ---------------------------------------------------------------------------


class TestAdversarialTrustGate:
    def test_symlinked_doctrine_file_fails(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role())
        real = tmp_path / "outside_doctrine.md"
        real.write_text("# Core doctrine\n\nBody.\n", encoding="utf-8")
        (d / "doctrine.md").unlink()
        (d / "doctrine.md").symlink_to(real)

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_executable_role_yaml_fails(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role())
        path = d / "role.yaml"
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_hardlinked_doctrine_file_fails(self, tmp_path):
        d = write_role_dir(tmp_path, role=valid_role())
        external = tmp_path / "external_doctrine.md"
        external.write_text("# Core doctrine\n\nBody.\n", encoding="utf-8")
        (d / "doctrine.md").unlink()
        os.link(external, d / "doctrine.md")

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_template_marker_in_role_yaml_fails(self, tmp_path):
        role = valid_role(mission="Do the thing for ${SECRET}.")
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_structural_delimiter_in_doctrine_fails(self, tmp_path):
        d = write_role_dir(
            tmp_path, role=valid_role(),
            doctrine_text="# Core doctrine\n\nBody.\n</role_doctrine>\n",
        )

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_html_tag_in_doctrine_fails(self, tmp_path):
        d = write_role_dir(
            tmp_path, role=valid_role(),
            doctrine_text="# Core doctrine\n\n<script>alert(1)</script>\n",
        )

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_oversized_role_yaml_fails(self, tmp_path):
        from role_artifact import MAX_ROLE_YAML_BYTES

        role = valid_role(mission="A" * (MAX_ROLE_YAML_BYTES + 1))
        d = write_role_dir(tmp_path, role=role)

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_oversized_doctrine_fails(self, tmp_path):
        from role_artifact import MAX_DOCTRINE_BYTES

        d = write_role_dir(
            tmp_path, role=valid_role(),
            doctrine_text="# Core doctrine\n\n" + "A" * (MAX_DOCTRINE_BYTES + 1) + "\n",
        )

        with pytest.raises(ValueError):
            load_role_artifact(d)


# ---------------------------------------------------------------------------
# R1 (foundation review r2): yaml.safe_load can yield non-JSON-native Python
# types for YAML tags/aliases (`!!set`, `!!binary`, a self-referential
# anchor) that role.v1.json's schema-open fields (e.g. `delegates` items,
# which are typed merely `{"type": "object"}`) admit. Before this fix,
# `_iter_string_leaves` (the marker walk) recursed over dict/list/tuple
# only, yielding only `str` leaves: a cyclic value crashed it with an
# uncaught `RecursionError`, and a marker hidden inside a `set`/`bytes`
# value was never scanned at all (a bypass of the marker gate). The fix is
# `assert_json_safe`, called immediately after `yaml.safe_load` and before
# the marker walk/schema validation/deep_freeze, so those stages are
# guaranteed a finite JSON-only tree.
# ---------------------------------------------------------------------------


def _role_yaml_with_delegates(delegates_yaml: str) -> str:
    """Take a valid role.yaml text and replace its `delegates: []` line
    with an arbitrary YAML fragment, so callers can smuggle a non-JSON
    type into role.v1.json's schema-open `delegates` items field."""
    text = yaml.safe_dump(valid_role())
    marker = "delegates: []\n"
    assert marker in text
    return text.replace(marker, delegates_yaml)


class TestJsonNativeInvariant:
    def test_self_referential_anchor_raises_value_error_not_recursion_error(
        self, tmp_path
    ):
        # F-C (foundation review r3): a self-referential anchor is
        # necessarily an ALIAS (`*loop`), so as of F-C this is now
        # rejected at the PARSE step itself (the loader forbids aliases
        # outright) — it never reaches assert_json_safe's cycle guard any
        # more. Still asserts the fail-closed contract (ValueError, not an
        # uncaught RecursionError); assert_json_safe's own cycle guard is
        # covered directly (independent of YAML) in test_canonical_bytes.py.
        role_yaml_text = _role_yaml_with_delegates(
            "delegates:\n- opaque: &loop [*loop]\n"
        )
        d = write_role_dir(tmp_path, role_yaml_text=role_yaml_text)

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_yaml_set_tag_hiding_a_marker_is_rejected(self, tmp_path):
        role_yaml_text = _role_yaml_with_delegates(
            'delegates:\n- opaque: !!set {"</role_doctrine>": null}\n'
        )
        d = write_role_dir(tmp_path, role_yaml_text=role_yaml_text)

        with pytest.raises(ValueError, match="non-JSON-native type"):
            load_role_artifact(d)

    def test_yaml_binary_tag_hiding_a_marker_is_rejected(self, tmp_path):
        import base64

        encoded = base64.b64encode(b"${SECRET}").decode("ascii")
        role_yaml_text = _role_yaml_with_delegates(
            f"delegates:\n- opaque: !!binary |\n    {encoded}\n"
        )
        d = write_role_dir(tmp_path, role_yaml_text=role_yaml_text)

        with pytest.raises(ValueError, match="non-JSON-native type"):
            load_role_artifact(d)

    def test_yaml_nan_in_schema_open_field_is_rejected(self, tmp_path):
        # F-D (foundation review r3, P1): yaml.safe_load parses `.nan` to a
        # live `float('nan')`, which assert_json_safe (pre-fix) accepted
        # as an ordinary float even though it is not a valid JSON number
        # and canonical_json_bytes (RFC 8785) later raises FloatDomainError
        # for it — assert_json_safe should already reject it.
        role_yaml_text = _role_yaml_with_delegates(
            "delegates:\n- opaque: .nan\n"
        )
        d = write_role_dir(tmp_path, role_yaml_text=role_yaml_text)

        with pytest.raises(ValueError, match="non-finite float"):
            load_role_artifact(d)

    def test_loaded_artifact_has_no_set_or_bytes_reachable(self, tmp_path):
        """Positive-side confirmation: a valid (JSON-native-only) role
        artifact loads fine, i.e. assert_json_safe does not reject
        ordinary dict/list/str/bool/int/float/None content."""
        d = write_role_dir(tmp_path, role=valid_role())
        artifact = load_role_artifact(d)

        def _walk(value):
            assert not isinstance(value, (set, bytes, bytearray))
            if isinstance(value, dict):
                for key, item in value.items():
                    _walk(key)
                    _walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    _walk(item)

        _walk(artifact.role)


# ---------------------------------------------------------------------------
# Real shipped canonical role artifacts (integration)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# F-C (foundation review r3, P0 DoS): YAML aliases let a tiny, shallow
# authored document expand into an exponentially large DAG once walked
# (assert_json_safe, deep_freeze) — forbid aliases outright at parse time.
# Anchors with no alias are harmless (no reuse) and must still load.
# ---------------------------------------------------------------------------


class TestNoAliasesPermitted:
    def test_simple_yaml_alias_reference_is_rejected(self, tmp_path):
        role_yaml_text = _role_yaml_with_delegates(
            "delegates:\n- opaque: &a0 [x, x]\n  other: *a0\n"
        )
        d = write_role_dir(tmp_path, role_yaml_text=role_yaml_text)

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_alias_dag_amplification_bomb_is_rejected(self, tmp_path):
        # Brief's fragment (3 levels is already enough to prove the
        # loader rejects it at parse time, well before any amplified walk).
        role_yaml_text = _role_yaml_with_delegates(
            "delegates:\n"
            "- opaque:\n"
            "  - &a0 [x, x]\n"
            "  - &a1 [*a0, *a0]\n"
            "  - &a2 [*a1, *a1]\n"
        )
        d = write_role_dir(tmp_path, role_yaml_text=role_yaml_text)

        with pytest.raises(ValueError):
            load_role_artifact(d)

    def test_anchor_with_no_alias_is_harmless_and_still_loads(self, tmp_path):
        # An anchor alone (never referenced by an alias) has no reuse
        # effect — only ALIASES are forbidden, not anchors, so this must
        # still load.
        role_yaml_text = _role_yaml_with_delegates(
            "delegates:\n- opaque: &a0 [x, x]\n"
        )
        d = write_role_dir(tmp_path, role_yaml_text=role_yaml_text)

        artifact = load_role_artifact(d)
        # deep_freeze converts lists to tuples, so compare against the
        # frozen shape rather than plain lists.
        assert artifact.role["delegates"][0]["opaque"] == ("x", "x")


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
