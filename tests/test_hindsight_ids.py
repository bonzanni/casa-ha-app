"""Fail-fast Hindsight bank-id builder (spec §8.1). Mirrors honcho_ids.py:
silent sanitization is forbidden — bad input must raise, never be coerced.
The Hindsight server itself does NOT validate ids (probed live 2026-06-02:
dots/uppercase/100-char all accepted), so this client-side guard is the
only thing protecting Casa's bank namespace."""
from __future__ import annotations

import pytest

from hindsight_ids import bank_id

pytestmark = [pytest.mark.unit]


def test_builds_role_bank() -> None:
    assert bank_id("casa", "assistant") == "casa-assistant"
    assert bank_id("casa", "butler") == "casa-butler"


def test_single_part() -> None:
    assert bank_id("casa-finance") == "casa-finance"


@pytest.mark.parametrize(
    "bad",
    ["casa.finance", "casa/finance", "casa finance", "üñ", "casa$", ""],
)
def test_rejects_out_of_charset(bad: str) -> None:
    with pytest.raises(ValueError):
        bank_id("casa", bad)


def test_rejects_empty_call() -> None:
    with pytest.raises(ValueError):
        bank_id()


def test_rejects_non_str() -> None:
    with pytest.raises(ValueError):
        bank_id("casa", 3)  # type: ignore[arg-type]


def test_rejects_overlong() -> None:
    with pytest.raises(ValueError):
        bank_id("casa", "x" * 100)
