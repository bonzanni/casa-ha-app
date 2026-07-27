"""casa_eval framework seam — ABC + dataclasses.

Tester is the public surface that pytest today and the future Builder MCP
tool will both call. The dataclasses are JSON-round-trippable so Builder
can cross process boundaries; Report.report_schema is the contract
version string Builder gates on.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

import yaml


@dataclass
class Case:
    input: Any
    expected: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Suite:
    suite_id: str
    description: str
    cases: list[Case]

    @classmethod
    def from_yaml(cls, path: str) -> "Suite":
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        cases = [
            Case(
                input=c["input"],
                expected=c["expected"],
                metadata=c.get("metadata") or {},
            )
            for c in data.get("cases", [])
        ]
        return cls(
            suite_id=data["suite_id"],
            description=data.get("description", ""),
            cases=cases,
        )


@dataclass
class Failure:
    case_index: int
    input: Any
    expected: Any
    actual: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    tester_id: str
    suite_id: str
    config: dict[str, Any]
    total: int
    passed: int
    failed: int
    accuracy: float
    metrics: dict[str, float]
    failures: list[Failure]
    timestamp: str
    report_schema: str = "1"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "Report":
        data = json.loads(s)
        failures = [Failure(**f) for f in data.pop("failures", [])]
        return cls(failures=failures, **data)


@dataclass
class Recommendation:
    tester_id: str
    axis: str
    current: Any
    recommended: Any
    justification: str
    evidence: dict[Any, Report]

    def to_json(self) -> str:
        return json.dumps({
            "tester_id": self.tester_id,
            "axis": self.axis,
            "current": self.current,
            "recommended": self.recommended,
            "justification": self.justification,
            # JSON object keys must be strings; stringify numeric axis values.
            "evidence": {
                str(k): (asdict(v) if isinstance(v, Report) else v)
                for k, v in self.evidence.items()
            },
        })


class Tester(ABC):
    id: ClassVar[str]
    optimization_axes: ClassVar[list[str]]
    optimization_bounds: ClassVar[dict[str, tuple[float, float]]]

    @abstractmethod
    def load_suite(self, path: str) -> Suite: ...

    @abstractmethod
    def run(self, suite: Suite, **opts: Any) -> Report: ...

    def sweep(
        self, suite: Suite, axis: str, values: list[Any],
    ) -> dict[Any, Report]:
        """Default impl: run once per value. Override only if the subclass
        needs cross-run state (e.g., a single warm model)."""
        if axis not in self.optimization_axes:
            raise ValueError(
                f"{self.id}: unknown axis {axis!r}; "
                f"supported: {self.optimization_axes}"
            )
        return {v: self.run(suite, **{axis: v}) for v in values}

    @abstractmethod
    def recommend_from_sweep(
        self, reports: dict[Any, Report],
    ) -> Recommendation: ...
