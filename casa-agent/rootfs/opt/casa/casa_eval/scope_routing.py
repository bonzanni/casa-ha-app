"""ScopeRoutingTester — evaluates scope-routing accuracy against a
labelled probe suite, with a fixed e5-large backend and a tunable
threshold.

The tester instantiates its own ScopeRegistry so it never shares
LRU-cache state with the live registry in casa_core.py. Backend model
selection is intentionally frozen: optimization_axes = ["threshold"]
only (see spec §2).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from casa_eval import _register
from casa_eval.base import (
    Case, Failure, Recommendation, Report, Suite, Tester,
)
from scope_registry import ScopeLibrary, ScopeRegistry


DEFAULT_THRESHOLD = 0.35
DEFAULT_SCOPE_FALLBACK = "personal"


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


@_register
class ScopeRoutingTester(Tester):
    id = "scope_routing"
    optimization_axes = ["threshold"]
    optimization_bounds = {"threshold": (0.20, 0.50)}

    def __init__(self, scope_library: ScopeLibrary) -> None:
        self._library = scope_library

    def load_suite(self, path: str) -> Suite:
        return Suite.from_yaml(path)

    def run(
        self, suite: Suite, *, threshold: float = DEFAULT_THRESHOLD,
    ) -> Report:
        registry = ScopeRegistry(self._library, threshold=threshold)
        asyncio.run(registry.prepare())

        all_scopes = self._library.names()
        failures: list[Failure] = []
        passed = 0
        winner_scores: list[float] = []
        margins: list[float] = []
        top2_hits = 0
        fallback_count = 0
        latencies_ms: list[float] = []

        for idx, case in enumerate(suite.cases):
            default_scope = case.metadata.get(
                "default_scope", DEFAULT_SCOPE_FALLBACK,
            )

            t0 = time.perf_counter()
            scores = registry.score(case.input, all_scopes)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

            actual = registry.argmax_scope(scores, default_scope=default_scope)

            # top-1 winner score (for metrics — separate from fallback accounting)
            if scores:
                ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
                top_winner, top_score = ordered[0]
                second_score = ordered[1][1] if len(ordered) > 1 else 0.0
                winner_scores.append(top_score)
                margins.append(top_score - second_score)
                if top_score < threshold:
                    fallback_count += 1
                expected_in_top2 = case.expected in {
                    s for s, _ in ordered[:2]
                }
                if expected_in_top2:
                    top2_hits += 1

            if actual == case.expected:
                passed += 1
            else:
                failures.append(Failure(
                    case_index=idx,
                    input=case.input,
                    expected=case.expected,
                    actual=actual,
                    extra={
                        "scores": {k: float(v) for k, v in scores.items()},
                        "margin": float(
                            margins[-1] if margins else 0.0,
                        ),
                    },
                ))

        total = len(suite.cases)
        accuracy = passed / total if total else 0.0

        metrics: dict[str, float] = {
            "accuracy": accuracy,
            "top2_accuracy": (top2_hits / total) if total else 0.0,
            "fallback_rate": (fallback_count / total) if total else 0.0,
            "mean_winner_score": mean(winner_scores) if winner_scores else 0.0,
            "mean_margin": mean(margins) if margins else 0.0,
            "p50_latency_ms": _percentile(latencies_ms, 50),
            "p95_latency_ms": _percentile(latencies_ms, 95),
        }

        return Report(
            tester_id=self.id,
            suite_id=suite.suite_id,
            config={"threshold": threshold},
            total=total,
            passed=passed,
            failed=total - passed,
            accuracy=accuracy,
            metrics=metrics,
            failures=failures,
            timestamp=datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ).replace("+00:00", "Z"),
        )

    def recommend_from_sweep(
        self, reports: dict[Any, Report],
    ) -> Recommendation:
        """Policy:
          1. Filter to reports where fallback_rate <= 0.20.
          2. Also filter out values outside optimization_bounds["threshold"].
          3. Among survivors, pick max accuracy; tiebreak on lower fallback_rate.
          4. If no survivor, recommend None with a 'too noisy' justification.
        """
        lower, upper = self.optimization_bounds["threshold"]
        # "current" = what the live system runs today. We don't try to
        # infer it from the sweep (reports might not even include it);
        # hardcode to the shipped default so Builder sees a stable value
        # until it passes an explicit baseline in 3.5.
        current = DEFAULT_THRESHOLD

        survivors = [
            (v, r) for v, r in reports.items()
            if r.metrics.get("fallback_rate", 1.0) <= 0.20
            and lower <= v <= upper
        ]

        if not survivors:
            return Recommendation(
                tester_id=self.id,
                axis="threshold",
                current=current,
                recommended=None,
                justification=(
                    "No threshold in the swept range satisfied "
                    "fallback_rate <= 0.20 AND lies within "
                    f"optimization_bounds {self.optimization_bounds['threshold']}. "
                    "The data is too noisy to recommend a change; "
                    "grow the probe suite or inspect the sweep table."
                ),
                evidence=reports,
            )

        survivors.sort(
            key=lambda kv: (
                -kv[1].accuracy,
                kv[1].metrics.get("fallback_rate", 1.0),
            ),
        )
        best_v, best_r = survivors[0]
        return Recommendation(
            tester_id=self.id,
            axis="threshold",
            current=current,
            recommended=best_v,
            justification=(
                f"threshold={best_v} gives accuracy={best_r.accuracy:.3f} "
                f"with fallback_rate={best_r.metrics['fallback_rate']:.3f}; "
                f"picked over {len(survivors) - 1} other survivor(s)."
            ),
            evidence=reports,
        )
