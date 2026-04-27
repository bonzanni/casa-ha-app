"""Scope-routing tester tests — fast (mocked) + full + sweep.

Fast mode uses a deterministic _FakeEmbedder (same pattern as
test_scope_registry.py) so it runs in CI. Full mode hits the real
e5-large via CASA_REAL_EMBED=1. Sweep mode is informational only
(no assertions on the accuracy curve), gated by CASA_EVAL_SWEEP=1.

ACCURACY_BASELINE is bumped by code edit only — never by fixture edit.
"""

from __future__ import annotations

import os
import textwrap

import pytest


ACCURACY_BASELINE = 0.85  # minimum full-mode accuracy on default.yaml.
# Raised from 0.80 in v0.8.5 after replacing the prose scope descriptions
# with keyword corpora targeting the 7 cross-cutting probes that lost
# at margins <0.02 under the v0.8.4 prose corpus. Measured 0.943 on the
# 35-case seed fixture against e5-large after the corpora swap; 0.85 is
# kept as the gate for headroom as the fixture grows. See
# defaults/policies/scopes.yaml top-of-file authoring contract for the
# format Builder will follow when editing the per-instance overlay.
# If a future fixture grows in size, the baseline may need adjustment;
# track in a follow-up rather than weakening this gate inline.
FALLBACK_CAP = 0.20       # maximum full-mode fallback_rate


SCOPES_YAML_TEXT = """\
schema_version: 2
scopes:
  personal:
    minimum_trust: authenticated
    kind: topical
    description: |
      personal private life correspondence non-work plans.
  business:
    minimum_trust: authenticated
    kind: topical
    description: |
      business professional work career meetings deadlines.
  finance:
    minimum_trust: authenticated
    kind: topical
    description: |
      finance invoices bills payments banking taxes VAT.
  house:
    minimum_trust: household-shared
    kind: topical
    description: |
      house appliances plumbing heating lights contractors sensors.
"""


class _FakeEmbedder:
    """First-character slot embedder (mirrors test_scope_registry.py)."""

    def __init__(self, model_name: str = "fake", **_: object) -> None:
        self.model_name = model_name

    def embed(self, texts):
        import numpy as np
        slots = {"p": 0, "b": 1, "f": 2, "h": 3}
        for t in texts:
            v = np.zeros(4, dtype=float)
            if t:
                key = t.strip().lower()[:1]
                v[slots.get(key, 0)] = 1.0
            yield v


def _suite_dict_inline():
    """In-memory suite for fast mode — 'p'/'b'/'f'/'h' probes only."""
    from casa_eval.base import Case, Suite
    return Suite(
        suite_id="scope_routing.mocked",
        description="first-char probes for fast mode",
        cases=[
            Case(input="personal friend hi", expected="personal"),
            Case(input="business meeting note", expected="business"),
            Case(input="finance invoice due", expected="finance"),
            Case(input="house lights off", expected="house"),
            # One intentionally-wrong case to exercise Failure path.
            Case(input="f prefix but labelled house", expected="house",
                 metadata={"source": "unit-mismatch"}),
        ],
    )


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """Scope library + monkeypatched fake embedder factory."""
    import scope_registry as sr
    monkeypatch.setattr(sr, "_load_text_embedding_cls", lambda: _FakeEmbedder)

    f = tmp_path / "scopes.yaml"
    f.write_text(textwrap.dedent(SCOPES_YAML_TEXT), encoding="utf-8")
    return sr.load_scope_library(str(f))


class TestScopeRoutingTesterFast:
    def test_registered_via_import(self):
        import casa_eval
        assert "scope_routing" in casa_eval.list_testers()

    def test_run_returns_report_with_correct_totals(self, lib):
        from casa_eval.scope_routing import ScopeRoutingTester

        tester = ScopeRoutingTester(scope_library=lib)
        report = tester.run(_suite_dict_inline(), threshold=0.35)
        assert report.tester_id == "scope_routing"
        assert report.suite_id == "scope_routing.mocked"
        assert report.total == 5
        # 4 correct, 1 mismatched (the 'f'-but-labelled-house case).
        assert report.passed == 4
        assert report.failed == 1
        assert report.accuracy == pytest.approx(0.8)
        assert report.config == {"threshold": 0.35}

    def test_failure_extra_includes_scores_and_margin(self, lib):
        from casa_eval.scope_routing import ScopeRoutingTester

        tester = ScopeRoutingTester(scope_library=lib)
        report = tester.run(_suite_dict_inline(), threshold=0.35)
        assert len(report.failures) == 1
        f = report.failures[0]
        assert f.expected == "house"
        assert f.actual == "finance"  # 'f' prefix -> finance wins
        assert "scores" in f.extra
        assert set(f.extra["scores"].keys()) == {
            "personal", "business", "finance", "house",
        }
        assert "margin" in f.extra
        assert f.extra["margin"] == pytest.approx(1.0)  # finance=1.0 vs others=0.0

    def test_metrics_shape(self, lib):
        from casa_eval.scope_routing import ScopeRoutingTester

        tester = ScopeRoutingTester(scope_library=lib)
        report = tester.run(_suite_dict_inline(), threshold=0.35)
        required = {
            "accuracy", "top2_accuracy", "fallback_rate",
            "mean_winner_score", "mean_margin",
            "p50_latency_ms", "p95_latency_ms",
        }
        assert required.issubset(report.metrics.keys())
        # fallback_rate sanity: the 'f-labelled-house' case has winner=1.0,
        # above threshold, so zero fallbacks in this mocked suite.
        assert report.metrics["fallback_rate"] == 0.0

    def test_run_uses_default_scope_from_metadata(self, lib):
        from casa_eval.base import Case, Suite
        from casa_eval.scope_routing import ScopeRoutingTester

        # Empty inputs embed to zero-vectors -> cosine=0 for every scope ->
        # every case is below threshold -> falls back to default_scope.
        # Case 0 overrides default_scope via metadata; case 1 uses the
        # tester-level DEFAULT_SCOPE_FALLBACK ("personal").
        suite = Suite(
            suite_id="s", description="",
            cases=[
                Case(input="", expected="house",
                     metadata={"default_scope": "house"}),
                Case(input="", expected="personal"),
            ],
        )
        tester = ScopeRoutingTester(scope_library=lib)
        report = tester.run(suite, threshold=0.35)
        # Both cases pass because the fallback matches `expected` in each.
        assert report.passed == 2
        assert report.metrics["fallback_rate"] == 1.0

    def test_report_json_roundtrip(self, lib):
        from casa_eval.scope_routing import ScopeRoutingTester
        from casa_eval.base import Report

        tester = ScopeRoutingTester(scope_library=lib)
        report = tester.run(_suite_dict_inline(), threshold=0.35)
        roundtripped = Report.from_json(report.to_json())
        assert roundtripped.total == report.total
        assert roundtripped.metrics["fallback_rate"] == report.metrics["fallback_rate"]


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    "fixtures", "eval", "scope_routing", "default.yaml",
)

PROD_SCOPES_YAML = os.path.join(
    os.path.dirname(__file__), "..",
    "casa-agent", "rootfs", "opt", "casa",
    "defaults", "policies", "scopes.yaml",
)


@pytest.mark.skipif(
    os.environ.get("CASA_REAL_EMBED") != "1",
    reason="set CASA_REAL_EMBED=1 to run full-mode real-embedder eval",
)
class TestScopeRoutingTesterFull:
    def test_fixture_loads_and_has_enough_cases(self):
        from collections import Counter
        from casa_eval.base import Suite
        from scope_registry import load_scope_library

        suite = Suite.from_yaml(FIXTURE_PATH)
        assert suite.suite_id == "scope_routing.default"
        assert len(suite.cases) >= 30, (
            f"fixture must hold >=30 probes, got {len(suite.cases)}"
        )
        lib = load_scope_library(PROD_SCOPES_YAML)
        valid_scopes = set(lib.names())
        bad = [c.expected for c in suite.cases if c.expected not in valid_scopes]
        assert not bad, (
            f"unknown expected scopes: {set(bad)}; valid: {valid_scopes}"
        )
        counts = Counter(c.expected for c in suite.cases)
        for scope, n in counts.items():
            assert n >= 5, f"scope {scope!r} has only {n} cases; need >=5"

    def test_full_mode_meets_accuracy_baseline(self):
        """Real e5-large against the default fixture.

        Uses the actual scopes.yaml shipped in the addon (not the
        4-scope mocked YAML), so descriptions match production.
        """
        from scope_registry import load_scope_library
        from casa_eval.base import Suite
        from casa_eval.scope_routing import ScopeRoutingTester

        lib = load_scope_library(PROD_SCOPES_YAML)
        tester = ScopeRoutingTester(scope_library=lib)
        suite = Suite.from_yaml(FIXTURE_PATH)
        report = tester.run(suite, threshold=0.35)

        # Pretty-print failures to stderr for the operator before asserting.
        if report.failures:
            import sys
            print("\n--- scope-routing eval failures ---", file=sys.stderr)
            for f in report.failures:
                print(
                    f"[{f.case_index}] expected={f.expected!r} actual={f.actual!r} "
                    f"margin={f.extra.get('margin'):.3f} input={f.input!r}",
                    file=sys.stderr,
                )
            print(f"accuracy={report.accuracy:.3f} "
                  f"fallback_rate={report.metrics['fallback_rate']:.3f}",
                  file=sys.stderr)

        assert report.accuracy >= ACCURACY_BASELINE, (
            f"accuracy {report.accuracy:.3f} < baseline {ACCURACY_BASELINE}"
        )
        assert report.metrics["fallback_rate"] <= FALLBACK_CAP, (
            f"fallback_rate {report.metrics['fallback_rate']:.3f} "
            f"> cap {FALLBACK_CAP}"
        )


class TestRecommendFromSweep:
    def _fake_report(self, threshold, accuracy, fallback_rate):
        from casa_eval.base import Report
        return Report(
            tester_id="scope_routing",
            suite_id="scope_routing.default",
            config={"threshold": threshold},
            total=30,
            passed=int(round(accuracy * 30)),
            failed=30 - int(round(accuracy * 30)),
            accuracy=accuracy,
            metrics={"fallback_rate": fallback_rate},
            failures=[],
            timestamp="2026-04-21T00:00:00Z",
        )

    def test_picks_max_accuracy_among_survivors(self, lib):
        from casa_eval.scope_routing import ScopeRoutingTester

        reports = {
            0.25: self._fake_report(0.25, 0.88, fallback_rate=0.05),
            0.30: self._fake_report(0.30, 0.92, fallback_rate=0.10),  # winner
            0.35: self._fake_report(0.35, 0.90, fallback_rate=0.15),
            0.40: self._fake_report(0.40, 0.86, fallback_rate=0.25),  # over cap, excluded
        }
        rec = ScopeRoutingTester(scope_library=lib).recommend_from_sweep(reports)
        assert rec.tester_id == "scope_routing"
        assert rec.axis == "threshold"
        assert rec.recommended == 0.30
        assert rec.current == 0.35
        assert "fallback_rate" in rec.justification or "accuracy" in rec.justification

    def test_tiebreak_on_lower_fallback_rate(self, lib):
        from casa_eval.scope_routing import ScopeRoutingTester

        reports = {
            0.25: self._fake_report(0.25, 0.90, fallback_rate=0.15),
            0.30: self._fake_report(0.30, 0.90, fallback_rate=0.08),  # lower fallback -> wins
        }
        rec = ScopeRoutingTester(scope_library=lib).recommend_from_sweep(reports)
        assert rec.recommended == 0.30

    def test_refuses_outside_bounds(self, lib):
        from casa_eval.scope_routing import ScopeRoutingTester

        reports = {
            0.15: self._fake_report(0.15, 0.95, fallback_rate=0.02),  # below 0.20 lower bound
            0.35: self._fake_report(0.35, 0.80, fallback_rate=0.10),
        }
        rec = ScopeRoutingTester(scope_library=lib).recommend_from_sweep(reports)
        # 0.15 is below optimization_bounds["threshold"] = (0.20, 0.50),
        # so the tester must not recommend it.
        assert rec.recommended == 0.35

    def test_returns_none_when_all_exceed_fallback_cap(self, lib):
        from casa_eval.scope_routing import ScopeRoutingTester

        reports = {
            0.35: self._fake_report(0.35, 0.90, fallback_rate=0.25),
            0.40: self._fake_report(0.40, 0.85, fallback_rate=0.30),
        }
        rec = ScopeRoutingTester(scope_library=lib).recommend_from_sweep(reports)
        assert rec.recommended is None
        assert "too noisy" in rec.justification.lower() \
            or "fallback" in rec.justification.lower()


@pytest.mark.skipif(
    os.environ.get("CASA_EVAL_SWEEP") != "1" or
    os.environ.get("CASA_REAL_EMBED") != "1",
    reason="set CASA_EVAL_SWEEP=1 CASA_REAL_EMBED=1 to run the sweep report",
)
class TestScopeRoutingSweep:
    def test_sweep_prints_table_and_recommendation(self):
        """Informational — never fails. Operator reads the printed table."""
        import sys
        from scope_registry import load_scope_library
        from casa_eval.base import Suite
        from casa_eval.scope_routing import ScopeRoutingTester

        lib = load_scope_library(PROD_SCOPES_YAML)
        tester = ScopeRoutingTester(scope_library=lib)
        suite = Suite.from_yaml(FIXTURE_PATH)
        values = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45]
        reports = tester.sweep(suite, axis="threshold", values=values)

        print("\n--- scope-routing sweep ---", file=sys.stderr)
        print("threshold |  accuracy  | fallback  | top2  | mean_winner",
              file=sys.stderr)
        for v in values:
            r = reports[v]
            print(
                f"  {v:.2f}    |   {r.accuracy:.3f}    |   {r.metrics['fallback_rate']:.3f}   "
                f"| {r.metrics['top2_accuracy']:.3f} |    {r.metrics['mean_winner_score']:.3f}",
                file=sys.stderr,
            )

        rec = tester.recommend_from_sweep(reports)
        print(f"\nRecommendation: {rec.recommended} (current={rec.current})",
              file=sys.stderr)
        print(f"Justification: {rec.justification}", file=sys.stderr)
