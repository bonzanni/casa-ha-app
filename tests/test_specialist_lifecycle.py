import pytest

from specialist_lifecycle import check_slug_uniqueness, satisfy_config


def test_slug_colliding_with_a_fixed_resident_slot_is_rejected() -> None:
    with pytest.raises(ValueError, match="collides"):
        check_slug_uniqueness(
            candidate_slug="butler", fixed_role_slots=frozenset({"assistant", "butler", "concierge"}),
            installed_specialist_slugs=frozenset(),
        )


def test_slug_colliding_with_an_installed_specialist_is_rejected() -> None:
    with pytest.raises(ValueError, match="collides"):
        check_slug_uniqueness(
            candidate_slug="finance", fixed_role_slots=frozenset({"assistant", "butler", "concierge"}),
            installed_specialist_slugs=frozenset({"finance"}),
        )


def test_bare_slug_collision_key_is_never_case_or_unicode_folded() -> None:
    check_slug_uniqueness(  # does NOT raise — different bare strings
        candidate_slug="Finance", fixed_role_slots=frozenset(), installed_specialist_slugs=frozenset({"finance"}),
    )


def test_new_slug_is_accepted() -> None:
    check_slug_uniqueness(
        candidate_slug="mtg", fixed_role_slots=frozenset({"assistant", "butler", "concierge"}),
        installed_specialist_slugs=frozenset({"finance"}),
    )


def test_satisfy_config_reports_missing_required_names() -> None:
    schema = {"required": ["api_base", "api_token"], "secret_names": ["api_token"]}
    satisfied, missing = satisfy_config(schema=schema, provided_non_secret={"api_base": "https://x"}, provided_secret_names=frozenset())
    assert satisfied is False
    assert missing == ["api_token"]


def test_satisfy_config_true_when_secret_name_is_present() -> None:
    schema = {"required": ["api_base", "api_token"], "secret_names": ["api_token"]}
    satisfied, missing = satisfy_config(schema=schema, provided_non_secret={"api_base": "https://x"}, provided_secret_names=frozenset({"api_token"}))
    assert satisfied is True
    assert missing == []
