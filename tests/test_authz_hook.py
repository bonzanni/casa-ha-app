"""Task 7 (A:§3.2): the fail-closed PreToolUse authorization hook + its wiring
into residents and specialists (executors get nothing).

The hook is the enforcement core of v0.76.0. It:
  - passes unprotected tools through untouched (never touches store/coordinator);
  - denies FIRST for engagement provenance (no challenge, no role-assert), then
    for unsupported transport/execution, then for an explicit role mismatch;
  - checks provenance BEFORE consuming any grant (a copied webhook chat/user id
    can never spend a grant);
  - consumes a single-use, argument-bound grant if one exists (allow ⇒ ``{}``);
  - otherwise posts (or reuses) ONE confirmation challenge and denies, naming
    the button — settled_post delivery_failed / inactive get their own denials;
  - wraps the entire protected path fail-closed: every non-CancelledError
    exception at ANY stage is logged and returns a VALID SDK deny;
    CancelledError propagates.

Coordinator/broker/channel test doubles mirror ``tests/test_authz_grants.py``
(the real ``VerdictBroker`` + a recording ``_FakeChannel``) so the settled_post
outcomes are driven END-TO-END through the real coordinator.
"""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

import agent as agent_mod
import tools as tools_mod
import provenance as provenance_mod
import authz_grants
from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantKey, GrantStore,
    canonical_args_hash, make_resident_authz_hook,
    _DENY_ENGAGEMENT, _DENY_UNSUPPORTED_ORIGIN, _DENY_ROLE_MISMATCH,
    _DENY_UNRENDERABLE, _DENY_PENDING, _DENY_DELIVERY_FAILED, _DENY_INACTIVE,
    _DENY_POSTED, _DENY_INTERNAL,
)

pytestmark = pytest.mark.unit


TOOL = "mcp__plugin_p_p__invoice_reset"
ARTIFACT = "artifact-1"
PROTECTED = {TOOL: ARTIFACT}


# ---------------------------------------------------------------------------
# Origin + hook drivers
# ---------------------------------------------------------------------------


def _origin(**overrides) -> dict:
    """A dm/direct finance origin by default (role == execution_role)."""
    base = {
        "role": "finance",
        "channel": "telegram",
        "chat_id": "42",
        "user_id": 100,
        "cid": "abc",
        "message_type": "channel_in",
        "source": "telegram",
        "execution_role": "finance",
    }
    base.update(overrides)
    return base


class _OriginCtx:
    """Set agent.origin_var (+ optionally tools.engagement_var) for the block."""

    def __init__(self, origin: dict | None, *, engaged: bool = False):
        self._origin = origin
        self._engaged = engaged

    def __enter__(self):
        self._otok = agent_mod.origin_var.set(self._origin)
        eng = Mock(name="EngagementRecord") if self._engaged else None
        self._etok = tools_mod.engagement_var.set(eng)
        return self

    def __exit__(self, *exc):
        agent_mod.origin_var.reset(self._otok)
        tools_mod.engagement_var.reset(self._etok)


def _mk_hook(*, role="finance", protected=None, deps=None, deps_factory=None):
    protected = PROTECTED if protected is None else protected
    if deps_factory is None:
        def deps_factory():
            return deps
    return make_resident_authz_hook(role, protected, deps_factory)


async def _call(hook, *, tool_name=TOOL, tool_input=None):
    if tool_input is None:
        tool_input = {"amount": 10}
    return await hook({"tool_name": tool_name, "tool_input": tool_input},
                      None, {})


def _deny_reason(out):
    hso = (out or {}).get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") == "deny", out
    return hso.get("permissionDecisionReason", "")


def _expected_key(tool_input, *, role="finance", operator_id=100, chat_id=42,
                  artifact=ARTIFACT, tool=TOOL) -> GrantKey:
    return GrantKey(operator_id=operator_id, chat_id=chat_id,
                    enforcement_role=role, artifact_id=artifact,
                    tool_name=tool, args_hash=canonical_args_hash(tool_input))


# ---------------------------------------------------------------------------
# Coordinator / broker / channel doubles (mirror test_authz_grants.py)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self):
        self.posts: list = []
        self.edits: list = []
        self.dispatches: list = []
        self.post_result: int | None = 55
        self.post_raises = False
        self.post_gate: asyncio.Event | None = None
        self.dispatch_result = True

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        if self.post_gate is not None:
            await self.post_gate.wait()
        if self.post_raises:
            raise RuntimeError("post boom")
        return self.post_result

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _dispatch_button_continuation(self, **kw):
        self.dispatches.append(kw)
        return self.dispatch_result


def _fresh_coord(monkeypatch, *, ttl=None):
    """A fresh real broker (monkeypatched into the singleton slot the
    coordinator resolves at call time), a fresh coordinator, a fake channel."""
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    if ttl is not None:
        monkeypatch.setattr(authz_grants, "_CHALLENGE_TTL_S", ttl)
    return broker, ChallengeCoordinator(), _FakeChannel()


async def _settle(n: int = 6):
    for _ in range(n):
        await asyncio.sleep(0)


class _ExplodingStore:
    """A grant store whose every method raises — proves the passthrough path
    never touches it, and stands in for the injected store-stage failure."""

    def consume(self, key):
        raise AssertionError("store must not be consulted here")

    def mint(self, key, **kw):
        raise AssertionError("store must not be minted here")


class _ExplodingCoord:
    def get_or_create(self, *a, **kw):
        raise AssertionError("coordinator must not be consulted here")


# ===========================================================================
# Passthrough
# ===========================================================================


class TestPassthrough:
    async def test_unprotected_tool_returns_empty_no_deps_no_store_touch(self):
        called = {"factory": False}

        def factory():
            called["factory"] = True
            return AuthzDeps(channel=_FakeChannel(), grants=_ExplodingStore(),
                             challenges=_ExplodingCoord())

        hook = _mk_hook(deps_factory=factory)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_name="Read", tool_input={"x": 1})
        assert out == {}
        assert called["factory"] is False  # deps never resolved for passthrough


# ===========================================================================
# Provenance gates (before any grant lookup)
# ===========================================================================


class TestProvenanceGates:
    async def test_engagement_denies_without_challenge_no_role_assert(self):
        """execution=engagement ⇒ deny WITHOUT a challenge, and NO closure-role
        assertion even when execution_role mismatches the hook's role."""
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(role="finance", deps=deps)
        # engaged, AND execution_role != role — must still deny cleanly.
        with _OriginCtx(_origin(execution_role="butler"), engaged=True):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_ENGAGEMENT
        assert channel.posts == []  # no challenge

    async def test_other_transport_denies_without_challenge(self):
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        # webhook-style turn: not a telegram channel_in
        with _OriginCtx(_origin(message_type="webhook", source="webhook")):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_UNSUPPORTED_ORIGIN
        assert channel.posts == []

    async def test_role_mismatch_explicit_deny(self):
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        # dm/direct origin (role==execution_role==butler) but the hook's closure
        # role is finance — an explicit deny, not an assert.
        hook = _mk_hook(role="finance", deps=deps)
        with _OriginCtx(_origin(role="butler", execution_role="butler")):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_ROLE_MISMATCH
        assert channel.posts == []

    async def test_no_channel_reachable_denies_unsupported_origin(self):
        """deps_factory returning None (no DM) ⇒ the unsupported-origin deny."""
        hook = _mk_hook(deps_factory=lambda: None)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_UNSUPPORTED_ORIGIN

    async def test_provenance_checked_before_consume_grant_intact(self):
        """A webhook turn with a LIVE grant consumes NOTHING (provenance denies
        first) — the grant remains consumable afterward (B4)."""
        store = GrantStore()
        ti = {"amount": 10}
        key = _expected_key(ti)
        store.mint(key)
        # spy: if the hook ever calls consume, it would spend the grant.
        deps = AuthzDeps(channel=_FakeChannel(), grants=store,
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin(message_type="webhook", source="webhook")):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_UNSUPPORTED_ORIGIN
        assert store.consume(key) is True  # grant untouched by the hook


# ===========================================================================
# Consume / challenge behaviour
# ===========================================================================


class TestConsumeAndChallenge:
    async def test_happy_path_challenge_posted_once_deny_names_button(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        reason = _deny_reason(out)
        assert reason == _DENY_POSTED
        assert "confirmation button" in reason and "Approve" in reason
        assert len(channel.posts) == 1  # exactly one keyboard

    async def test_pending_dedup_second_call_one_keyboard(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out1 = await _call(hook)
            out2 = await _call(hook)
        assert _deny_reason(out1) == _DENY_POSTED
        assert _deny_reason(out2) == _DENY_PENDING
        assert len(channel.posts) == 1  # ONE keyboard for both calls

    async def test_mint_then_identical_call_allows(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=ti)
        assert out == {}  # allow
        assert channel.posts == []

    async def test_third_call_rechallenges_single_use(self, monkeypatch):
        """mint → 1st identical call allows; 2nd identical call (grant spent) →
        re-challenge (single-use)."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            first = await _call(hook, tool_input=ti)
            second = await _call(hook, tool_input=ti)
        assert first == {}
        assert _deny_reason(second) == _DENY_POSTED
        assert len(channel.posts) == 1  # the re-challenge

    async def test_args_drift_rechallenges(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        store.mint(_expected_key({"amount": 10}))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input={"amount": 999})  # drift
        assert _deny_reason(out) == _DENY_POSTED
        assert len(channel.posts) == 1

    async def test_enforcement_role_mismatch_cannot_consume(self, monkeypatch):
        """Two roles, same plugin+args: a grant minted for finance cannot be
        consumed by butler — its key's enforcement_role differs (B5)."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti, role="finance"))  # minted for finance
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(role="butler", deps=deps)  # butler tries to consume
        with _OriginCtx(_origin(role="butler", execution_role="butler")):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_POSTED  # no consume ⇒ challenge

    async def test_artifact_mismatch_cannot_consume(self, monkeypatch):
        """A grant minted against a stale artifact_id cannot be consumed after a
        plugin update changes the resolved artifact."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti, artifact="OLD-artifact"))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        # hook's protected map names the NEW artifact.
        hook = _mk_hook(protected={TOOL: ARTIFACT}, deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_POSTED

    async def test_restart_loses_grants_fresh_store_rechallenges(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_coord(monkeypatch)
        old_store = GrantStore()
        ti = {"amount": 10}
        old_store.mint(_expected_key(ti))
        # a fresh process ⇒ a brand-new empty store; the grant is gone.
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_POSTED
        assert old_store.consume(_expected_key(ti)) is True  # old one still had it

    async def test_delegated_target_role_is_originating_resident(
        self, monkeypatch,
    ):
        """A delegated specialist's challenge routes back to the ORIGINATING
        resident (origin.role, e.g. Ellen), not the enforcement role."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(role="finance", deps=deps)
        # origin.role = assistant (Ellen), execution_role = finance ⇒ delegated.
        with _OriginCtx(_origin(role="assistant", execution_role="finance")):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_POSTED
        # the broker record's meta names Ellen as the continuation target.
        ((chat_id, rid, _text, _opts),) = channel.posts
        meta = broker.get_meta(namespace="resident_ask",
                               scope=f"authz:{chat_id}", request_id=rid)
        assert meta["target_role"] == "assistant"
        assert meta["enforcement_role"] == "finance"


# ===========================================================================
# Unrenderable / oversized args
# ===========================================================================


class TestUnrenderable:
    async def test_unserializable_args_deny_without_challenge(self):
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=GrantStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input={"bad": {1, 2, 3}})  # a set
        assert _deny_reason(out) == _DENY_UNRENDERABLE
        assert channel.posts == []  # no coordinator/keyboard touched

    async def test_args_too_large_refusal_denies_no_keyboard(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        big = {"blob": "x" * 5000}  # rendered challenge exceeds the 3900 ceiling
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=big)
        assert _deny_reason(out) == _DENY_UNRENDERABLE
        assert channel.posts == []  # refused before any post


# ===========================================================================
# settled_post outcomes (driven end-to-end through the real coordinator)
# ===========================================================================


class TestSettledPostOutcomes:
    async def test_delivery_failed_deny(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        channel.post_raises = True  # the keyboard post fails
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_DELIVERY_FAILED

    async def test_inactive_timeout_while_posting_deny(self, monkeypatch):
        """The TTL fires while the post is still blocked in flight — even though
        the post then succeeds, the button is already expired (inactive)."""
        broker, coord, channel = _fresh_coord(monkeypatch, ttl=0.02)
        channel.post_gate = asyncio.Event()
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            task = asyncio.ensure_future(_call(hook))
            await asyncio.sleep(0)      # hook runs; post blocks on the gate
            await asyncio.sleep(0.05)   # the 0.02s TTL fires while blocked
            channel.post_gate.set()     # post now completes (returns a mid)
            out = await task
        assert _deny_reason(out) == _DENY_INACTIVE
        await _settle()
        assert len(channel.posts) == 1

    async def test_inactive_new_while_posting_deny(self, monkeypatch):
        """/new cancels the chat's authz scope while the post is in flight."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        channel.post_gate = asyncio.Event()
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            task = asyncio.ensure_future(_call(hook))
            await asyncio.sleep(0)      # hook runs; post blocks
            broker.cancel_scope(namespace="resident_ask", scope="authz:42",
                                reason="new_session")
            channel.post_gate.set()
            out = await task
        assert _deny_reason(out) == _DENY_INACTIVE
        await _settle()


# ===========================================================================
# Fail-closed wrapper — injected failures per stage
# ===========================================================================


class _RaisingConsumeStore(GrantStore):
    def consume(self, key):
        raise RuntimeError("store boom")


class _RaisingCoord:
    def get_or_create(self, *a, **kw):
        raise RuntimeError("coordinator boom")


class _RaisingSettleHandle:
    created = True
    refused = None

    async def settled_post(self):
        raise RuntimeError("posting boom")


class _RaisingSettleCoord:
    def get_or_create(self, *a, **kw):
        return _RaisingSettleHandle()


class TestFailClosedWrapper:
    async def test_provenance_stage_raise_denies(self, monkeypatch):
        monkeypatch.setattr(
            provenance_mod, "turn_provenance",
            Mock(side_effect=RuntimeError("prov boom")))
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_store_stage_raise_denies(self):
        deps = AuthzDeps(channel=_FakeChannel(), grants=_RaisingConsumeStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_canonicalization_stage_raise_denies(self, monkeypatch):
        # A NON-ValueError from canonicalization ⇒ the internal-error deny (a
        # ValueError is the semantic unrenderable deny, tested separately).
        monkeypatch.setattr(
            authz_grants, "canonical_args_json",
            Mock(side_effect=RuntimeError("canon boom")))
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_coordinator_stage_raise_denies(self):
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_RaisingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_posting_stage_raise_denies(self):
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_RaisingSettleCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_deps_factory_raise_denies(self):
        def factory():
            raise RuntimeError("factory boom")

        hook = _mk_hook(deps_factory=factory)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_cancelled_error_propagates(self, monkeypatch):
        monkeypatch.setattr(
            provenance_mod, "turn_provenance",
            Mock(side_effect=asyncio.CancelledError()))
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            with pytest.raises(asyncio.CancelledError):
                await _call(hook)


# ===========================================================================
# Wiring — residents AND specialists carry the appended matcher WITH the
# pre-existing settings guard intact; executors get NOTHING.
# ===========================================================================


def _authz_matcher(opts):
    for m in (opts.hooks or {}).get("PreToolUse", []):
        for h in getattr(m, "hooks", []):
            if getattr(h, "_casa_authz_role", None) is not None:
                return m, getattr(h, "_casa_authz_role")
    return None


def _has_settings_guard(opts):
    pre = (opts.hooks or {}).get("PreToolUse", [])
    return any(getattr(m, "matcher", None)
               == "Write|Edit|MultiEdit|NotebookEdit|Bash" for m in pre)


_PROTECTED_MANIFEST = {"casa": {"protectedTools": ["invoice_reset"]}}


class TestWiring:
    def test_resident_options_append_authz_matcher_guard_intact(self, tmp_path):
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        # Import the resident-options test harness helpers.
        from test_agent_plugin_binding import _make_agent

        store = tmp_path / "store"
        e = entry("p", ["resident:assistant"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        a = _make_agent(tmp_path, role="assistant")

        async def run():
            return await a._build_options(
                channel="telegram", channel_key="k", is_fresh=True,
                resume_sid=None, user_text="hi")

        opts = asyncio.run(run())
        found = _authz_matcher(opts)
        assert found is not None, "resident options missing the authz matcher"
        _matcher, role = found
        assert role == "assistant"
        assert _has_settings_guard(opts), "settings guard clobbered"

    def test_specialist_options_append_authz_matcher_guard_intact(
        self, tmp_path, monkeypatch,
    ):
        import plugin_registry
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _spec_cfg

        store = tmp_path / "store"
        e = entry("p", ["specialist:finance"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
        resolution = plugin_registry.resolve_for("specialist:finance")

        opts = tools_mod._build_specialist_options(
            _spec_cfg("finance"), resolution=resolution)
        found = _authz_matcher(opts)
        assert found is not None, "specialist options missing the authz matcher"
        _matcher, role = found
        assert role == "finance"
        assert _has_settings_guard(opts), "settings guard clobbered"
        # artifact ids flow from protected_map, not an injected set.
        from plugin_grants import protected_map
        assert protected_map(resolution) == {
            "mcp__plugin_p_p__invoice_reset": e["artifact_id"]}

    def test_specialist_without_protected_tools_has_no_authz_matcher(
        self, tmp_path, monkeypatch,
    ):
        import plugin_registry
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _spec_cfg

        store = tmp_path / "store"
        e = entry("p", ["specialist:finance"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}})  # none
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
        resolution = plugin_registry.resolve_for("specialist:finance")

        opts = tools_mod._build_specialist_options(
            _spec_cfg("finance"), resolution=resolution)
        assert _authz_matcher(opts) is None
        assert _has_settings_guard(opts)  # guard still present

    def test_executor_options_have_no_authz_matcher(self, tmp_path, monkeypatch):
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _exec_defn

        store = tmp_path / "store"
        e = entry("p", ["executor:probe-exec"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)

        opts = tools_mod._build_executor_options(
            _exec_defn(), executor_type="probe-exec")
        assert _authz_matcher(opts) is None  # executors get NOTHING
