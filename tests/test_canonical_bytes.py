"""Direct coverage for the binding canonical-text/checksum primitives in
`canonical_bytes.py`: NFC normalization, line-ending/whitespace folding,
terminal-newline discipline, and the sha256 checksum helpers used to bind
persona/resident content."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from types import MappingProxyType

import pytest

from canonical_bytes import (
    assert_json_safe,
    canonical_text,
    checksum_bytes,
    checksum_json,
    deep_freeze,
)


# ---------------------------------------------------------------------------
# canonical_text
# ---------------------------------------------------------------------------


def test_canonical_text_nfc_normalizes_decomposed_sequences() -> None:
    # "e" + COMBINING ACUTE ACCENT (U+0301) is the NFD decomposition of "é".
    decomposed = "café"
    assert unicodedata.normalize("NFC", decomposed) == "café"
    result = canonical_text(decomposed)
    assert result == "café\n"
    assert unicodedata.is_normalized("NFC", result)


def test_canonical_text_crlf_folds_to_lf() -> None:
    assert canonical_text("line1\r\nline2\r\n") == "line1\nline2\n"


def test_canonical_text_lone_cr_folds_to_lf() -> None:
    assert canonical_text("line1\rline2\r") == "line1\nline2\n"


def test_canonical_text_strips_trailing_spaces_and_tabs_per_line() -> None:
    assert canonical_text("line1   \nline2\t\t\nline3 \t \n") == (
        "line1\nline2\nline3\n"
    )


def test_canonical_text_result_ends_with_exactly_one_terminal_newline() -> None:
    assert canonical_text("no trailing newline").endswith("\n")
    assert not canonical_text("no trailing newline").endswith("\n\n")

    many_trailing = "content\n\n\n\n"
    result = canonical_text(many_trailing)
    assert result.endswith("\n")
    assert not result.endswith("\n\n")
    assert result == "content\n"


def test_canonical_text_empty_string_yields_single_newline() -> None:
    assert canonical_text("") == "\n"


def test_canonical_text_is_idempotent() -> None:
    for text in (
        "café  \r\n  trailing  \r\nmore\r",
        "already\ncanonical\n",
        "",
        "   \t  \n\n\n",
    ):
        once = canonical_text(text)
        twice = canonical_text(once)
        assert once == twice


# ---------------------------------------------------------------------------
# checksum_bytes
# ---------------------------------------------------------------------------

_SHA256_HEX_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def test_checksum_bytes_shape_is_sha256_prefix_plus_64_lowercase_hex_chars() -> None:
    result = checksum_bytes(b"some content")
    assert _SHA256_HEX_RE.fullmatch(result)
    assert len(result) == len("sha256:") + 64


def test_checksum_bytes_matches_hashlib_sha256_for_known_input() -> None:
    data = b"the quick brown fox"
    expected = "sha256:" + hashlib.sha256(data).hexdigest()
    assert checksum_bytes(data) == expected


def test_checksum_bytes_empty_input_matches_known_sha256_empty_digest() -> None:
    # The well-known SHA-256 digest of the empty byte string.
    assert checksum_bytes(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


# ---------------------------------------------------------------------------
# checksum_json
# ---------------------------------------------------------------------------


def test_checksum_json_is_order_independent() -> None:
    first = {"speaker_kind": "user", "user_peer": "telegram_123", "user_id": None}
    second = {"user_id": None, "user_peer": "telegram_123", "speaker_kind": "user"}
    assert first != list(first.items())  # sanity: dicts really do differ in insertion order
    assert list(first.keys()) != list(second.keys())
    assert checksum_json(first) == checksum_json(second)


def test_checksum_json_differs_for_different_values() -> None:
    assert checksum_json({"a": 1}) != checksum_json({"a": 2})


def test_checksum_json_matches_checksum_bytes_of_canonical_json() -> None:
    from canonical_bytes import canonical_json_bytes

    value = {"b": 2, "a": 1}
    assert checksum_json(value) == checksum_bytes(canonical_json_bytes(value))


# ---------------------------------------------------------------------------
# assert_json_safe (R1, foundation review r2)
#
# yaml.safe_load can still yield non-JSON-native Python types for YAML
# tags/aliases (`!!set` -> set, `!!binary` -> bytes) and self-referential
# anchors (cyclic dict/list). assert_json_safe is the invariant that must
# run right after parsing, before any recursive walk (marker scan,
# deep_freeze) that would otherwise crash on a cycle or silently miss
# content hidden in a non-dict/list/scalar container.
# ---------------------------------------------------------------------------


def test_assert_json_safe_accepts_a_normal_nested_json_dict() -> None:
    value = {
        "a": 1,
        "b": [1, 2.5, "three", True, False, None],
        "c": {"nested": {"deeper": ["x", "y"]}},
    }
    assert assert_json_safe(value) is None  # does not raise


def test_assert_json_safe_rejects_a_set() -> None:
    with pytest.raises(ValueError, match="non-JSON-native type"):
        assert_json_safe({"a": {"b"}})


def test_assert_json_safe_rejects_bytes() -> None:
    with pytest.raises(ValueError, match="non-JSON-native type"):
        assert_json_safe({"a": b"binary"})


def test_assert_json_safe_rejects_a_cyclic_dict() -> None:
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="cyclic"):
        assert_json_safe(cyclic)


def test_assert_json_safe_rejects_a_cyclic_list() -> None:
    cyclic: list = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="cyclic"):
        assert_json_safe(cyclic)


def test_assert_json_safe_rejects_depth_over_max_depth() -> None:
    value: object = "leaf"
    for _ in range(70):
        value = [value]
    with pytest.raises(ValueError, match="nesting exceeds limit"):
        assert_json_safe(value)


def test_assert_json_safe_accepts_depth_at_max_depth() -> None:
    value: object = "leaf"
    for _ in range(64):
        value = [value]
    assert assert_json_safe(value) is None  # does not raise


def test_assert_json_safe_rejects_non_string_dict_key() -> None:
    with pytest.raises(ValueError, match="non-string key"):
        assert_json_safe({1: "a"})


def test_assert_json_safe_rejects_a_tuple() -> None:
    with pytest.raises(ValueError, match="non-JSON-native type"):
        assert_json_safe(("a", "b"))


# ---------------------------------------------------------------------------
# deep_freeze cycle/depth hardening (R1, foundation review r2)
#
# deep_freeze is a shared primitive independent of any caller's own
# guards, so it must fail closed (ValueError) rather than crash
# (RecursionError) on a pathological input on its own.
# ---------------------------------------------------------------------------


def test_deep_freeze_still_freezes_ordinary_nested_content() -> None:
    value = {"a": [1, {"b": 2}], "c": (3, 4)}
    frozen = deep_freeze(value)
    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["a"], tuple)
    assert isinstance(frozen["a"][1], MappingProxyType)
    assert frozen["c"] == (3, 4)


def test_deep_freeze_cyclic_dict_raises_value_error_not_recursion_error() -> None:
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="cyclic"):
        deep_freeze(cyclic)


def test_deep_freeze_cyclic_list_raises_value_error_not_recursion_error() -> None:
    cyclic: list = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="cyclic"):
        deep_freeze(cyclic)


def test_deep_freeze_excessive_depth_raises_value_error() -> None:
    value: object = "leaf"
    for _ in range(1000):
        value = [value]
    with pytest.raises(ValueError, match="nesting exceeds limit"):
        deep_freeze(value)
