from pathlib import Path

import pytest

from role_slot import FIXED_RESIDENT_SLOTS, RoleValidationError, validate_role_shape


def test_no_fourth_resident_slot_validates() -> None:
    with pytest.raises(RoleValidationError, match="fixed resident"):
        validate_role_shape({
            "api_version": "casa.role/v1", "id": "resident:steward", "kind": "resident",
            "slot": "steward", "channels": ["telegram"], "session": {"strategy": "persistent"},
            "persona": {"policy": "required", "compatibility": ["casa/x@>=0.1.0 <1.0.0"]},
        })


def test_fixed_resident_directories_are_exactly_the_three_slots() -> None:
    roles_dir = Path("casa-agent/rootfs/opt/casa/defaults/roles/resident")
    assert {p.name for p in roles_dir.iterdir() if p.is_dir()} == set(FIXED_RESIDENT_SLOTS)


def test_no_configurator_tool_can_create_rename_or_remove_a_resident() -> None:
    """Domain-boundary proof (spec §8): grep the configurator's tool surface for any
    verb that could mutate the resident set, and assert none exists. A resident's
    PERSONA can change (a binding operation, Task 8); the SLOT itself cannot."""
    import re

    tools_src = Path("casa-agent/rootfs/opt/casa/tools.py").read_text(encoding="utf-8")
    forbidden = re.compile(r"@tool\(\s*\"(resident_(add|remove|rename|create|delete))\"")
    assert not forbidden.search(tools_src)
