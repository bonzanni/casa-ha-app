"""Guard: residents can actually reach long-term memory on-demand.

Regression — the `recall_memory` pull tool was missing from the resident
`tools.allowed` lists (2026-07-09 diagnosis). Because `_plan_load` auto-injects
a recall ONLY on a fresh session, a resident on a RESUMED or SCHEDULED turn
(heartbeat / morning-briefing) and the VOICE channel (which never auto-recalls
and, at `friends` clearance, gets no overlay) had NO memory-read path at all —
yet their prompts instruct them to use it. See memory
`recall-memory-tool-missing-bug`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults" / "agents"
RECALL_TOOL = "mcp__casa-framework__recall_memory"


def _allowed(role: str) -> list[str]:
    data = yaml.safe_load((AGENTS / role / "runtime.yaml").read_text(encoding="utf-8"))
    return (data.get("tools") or {}).get("allowed") or []


def test_assistant_allows_recall_memory() -> None:
    """Ellen runs heartbeat + morning-briefing on resumed telegram sessions
    (no auto-inject) and her prompts direct her to `recall_memory`."""
    assert RECALL_TOOL in _allowed("assistant")


def test_butler_allows_recall_memory() -> None:
    """The voice channel routes to butler (casa_core default_agent) and voice
    never auto-recalls — the pull tool is its only long-term-memory path."""
    assert RECALL_TOOL in _allowed("butler")


def test_prompt_referenced_recall_memory_is_allowed() -> None:
    """Invariant: any agent whose prompt text names `recall_memory` must have
    the tool in its allowed list, or the instruction is unfulfillable."""
    offenders = []
    for runtime in AGENTS.rglob("runtime.yaml"):
        role_dir = runtime.parent
        prompts = list((role_dir / "prompts").glob("*.md")) if (role_dir / "prompts").is_dir() else []
        mentions = any("recall_memory" in p.read_text(encoding="utf-8") for p in prompts)
        if not mentions:
            continue
        data = yaml.safe_load(runtime.read_text(encoding="utf-8"))
        allowed = (data.get("tools") or {}).get("allowed") or []
        if RECALL_TOOL not in allowed:
            offenders.append(role_dir.name)
    assert not offenders, (
        f"agents reference recall_memory in prompts but do not allow it: {offenders}"
    )
