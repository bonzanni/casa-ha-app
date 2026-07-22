"""Personality Phase A, Task 14: the lean, privacy-safe explanation store.

Holds ephemeral per-turn "why did the agent answer this way" records —
role identity, binding provenance, the compiled-prompt digest that was
actually sent, memory attributions, tool calls, and denials — so an
operator can inspect a single correlation id's provenance without
re-deriving it from logs.

Privacy is enforced at the STORE boundary, not by the caller:

* :meth:`ExplanationStore.record` REJECTS (raises ``ValueError``) any
  record whose JSON encoding contains a reserved ``casa-source-``
  provenance tag (spec: reserved tags are Hindsight-internal and must
  never leak into an operator-facing surface).
* :meth:`ExplanationStore.get` strips ``system_prompt``/``memory_text``
  AND the ``memory_tiers`` sensitivity-tier metadata (GH #202) by default;
  only ``show_sensitive=True`` (gated by the admin route's ``confirmed=true``
  requirement, and by ``casactl``'s interactive ``SHOW`` confirmation)
  returns them. ``memory_attributions`` stay visible by default — they are
  already clearance/surface-gated identity labels (never tier tokens, never
  reserved ``casa-source-`` tags).

Storage is atomic (temp file + chmod 0600 + ``os.replace``), TTL-pruned
(24h), and capped at 1000 records — this is a debugging aid, not durable
storage; restart or TTL expiry losing a record is fine.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

EXPLANATION_TTL_SECONDS = 86400
EXPLANATION_MAX_RECORDS = 1000
_CID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SENSITIVE = {"system_prompt", "memory_text"}
# GH #202: the memory sensitivity-tier tokens are metadata that reveals which
# clearance tiers a turn's recall touched — gated behind the SAME explicit
# show_sensitive confirmation as the prompt/memory prose (attribution labels,
# which are already clearance-gated, stay visible).
_SENSITIVE_TIER_META = {"memory_tiers"}


@dataclass(frozen=True, slots=True)
class ExplanationRecord:
    correlation_id: str
    role_id: str
    kind: str
    resolved_model: str
    persona_ref: str | None
    role_checksum: str
    binding_digest: str | None
    dependency_digests: tuple[str, ...]
    effective_config_digest: str | None
    lifecycle_state: str | None
    projection: str
    static_prompt_digest: str
    static_prompt_estimated_tokens: int
    memory_tiers: tuple[str, ...]
    memory_attributions: tuple[str, ...]
    tool_calls: tuple[str, ...]
    denials: tuple[str, ...]
    system_prompt: str | None = None
    memory_text: str | None = None


class ExplanationStore:
    """One JSON file per correlation id under ``root``.

    ``now`` is injectable (defaults to :func:`time.time`) so tests can
    control TTL/prune behavior deterministically without patching the
    module-global ``time.time`` or any ``asyncio.sleep`` (memory-cage
    rule — see CLAUDE.md).
    """

    def __init__(self, root: Path = Path("/data/explanations"), *, now: Callable[[], float] = time.time) -> None:
        self._root = root
        self._now = now

    def _path(self, correlation_id: str) -> Path:
        if not isinstance(correlation_id, str) or not _CID.fullmatch(correlation_id):
            raise ValueError("invalid correlation id")
        return self._root / f"{correlation_id}.json"

    def record(self, record: ExplanationRecord) -> None:
        payload = asdict(record)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if "casa-source-" in encoded:
            raise ValueError("reserved provenance tags cannot enter explanations")
        path = self._path(record.correlation_id)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        # TTL/prune read the file's mtime as the record's age. Force it to
        # the injectable `now` (not whatever the real OS clock says) so a
        # test-supplied `now` callable drives TTL/prune deterministically —
        # os.utime accepts any epoch float, real or test-fictional.
        now = self._now()
        os.utime(path, (now, now))
        self.prune()

    def get(self, correlation_id: str, *, show_sensitive: bool = False) -> dict[str, object]:
        path = self._path(correlation_id)
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise KeyError(correlation_id) from exc
        if self._now() - mtime > EXPLANATION_TTL_SECONDS:
            raise KeyError(correlation_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not show_sensitive:
            for key in (*_SENSITIVE, *_SENSITIVE_TIER_META):
                value.pop(key, None)
        return value

    def prune(self) -> None:
        if not self._root.is_dir():
            return
        files = sorted(self._root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        cutoff = self._now() - EXPLANATION_TTL_SECONDS
        for index, path in enumerate(files):
            if index >= EXPLANATION_MAX_RECORDS or path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
