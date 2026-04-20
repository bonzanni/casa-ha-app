"""Tests for scope_registry.py — scope library + registry."""

from __future__ import annotations

import textwrap

import pytest


def _write(path, text: str) -> None:
    path.write_text(textwrap.dedent(text), encoding="utf-8")


SCOPES_YAML = """\
schema_version: 1
scopes:
  personal:
    minimum_trust: authenticated
    description: |
      Private life: relationships, friendships, non-work correspondence.
  house:
    minimum_trust: household-shared
    description: |
      The physical house: appliances, lights, plumbing, contractors.
"""


class TestScopeLibraryLoad:
    def test_loads_valid_scopes(self, tmp_path):
        from scope_registry import load_scope_library

        scopes_file = tmp_path / "scopes.yaml"
        _write(scopes_file, SCOPES_YAML)

        lib = load_scope_library(str(scopes_file))
        assert lib.names() == ["personal", "house"]
        assert lib.minimum_trust("personal") == "authenticated"
        assert lib.minimum_trust("house") == "household-shared"
        assert "Private life" in lib.description("personal")

    def test_missing_file_raises(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        with pytest.raises(ScopeError, match="not found"):
            load_scope_library(str(tmp_path / "missing.yaml"))

    def test_wrong_schema_version_raises(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        f = tmp_path / "scopes.yaml"
        _write(f, "schema_version: 99\nscopes: {personal: {minimum_trust: authenticated, description: '01234567890123456789'}}\n")

        with pytest.raises(ScopeError, match=r"schema violation"):
            load_scope_library(str(f))

    def test_unknown_trust_tier_raises(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        f = tmp_path / "scopes.yaml"
        _write(f, textwrap.dedent("""\
            schema_version: 1
            scopes:
              weird:
                minimum_trust: supercalifragilistic
                description: |
                  Long enough description to pass the minLength check.
        """))

        with pytest.raises(ScopeError, match=r"schema violation"):
            load_scope_library(str(f))

    def test_description_too_short_raises(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        f = tmp_path / "scopes.yaml"
        _write(f, textwrap.dedent("""\
            schema_version: 1
            scopes:
              x:
                minimum_trust: authenticated
                description: short
        """))

        with pytest.raises(ScopeError, match=r"schema violation"):
            load_scope_library(str(f))

    def test_unknown_name_raises_on_lookup(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        f = tmp_path / "scopes.yaml"
        _write(f, SCOPES_YAML)
        lib = load_scope_library(str(f))

        with pytest.raises(ScopeError, match="unknown scope"):
            lib.minimum_trust("nonexistent")


# ---------------------------------------------------------------------------
# TestScopeRegistryTrust (non-embedding methods)
# ---------------------------------------------------------------------------


class TestTrustPermits:
    @pytest.fixture
    def lib(self, tmp_path):
        from scope_registry import load_scope_library

        f = tmp_path / "scopes.yaml"
        _write(f, SCOPES_YAML)
        return load_scope_library(str(f))

    def test_trust_ordering(self, lib):
        from scope_registry import ScopeRegistry

        reg = ScopeRegistry(lib)
        # authenticated scope on authenticated channel → permitted
        assert reg.trust_permits("personal", "authenticated") is True
        # authenticated scope on household-shared channel → denied
        assert reg.trust_permits("personal", "household-shared") is False
        # household-shared scope on authenticated channel → permitted
        assert reg.trust_permits("house", "authenticated") is True
        # household-shared scope on household-shared channel → permitted
        assert reg.trust_permits("house", "household-shared") is True
        # internal trumps everything
        assert reg.trust_permits("personal", "internal") is True
        assert reg.trust_permits("house", "internal") is True
        # public channel sees nothing that isn't minimum_trust=public
        assert reg.trust_permits("personal", "public") is False
        assert reg.trust_permits("house", "public") is False

    def test_filter_readable(self, lib):
        from scope_registry import ScopeRegistry

        reg = ScopeRegistry(lib)
        # agent readable=[personal, house], channel authenticated → both
        assert reg.filter_readable(
            ["personal", "house"], "authenticated",
        ) == ["personal", "house"]
        # same agent on household-shared → only house
        assert reg.filter_readable(
            ["personal", "house"], "household-shared",
        ) == ["house"]
        # unknown scope in agent list → silently dropped
        assert reg.filter_readable(
            ["personal", "mystery", "house"], "authenticated",
        ) == ["personal", "house"]


class TestActiveSetAndArgmax:
    @pytest.fixture
    def reg(self, tmp_path):
        from scope_registry import load_scope_library, ScopeRegistry

        f = tmp_path / "scopes.yaml"
        _write(f, SCOPES_YAML)
        return ScopeRegistry(load_scope_library(str(f)), threshold=0.35)

    def test_active_from_scores_above_threshold(self, reg):
        scores = {"personal": 0.1, "house": 0.5}
        assert reg.active_from_scores(scores, default_scope="personal") == ["house"]

    def test_active_from_scores_all_below_falls_back_to_default(self, reg):
        scores = {"personal": 0.1, "house": 0.2}
        assert reg.active_from_scores(scores, default_scope="personal") == ["personal"]

    def test_active_from_scores_empty_input_falls_back_to_default(self, reg):
        assert reg.active_from_scores({}, default_scope="personal") == ["personal"]

    def test_argmax_picks_highest(self, reg):
        scores = {"personal": 0.4, "house": 0.8}
        assert reg.argmax_scope(scores, default_scope="personal") == "house"

    def test_argmax_falls_back_when_all_below_threshold(self, reg):
        scores = {"personal": 0.1, "house": 0.2}
        assert reg.argmax_scope(scores, default_scope="personal") == "personal"

    def test_argmax_empty_input_returns_default(self, reg):
        assert reg.argmax_scope({}, default_scope="house") == "house"


# ---------------------------------------------------------------------------
# TestEmbedding (with a fake TextEmbedding to avoid loading the real model)
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Deterministic fake: maps first character of text → a unit vector slot.

    The four probe chars we use in tests ('p', 'b', 'f', 'h') map to
    slots 0..3 of a 4-dim vector. Cosine similarity between two fake
    embeddings is 1.0 iff both texts start with the same probe char.
    """

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


EMBED_SCOPES_YAML = """\
schema_version: 1
scopes:
  personal:
    minimum_trust: authenticated
    description: |
      personal private life correspondence non-work plans.
  business:
    minimum_trust: authenticated
    description: |
      business professional work career meetings deadlines.
  finance:
    minimum_trust: authenticated
    description: |
      finance invoices bills payments banking taxes VAT.
  house:
    minimum_trust: household-shared
    description: |
      house appliances plumbing heating lights contractors sensors.
"""


class TestScopeRegistryPrepareAndScore:
    @pytest.fixture
    def reg(self, tmp_path, monkeypatch):
        from scope_registry import load_scope_library, ScopeRegistry
        import scope_registry as sr

        monkeypatch.setattr(sr, "_load_text_embedding_cls", lambda: _FakeEmbedder)

        f = tmp_path / "scopes.yaml"
        _write(f, EMBED_SCOPES_YAML)
        lib = load_scope_library(str(f))
        return ScopeRegistry(lib, threshold=0.35)

    @pytest.mark.asyncio
    async def test_prepare_embeds_all_scopes(self, reg):
        await reg.prepare()
        assert set(reg._embeddings.keys()) == {"personal", "business", "finance", "house"}
        assert reg._degraded is False

    @pytest.mark.asyncio
    async def test_score_finance_wins_on_finance_text(self, reg):
        await reg.prepare()
        scores = reg.score("finance stuff", ["personal", "business", "finance", "house"])
        assert scores["finance"] == pytest.approx(1.0)
        assert scores["house"] == pytest.approx(0.0)
        assert reg.argmax_scope(scores, default_scope="personal") == "finance"

    @pytest.mark.asyncio
    async def test_score_filters_to_requested_scopes(self, reg):
        await reg.prepare()
        scores = reg.score("finance stuff", ["personal", "house"])
        assert set(scores.keys()) == {"personal", "house"}
        # text starts with 'f', neither scope's description starts with 'f'
        assert all(v == pytest.approx(0.0) for v in scores.values())

    @pytest.mark.asyncio
    async def test_score_empty_scope_list_returns_empty_dict(self, reg):
        await reg.prepare()
        assert reg.score("anything", []) == {}


class TestScopeRegistryDegradedMode:
    @pytest.fixture
    def reg(self, tmp_path, monkeypatch):
        from scope_registry import load_scope_library, ScopeRegistry
        import scope_registry as sr

        def _broken_loader():
            raise RuntimeError("no model bucket")

        monkeypatch.setattr(sr, "_load_text_embedding_cls", _broken_loader)

        f = tmp_path / "scopes.yaml"
        _write(f, EMBED_SCOPES_YAML)
        return ScopeRegistry(load_scope_library(str(f)), threshold=0.35)

    @pytest.mark.asyncio
    async def test_prepare_degrades_on_model_failure(self, reg, caplog):
        import logging

        caplog.set_level(logging.ERROR, logger="scope_registry")
        await reg.prepare()
        assert reg._degraded is True
        assert any("no model bucket" in rec.message or "degraded" in rec.message.lower()
                   for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_score_in_degraded_mode_returns_all_ones(self, reg):
        await reg.prepare()
        scores = reg.score("anything", ["personal", "house"])
        assert scores == {"personal": 1.0, "house": 1.0}
