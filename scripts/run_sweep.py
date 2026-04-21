#!/usr/bin/env python3
"""One-off sweep runner — sweep threshold on the default probe suite
and print the recommendation. Used for Phase 3.2.1 weight optimization."""

from __future__ import annotations

import os
import sys

# Prepend the addon's Python root so casa_eval + scope_registry are importable.
_here = os.path.dirname(os.path.abspath(__file__))
_addon = os.path.join(_here, "..", "casa-agent", "rootfs", "opt", "casa")
sys.path.insert(0, _addon)

from scope_registry import load_scope_library
from casa_eval.base import Suite
from casa_eval.scope_routing import ScopeRoutingTester


def main() -> int:
    lib = load_scope_library(os.path.join(_addon, "defaults", "policies", "scopes.yaml"))
    suite_path = os.path.join(
        _here, "..", "tests", "fixtures", "eval", "scope_routing", "default.yaml",
    )
    tester = ScopeRoutingTester(scope_library=lib)
    suite = Suite.from_yaml(suite_path)
    values = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45]
    reports = tester.sweep(suite, axis="threshold", values=values)

    print("--- scope-routing sweep ---")
    print("threshold | accuracy | fallback | top2  | mean_winner")
    for v in values:
        r = reports[v]
        print(
            f"  {v:.2f}    |  {r.accuracy:.3f}  |  {r.metrics['fallback_rate']:.3f}  "
            f"| {r.metrics['top2_accuracy']:.3f} |    {r.metrics['mean_winner_score']:.3f}"
        )

    rec = tester.recommend_from_sweep(reports)
    print(f"\nRecommendation: {rec.recommended} (current={rec.current})")
    print(f"Justification: {rec.justification}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
