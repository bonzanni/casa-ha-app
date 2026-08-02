"""casa.callbacks manifest parse + declaration-digest consent identity
(authorization-callback facility)."""
from __future__ import annotations

import pytest

from plugin_callbacks import (
    MAX_CALLBACKS,
    MAX_EFFECTIVE_LEN,
    ack_identity,
    declaration_digest,
    effective_name,
    parse_and_validate,
)

pytestmark = pytest.mark.unit


def _m(callbacks):
    return {"casa": {"callbacks": callbacks}}


# --- happy path --------------------------------------------------------

def test_valid_single_entry():
    cbs, errs = parse_and_validate("elevenlabs", _m([{"name": "oauth"}]))
    assert errs == []
    assert cbs == [{"declared": "oauth", "effective": "plg-elevenlabs--oauth"}]


def test_effective_name_helper():
    assert effective_name("el", "oauth") == "plg-el--oauth"


def test_constants():
    assert MAX_CALLBACKS == 4
    assert MAX_EFFECTIVE_LEN == 128


# --- absent / malformed casa -------------------------------------------

def test_absent_callbacks_is_empty_not_error():
    assert parse_and_validate("p", {"casa": {}}) == ([], [])
    assert parse_and_validate("p", {}) == ([], [])
    assert parse_and_validate("p", {"casa": "nonsense"}) == ([], [])
    assert parse_and_validate("p", {"casa": {"callbacks": None}}) == ([], [])


def test_non_list_callbacks_rejected():
    _, errs = parse_and_validate("p", {"casa": {"callbacks": {"name": "x"}}})
    assert errs


def test_non_dict_entry_rejected():
    _, errs = parse_and_validate("p", _m(["not-a-dict"]))
    assert errs


# --- entry shape: exactly {"name"} --------------------------------------

def test_unknown_key_rejected():
    _, errs = parse_and_validate("p", _m([{"name": "oauth", "bogus": 1}]))
    assert any("bogus" in e or "unknown" in e.lower() for e in errs)


def test_missing_name_key_rejected():
    _, errs = parse_and_validate("p", _m([{}]))
    assert errs


# --- naming ---------------------------------------------------------------

def test_bad_name_charset_rejected():
    _, errs = parse_and_validate("p", _m([{"name": "has space"}]))
    assert errs


def test_double_dash_in_declared_rejected():
    _, errs = parse_and_validate("p", _m([{"name": "a--b"}]))
    assert any("--" in e for e in errs)


def test_double_dash_in_plugin_name_rejected():
    _, errs = parse_and_validate("a--b", _m([{"name": "x"}]))
    assert errs


def test_plg_prefixed_declared_rejected():
    _, errs = parse_and_validate("p", _m([{"name": "plg-x"}]))
    assert errs


def test_duplicate_declared_names_rejected():
    _, errs = parse_and_validate("p", _m([{"name": "d"}, {"name": "d"}]))
    assert any("duplicate" in e.lower() for e in errs)


# ---------------------------------------------------------------------------
# Injectivity rails (mirrors plugin_triggers.py:124-162 verbatim): with both
# dash edges banned, the FIRST '--' in an effective name is always exactly
# the plugin/declared separator.
# ---------------------------------------------------------------------------

def test_plugin_name_trailing_dash_rejected():
    _, errs = parse_and_validate("a-", _m([{"name": "x"}]))
    assert any("end with '-'" in e for e in errs)


def test_declared_name_leading_dash_rejected():
    _, errs = parse_and_validate("a", _m([{"name": "-x"}]))
    assert any("start with '-'" in e for e in errs)


def test_dash_edge_collision_pair_is_fully_rejected():
    """Neither producer of the ambiguous effective name survives."""
    _, e1 = parse_and_validate("a-", _m([{"name": "x"}]))
    _, e2 = parse_and_validate("a", _m([{"name": "-x"}]))
    assert e1 and e2


# --- counts -----------------------------------------------------------

def test_too_many_callbacks_rejected():
    many = [{"name": f"c{i}"} for i in range(5)]
    _, errs = parse_and_validate("p", _m(many))
    assert any("4" in e or "too many" in e.lower() for e in errs)


# --- effective-length boundary (128, not the trigger's 64) -----------------

_LONG_PLUGIN = "casa-specialist-finance.enable-banking"


def test_effective_name_length_ok_at_128():
    prefix_len = len(f"plg-{_LONG_PLUGIN}--")
    declared = "x" * (128 - prefix_len)
    cbs, errs = parse_and_validate(_LONG_PLUGIN, _m([{"name": declared}]))
    assert errs == []
    assert len(cbs[0]["effective"]) == 128


def test_effective_name_length_rejected_at_129():
    prefix_len = len(f"plg-{_LONG_PLUGIN}--")
    declared = "x" * (129 - prefix_len)
    _, errs = parse_and_validate(_LONG_PLUGIN, _m([{"name": declared}]))
    assert any("128" in e or "long" in e.lower() for e in errs)


# --- digest / identity -----------------------------------------------------

def test_declaration_digest_deterministic():
    entry = {"declared": "oauth", "effective": "plg-el--oauth"}
    d1 = declaration_digest(entry)
    d2 = declaration_digest(dict(entry))
    assert d1 == d2
    assert len(d1) == 64  # sha256 hex


def test_declaration_digest_changes_with_name():
    d1 = declaration_digest({"declared": "oauth", "effective": "plg-el--oauth"})
    d2 = declaration_digest({"declared": "oauth2", "effective": "plg-el--oauth2"})
    assert d1 != d2


def test_ack_identity_deterministic():
    digest = declaration_digest({"declared": "oauth", "effective": "plg-el--oauth"})
    id1 = ack_identity("el", "plg-el--oauth", digest)
    id2 = ack_identity("el", "plg-el--oauth", digest)
    assert id1 == id2
    assert len(id1) == 64


def test_ack_identity_changes_with_declared_name():
    digest_a = declaration_digest({"declared": "oauth"})
    digest_b = declaration_digest({"declared": "oauth2"})
    id_a = ack_identity("el", "plg-el--oauth", digest_a)
    id_b = ack_identity("el", "plg-el--oauth2", digest_b)
    assert id_a != id_b


def test_ack_identity_excludes_artifact():
    """Same (plugin, effective, digest) inputs always yield the same
    identity — there is no artifact_id input to vary."""
    digest = declaration_digest({"declared": "oauth"})
    id1 = ack_identity("el", "plg-el--oauth", digest)
    id2 = ack_identity("el", "plg-el--oauth", digest)
    assert id1 == id2


def test_ack_identity_has_no_artifact_parameter():
    with pytest.raises(TypeError):
        ack_identity("el", "plg-el--oauth", "digest", artifact_id="x")
