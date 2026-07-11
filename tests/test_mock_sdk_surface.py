"""Mock-SDK drift guard.

The e2e tiers (test-local/Dockerfile.test) force-install
``test-local/mock-claude-sdk`` over the real claude-agent-sdk. Every name
the app imports from ``claude_agent_sdk`` at module scope, and every
keyword ``agent._build_options`` passes to ``ClaudeAgentOptions``, must
exist in the mock — otherwise the container crashes at boot in CI while
the unit gate (which runs against the REAL SDK) stays green. That exact
failure shipped twice: plugins= (v0.5.9 era, see the mock's inline
comment) and StreamEvent/include_partial_messages (v0.67.0, QA run
29160219650). This test moves the failure into the local gate.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent
_APP = _REPO / "casa-agent" / "rootfs" / "opt" / "casa"
_MOCK = _REPO / "test-local" / "mock-claude-sdk" / "claude_agent_sdk" / "__init__.py"


def _load_mock():
    spec = importlib.util.spec_from_file_location("_mock_claude_agent_sdk", _MOCK)
    mod = importlib.util.module_from_spec(spec)
    # Register under the THROWAWAY name only (dataclass creation resolves
    # cls.__module__ via sys.modules on CPython 3.12); the real
    # sys.modules["claude_agent_sdk"] used by the rest of the suite stays
    # untouched.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _module_level_sdk_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:  # module scope only — lazy imports are exempt
        if isinstance(node, ast.ImportFrom) and node.module == "claude_agent_sdk":
            names.update(alias.name for alias in node.names)
    return names


def _build_options_kwargs() -> set[str]:
    tree = ast.parse((_APP / "agent.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "ClaudeAgentOptions":
                return {kw.arg for kw in node.keywords if kw.arg is not None}
    raise AssertionError("no ClaudeAgentOptions(...) call found in agent.py")


def test_mock_exports_every_name_the_app_imports():
    mock = _load_mock()
    missing: dict[str, set[str]] = {}
    for py in sorted(_APP.rglob("*.py")):
        wanted = _module_level_sdk_imports(py)
        gaps = {n for n in wanted if not hasattr(mock, n)}
        if gaps:
            missing[str(py.relative_to(_REPO))] = gaps
    assert not missing, (
        f"mock SDK is missing names the app imports at module scope: {missing} "
        "— add them to test-local/mock-claude-sdk/claude_agent_sdk/__init__.py "
        "or the e2e container will crash at boot (mock-SDK drift)"
    )


def test_mock_options_accept_every_kwarg_build_options_passes():
    mock = _load_mock()
    fields = {f.name for f in dataclasses.fields(mock.ClaudeAgentOptions)}
    passed = _build_options_kwargs()
    gaps = passed - fields
    assert not gaps, (
        f"mock ClaudeAgentOptions lacks fields agent._build_options passes: "
        f"{gaps} — every new kwarg needs a mock field (default that ignores it)"
    )


def test_mock_exports_streamevent_with_event_payload():
    mock = _load_mock()
    ev = mock.StreamEvent(event={"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": "x"}})
    assert ev.event["delta"]["text"] == "x"
    assert "StreamEvent" in mock.__all__
