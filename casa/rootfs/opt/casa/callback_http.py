"""The public ``GET /callback/{name}`` endpoint (spec §6; INV-CB-001/002/
004/005/006).

This is the authorization-callback facility's only unauthenticated surface,
and the only one an attacker can address directly. Three properties shape
every line below.

**One response, always.** Success and every refusal cause — unknown name,
missing/malformed state, unknown state, expired pending, replay, an existing
result, a write failure, an internal fault — return the *same* 303 to the
fixed, query-less ``/callback/done``. A differentiated status, header or
target would be an enumeration oracle: it would tell a prober which effective
names are routed and which states are live. That is also why there is no 429:
flood handling damps internal *emission* (below), never the response
(spec §6 step 1; INV-CB-005). Nothing on the request path can raise past the
outer guard, because a 500 is a differentiated response.

**Nothing query-derived is logged, and nothing request-derived is rendered.**
The query string carries a bearer credential (an OAuth authorization code).
Handler logs carry a reason enum, a cid and the *effective* name only —
except ``unknown_name``, which logs a fixed sentinel, since the route
component is attacker-controlled. The access logger suppresses the query for
``/callback/`` (see ``casa_core_middleware.QUERY_SUPPRESSED_PREFIXES``) and
nginx does the same for its own access log. The two pages are static
constants: zero interpolation of request data (INV-CB-006).

**Opaque relay.** Casa interprets exactly one parameter — ``state``, parsed
strictly from the RAW query string, never the framework's decoded view, so a
percent-encoded or duplicated ``state`` cannot smuggle a different value past
the charset gate. Everything else is recorded verbatim (``raw_query``) plus
an ordered decoded view (``query``) for the consumer; no provider-specific
parsing exists here (INV-CB-004).

The route produces no turn: no ingress-identity row, no clearance, no
provenance. That is deliberate — a browser redirect is not an authenticated
principal, and the deposit is picked up by the plugin that minted the state.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from aiohttp import web

import callback_spool
from rate_limit import RateLimiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

DONE_PATH = "/callback/done"

#: Sampling threshold, per key, per minute. ``0`` (and any negative or
#: unparseable value) clamps to the default rather than disabling the bucket:
#: ``RateLimiter`` reads capacity 0 as "no limit", which would turn the
#: sampler into an unbounded log amplifier under exactly the flood it exists
#: to damp.
RATE_ENV = "CALLBACK_RATE_PER_MIN"
DEFAULT_RATE_PER_MIN = 60

#: Logged in place of an unrouted route component (INV-CB-006): the component
#: is attacker-controlled and must never reach a log line.
UNKNOWN_SENTINEL = "<unknown>"

#: Outcome vocabulary (spec §6 "Log hygiene"). ``expired`` is part of the
#: facility's vocabulary but is NOT reachable from this handler:
#: ``CallbackSpool.claim`` refuses expired, replayed and never-minted states
#: identically by design, so the handler sees one refusal and logs
#: ``no_pending``. The sweep/recovery passes own the ``expired`` case.
REASONS = frozenset({
    "ok", "unknown_name", "bad_state", "no_pending", "expired",
    "result_exists", "write_failed",
})

#: State grammar (spec §6 step 3) — checked against the RAW value, so a
#: percent-escape is a rejection rather than a decode.
STATE_MIN = 22
STATE_MAX = 256
_STATE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

#: Pre-filesystem caps (spec §6 step 3). aiohttp already bounds the request
#: line, but the record is written to ``/data`` and read by a plugin, so the
#: captured size is bounded here too. A violation is a neutral refusal — no
#: truncated record is ever stored, because a truncated credential is worse
#: than none.
MAX_RAW_QUERY = 4096
MAX_PAIRS = 32
MAX_KEY = 128
MAX_VALUE = 2048

#: Sent on BOTH the 303 and the done page (spec §6 step 8).
SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
}

#: The one user-visible string of the whole facility. Deliberately neutral:
#: it is shown for a provider error and for a replayed link just as for a
#: success, and it nudges the operator back to the chat where the plugin is
#: waiting. Static — no request data reaches it.
DONE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Casa</title>
<style>
:root { color-scheme: light dark; }
body { margin: 0; min-height: 100vh; display: flex; align-items: center;
       justify-content: center; font-family: system-ui, -apple-system,
       "Segoe UI", Roboto, sans-serif; background: #f6f7f9; color: #14161a; }
main { max-width: 30rem; padding: 2rem; text-align: center; }
p { font-size: 1.125rem; line-height: 1.6; margin: 0; }
@media (prefers-color-scheme: dark) {
  body { background: #14161a; color: #f6f7f9; }
}
</style>
</head>
<body>
<main>
<p>Response received. You can close this tab and return to your chat.</p>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# query parsing (spec §6 steps 3 and 6)
# ---------------------------------------------------------------------------


def _segments(raw_query: str) -> list[str] | None:
    """Split a raw query on ``&`` and apply the pre-filesystem caps.

    ``None`` means "refuse" — the caller renders the same neutral response it
    renders for everything else.
    """
    if len(raw_query) > MAX_RAW_QUERY:
        return None
    segments = [seg for seg in raw_query.split("&") if seg]
    if len(segments) > MAX_PAIRS:
        return None
    for seg in segments:
        key, _sep, value = seg.partition("=")
        if len(key) > MAX_KEY or len(value) > MAX_VALUE:
            return None
    return segments


def _extract_state(raw_query: str) -> str | None:
    """The single ``state`` value, or ``None`` for every malformed case.

    Parsed from the RAW query on purpose (spec §6 step 3): the framework's
    decoded view merges duplicates and percent-decodes, either of which would
    let a prober present one value to casa's charset gate and another to the
    filesystem. Requires *exactly one* ``state`` occurrence — a bare ``state``
    counts as one — length 22-256 and the URL-safe charset. No decoding
    happens, so ``%20`` or ``+`` is a rejection, not a space.
    """
    segments = _segments(raw_query)
    if segments is None:
        return None
    found: str | None = None
    seen = 0
    for seg in segments:
        key, sep, value = seg.partition("=")
        if key != "state":
            continue
        seen += 1
        if seen > 1:
            return None
        found = value if sep else ""
    if found is None:
        return None
    if not (STATE_MIN <= len(found) <= STATE_MAX):
        return None
    if not all(ch in _STATE_CHARS for ch in found):
        return None
    return found


def _decode_component(raw: str) -> str:
    """Decode one key or value per spec §6 step 6.

    ``+`` becomes a space; a valid ``%XX`` percent-decodes to that byte; a
    malformed ``%`` sequence is left verbatim (a provider that emits one is
    buggy, not hostile, and the consumer still has ``raw_query`` to work
    from). The assembled bytes decode as UTF-8 with ``errors="replace"``, so
    an invalid encoding yields a record rather than an exception on the
    request path.
    """
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "+":
            out.append(0x20)
            i += 1
            continue
        if ch == "%":
            pair = raw[i + 1:i + 3]      # short at the end of the string
            if len(pair) == 2 and all(c in _HEX_DIGITS for c in pair):
                out.append(int(pair, 16))
                i += 3
                continue
        out += ch.encode("utf-8", "replace")
        i += 1
    return out.decode("utf-8", errors="replace")


def _decoded_pairs(raw_query: str) -> list[list[str]]:
    """The ordered decoded view: duplicates, order and bare keys preserved
    (a bare key decodes to ``["key", ""]``). Only the FIRST ``=`` splits, so
    an unencoded ``=`` inside a value survives."""
    pairs: list[list[str]] = []
    for seg in raw_query.split("&"):
        if not seg:
            continue
        key, _sep, value = seg.partition("=")
        pairs.append([_decode_component(key), _decode_component(value)])
    return pairs


# ---------------------------------------------------------------------------
# outcome sampler (spec §6 step 1)
# ---------------------------------------------------------------------------


def _rate_per_min(env: dict[str, str] | None = None) -> int:
    """Read :data:`RATE_ENV`, clamping 0, negatives and junk to the default.

    Same shape as ``casa_core._env_int_or`` but with the clamp the sampler
    needs: this bucket must never be *disabled*, only sized.
    """
    env = env if env is not None else os.environ
    raw = env.get(RATE_ENV)
    if raw is None or raw == "":
        return DEFAULT_RATE_PER_MIN
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d",
                       RATE_ENV, raw, DEFAULT_RATE_PER_MIN)
        return DEFAULT_RATE_PER_MIN
    if value <= 0:
        logger.warning("%s=%d disables log sampling; using default %d",
                       RATE_ENV, value, DEFAULT_RATE_PER_MIN)
        return DEFAULT_RATE_PER_MIN
    return value


class OutcomeSampler:
    """Token bucket over *log emission only*.

    Keys are bounded by construction: one per routed effective name plus one
    shared :data:`UNKNOWN_SENTINEL` key for every unrouted probe, so an
    attacker cycling names cannot grow the bucket dict. The keying is
    invisible externally — it gates ``logger.info``, never the response — so
    it creates no oracle.

    :meth:`should_log` returns ``(emit, suppressed_since_last_emit)``; the
    caller emits a one-line summary when the second value is non-zero, so a
    flood stays diagnosable from the logs it did not print.
    """

    def __init__(self, *, capacity: int | None = None,
                 now: Callable[[], float] | None = None) -> None:
        self._limiter = RateLimiter(
            capacity=capacity if capacity is not None else _rate_per_min(),
            window_s=60.0, now=now,
        )
        self._suppressed: dict[str, int] = {}

    def should_log(self, key: str) -> tuple[bool, int]:
        if self._limiter.check(key).allowed:
            # pop, not read: a key with nothing suppressed leaves no residue,
            # so the dict tracks only keys currently under damping.
            return True, self._suppressed.pop(key, 0)
        self._suppressed[key] = self._suppressed.get(key, 0) + 1
        return False, 0


# ---------------------------------------------------------------------------
# delivery-nudge seam (spec §7; Task 7)
# ---------------------------------------------------------------------------


def _nudge(plugin: str, result_hash: str) -> None:
    """Signal the in-process delivery-nudge worker that a result landed.

    Deliberately a module-level function (not a handler argument) so the
    wiring in Task 7 — ``callback_episodes`` — replaces one seam rather than
    every construction site, and so tests can observe the kick.

    Whatever lands here must stay **non-durable and O(1) on the request
    path**: the episode ledger is a load-scan-rewrite JSON store, and writing
    it here would make request work grow with ledger size. Durability comes
    from the recovery invariant instead — any result without a settled
    episode is re-enqueued by the boot and periodic passes — so a lost kick
    converges rather than dropping the flow.
    """
    return None


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------


def _neutral_redirect() -> web.Response:
    """THE response. Every outcome returns this and only this — one
    constructor, so no early return can drift from another (INV-CB-005).

    Built explicitly rather than via ``HTTPSeeOther`` so the body, charset and
    content type are pinned here instead of tracking aiohttp's exception
    rendering.
    """
    return web.Response(
        status=303, body=b"", content_type="text/plain", charset="utf-8",
        headers={"Location": DONE_PATH, **SECURITY_HEADERS},
    )


def make_done_handler():
    """``GET /callback/done`` — the static neutral page.

    Registered BEFORE the wildcard in ``casa_core`` so it can never fall
    through to it. Effective names always carry the ``plg-`` prefix, so
    ``done`` can never be a real callback name either; the ordering is belt
    and braces, and a redirect-following test pins the no-loop property.
    """
    async def done_handler(request: web.Request) -> web.Response:
        return web.Response(
            text=DONE_PAGE, content_type="text/html", charset="utf-8",
            headers=dict(SECURITY_HEADERS),
        )

    return done_handler


# ---------------------------------------------------------------------------
# the handler
# ---------------------------------------------------------------------------


def make_callback_handler(
    *,
    trigger_registry: Any,
    spool_provider: Callable[[], Any] | None = None,
    sampler: OutcomeSampler | None = None,
    clock: Callable[[], float] = time.time,
):
    """Build the ``GET /callback/{name}`` handler.

    ``spool_provider`` is resolved per request (default
    :func:`callback_spool.get_spool`) so a boot that has not wired the spool
    yet — or a reload that replaced it — degrades to a neutral refusal
    instead of holding a stale reference.
    """
    provider = spool_provider or callback_spool.get_spool
    sampler = sampler if sampler is not None else OutcomeSampler()

    def _emit(key: str, reason: str, cid: str) -> None:
        """One INFO per outcome, sampled. Carries the reason enum, the key
        and the cid — never the state, its hash, or any query content."""
        try:
            emit, suppressed = sampler.should_log(key)
            if not emit:
                return
            if suppressed:
                logger.info(
                    "callback: %d outcome lines suppressed for name=%s "
                    "since the last one (flood damping)", suppressed, key)
            logger.info("callback: outcome=%s name=%s cid=%s",
                        reason, key, cid)
        except Exception:  # noqa: BLE001 — observability must not shape the response
            pass

    async def callback_handler(request: web.Request) -> web.Response:
        cid = "-"
        key = UNKNOWN_SENTINEL
        reason = "write_failed"
        try:
            cid = request.get("cid") or "-"
            key, reason = _process(request, cid)
        except Exception as exc:  # noqa: BLE001
            # A 500 is a differentiated response, i.e. an oracle. The
            # exception TYPE is all that is logged: a traceback renders the
            # exception's repr, and an OSError from the spool carries its
            # operand names — i.e. a state hash, which INV-CB-006 keeps out
            # of the logs along with the state and the query itself.
            logger.warning(
                "callback: handler fault (%s); request data withheld",
                type(exc).__name__)
            key, reason = UNKNOWN_SENTINEL, "write_failed"
        _emit(key, reason, cid)
        return _neutral_redirect()

    def _process(request: web.Request, cid: str) -> tuple[str, str]:
        """Run the flow, returning ``(sampler_key, reason)``. Never returns a
        response: the caller renders the one neutral 303 for every path, so a
        forgotten header here is structurally impossible."""
        received_at = float(clock())
        name = request.match_info.get("name", "")

        # 1. Name lookup (spec §6 step 2). A miss is neutral — deliberately
        #    unlike the webhook route's 404: probes must not be able to tell
        #    an unknown name from an unknown state.
        entry = trigger_registry.get_callback(name)
        plugin = entry.get("plugin") if isinstance(entry, dict) else None
        if not isinstance(plugin, str) or not plugin:
            # Includes a malformed overlay entry: the name may be routed, but
            # without a plugin there is no spool to deposit into, and pairing
            # `unknown_name` with the sentinel unconditionally is what makes
            # the INV-CB-006 rule mechanical.
            return UNKNOWN_SENTINEL, "unknown_name"
        effective = entry.get("effective")
        if not isinstance(effective, str) or not effective:
            effective = name

        # 2. State extraction from the raw query (spec §6 step 3).
        raw_query = request.rel_url.raw_query_string
        state = _extract_state(raw_query)
        if state is None:
            return effective, "bad_state"

        spool = provider()
        if spool is None:
            logger.warning("callback: no spool wired; refusing neutrally")
            return effective, "write_failed"

        # 3. Atomic claim (spec §6 steps 4-5). Replay, expired and
        #    never-minted all lose identically inside claim().
        try:
            claim = spool.claim(plugin, callback_spool.state_hash(state),
                                now=received_at)
        except (OSError, ValueError):
            # ValueError = an unsafe plugin name reached the overlay; OSError
            # = a filesystem fault. Neither may become a 500.
            logger.warning("callback: claim failed for name=%s", effective)
            return effective, "write_failed"
        if claim is None:
            return effective, "no_pending"

        # 4. Result record + publish-once (spec §6 step 6). The decode runs
        #    only for a won claim, so a probe flood never pays for it.
        record = {
            "v": 1,
            "plugin": plugin,
            "effective": effective,
            "received_at": received_at,
            "raw_query": raw_query,
            "query": _decoded_pairs(raw_query),
        }
        try:
            published = spool.publish_result(claim, record)
            failure = "result_exists"
        except Exception:  # noqa: BLE001
            logger.warning("callback: result publish raised for name=%s",
                           effective)
            published, failure = False, "write_failed"
        if not published:
            # The claim is dropped either way: the state stays consumed
            # (INV-CB-002), and the credential was never written to results/.
            # publish_result already removes the claim on the EEXIST anomaly,
            # so this is a no-op there; on a staging failure it converts the
            # spool's "leave it for recovery to restore" into a fail-closed
            # single-use consumption, which is the safer direction for an
            # unauthenticated endpoint.
            try:
                spool.discard_claim(claim)
            except (OSError, ValueError):
                logger.warning("callback: claim discard failed for name=%s",
                               effective)
            return effective, failure

        # 5. Nudge kick (spec §7) — non-durable, and never load-bearing for
        #    the response: the result is already published and durable.
        try:
            _nudge(plugin, claim.state_hash)
        except Exception:  # noqa: BLE001
            logger.warning("callback: delivery nudge kick failed for name=%s",
                           effective)
        return effective, "ok"

    return callback_handler
