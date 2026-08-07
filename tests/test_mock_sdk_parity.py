"""#447: the mock SDK must accept every option kwarg Casa actually passes.

The e2e container swaps the real ``claude_agent_sdk`` for
``test-local/mock-claude-sdk/``. Unit tests do NOT — they import the host's
REAL, pinned SDK — so a new ``ClaudeAgentOptions`` kwarg added to the app
keeps the unit gate green while every turn inside the container dies on
``TypeError: ClaudeAgentOptions.__init__() got an unexpected keyword
argument``. The failure surfaces two steps later as an empty
``/data/sessions.json``, and the assertion names the wrong subsystem.

That has now happened three times: ``plugins`` (v0.5.9), ``plugins`` again
(commit 28b8748) and ``env`` (v0.154.0 — found four releases late, with the
whole e2e tier dead in between).

This test closes the loop statically: it reads every kwarg name handed to a
``ClaudeAgentOptions(...)`` constructor anywhere in the app tree and asserts
the mock dataclass declares a field for each. Static, so it needs neither
SDK importable side by side.

**What it does and does not see.** The sweep matches the literal name
``ClaudeAgentOptions``, so an aliased import or a locally rebound name would
be invisible to it, and a ``**kwargs`` splat carries no static names at all.
Rather than leave those as silent blind spots, each is detected and fails
this test loudly — a construction the sweep cannot read is reported as a hole
in the sweep, never as a pass. It still cannot see a name rebound through a
variable inside a function; that residual is stated, not claimed away.

Deliberately one-directional: the mock may declare fields Casa does not pass
(real-SDK fields kept for documentation). Only the reverse is a defect.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
APP_ROOT = REPO / "casa" / "rootfs" / "opt" / "casa"
MOCK_SDK = (REPO / "test-local" / "mock-claude-sdk" / "claude_agent_sdk"
            / "__init__.py")

# ``dataclasses.replace(opts, stderr=cb)`` needs the field just as much as the
# constructor does (sdk_logging.with_stderr_callback). Matched by the first
# argument's NAME, which is the only static signal available.
_OPTIONS_VAR_NAMES = {"options", "opts"}


def _sweep() -> tuple[dict[str, set[str]], list[str]]:
    """Return ``({kwarg: {"<relpath>:<line>", ...}}, [unreadable sites])``.

    The second element names constructions whose kwargs the sweep cannot
    read — an aliased import, a rebound module-level name, or a ``**kwargs``
    splat. Those are reported, not skipped.
    """
    found: dict[str, set[str]] = {}
    blind: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO)

        for node in ast.walk(tree):
            # An aliased import (`import ClaudeAgentOptions as CAO`) or a
            # rebinding (`Options = ClaudeAgentOptions`) makes every call
            # through the new name invisible to the name match below.
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (alias.name == "ClaudeAgentOptions"
                            and alias.asname not in (None,
                                                     "ClaudeAgentOptions")):
                        blind.append(
                            f"{rel}:{node.lineno} imports ClaudeAgentOptions "
                            f"as {alias.asname!r}"
                        )
            # Any rebinding, however spelled: `Options = ClaudeAgentOptions`,
            # `Options: type = ClaudeAgentOptions`, `Options = sdk.Claude…`.
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                rebinds = (
                    (isinstance(value, ast.Name)
                     and value.id == "ClaudeAgentOptions")
                    or (isinstance(value, ast.Attribute)
                        and value.attr == "ClaudeAgentOptions")
                )
                if rebinds:
                    blind.append(
                        f"{rel}:{node.lineno} rebinds ClaudeAgentOptions to "
                        "another name"
                    )

            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None)
            if name == "ClaudeAgentOptions":
                pass
            elif name == "replace" and node.args and isinstance(
                    node.args[0], ast.Name
            ) and node.args[0].id in _OPTIONS_VAR_NAMES:
                pass
            else:
                continue
            where = f"{rel}:{node.lineno}"
            for kw in node.keywords:
                if kw.arg is None:
                    blind.append(f"{where} passes **kwargs")
                    continue
                found.setdefault(kw.arg, set()).add(where)
    return found, blind


def _kwargs_passed_to_options() -> dict[str, set[str]]:
    return _sweep()[0]


def _mock_option_fields() -> set[str]:
    tree = ast.parse(MOCK_SDK.read_text(encoding="utf-8"), filename=str(MOCK_SDK))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ClaudeAgentOptions":
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(
        f"no ClaudeAgentOptions class found in {MOCK_SDK} — the mock moved, "
        "and this parity check has been silently inert"
    )


def test_fixtures_are_where_this_test_thinks_they_are():
    assert APP_ROOT.is_dir(), APP_ROOT
    assert MOCK_SDK.is_file(), MOCK_SDK


def test_app_passes_at_least_the_known_option_kwargs():
    """Guard the AST sweep itself — an empty sweep would pass vacuously."""
    passed = _kwargs_passed_to_options()
    for expected in ("model", "system_prompt", "cwd", "env", "plugins"):
        assert expected in passed, (
            f"{expected!r} is passed to ClaudeAgentOptions in the app tree "
            "but the AST sweep did not find it — the sweep is broken, not "
            "the mock"
        )


def test_mock_sdk_accepts_every_option_kwarg_the_app_passes():
    passed = _kwargs_passed_to_options()
    declared = _mock_option_fields()
    missing = {k: sorted(v) for k, v in passed.items() if k not in declared}
    assert not missing, (
        "the mock SDK's ClaudeAgentOptions is missing field(s) the app "
        f"passes: {missing}.\nInside the e2e container this raises TypeError "
        "on EVERY turn; the unit suite cannot see it because it imports the "
        f"host's real SDK. Add the field to {MOCK_SDK.relative_to(REPO)}."
    )


def test_no_construction_is_invisible_to_the_sweep():
    """A site the sweep cannot read must fail loudly, not pass silently."""
    _found, blind = _sweep()
    assert not blind, (
        "these ClaudeAgentOptions constructions carry kwargs this parity "
        f"sweep cannot read statically: {blind}.\nEither construct with "
        "explicit keywords under the literal name, or extend _sweep() to "
        "cover the new form — a construction it cannot see is a mock-drift "
        "hole exactly like the ones this test exists to catch."
    )


@pytest.mark.parametrize("field_name", ["env", "plugins"])
def test_the_historically_dropped_fields_are_present(field_name):
    """Named pins for the two fields whose absence killed the e2e tier."""
    assert field_name in _mock_option_fields()
