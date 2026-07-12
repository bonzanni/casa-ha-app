"""§3.10 plugin-health report: structured fingerprints, carry-forward dedup,
first-contact notice."""
from __future__ import annotations

import pytest

import plugin_health
from plugin_registry import PluginIssue

pytestmark = pytest.mark.unit


def _issue(name="p", target="specialist:finance", stage="resolve",
           reason_code="corrupt_artifact", artifact_id="a" * 64):
    return PluginIssue(name=name, target=target, stage=stage,
                       reason_code=reason_code, artifact_id=artifact_id)


def test_fingerprint_stable_and_field_sensitive():
    a = _issue()
    b = _issue()
    assert plugin_health.fingerprint(a) == plugin_health.fingerprint(b)
    # different reason_code → different fingerprint
    c = _issue(reason_code="artifact_missing")
    assert plugin_health.fingerprint(c) != plugin_health.fingerprint(a)
    # different target → different fingerprint
    d = _issue(target="resident:assistant")
    assert plugin_health.fingerprint(d) != plugin_health.fingerprint(a)


def test_write_load_roundtrip(tmp_path):
    p = tmp_path / "health.json"
    rep = plugin_health.write_report(issues=[_issue()], warnings=[], path=p)
    assert rep["schema_version"] == 1
    assert rep["issues"][0]["reason_code"] == "corrupt_artifact"
    loaded = plugin_health.load_report(p)
    assert loaded == rep


def test_carry_forward_dedup_and_reappearance(tmp_path):
    p = tmp_path / "health.json"
    x = _issue()
    fp_x = plugin_health.fingerprint(x)

    rA = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(rA) == [fp_x]
    plugin_health.mark_notified([fp_x], path=p)

    rB = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(rB) == []          # stays notified

    plugin_health.write_report(issues=[], warnings=[], path=p)  # X resolved
    rD = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(rD) == [fp_x]       # NEW again


def test_first_contact_notice_matches_role_and_registry_wide(tmp_path):
    p = tmp_path / "health.json"
    plugin_health.write_report(
        issues=[_issue(name="lesina-invoice", target="specialist:finance")],
        warnings=[], path=p)
    assert "lesina-invoice" in plugin_health.first_contact_notice("finance", p)
    assert "operator has been notified" in \
        plugin_health.first_contact_notice("finance", p)
    assert plugin_health.first_contact_notice("assistant", p) is None

    # registry-wide (target=None) matches ANY role
    plugin_health.write_report(
        issues=[_issue(name="*", target=None, stage="registry",
                       reason_code="registry_invalid", artifact_id=None)],
        warnings=[], path=p)
    assert plugin_health.first_contact_notice("assistant", p) is not None


def test_first_contact_notice_absent_or_empty(tmp_path):
    assert plugin_health.first_contact_notice("finance",
                                              tmp_path / "nope.json") is None
    p = tmp_path / "health.json"
    plugin_health.write_report(issues=[], warnings=[], path=p)
    assert plugin_health.first_contact_notice("finance", p) is None


def test_first_contact_notice_caps_at_two_plus_more(tmp_path):
    p = tmp_path / "health.json"
    plugin_health.write_report(issues=[
        _issue(name="a", reason_code="corrupt_artifact"),
        _issue(name="b", reason_code="artifact_missing"),
        _issue(name="c", reason_code="reload_required"),
    ], warnings=[], path=p)
    notice = plugin_health.first_contact_notice("finance", p)
    assert "a (corrupt_artifact)" in notice and "b (artifact_missing)" in notice
    assert "+1 more" in notice and "c (" not in notice
