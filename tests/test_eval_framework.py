"""Contract tests for the casa_eval framework seam (ABC + dataclasses).

Always-on: no env gates. These assert the public surface that the future
Builder MCP tool and the shipped ScopeRoutingTester both rely on.
"""

from __future__ import annotations

import json
import pytest


class TestDataclassRoundtrip:
    def test_report_json_roundtrip_preserves_all_fields(self):
        from casa_eval.base import Report, Failure

        r = Report(
            tester_id="scope_routing",
            suite_id="scope_routing.default",
            config={"threshold": 0.35},
            total=10,
            passed=9,
            failed=1,
            accuracy=0.9,
            metrics={"fallback_rate": 0.1, "top2_accuracy": 1.0},
            failures=[Failure(
                case_index=3,
                input="foo",
                expected="finance",
                actual="personal",
                extra={"scores": {"personal": 0.4, "finance": 0.35}, "margin": 0.05},
            )],
            timestamp="2026-04-21T12:00:00Z",
        )

        r2 = Report.from_json(r.to_json())
        assert r2.tester_id == r.tester_id
        assert r2.suite_id == r.suite_id
        assert r2.config == r.config
        assert r2.total == r.total
        assert r2.accuracy == r.accuracy
        assert r2.metrics == r.metrics
        assert len(r2.failures) == 1
        assert r2.failures[0].extra == r.failures[0].extra
        assert r2.report_schema == "1"

    def test_recommendation_json_roundtrip(self):
        from casa_eval.base import Recommendation, Report

        stub = Report(
            tester_id="x", suite_id="y", config={}, total=1, passed=1,
            failed=0, accuracy=1.0, metrics={}, failures=[],
            timestamp="2026-04-21T00:00:00Z",
        )
        rec = Recommendation(
            tester_id="scope_routing",
            axis="threshold",
            current=0.35,
            recommended=0.30,
            justification="lower threshold improves accuracy on fixture",
            evidence={0.30: stub, 0.35: stub},
        )
        payload = rec.to_json()
        parsed = json.loads(payload)
        assert parsed["recommended"] == 0.30
        assert set(parsed["evidence"].keys()) == {"0.3", "0.35"}  # JSON keys always str


class TestSuiteLoader:
    def test_suite_from_yaml_parses_cases(self, tmp_path):
        from casa_eval.base import Suite

        path = tmp_path / "suite.yaml"
        path.write_text(
            "suite_id: demo\n"
            "description: tiny demo suite\n"
            "cases:\n"
            "  - input: hello\n"
            "    expected: personal\n"
            "    metadata: {channel: telegram}\n"
            "  - input: turn off the lights\n"
            "    expected: house\n",
            encoding="utf-8",
        )
        s = Suite.from_yaml(str(path))
        assert s.suite_id == "demo"
        assert len(s.cases) == 2
        assert s.cases[0].input == "hello"
        assert s.cases[0].expected == "personal"
        assert s.cases[0].metadata == {"channel": "telegram"}
        assert s.cases[1].metadata == {}


class TestTesterRegistry:
    def test_list_and_get_empty_registry(self):
        # With no testers registered, list_testers() returns [] and
        # get_tester raises KeyError on unknown ids.
        import importlib
        import casa_eval
        importlib.reload(casa_eval)
        assert casa_eval.list_testers() == []
        with pytest.raises(KeyError):
            casa_eval.get_tester("scope_routing")


class TestSweepContract:
    def test_sweep_rejects_unknown_axis(self):
        from casa_eval.base import Tester, Suite

        class DummyTester(Tester):
            id = "dummy"
            optimization_axes = ["alpha"]
            optimization_bounds = {"alpha": (0.0, 1.0)}

            def load_suite(self, path): return Suite(
                suite_id="s", description="", cases=[],
            )

            def run(self, suite, **opts): return None

            def recommend_from_sweep(self, reports): return None

        with pytest.raises(ValueError, match="unknown axis"):
            DummyTester().sweep(
                Suite(suite_id="s", description="", cases=[]),
                axis="beta",
                values=[0.1, 0.2],
            )

    def test_sweep_default_impl_calls_run_once_per_value(self):
        from casa_eval.base import Tester, Suite

        class CountingTester(Tester):
            id = "count"
            optimization_axes = ["alpha"]
            optimization_bounds = {"alpha": (0.0, 1.0)}
            calls = []

            def load_suite(self, path): ...

            def run(self, suite, **opts):
                CountingTester.calls.append(opts)
                return f"report@{opts['alpha']}"

            def recommend_from_sweep(self, reports): ...

        t = CountingTester()
        suite = Suite(suite_id="s", description="", cases=[])
        reports = t.sweep(suite, axis="alpha", values=[0.1, 0.2, 0.3])
        assert reports == {0.1: "report@0.1", 0.2: "report@0.2", 0.3: "report@0.3"}
        assert CountingTester.calls == [{"alpha": 0.1}, {"alpha": 0.2}, {"alpha": 0.3}]
