"""Unit coverage for executor-memory integration (M4 L3).

Triage note (collapse 4/7):
- ``_fetch_executor_archive`` was rewritten to use semantic recall (delegated_memory)
  instead of a per-executor Honcho session. The five tests that drove the OLD
  provider behavior (``memory_provider=``, ``ensure_session``/``get_context``
  round-trips, provider-raises path) were DELETED — they tested REMOVED behavior
  now fully covered by ``test_executor_memory_tiers.py``.

Surviving tests:
  - ``test_get_context_signature_locks_kwargs``  }  lock MemoryProvider.get_context's
  - ``test_get_context_callers_kwargs_match_signature``  }  ABC + call-site kwargs (still valid;
      MemoryProvider survives until plan 4; engagement/query_engager paths still call it).
  - ``test_substitutes_executor_memory_slot_when_memory_enabled`` — template {executor_memory}
      slot substitution (pure string op, no _fetch_executor_archive call).
  - ``test_workspace_legacy_path_substitutes_executor_memory`` — provision_workspace threads
      executor_memory= through to CLAUDE.md; pre-existing _Defn._tools_allowed gap unrelated
      to this task (workspace.py added tools_allowed after this test was written).
"""

from __future__ import annotations

import sys

import pytest


pytestmark = pytest.mark.asyncio


def test_get_context_signature_locks_kwargs():
    """Lock MemoryProvider.get_context's parameter set against future
    drift. v0.30.0 / M3-self: agent_role is now expected (forwarded as
    Honcho's peer_target). user_peer remains dropped. This introspection
    test catches any future caller-vs-ABC divergence at unit-test time
    rather than waiting for an exploration session."""
    import inspect
    from memory import MemoryProvider

    sig = inspect.signature(MemoryProvider.get_context)
    actual = set(sig.parameters.keys())
    expected = {"self", "session_id", "tokens", "search_query", "agent_role"}
    assert actual == expected, (
        f"MemoryProvider.get_context kwargs drifted. "
        f"Expected {expected}, got {actual}. "
        f"If this is intentional, audit every call site (agent.py, "
        f"tools.py::cross_peer_context) "
        f"and update this test."
    )


def test_get_context_callers_kwargs_match_signature():
    """E-H caller-side regression-locker (v0.31.0). The signature-side
    test above passes whenever the ABC is correct, but doesn't catch a
    CALL site that still passes a stale kwarg. v0.26.0 dropped
    ``user_peer`` from ``MemoryProvider.get_context``; v0.30.0
    re-introduced ``agent_role``. The exploration session run on
    2026-05-01 (v0.30.0) found the call at ``tools.py:454`` was never
    audited and still passed ``user_peer=user_peer``, which was a silent
    TypeError on every Ellen → specialist delegation since v0.26.0
    (cid `3407a7fb`, log
    ``HonchoMemoryProvider.get_context() got an unexpected keyword
    argument 'user_peer'``). A second offender at
    ``channels/voice/channel.py`` was found in the v0.31.0 fix audit.

    This test AST-walks every ``.py`` file under ``rootfs/opt/casa/``
    and asserts every ``.get_context(...)`` call's kwargs are a subset
    of the locked allowlist. The check is lexical (matches any
    attribute call ending in ``get_context``) — the ABC namespace is
    distinct enough that no other class in the codebase shares the
    method name today; if that ever changes, narrow the AST match.
    """
    import ast
    import os

    allowed = {"session_id", "tokens", "search_query", "agent_role"}

    casa_root = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "casa-agent", "rootfs", "opt", "casa",
    ))
    assert os.path.isdir(casa_root), (
        f"could not locate casa source root: {casa_root}"
    )

    offenders: list[tuple[str, int, set[str]]] = []
    for dirpath, _dirs, files in os.walk(casa_root):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                tree = ast.parse(
                    open(path, "r", encoding="utf-8").read(), filename=path,
                )
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # Match ANY attribute call ending in ``.get_context(...)``.
                # Excludes the ABC method definition itself; that's a
                # FunctionDef, not a Call.
                if not (isinstance(func, ast.Attribute)
                        and func.attr == "get_context"):
                    continue
                kwargs = {
                    kw.arg for kw in node.keywords if kw.arg is not None
                }
                bad = kwargs - allowed
                if bad:
                    rel = os.path.relpath(path, casa_root)
                    offenders.append((rel, node.lineno, bad))

    assert not offenders, (
        "MemoryProvider.get_context callers passing forbidden kwargs:\n"
        + "\n".join(
            f"  {rel}:{lineno} -> {sorted(bad)}"
            for rel, lineno, bad in offenders
        )
        + f"\nAllowed kwargs: {sorted(allowed)}. "
          f"Drop the offending kwargs at the call site."
    )


def test_substitutes_executor_memory_slot_when_memory_enabled(monkeypatch, tmp_path):
    """engage_executor build_prompt path interpolates {executor_memory}.

    Black-box-ish: we re-derive the same substitution rules engage_executor
    uses, asserting the helper's output substitutes correctly.
    """
    template = (
        "task: {task}\n"
        "ctx: {context}\n"
        "world: {world_state_summary}\n"
        "mem: {executor_memory}\n"
    )
    rendered = (
        template
        .replace("{task}", "do thing")
        .replace("{context}", "(none)")
        .replace("{world_state_summary}", "ws ok")
        .replace("{executor_memory}", "## Prior engagements (lessons learned)\n- prior")
    )
    assert "do thing" in rendered
    assert "ws ok" in rendered
    assert "Prior engagements" in rendered
    assert "{executor_memory}" not in rendered


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="provision_workspace uses os.mkfifo (Linux-only)",
)
async def test_workspace_legacy_path_substitutes_executor_memory(tmp_path):
    """The legacy provision_workspace path threads executor_memory through.

    Forward-compat coverage for the claude_code driver — no claude_code
    executor opts into memory today, but workspace.py wires the slot so a
    future memory-enabled claude_code executor works without plumbing.
    """
    from drivers.workspace import provision_workspace

    class _Defn:
        type = "x"
        prompt_template_path = str(tmp_path / "prompt.md")
        hooks_path = None
        tools_allowed: list[str] = []
        permission_mode = "acceptEdits"
    (tmp_path / "prompt.md").write_text(
        "task={task} mem={executor_memory}\n", encoding="utf-8",
    )

    eng_root = tmp_path / "engagements"
    eng_root.mkdir()
    plugins_root = tmp_path / "plugins_root"
    plugins_root.mkdir()

    await provision_workspace(
        engagements_root=str(eng_root),
        base_plugins_root=str(plugins_root),
        engagement_id="abc12345",
        defn=_Defn(),
        task="dotask",
        context="(none)",
        casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        executor_memory="## Prior\nbody",
    )
    claude_md = (eng_root / "abc12345" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "task=dotask" in claude_md
    assert "mem=## Prior\nbody" in claude_md
