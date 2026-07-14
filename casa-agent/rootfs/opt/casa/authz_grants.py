"""Canonical argument JSON + the single-use, TTL-bound authorization
GrantStore (A:§3.3, §3.6).

This module is Part 1 of 3 for the ``ask_user`` + authorization-grants
project (v0.76.0). Later tasks EXTEND it — Task 5 adds a
``ChallengeCoordinator`` (async settlement, two latches) and Task 7 adds
``make_resident_authz_hook``/``AuthzDeps`` and ``protected_map``
consumption. Keep this file leaf-level: no imports of ``agent``/``tools``
at module scope, new sections appended below rather than interleaved, and
the ``GRANTS`` singleton kept at the very bottom of its section so later
tasks can import it without reaching into internals.

Sections, top to bottom:

1. Canonical JSON + hash (§3.6) — pure functions used both to compute the
   ``GrantKey.args_hash`` and to render the challenge message body.
2. ``GrantKey`` + ``GrantStore`` (§3.3) — a thread-safe, single-use,
   TTL-bound store of "operator approved this exact tool call" grants.
   ``GRANTS`` is the process-wide singleton.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Canonical JSON (A:§3.6)
# ---------------------------------------------------------------------------


def canonical_args_json(tool_input: dict) -> str:
    """Render *tool_input* as canonical JSON.

    ``sort_keys=True`` gives a stable key order (recursively, for every
    nested dict), ``separators=(",", ":")`` drops the default whitespace,
    ``ensure_ascii=False`` leaves unicode unescaped, and ``allow_nan=False``
    rejects ``nan``/``inf``/``-inf`` — these cannot be rendered faithfully
    to an operator confirming a tool call, so they must never silently
    round-trip through a hash.

    Raises ``ValueError`` if *tool_input* contains a non-finite float
    (``allow_nan=False`` makes ``json.dumps`` raise ``ValueError`` for
    those) or any value ``json.dumps`` cannot serialize at all (a
    ``TypeError`` is re-raised as ``ValueError`` with a clearer message,
    so callers only need to catch one exception type).
    """
    try:
        return json.dumps(
            tool_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except TypeError as exc:
        raise ValueError(f"tool_input is not JSON-serializable: {exc}") from exc


def canonical_args_hash(tool_input: dict) -> str:
    """sha256 hexdigest of ``canonical_args_json(tool_input)`` (UTF-8)."""
    return hashlib.sha256(
        canonical_args_json(tool_input).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# GrantKey + GrantStore (A:§3.3)
# ---------------------------------------------------------------------------

# Grant TTL default, per the plan's Global Constraints: "grant TTL 300 s
# default; single-use".
DEFAULT_GRANT_TTL_S = 300.0


def normalize_role(target: str) -> str:
    """Strip a plugin target's tier qualifier so it matches
    ``GrantKey.enforcement_role`` (r2-B5): plugin targets are tier-qualified
    (``"specialist:finance"``) while the enforcement role is always plain
    (``"finance"``). Already-plain input (no ``":"``) is returned unchanged.
    Used at EVERY purge/cancel call site — tools.py's plugin-lifecycle
    mutations (plugin_update/plugin_remove/plugin_unassign) and reload.py's
    per-role reload seams — so a tier-qualified target reliably invalidates
    the plain-role grants/challenges it actually governs."""
    if not isinstance(target, str):
        return target
    _, sep, role = target.partition(":")
    return role if sep else target


@dataclass(frozen=True)
class GrantKey:
    """Identifies exactly one operator-approved tool call.

    ``artifact_id`` binds the grant to the resolved plugin artifact at
    challenge time — a plugin update mid-TTL changes the artifact and
    invalidates the grant. ``enforcement_role`` is the plain role (never
    tier-qualified) that is actually allowed to consume it.
    """

    operator_id: int
    chat_id: int
    enforcement_role: str
    artifact_id: str
    tool_name: str
    args_hash: str


@dataclass
class _Grant:
    """Internal record: when it expires, and whether it has been used."""

    expires_at: float
    used: bool = False


class GrantStore:
    """Thread-safe, single-use, TTL-bound store of operator-approved
    tool-call grants.

    A ``threading.Lock`` guards every mutation — not ``asyncio.Lock`` —
    because the SDK PreToolUse hook's thread-context relative to the
    event loop is uncertain (pinned in the plan as a load-bearing
    assumption); a real-thread concurrency test proves ``consume`` is
    genuinely serialized, not just coroutine-safe.

    ``_now`` is an injectable zero-arg clock (default ``time.monotonic``)
    so tests can drive TTL expiry deterministically without sleeping.
    """

    def __init__(self, *, _now: Callable[[], float] = time.monotonic) -> None:
        self._now = _now
        self._lock = threading.Lock()
        self._grants: dict[GrantKey, _Grant] = {}

    def mint(self, key: GrantKey, *, ttl_s: float = DEFAULT_GRANT_TTL_S) -> None:
        """Create a fresh, unused grant for *key*, replacing any live one.

        Minting always wins over whatever was there before — a stale
        unused grant, an already-consumed one, or an expired one are all
        simply overwritten with a new record.
        """
        grant = _Grant(expires_at=self._now() + ttl_s)
        with self._lock:
            self._grants[key] = grant

    def consume(self, key: GrantKey) -> bool:
        """Atomically mark *key* used and return ``True`` — exactly once.

        Returns ``False`` when no grant exists for *key*, it already
        expired, or it was already consumed. The whole check-and-mark
        happens under the lock, so N callers racing the SAME key see
        exactly one ``True``.
        """
        with self._lock:
            grant = self._grants.get(key)
            if grant is None:
                return False
            if grant.used or grant.expires_at <= self._now():
                return False
            grant.used = True
            return True

    def purge_chat(self, chat_id: int) -> int:
        """Drop every grant for *chat_id*. Returns the count removed."""
        return self._purge(lambda k: k.chat_id == chat_id)

    def purge_role(self, role: str) -> int:
        """Drop every grant whose ``enforcement_role`` is *role*."""
        return self._purge(lambda k: k.enforcement_role == role)

    def purge_artifact(self, artifact_id: str) -> int:
        """Drop every grant whose ``artifact_id`` is *artifact_id*."""
        return self._purge(lambda k: k.artifact_id == artifact_id)

    def sweep(self) -> int:
        """Drop every grant whose TTL has passed. Returns the count
        removed. Used grants that have not yet expired are left alone —
        this is a pure TTL sweep, not a used-grant reaper."""
        now = self._now()
        return self._purge(lambda k: self._grants[k].expires_at <= now)

    def _purge(self, predicate: Callable[[GrantKey], bool]) -> int:
        with self._lock:
            matched = [k for k in self._grants if predicate(k)]
            for k in matched:
                del self._grants[k]
            return len(matched)


# Process-wide singleton. Later tasks (the ask_user tool's /new purge, the
# PreToolUse authz hook's consume, plugin/reload lifecycle purges, and the
# casa_core.py hourly sweep job) all import THIS instance.
GRANTS = GrantStore()


# ---------------------------------------------------------------------------
# ChallengeCoordinator (A:§3.4) — Part 2 (Task 5)
# ---------------------------------------------------------------------------
#
# The coordinator is the single owner of the authorization-challenge
# lifecycle: it renders + size-validates the challenge, atomically registers
# ONE broker record per (full) GrantKey, drives the keyboard post on an owned
# background task, installs the authz finish hook, and reaps its own entry
# once BOTH the request future and the setup task have settled. It is
# LOOP-CONFINED — every entry point is called from a hook coroutine on the
# agent's event loop (documented invariant, NOT thread-safe) — so the
# atomic "check the dict, then register + spawn" happens with no await in
# between, and production concurrency is modelled as multiple hook coroutines
# on ONE loop.
#
# This section keeps ``authz_grants.py`` leaf-level: the broker singleton is
# resolved lazily by name (``verdict_broker.BROKER``) so tests can substitute
# it, and the Telegram channel is passed in as a duck-typed ``channel`` object
# (``post_dm_keyboard`` / ``edit_dm_message`` / ``_dispatch_button_continuation``)
# — there is NO import of ``channels`` / ``telegram`` here.

# Challenge message size ceiling (plan Global Constraints): a rendered
# challenge body longer than this is REFUSED rather than posted (Telegram's
# hard message cap is ~4096; 3900 leaves headroom for the keyboard + envelope).
_CHALLENGE_MAX_CHARS = 3900

# Challenge TTL (plan Global Constraints): 120 s.
_CHALLENGE_TTL_S = 120.0


def render_challenge_message(
    *, tool_name: str, enforcement_role: str, canonical_json: str,
) -> str:
    """Render the operator-facing challenge body: the FULL canonical args in a
    fenced block, framed by a single-question header. Size is measured on THIS
    string (``get_or_create`` refuses when it exceeds ``_CHALLENGE_MAX_CHARS``).
    """
    return (
        "\U0001F510 Authorization required\n\n"
        f"Approve {enforcement_role} calling {tool_name} with EXACTLY these "
        "arguments?\n\n"
        f"```\n{canonical_json}\n```"
    )


def _challenge_expired_text(tool_name: str) -> str:
    return f"⌛ this authorization request expired — {tool_name}"


def _broker() -> Any:
    """Resolve the process broker singleton by name so a test's monkeypatch of
    ``verdict_broker.BROKER`` is honored at call time."""
    import verdict_broker

    return verdict_broker.BROKER


@dataclass
class _Challenge:
    """Coordinator-owned record for one live authorization challenge.

    The two cleanup latches (``request_settled`` — the broker future has
    resolved; ``setup_settled`` — the owned setup driver's post/settlement is
    done) gate entry removal: whichever lands SECOND removes the entry, and
    removal is identity-guarded so a stale late latch can never evict a newer
    challenge registered under the same key by a retry.
    """

    key: GrantKey
    scope: str
    rid: str
    req: Any                       # verdict_broker.PendingRequest
    broker: Any                    # the VerdictBroker instance that owns req
    driver: "asyncio.Task | None" = None
    setup_settled: bool = False
    request_settled: bool = False


@dataclass
class ChallengeHandle:
    """Returned synchronously by ``get_or_create``.

    ``created`` is ``False`` when an identical challenge was already pending.
    ``refused`` is ``"args_too_large"`` when the rendered challenge exceeded
    the size ceiling (no entry / no keyboard / no registration). ``settled_post``
    awaits the coordinator-owned setup driver (shielded, so a cancelled HOOK
    caller never cancels the driver — exactly ONE keyboard is ever posted) and
    then classifies the outcome.
    """

    created: bool
    refused: str | None = None
    _challenge: _Challenge | None = field(default=None, repr=False)

    async def settled_post(self) -> str:
        """``"posted"`` | ``"delivery_failed"`` | ``"inactive"`` (r2-B2/r3-B3).

        ``inactive`` = the request settled TERMINALLY (TTL timeout or /new)
        before or while the post was in flight — even a post that then
        succeeds shows an immediately-expired button, so the caller must NOT
        treat it as a live confirmation.
        """
        ch = self._challenge
        if ch is None or ch.driver is None:
            # Refused (or otherwise no live challenge) — nothing was posted.
            return "inactive"
        # Shield: a cancelled hook caller must not cancel the owned driver.
        await asyncio.shield(ch.driver)
        fut = ch.req._future
        if not fut.done():
            return "posted"
        outcome = fut.result()
        if isinstance(outcome, dict) and outcome.get("outcome") == "delivery_failed":
            return "delivery_failed"
        return "inactive"


class ChallengeCoordinator:
    """Atomic challenge registration + async-settled setup driver + two-latch
    cleanup + the single-owner authz finish hook (A:§3.4)."""

    def __init__(self) -> None:
        self._entries: dict[GrantKey, _Challenge] = {}
        # Strong refs to the owned setup drivers so they are never GC'd
        # mid-flight; ``drain`` awaits exactly this set at shutdown.
        self._drivers: set[asyncio.Task] = set()

    # -- registration (SYNCHRONOUS, loop-confined, single owner) ------------

    def get_or_create(
        self, key: GrantKey, *, chat_id: int, operator_id: int,
        target_role: str, tool_name: str, canonical_json: str,
        enforcement_role: str, channel: Any,
    ) -> ChallengeHandle:
        existing = self._entries.get(key)
        if existing is not None:
            return ChallengeHandle(created=False, _challenge=existing)

        # Rendering + size validation runs BEFORE any insert [A:§3.4]:
        # oversized -> refused handle, NO entry, NO keyboard, NO registration.
        challenge_text = render_challenge_message(
            tool_name=tool_name, enforcement_role=enforcement_role,
            canonical_json=canonical_json,
        )
        if len(challenge_text) > _CHALLENGE_MAX_CHARS:
            return ChallengeHandle(created=True, refused="args_too_large")

        broker = _broker()
        rid = uuid.uuid4().hex
        scope = f"authz:{chat_id}"
        # register() shallow-copies this dict; the complete meta (minus the
        # self-referential on_commit_sync) is supplied up front.
        req, _created = broker.register(
            namespace="resident_ask", scope=scope, request_id=rid,
            timeout_s=_CHALLENGE_TTL_S, detached=True, supersede=False,
            meta={
                "kind": "authz",
                "chat_id": chat_id,
                "operator_id": operator_id,
                "target_role": target_role,
                "grant_key": key,
                "canonical_args_json": canonical_json,
                "enforcement_role": enforcement_role,
                "options": ["Approve", "Deny"],
                "_scope": scope,
            },
        )

        # The sync step MUST mutate the BROKER-OWNED req.meta (register()
        # shallow-copied our dict), so it is attached AFTER register with the
        # broker's own dict captured — mutating the caller's original would be
        # invisible to the finish hook (which reads req.meta["minted"]).
        def _on_commit_sync(idx: int, _meta: dict = req.meta, _key: GrantKey = key) -> None:
            # Runs in the Telegram callback IMMEDIATELY after a successful
            # commit (no await between): idx 0 -> mint + record; idx 1 -> no-op.
            if idx == 0:
                GRANTS.mint(_key)
                _meta["minted"] = True

        req.meta["on_commit_sync"] = _on_commit_sync

        ch = _Challenge(key=key, scope=scope, rid=rid, req=req, broker=broker)
        # Request latch: fires on EVERY terminal resolution of the future
        # (answered / no_answer / cancelled / delivery_failed).
        req._future.add_done_callback(
            lambda _f, _c=ch: self._settle_request(_c)
        )
        self._entries[key] = ch

        async def _post() -> Any:
            return await channel.post_dm_keyboard(
                chat_id=chat_id, request_id=rid, text=challenge_text,
                options=["Approve", "Deny"],
            )

        def _finish_factory(message_id: int) -> Callable[[dict], Any]:
            return self._make_finish_hook(
                channel=channel, chat_id=chat_id, operator_id=operator_id,
                target_role=target_role, enforcement_role=enforcement_role,
                tool_name=tool_name, canonical_json=canonical_json,
                rid=rid, message_id=message_id, req=req,
            )

        # Spawn the owned SETUP DRIVER (strong-ref'd): it does ONLY the
        # posting / setup settlement (single registration owner — register
        # already happened synchronously above).
        driver = asyncio.get_running_loop().create_task(
            self._drive(ch, _post, _finish_factory)
        )
        ch.driver = driver
        self._drivers.add(driver)
        driver.add_done_callback(self._drivers.discard)

        return ChallengeHandle(created=True, _challenge=ch)

    # -- owned setup driver -------------------------------------------------

    async def _drive(
        self, ch: _Challenge,
        post_factory: Callable[[], Any],
        finish_factory: Callable[[int], Callable[[dict], Any]],
    ) -> None:
        req = ch.req
        await ch.broker.ensure_posted(req, post_factory, finish_factory)
        if req._setup_task is None:
            # The request was ALREADY terminal before any post — ensure_posted
            # no-ops on a done future, so no setup task exists. Settle the
            # setup latch DIRECTLY (r4-B2).
            self._settle_setup(ch)
        else:
            # The setup task exists only now (r1-B1); attach the setup latch to
            # it (already done — ensure_posted awaited it — so the callback
            # fires on the next loop turn).
            req._setup_task.add_done_callback(
                lambda _t, _c=ch: self._settle_setup(_c)
            )

    # -- the single-owner authz finish hook ---------------------------------

    def _make_finish_hook(
        self, *, channel: Any, chat_id: int, operator_id: int,
        target_role: str, enforcement_role: str, tool_name: str,
        canonical_json: str, rid: str, message_id: int, req: Any,
    ) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id, _challenge_expired_text(tool_name),
                )
                return
            idx = outcome.get("option_index")
            if idx == 0:
                if not req.meta.get("minted"):
                    # Commit landed but the sync step never recorded the mint
                    # (raised + swallowed) — surface the internal error and
                    # NEVER dispatch an authorization the store can't back.
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "internal error recording the approval — "
                        "call the tool again",
                    )
                    return
                # Edit the SUCCESS state FIRST, then dispatch, then overwrite
                # ONLY on dispatch failure (ordered inside this one hook task).
                await channel.edit_dm_message(
                    chat_id, message_id, f"✅ approved — {tool_name}",
                )
                ok = await channel._dispatch_button_continuation(
                    chat_id=chat_id, user_id=operator_id,
                    target_role=target_role, request_id=rid,
                    text=(
                        "[authorization approved]: have "
                        f"{enforcement_role} call {tool_name} with EXACTLY "
                        f"these arguments:\n{canonical_json}"
                    ),
                )
                if not ok:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"approved, but delivery to {target_role} failed — "
                        "say 'retry' in chat",
                    )
            else:
                await channel.edit_dm_message(
                    chat_id, message_id, "❌ denied",
                )
                ok = await channel._dispatch_button_continuation(
                    chat_id=chat_id, user_id=operator_id,
                    target_role=target_role, request_id=rid,
                    text=f"[authorization denied]: do not retry {tool_name}",
                )
                if not ok:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"denied, but delivery to {target_role} failed — "
                        "say 'retry' in chat",
                    )

        return _finish

    # -- two-latch cleanup (coordinator-driven, never inside the edit) ------

    def _settle_request(self, ch: _Challenge) -> None:
        ch.request_settled = True
        self._maybe_remove(ch)

    def _settle_setup(self, ch: _Challenge) -> None:
        ch.setup_settled = True
        self._maybe_remove(ch)

    def _maybe_remove(self, ch: _Challenge) -> None:
        if not (ch.setup_settled and ch.request_settled):
            return
        # Identity-guarded: only evict THIS challenge, never a newer one that a
        # retry may have registered under the same key.
        if self._entries.get(ch.key) is ch:
            del self._entries[ch.key]

    # -- cancellation / drain ----------------------------------------------

    def cancel_matching(
        self, *, role: str | None = None, artifact: str | None = None,
        chat: int | None = None,
    ) -> int:
        """Cancel the broker records for every live challenge matching ANY of
        the provided filters (keyboard -> expired via the finish hook). Returns
        the number of records actually cancelled."""

        def _matches(k: GrantKey) -> bool:
            if role is not None and k.enforcement_role == role:
                return True
            if artifact is not None and k.artifact_id == artifact:
                return True
            if chat is not None and k.chat_id == chat:
                return True
            return False

        matched = [ch for k, ch in self._entries.items() if _matches(k)]
        n = 0
        for ch in matched:
            if ch.broker.cancel(
                namespace="resident_ask", scope=ch.scope,
                request_id=ch.rid, reason="challenge_cancelled",
            ):
                n += 1
        return n

    async def drain(self) -> None:
        """Await all outstanding setup-driver tasks (r4-B2/r5-B2). Called from
        casa_core's shutdown ladder AFTER ``BROKER.cancel_all()`` so a draining
        driver can only find a cancelled request (no fresh keyboard is posted)."""
        drivers = list(self._drivers)
        if drivers:
            await asyncio.gather(*drivers, return_exceptions=True)


# Process-wide singleton (mirrors GRANTS): casa_core's shutdown ladder and the
# resident authz hook (Task 7) import THIS instance.
CHALLENGES = ChallengeCoordinator()
