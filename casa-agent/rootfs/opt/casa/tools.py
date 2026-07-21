"""In-process MCP tools for the Casa framework."""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable

if TYPE_CHECKING:
    from trigger_registry import TriggerRegistry

from executor_registry import ExecutorRegistry
from plugin_env_conf import set_entry as _set_env_entry  # noqa: F401 — available for future use
from system_requirements.orchestrator import install_requirements, OrchestrationError
from system_requirements.manifest import (
    add_plugin_entry as add_manifest, remove_plugin_entry as remove_manifest,
)
import plugin_registry
import plugin_store
from plugin_grants import (
    declared_tools_for_resolution, grants_for_resolution, grants_for_resolved,
    make_fail_closed_can_use_tool, mcp_json_malformed,
    required_env_vars_for_resolved,
)
from authz_grants import CHALLENGES, GRANTS, normalize_role
from delegated_memory import delegated_recall, retain_delegated
from semantic_memory import RecallUnavailable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SdkMcpTool,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from claude_runtime import CLAUDE_CLI_PATH
from media_policies import MEDIA_POLICIES
import plugin_outbox
from error_kinds import _classify_error
from mcp_registry import McpServerRegistry
import sdk_logging
import specialist_limits
from tokens import extract_usage
from drivers.brief import normalize_brief, render_brief_task, validate_brief
from engagement_registry import TerminalPreconditionFailed, EngagementRecord, EngagementRegistry
from specialist_registry import (
    DelegationComplete,
    DelegationRecord,
    SpecialistRegistry,
)
from job_registry import (
    DeliveryState,
    ExecutionState,
    JobAuthorizationError,
    JobFailure,
    JobRouteCapacityError,
    JobTransitionError,
    VoiceJob,
)
from voice_job_result import (
    VOICE_JOB_OUTPUT_FORMAT,
    VoiceJobResultError,
    parse_voice_job_result,
    spoken_text_for,
    voice_identity_clearance,
)

logger = logging.getLogger(__name__)

# Plan 4a.1 §8: workspace retention for claude_code driver engagements.
_ENGAGEMENTS_ROOT = "/data/engagements"
_WORKSPACE_RETENTION_DAYS = 7

# Module-level references, initialized via init_tools()
_channel_manager: ChannelManager | None = None
_bus: MessageBus | None = None
_specialist_registry: SpecialistRegistry | None = None
_mcp_registry: McpServerRegistry | None = None
_agent_role_map: dict[str, "AgentConfig"] = {}  # merged residents + specialists
_trigger_registry: "TriggerRegistry | None" = None
_engagement_registry: EngagementRegistry | None = None
_executor_registry: "ExecutorRegistry | None" = None
_agent_registry = None  # AgentRegistry | None
_runtime = None  # CasaRuntime | None — set by init_tools(runtime=...)
# Task 6 (spec §4.6): specialist concurrency cap + per-role telemetry. Both
# default to None (no limiting / no telemetry) so callers that don't wire
# them — including every pre-Task-6 test — keep the old unbounded behaviour.
_specialist_limiter: "specialist_limits.SpecialistLimiter | None" = None
_specialist_telemetry: "specialist_limits.SpecialistTelemetry | None" = None
_voice_job_route_cap = 5
engagement_var: ContextVar[EngagementRecord | None] = ContextVar(
    "engagement_var", default=None,
)


def init_tools(
    channel_manager,
    bus,
    specialist_registry,
    mcp_registry=None,
    *,
    agent_role_map: dict | None = None,
    agent_registry=None,
    trigger_registry=None,
    engagement_registry=None,
    executor_registry=None,
    runtime=None,                         # NEW — Task C.1
    specialist_limiter=None,              # Task 6 (spec §4.6)
    specialist_telemetry=None,            # Task 6 (spec §4.6)
    voice_job_route_cap: int = 5,
) -> None:
    """Initialize module-level references used by tool implementations.

    ``mcp_registry`` is required for specialist MCP-tool resolution at
    delegation time. ``trigger_registry`` is required for the
    ``get_schedule`` tool; callers that don't pass it get a degraded
    tool that returns "not initialized" on every call.
    Accepts ``None`` for legacy callers that don't pass
    it (the `_build_specialist_options` code path degrades to empty MCP
    servers — the specialist still runs but with only built-in tools).

    ``agent_role_map`` is a merged dict of role→AgentConfig covering both
    residents and specialists. It is REQUIRED for delegation authorization
    (A1): the ACL keys the caller's declared delegates off this map, so an
    omitted or empty map means no caller can be authorized to delegate at
    all. Only target *resolution* retains the ``specialist_registry``
    fallback for back-compat.

    ``runtime`` is the CasaRuntime container. Optional during migration
    (Task C.1); becomes required once all callsites use it (Task C.4).

    ``specialist_limiter``/``specialist_telemetry`` (Task 6, spec §4.6):
    optional concurrency cap + per-role telemetry. ``None`` (the default)
    disables both — ``_prelaunch`` skips the concurrency gate entirely and
    ``_run_delegated_agent`` skips cost/usage recording, matching the
    pre-Task-6 unbounded behaviour for every caller that doesn't wire them.
    """
    global _channel_manager, _bus, _specialist_registry, _mcp_registry, \
        _agent_role_map, _agent_registry, _trigger_registry, \
        _engagement_registry, _executor_registry, _runtime, \
        _specialist_limiter, _specialist_telemetry, \
        _voice_job_route_cap  # noqa: PLW0603
    if (isinstance(voice_job_route_cap, bool)
            or not isinstance(voice_job_route_cap, int)
            or not 1 <= voice_job_route_cap <= 20):
        raise ValueError("voice_job_route_cap must be an integer from 1 to 20")
    _channel_manager = channel_manager
    _bus = bus
    _specialist_registry = specialist_registry
    _mcp_registry = mcp_registry
    _agent_role_map = dict(agent_role_map or {})
    _agent_registry = agent_registry
    _trigger_registry = trigger_registry
    _engagement_registry = engagement_registry
    _executor_registry = executor_registry
    _runtime = runtime
    _specialist_limiter = specialist_limiter
    _specialist_telemetry = specialist_telemetry
    _voice_job_route_cap = voice_job_route_cap


def sync_agent_role_map(runtime: Any) -> None:
    """Rebuild the delegation role map from live runtime state.

    Called by the reload handlers after an agent/agents swap. Without
    this, the map stays a boot-time snapshot and ``delegate_to_agent``
    keeps resolving PRE-reload AgentConfigs — a specialist
    ``tools.allowed`` grant stays inert for every fresh delegation until
    a full add-on restart, even though ``casa_reload`` reports ok (P-6,
    live run 2026-07-11). Overlapping roles keep the resident entry and
    warn instead of raising: a reload must not brick on a collision
    boot would have rejected.
    """
    global _agent_role_map  # noqa: PLW0603
    residents = dict(getattr(runtime, "role_configs", {}) or {})
    registry = getattr(runtime, "specialist_registry", None)
    specialists = dict(registry.all_configs()) if registry is not None else {}
    merged = dict(residents)
    for name, cfg in specialists.items():
        if name in merged:
            logger.warning(
                "sync_agent_role_map: role %r exists in both tiers — "
                "resident entry wins", name,
            )
            continue
        merged[name] = cfg
    _agent_role_map = merged


@tool(
    "send_message",
    "Send a message to a user through a communication channel.",
    {"message": str, "channel": str},
)
async def send_message(args: dict) -> dict:
    """Send a message through a named channel."""
    message = args.get("message", "")
    channel = args.get("channel", "telegram")

    # Release A / Layer 1 egress binding: an UNTRUSTED webhook turn
    # (channel=="webhook" and not an explicit `invoke`) may notify only the
    # operator's Telegram surface — the caller-selected channel is ignored so
    # third-party content can't be relayed to arbitrary channels (confused
    # deputy). Trusted /invoke and all non-webhook origins are unaffected.
    _origin = _snapshot_origin()
    if _origin.get("channel") == "webhook" and _origin.get("_origin_route") != "invoke":
        channel = "telegram"

    if _channel_manager is None:
        return {"content": [{"type": "text", "text": "Error: tools not initialized"}]}

    ch = _channel_manager.get(channel)
    if ch is None:
        return {"content": [{"type": "text", "text": f"Error: channel '{channel}' not found"}]}

    await ch.send(message, {})
    return {"content": [{"type": "text", "text": f"Message sent via {channel}."}]}


# ---------------------------------------------------------------------------
# send_media — reusable agent->channel media delivery (v0.73.0, spec §3).
# ---------------------------------------------------------------------------

_CAPTION_MAX = 1024
_FILENAME_MAX_BYTES = 255


def _validate_delivery_filename(name: str, kind: str) -> str | None:
    """Return *name* if it is a safe delivered filename for *kind*, else None.
    basename-only, control-free, <=255 bytes, extension (case-insensitive) in the
    kind's allowlist."""
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        return None
    if any(ord(c) < 0x20 for c in name):
        return None
    if len(name.encode("utf-8")) > _FILENAME_MAX_BYTES:
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in MEDIA_POLICIES[kind].extensions:
        return None
    return name


async def _classify_send(ch, content, kind, filename, origin, caption) -> dict:
    """Attempt the channel send and classify the outcome into a payload dict.
    Never raises a send/channel exception (the caller's finally still cleans up
    the claim)."""
    from telegram.error import (
        BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
    )
    try:
        await ch.send_media(content, kind, filename, context=origin, caption=caption)
        return {"status": "ok", "kind_error": None, "kind": kind,
                "filename": filename,
                "summary": f"delivered {kind} {filename!r}"}
    except NotImplementedError:
        return {"status": "error", "kind_error": "unsupported_channel", "kind": kind,
                "message": "channel cannot deliver media"}
    except (BadRequest, Forbidden) as exc:
        return {"status": "error", "kind_error": "rejected", "kind": kind,
                "message": f"telegram refused: {type(exc).__name__}"}
    except (TimedOut, NetworkError, RetryAfter) as exc:
        return {"status": "error", "kind_error": "delivery_uncertain", "kind": kind,
                "message": f"delivery uncertain: {type(exc).__name__}; not retried"}
    except TelegramError as exc:
        return {"status": "error", "kind_error": "delivery_uncertain", "kind": kind,
                "message": f"delivery uncertain: {type(exc).__name__}; not retried"}
    except RuntimeError:
        return {"status": "error", "kind_error": "channel_unavailable", "kind": kind,
                "message": "channel not started"}


@tool(
    "send_media",
    "Deliver a media file (document/photo/audio/voice) from the plugin outbox "
    "to the user over the originating channel. Pass the outbox path returned by "
    "a producer tool; the bytes never enter the model context.",
    {"type": "object",
     "properties": {
         "path": {"type": "string"},
         "kind": {"type": "string", "enum": list(MEDIA_POLICIES)},
         "caption": {"type": "string"},
         "filename": {"type": "string"}},
     "required": ["path", "kind"]},
)
async def send_media(args: dict) -> dict:
    try:
        # 0. Validate arguments up front (the Casa MCP forwarding path does NOT
        #    enforce the JSON schema). `kind` MUST be a str before the dict
        #    membership test — `[] in MEDIA_POLICIES` raises TypeError.
        path = args.get("path")
        kind = args.get("kind")
        caption = args.get("caption")
        filename_arg = args.get("filename")
        if not isinstance(path, str) or not path:
            return _result({"status": "error", "kind_error": "invalid_arguments",
                            "message": "path must be a non-empty string"})
        if not isinstance(kind, str) or kind not in MEDIA_POLICIES:
            return _result({"status": "error", "kind_error": "invalid_arguments",
                            "message": f"unknown kind {kind!r}"})
        if caption is not None and not isinstance(caption, str):
            return _result({"status": "error", "kind_error": "invalid_arguments",
                            "message": "caption must be a string"})
        if filename_arg is not None and not isinstance(filename_arg, str):
            return _result({"status": "error", "kind_error": "invalid_arguments",
                            "message": "filename must be a string"})

        # 1. Resolve target chat — NO default fallback. Active engagement first
        #    (HTTP engagement handlers bind engagement_var, not origin_var); a
        #    delegated specialist turn carries the origin via origin_var.
        eng = engagement_var.get(None)
        origin = dict(eng.origin) if eng is not None else _snapshot_origin()
        if not origin:
            return _result({"status": "error", "kind_error": "no_origin",
                            "message": "no turn origin"})
        if origin.get("channel") != "telegram":
            return _result({"status": "error", "kind_error": "invalid_origin",
                            "message": f"origin channel {origin.get('channel')!r} "
                                       "cannot receive media"})
        raw_chat = origin.get("chat_id")
        if isinstance(raw_chat, bool):
            chat_id = None
        elif isinstance(raw_chat, int):
            chat_id = raw_chat
        elif isinstance(raw_chat, str):
            try:
                chat_id = int(raw_chat)
            except ValueError:
                chat_id = None
        else:
            chat_id = None
        if not chat_id:
            return _result({"status": "error", "kind_error": "invalid_origin",
                            "message": "no numeric, nonzero chat_id in origin"})
        origin["chat_id"] = chat_id  # normalise to the validated int for dispatch

        # 2. Resolve channel (fail fast, before claiming).
        if _channel_manager is None:
            return _result({"status": "error", "kind_error": "channel_unavailable",
                            "message": "tools not initialised"})
        ch = _channel_manager.get(origin.get("channel", "telegram"))
        if ch is None:
            return _result({"status": "error", "kind_error": "channel_unavailable",
                            "message": "channel not registered"})
        if caption is not None and len(caption) > _CAPTION_MAX:
            caption = caption[:_CAPTION_MAX]

        outbox = plugin_outbox.get_outbox()
        if outbox is None:
            return _result({"status": "error", "kind_error": "internal_error",
                            "message": "outbox not initialised"})

        # 3. Claim FIRST — the path guard (outside_outbox / missing / bad
        #    basename) runs BEFORE filename validation, so an out-of-outbox path
        #    reports `outside_outbox`, not `bad_name`. Pre-claim errors own
        #    nothing and return directly.
        try:
            claim = await asyncio.to_thread(outbox.claim, path)
        except plugin_outbox.OutboxError as exc:
            return _result({"status": "error", "kind_error": exc.kind,
                            "message": str(exc)})

        # 4. Claim is OWNED — remove it on EVERY outcome (including bad_name and
        #    every guard/capture/send error).
        cleanup_warning = None
        try:
            try:
                # Delivered filename: an explicit arg (incl. "" -> bad_name) else
                # the path basename; validated against the kind's extensions.
                candidate = (filename_arg if filename_arg is not None
                             else os.path.basename(path))
                filename = _validate_delivery_filename(candidate, kind)
                if filename is None:
                    payload = {"status": "error", "kind_error": "bad_name",
                               "kind": kind,
                               "message": "filename not valid for the media kind"}
                else:
                    content = await asyncio.to_thread(outbox.capture, claim, kind)
                    payload = await _classify_send(
                        ch, content, kind, filename, origin, caption)
            except plugin_outbox.OutboxError as exc:
                payload = {"status": "error", "kind_error": exc.kind,
                           "kind": kind, "message": str(exc)}
        finally:
            try:
                await asyncio.to_thread(outbox.remove_claim, claim)
            except Exception as ce:  # noqa: BLE001 — cleanup best-effort
                cleanup_warning = f"claim cleanup failed: {type(ce).__name__}"
                logger.warning("send_media claim cleanup failed for %s: %s",
                               claim, ce)
        if cleanup_warning and payload.get("status") == "ok":
            payload["cleanup_warning"] = cleanup_warning
        return _result(payload)
    except Exception:  # noqa: BLE001 — nothing escapes; worst case is a result
        logger.exception("send_media: unexpected failure")
        return _result({"status": "error", "kind_error": "internal_error",
                        "message": "unexpected failure"})


# ---------------------------------------------------------------------------
# react — lightweight emoji ack on the LATEST operator message (R5, v0.89.0)
#
# Stateless, order-safe, active-record-gated. Reads the current-inbound target
# recorded by TelegramChannel.handle_update (keyed by engagement_id) and sets
# ONE emoji reaction on it. The correctness guarantee is the NON-LIVE
# REJECTION: internal_handlers binds engagement_var to an ACTIVE record only,
# so a terminated/missing engagement yields None here and a stale target can
# never produce a reaction. NEVER falls back to eng.origin; NEVER raises (a
# raise would abort the turn) — every failure is a soft result.
# ---------------------------------------------------------------------------


@tool(
    "react",
    "Drop a lightweight emoji reaction (e.g. 👍 done, 👀 on it) on the "
    "operator's LATEST message in this engagement topic. A non-decisional, "
    "non-blocking acknowledgement — NEVER an approval or an answer to an "
    "`ask`; route every decision through the verdict broker / `ask` instead.",
    {"type": "object",
     "properties": {"emoji": {"type": "string"}},
     "required": ["emoji"]},
)
async def react(args: dict) -> dict:
    try:
        emoji = args.get("emoji")
        if not isinstance(emoji, str) or not emoji.strip():
            return _result({"status": "error", "kind": "invalid_arguments",
                            "message": "emoji must be a non-empty string"})

        # Non-live rejection = the correctness guarantee. engagement_var is
        # bound ONLY for an ACTIVE record (internal_handlers), so a terminated
        # / missing engagement yields None → fail soft. NEVER use eng.origin.
        eng = engagement_var.get(None)
        if eng is None or getattr(eng, "status", None) != "active":
            return _result({"status": "no_current_inbound",
                            "message": "no active engagement to react in"})

        # The current-inbound target lives on the TelegramChannel (the only
        # channel with engagement topics). No target = restart / no inbound
        # yet / cleared → soft no-op.
        ch = _channel_manager.get("telegram") if _channel_manager is not None else None
        if ch is None or not hasattr(ch, "get_current_inbound"):
            return _result({"status": "no_current_inbound",
                            "message": "no channel able to set reactions"})
        target = ch.get_current_inbound(eng.id)
        if target is None:
            return _result({"status": "no_current_inbound",
                            "message": "no current inbound message recorded"})
        chat_id, _topic_id, message_id = target

        from telegram.error import (
            BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError,
            TimedOut,
        )
        try:
            await ch.set_reaction(chat_id, message_id, emoji)
        except (BadRequest, Forbidden) as exc:
            return _result({"status": "error", "kind": "invalid_emoji",
                            "message": f"telegram refused reaction: "
                                       f"{type(exc).__name__}"})
        except (TimedOut, NetworkError, RetryAfter, TelegramError) as exc:
            return _result({"status": "error", "kind": "delivery_uncertain",
                            "message": f"reaction delivery uncertain: "
                                       f"{type(exc).__name__}; not retried"})
        except RuntimeError:
            return _result({"status": "no_current_inbound",
                            "message": "channel not started"})
        return _result({"status": "ok", "reaction": emoji,
                        "message_id": message_id,
                        "summary": f"reacted {emoji} on the latest operator "
                                   "message"})
    except Exception:  # noqa: BLE001 — never abort a turn; worst case is a result
        logger.exception("react: unexpected failure")
        return _result({"status": "error", "kind": "internal_error",
                        "message": "unexpected failure"})


# ---------------------------------------------------------------------------
# ask_user — resident DM button questions (v0.76.0 W5b, A:§2)
#
# Two-turn, detached: REGISTER -> POST -> RETURN. The tool never awaits the
# operator's tap — it registers a `resident_ask` broker request scoped
# `dm:<chat_id>`, posts the inline keyboard, and returns `awaiting_user`
# immediately. The operator's tap (or a same-DM typed reply, or /new, or a
# timeout, or Casa shutdown) resolves the request later; the broker finish
# hook installed here is the SINGLE owner of the post-commit keyboard edit +
# synthetic-turn dispatch (r2-B1/r3-B1 single-owner contract).
# ---------------------------------------------------------------------------

# W5 ask contract limits (design §W5, shared by ask_user — A:§2).
_ASK_QUESTION_MAX = 1024
_ASK_OPTIONS_MIN = 2
_ASK_OPTIONS_MAX = 8
_ASK_OPTION_LABEL_MAX = 48
_ASK_TIMEOUT_DEFAULT = 300.0
_ASK_TIMEOUT_MIN = 30.0
_ASK_TIMEOUT_MAX = 570.0

ASK_USER_SCHEMA = {
    # r1-B3: explicit JSON Schema — only question+options are required;
    # timeout_s is optional (defaults to _ASK_TIMEOUT_DEFAULT, clamped).
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "timeout_s": {"type": "number"},
    },
    "required": ["question", "options"],
}


def _ask_user_validate(args: dict) -> tuple[str | None, list[str] | None, float | None, str | None]:
    """Validate ask_user args against the W5 ask contract.

    Returns ``(question, options, timeout_s, error_message)``. On success
    ``error_message`` is ``None`` and the other three are populated
    (``timeout_s`` already clamped to ``[_ASK_TIMEOUT_MIN,
    _ASK_TIMEOUT_MAX]``). On failure only ``error_message`` is set.
    """
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return None, None, None, "question must be a non-empty string"
    if len(question) > _ASK_QUESTION_MAX:
        return None, None, None, (
            f"question must be at most {_ASK_QUESTION_MAX} characters"
        )

    options = args.get("options")
    if not isinstance(options, list):
        return None, None, None, "options must be a list of strings"
    if not (_ASK_OPTIONS_MIN <= len(options) <= _ASK_OPTIONS_MAX):
        return None, None, None, (
            f"options must have between {_ASK_OPTIONS_MIN} and "
            f"{_ASK_OPTIONS_MAX} entries"
        )
    for opt in options:
        if not isinstance(opt, str) or not opt.strip():
            return None, None, None, "every option must be a non-empty string"
        if len(opt) > _ASK_OPTION_LABEL_MAX:
            return None, None, None, (
                f"option labels must be at most {_ASK_OPTION_LABEL_MAX} "
                "characters"
            )
    if len(set(options)) != len(options):
        return None, None, None, "options must be unique"

    timeout_arg = args.get("timeout_s")
    if timeout_arg is None:
        timeout_s = _ASK_TIMEOUT_DEFAULT
    else:
        if isinstance(timeout_arg, bool) or not isinstance(timeout_arg, (int, float)):
            return None, None, None, "timeout_s must be a number"
        timeout_s = float(timeout_arg)
    timeout_s = max(_ASK_TIMEOUT_MIN, min(_ASK_TIMEOUT_MAX, timeout_s))

    return question, list(options), timeout_s, None


@tool(
    "ask_user",
    "Ask the operator a multiple-choice question with tappable buttons in "
    "their DM. Two-turn: returns awaiting_user immediately; the answer "
    "arrives as the user's next message. NOT an authorization mechanism.",
    ASK_USER_SCHEMA,
)
async def ask_user(args: dict) -> dict:
    question, options, timeout_s, err = _ask_user_validate(args)
    if err is not None:
        return _result({"status": "error", "kind": "invalid_arguments",
                        "message": err})

    from provenance import strict_positive_id, turn_provenance

    prov = turn_provenance()
    if prov.transport not in ("dm", "button") or prov.execution != "direct":
        return _result({
            "status": "error", "kind": "unsupported_origin",
            "message": (
                "ask_user requires a direct, genuine inbound-DM turn "
                f"(got transport={prov.transport!r} execution={prov.execution!r})"
            ),
        })

    origin = _snapshot_origin()
    chat_id = strict_positive_id(origin.get("chat_id"))
    operator_id = strict_positive_id(origin.get("user_id"))
    if chat_id is None or operator_id is None:
        return _result({
            "status": "error", "kind": "unsupported_origin",
            "message": "ask_user requires a valid chat_id/user_id origin",
        })

    if _channel_manager is None:
        return _result({"status": "error", "kind": "delivery_failed",
                        "message": "tools not initialized"})
    channel = _channel_manager.get(origin.get("channel", "telegram"))
    if channel is None:
        return _result({"status": "error", "kind": "delivery_failed",
                        "message": "channel not available"})

    from verdict_broker import BROKER
    from channels.channel_handlers import render_ask_body

    rid = uuid.uuid4().hex
    target_role = origin.get("role")
    scope = f"dm:{chat_id}"
    # v0.81.0 (W-R3b): the DM MESSAGE body carries the FULL options VERBATIM +
    # 1-based numbered (the SAME single-source renderer the engagement asks use),
    # while the buttons carry only short number-prefixed labels. Both the post
    # and the settle edits derive from this one ``body`` so they can never
    # disagree (mirrors the engagement single-source discipline).
    body = render_ask_body(None, question, list(options))
    # Static meta BEFORE post (register() shallow-copies whatever dict we
    # pass, so the complete dict is supplied up front rather than mutated
    # after the fact). No `on_commit_sync` — plain asks record nothing at
    # commit time (that's the authz challenge's job, Task 5+).
    req, _created = BROKER.register(
        namespace="resident_ask", scope=scope, request_id=rid,
        timeout_s=timeout_s, detached=True, supersede=True,
        meta={
            "options": list(options),
            "chat_id": chat_id,
            "operator_id": operator_id,
            "target_role": target_role,
            "kind": "ask",
            "_scope": scope,
        },
    )

    async def _post():
        return await channel.post_dm_keyboard(
            chat_id=chat_id, request_id=rid, text=body, options=list(options),
            short_labels=True,
        )

    def _finish_factory(message_id: int):
        async def _finish(outcome: dict) -> None:
            # r2-B1/r3-B1 single-owner finish hook: on `answered` edit the
            # keyboard to the answered label FIRST, then dispatch the
            # synthetic continuation, then overwrite with a visible failure
            # text ONLY if dispatch failed. no_answer/cancelled -> expired.
            if outcome.get("outcome") != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"{body}\n\n(this question has expired)",
                )
                return
            idx = outcome["option_index"]
            chosen = options[idx]
            await channel.edit_dm_message(
                chat_id, message_id, f"{body}\n\nAnswered: {chosen}",
            )
            ok = await channel._dispatch_button_continuation(
                chat_id=chat_id, user_id=operator_id, target_role=target_role,
                request_id=rid, text=f"[button answer to {rid}]: {chosen}",
            )
            if not ok:
                await channel.edit_dm_message(
                    chat_id, message_id,
                    "answer received but delivery failed — please type it",
                )
        return _finish

    await BROKER.ensure_posted(req, _post, _finish_factory)

    if "message_id" not in req.meta:
        # _run_setup already unregistered the request on a post failure
        # (post() returned None or raised) — no dangling pending record.
        return _result({
            "status": "error", "kind": "delivery_failed",
            "message": "could not deliver the question to the operator",
        })
    return _result({"status": "awaiting_user", "request_id": rid})


# ---------------------------------------------------------------------------
# delegate_to_agent — Phase 3.1
# ---------------------------------------------------------------------------


# Phase 3.1: sync-mode wait ceiling. 60 s per spec §6.3. Exposed as a
# module-level constant so tests can monkeypatch to drive the degraded
# path without waiting a minute.
_SYNC_WAIT_TIMEOUT_S: float = 60.0

# Phase 3.5 (Plan 4b): max delegation depth. depth=0 is a direct call from
# a resident; depth>=1 is a delegated turn. Cap at 1 to prevent chains.
_MAX_DELEGATION_DEPTH: int = 1

# A4: voice turn budget. Voice has no follow-up channel to deliver a LATER
# completion notification on, so a synchronous specialist wait on voice is
# bounded by the turn's own deadline (channels/voice/channel.py's
# _voice_turn_budget_s(), propagated as origin["voice_deadline"]) instead
# of the general _SYNC_WAIT_TIMEOUT_S ceiling. _VOICE_FALLBACK_RESERVE_S is
# held back from the wait so there is always time left to cancel, tear
# down, and speak the deadline_exceeded response before the client/infra
# timeout fires. _VOICE_TEARDOWN_BOUND_S bounds how long we wait for a
# cancelled specialist task to actually finish unwinding.
_VOICE_FALLBACK_RESERVE_S: float = 5.0
_VOICE_TEARDOWN_BOUND_S: float = 2.0


# G-2 hotfix (v0.33.1): defensive reload guard.
#
# v0.33.0's doctrine fix (invert canonical order to commit -> reload ->
# emit_completion) failed to converge live (verify cid `a9313680`
# 2026-05-01 11:39:57Z): model still skipped the reload tool_use after
# reading the new completion.md + reload.md and emitted the same false-
# positive narration ("Reload triggered to apply") without actually
# calling casa_reload. Exploration2 G-2 reproduced unchanged.
#
# Per kickoff: "Recommend doing the doctrine fix first; add the
# defensive guard only if doctrine fix doesn't converge after 2
# retries." Reverification confirmed non-convergence on the first
# active retry — we don't have budget to retry-and-pray, and the
# operator-visible failure mode (artifact COMMITTED BUT INERT) is
# severe.
#
# Mechanism: track per-engagement "did we still owe a reload at
# emit_completion time?" via a module-level set, populated by
# config_git_commit when the SHA points at a real commit, drained by
# the reload tools, and inspected at emit_completion entry. If still
# pending, force-call ``casa_reload`` (the safe-default — hard reload
# is always correct, just slower than soft for triggers-only changes)
# and emit a WARNING citing the engagement id.
_ENGAGEMENTS_PENDING_RELOAD: set[str] = set()

# H-1 fix (v0.34.0): casa_reload's Supervisor restart races the SDK
# subprocess — the POST returns in <1s but Supervisor's container kill
# arrives ~13s later, cancelling the SDK before the model can call
# emit_completion. Result: engagement stuck status=active, no user-DM
# completion message, _finalize_engagement never runs.
#
# Mechanism: when ``casa_reload`` is called inside an active engagement
# (engagement_var bound), it does NOT POST to Supervisor. Instead it
# adds engagement.id to this set and returns immediately. The actual
# Supervisor POST is performed at the end of _finalize_engagement —
# AFTER the bus-message write + engagement-summary retain land — so the
# user-DM "Done" relay survives the addon kill. Out-of-engagement
# casa_reload calls (operator-triggered via /invoke) still POST
# inline since there is no engagement to wait for.
_ENGAGEMENTS_DEFERRED_HARD_RELOAD: set[str] = set()


def _snapshot_origin() -> dict:
    """Copy the current origin at handler entry (AR-2, pooling spec §Q7).

    With a pooled SDK client, ``origin_var`` in the read-task context is
    bound to a MUTABLE holder rewritten at each turn start. Any handler
    that keeps the reference across an await that can outlive its turn
    (delegations most of all) would read the NEXT turn's origin — for
    the delegation retain gate that is a clearance violation, not just
    misattribution. Snapshot once, at entry, before any await."""
    import agent as agent_mod
    return dict(agent_mod.origin_var.get(None) or {})


def _result(payload: dict, *, is_error: bool | None = None) -> dict:
    """Wrap a JSON-serializable payload as the tool's MCP content.

    F-7 (v0.32.0): when ``payload["status"] == "error"`` (or the caller
    explicitly passes ``is_error=True``), set ``is_error: True`` on the
    envelope. Without this flag the SDK's ``ToolResultBlock`` defaults
    ``is_error=False`` and ``sdk_logging.log_tool_result`` emits
    ``ok=True`` even for failures — operators reading turn telemetry
    would think a registry-rejected ``engage_executor`` actually spawned.
    Auto-detection via ``payload["status"]`` keeps the existing call sites
    untouched while making every error path consistently observable.

    O-1 (v0.37.9): also recognise ``payload["ok"] is False`` so the
    install/uninstall plugin envelopes (which use ``{"ok": False,
    "error": ...}`` instead of ``{"status": "error", ...}``) surface as
    MCP errors too. Live evidence (2026-05-14 P29.1 cid ``52240634``):
    ``tool_result name=install_casa_plugin ok=True ms=12594`` for a
    ``plugin_not_in_marketplace`` failure — telemetry reported the
    failure as success, contradicting F-7's intent.

    The dict key MUST be ``is_error`` (snake_case): the Anthropic Agent
    SDK's MCP server adapter reads
    ``result.get("is_error", False)`` (see
    ``claude_agent_sdk/__init__.py:512``) and converts to the MCP wire
    field ``isError`` itself. Passing ``isError`` here gets silently
    dropped on the way to the model.
    """
    if is_error is None:
        is_error = (
            payload.get("status") == "error"
            or payload.get("ok") is False
        )
    envelope: dict = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        envelope["is_error"] = True
    return envelope


def _engagement_unavailable_result(origin: dict) -> dict:
    """R-2 (v0.69.7): the engagement supergroup/topic check failed — return an
    accurate error keyed on origin. A non-Telegram origin can't start an
    engagement at all (only the telegram channel carries the supergroup/topic
    machinery; full non-Telegram origination is backlogged), which is a
    different problem from a genuine telegram-side misconfiguration. The old
    single message ("set telegram_engagement_supergroup_id …") misdiagnosed the
    non-Telegram case as an add-on config gap."""
    origin_channel = origin.get("channel", "telegram")
    if origin_channel != "telegram":
        return _result({
            "status": "error", "kind": "engagement_wrong_origin",
            "message": (
                "engagements can only be initiated from Telegram; this request "
                f"originated from {origin_channel!r}. Non-Telegram origination "
                "is not supported yet."
            ),
        })
    return _result({
        "status": "error", "kind": "engagement_not_configured",
        "message": ("set telegram_engagement_supergroup_id in addon options "
                    "and verify the bot has can_manage_topics"),
    })


# Q-1 (v0.69.8, operator decision 2026-07-12): the SDK meta-tools that spawn a
# sub-agent. They bypass `allowed_tools` AND the v0.68.0 fail-closed
# can_use_tool callback (empirically: the CLI does not consult the callback for
# them), so a restricted agent could spawn a sub-agent that reaches a broad
# default toolset its own allowlist excludes. `disallowed_tools` IS enforced by
# the CLI (removes them from the surface), so specialists — and butler, via its
# runtime.yaml — are denied these. NOT `ToolSearch` (operator kept it: it is
# the deferred-tool-load mechanism and cannot spawn a sub-agent on its own).
_SUBAGENT_SPAWN_TOOLS = ("Agent", "Task")


def _with_subagent_spawn_disallowed(disallowed) -> list[str]:
    """Return ``disallowed`` (any iterable) plus the sub-agent-spawn tools,
    de-duplicated, order-stable."""
    out = list(disallowed)
    for t in _SUBAGENT_SPAWN_TOOLS:
        if t not in out:
            out.append(t)
    return out


def _resolution_from_recorded(plugin_artifacts) -> "plugin_registry.ResolutionResult":
    """Sol round-3 H7b: rebuild a ResolutionResult from an engagement's RECORDED
    plugin_artifacts (§3.8) so a resumed specialist loads exactly what it started
    with (paths AND derived grants), never re-resolving current assignments.
    Fails closed (raises) if a recorded artifact path is gone/corrupt/identity-
    mismatched — those are pre-v0.76 failure modes, unchanged here.

    A:§3.7 (r2-B6/r3-4): a malformed ``casa.protectedTools`` in one RECORDED
    artifact's manifest is PER-PLUGIN degradation, not a whole-resume abort —
    that one plugin is excluded (recorded as an issue) while every other
    recorded artifact, however many, still resolves and loads normally."""
    from plugin_registry import PluginIssue, ResolutionResult, ResolvedPlugin
    plugins = []
    issues = []
    for pa in plugin_artifacts or ():
        path = pa.get("path") if isinstance(pa, dict) else None
        if not path or not os.path.isdir(path):
            raise RuntimeError(
                f"cannot resume: recorded plugin artifact missing "
                f"(plugin_artifact_missing): {pa!r}")
        # Sol round-4: DEEP-validate before resume — a tampered/corrupt pinned
        # artifact, or one whose metadata identity no longer matches the recorded
        # artifact_id, must fail the resume closed (not silently load).
        if not plugin_store.validate_artifact(Path(path)):
            raise RuntimeError(
                f"cannot resume: recorded artifact corrupt: {pa!r}")
        _meta = plugin_store.read_metadata(Path(path)) or {}
        if pa.get("artifact_id") and _meta.get("artifact_id") != pa.get("artifact_id"):
            raise RuntimeError(
                f"cannot resume: recorded artifact identity mismatch: {pa!r}")
        manifest = {}
        try:
            manifest = json.loads((Path(path) / ".claude-plugin" / "plugin.json")
                                  .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            plugin_store.manifest_protected_tools(manifest)
        except plugin_store.StoreError:
            issues.append(PluginIssue(
                name=pa.get("name", ""), target=None, stage="resolve",
                reason_code="protected_tools_invalid",
                artifact_id=pa.get("artifact_id", "")))
            continue
        plugins.append(ResolvedPlugin(
            name=pa.get("name", ""), artifact_id=pa.get("artifact_id", ""),
            path=path, version=str(manifest.get("version", "")), manifest=manifest))
    return ResolutionResult(registry_valid=True, plugins=plugins, issues=issues)


def _build_specialist_options(
    cfg,
    *,
    resolution=None,
    extra_casa_tools: tuple[str, ...] = (),
    output_format=None,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a Tier 2 specialist invocation.

    Specialist memory is injected via prompt in :func:`_run_delegated_agent`
    (shared ``casa`` bank); SDK-level session resume stays disabled
    (``resume=None``) — memory enters via prompt injection, not SDK
    continuity. Hooks are
    resolved from the specialist's own ``cfg.hooks``. MCP servers are
    resolved via the shared registry — same pattern as
    :meth:`Agent._process` (agent.py step 4). Degrades to empty-dict
    when the registry is not bound (legacy callers / test harnesses)."""
    from hooks import resolve_hooks

    resolved_hooks = resolve_hooks(cfg.hooks, default_cwd=cfg.cwd)
    # Sol #5: inject the /config/plugins + settings.json guard code-side (like
    # residents — agent.py step 5). A delegated specialist with Bash could
    # otherwise `echo > /config/plugins/registry.json`, bypassing validation and
    # §3.9 sequencing. Fresh dict so the shared resolved hooks aren't mutated.
    from hooks import agent_home_settings_guard_matcher
    resolved_hooks = dict(resolved_hooks)
    resolved_hooks["PreToolUse"] = [
        *resolved_hooks.get("PreToolUse", []),
        agent_home_settings_guard_matcher(),
    ]

    # Unified plugin architecture (§3.3): resolve with the CONCRETE tier + role.
    # Sol #12: delegate_to_agent also routes RESIDENTS through this builder
    # (sync/async delegation of e.g. butler), so a hardcoded "specialist:"
    # dropped a delegated resident's resident:<role> plugins. Use the
    # authoritative AgentRegistry tier; fall back to specialist for unknown roles.
    # Sol round-3 H7b: a caller-supplied ``resolution`` is used verbatim so the
    # engagement record + the options share ONE resolve (no create-vs-builder
    # drift), and a resumed engagement rebuilds from its RECORDED artifacts.
    _role = getattr(cfg, "role", "unknown")
    if resolution is None:
        _tier = (_agent_registry.tier_for_role(_role)
                 if _agent_registry is not None else None) or "specialist"
        resolution = plugin_registry.resolve_for(f"{_tier}:{_role}")
    sdk_plugins = [{"type": "local", "path": rp.path}
                   for rp in resolution.plugins]

    # Authorization grants (A:§3.2): APPEND the fail-closed PreToolUse authz
    # matcher for this specialist's PROTECTED plugin tools, preserving the
    # settings guard already appended above. protected_map derives from the
    # SAME resolution — supplying GrantKey.artifact_id. The hook branches on
    # provenance, so this ONE builder (ephemeral delegations AND in_casa
    # specialist engagements) is correct: engagement turns deny cleanly. Only
    # appended when there are protected tools (matcher=None routes every call).
    from authz_grants import (
        AuthzDeps, CHALLENGES, GRANTS, make_resident_authz_hook,
    )
    from plugin_grants import protected_map
    _protected = protected_map(resolution)
    if _protected:
        from claude_agent_sdk import HookMatcher
        _authz_role = getattr(cfg, "role", "unknown")

        def _authz_deps_factory():
            ch = (_channel_manager.get("telegram")
                  if _channel_manager is not None else None)
            if ch is None:
                return None  # no DM reachable ⇒ unsupported-origin deny
            # Read the CURRENT loaded character name at call time (W2); a
            # specialist reload rebuilds cfg, so the next challenge names the
            # new display name. Defensive getattr: unusual cfgs degrade to the
            # role string at render time, never raise.
            _char = getattr(cfg, "character", None)
            return AuthzDeps(
                channel=ch, grants=GRANTS, challenges=CHALLENGES,
                display_name=getattr(_char, "name", None),
            )

        resolved_hooks["PreToolUse"] = [
            *resolved_hooks.get("PreToolUse", []),
            HookMatcher(hooks=[make_resident_authz_hook(
                _authz_role, _protected, _authz_deps_factory)]),
        ]

    agent_home = (cfg.cwd
                  or f"/config/agent-home/{getattr(cfg, 'role', 'unknown')}")

    # Skills via skills="all" below; strip any config-supplied "Skill"
    # (deprecated) — (f) v0.69.9.
    allowed_tools = [t for t in cfg.tools.allowed if t != "Skill"]
    # P-5a: installed ⇒ granted, by construction. Server-level grants from the
    # SAME resolved artifacts; disallowed_tools still wins at the CC layer.
    for grant in grants_for_resolution(resolution):
        if grant not in allowed_tools:
            allowed_tools.append(grant)
    for grant in extra_casa_tools:
        if grant not in allowed_tools:
            allowed_tools.append(grant)

    if _mcp_registry is not None:
        mcp_servers = _mcp_registry.resolve(
            cfg.mcp_server_names,
            role=getattr(cfg, "role", ""),
            allowed_tools=allowed_tools,
        )
    else:
        mcp_servers = {}
    skills = "all" if getattr(cfg.tools, "skills", "all") == "all" else None

    return ClaudeAgentOptions(
        model=cfg.model,
        cli_path=CLAUDE_CLI_PATH,
        system_prompt=cfg.system_prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=_with_subagent_spawn_disallowed(cfg.tools.disallowed),
        permission_mode=cfg.tools.permission_mode or "acceptEdits",
        max_turns=cfg.tools.max_turns,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks=resolved_hooks,
        cwd=agent_home,
        resume=None,
        setting_sources=["project"],
        skills=skills,
        plugins=sdk_plugins,
        output_format=output_format,
        stderr=(_discard_structured_stderr if output_format is not None else None),
        # P-5b: no relay exists on this path — deny ungranted tools fast
        # instead of hanging on an unanswerable CC prompt.
        can_use_tool=make_fail_closed_can_use_tool(
            getattr(cfg, "role", "unknown")),
    )


def _build_executor_options(
    defn,
    *,
    executor_type: str,
    resolution=None,
    plugin_paths: "list[str] | None" = None,
    extra_casa_tools: tuple[str, ...] = (),
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a Tier 3 Executor invocation.

    Unlike specialists, executors DO have MCP servers and structured hooks
    driven by their definition.yaml + hooks.yaml. Prompt is injected at
    engage_executor time - this helper does not set system_prompt.

    Plugins (§3.8/§3.9), in priority order: explicit ``plugin_paths`` (resume
    from recorded artifacts — never re-resolved), else a passed ``resolution``
    (engage_executor feeds the SAME result it gated + recorded — one resolve,
    one binding), else a fresh resolve for ``executor:<executor_type>``.
    Executors get NO grant merge + NO can_use_tool (they keep the relay).
    """
    from config import HooksConfig
    from hooks import resolve_hooks
    import yaml

    hooks_cfg = HooksConfig()
    if defn.hooks_path and os.path.isfile(defn.hooks_path):
        with open(defn.hooks_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        hooks_cfg = HooksConfig(pre_tool_use=list(raw.get("pre_tool_use") or []))

    resolved_hooks = resolve_hooks(hooks_cfg, default_cwd="/config")
    # Sol #5: in_casa executors (e.g. configurator, cwd=/config, Bash allowed)
    # could `echo > /config/plugins/registry.json` — inject the same code-side
    # guard so a Bash write to /config/plugins or settings.json is denied.
    from hooks import agent_home_settings_guard_matcher
    resolved_hooks = dict(resolved_hooks)
    resolved_hooks["PreToolUse"] = [
        *resolved_hooks.get("PreToolUse", []),
        agent_home_settings_guard_matcher(),
    ]

    if plugin_paths is not None:
        sdk_plugins = [{"type": "local", "path": p} for p in plugin_paths]
    else:
        if resolution is None:
            resolution = plugin_registry.resolve_for(
                f"executor:{executor_type}")
        sdk_plugins = [{"type": "local", "path": rp.path}
                       for rp in resolution.plugins]

    # Skills via skills="all" below; strip any config-supplied "Skill"
    # (deprecated) — (f) v0.69.9.
    allowed_tools = [t for t in defn.tools_allowed if t != "Skill"]
    for grant in extra_casa_tools:
        if grant not in allowed_tools:
            allowed_tools.append(grant)

    if _mcp_registry is not None:
        mcp_servers = _mcp_registry.resolve(
            defn.mcp_server_names,
            role=executor_type,
            allowed_tools=allowed_tools,
        )
    else:
        mcp_servers = {}

    # Executors (in_casa driver — Configurator, future Tier-3) operate on
    # the addon-config root rather than an agent-home, because their
    # mutation surface spans /config/ (agents/, marketplace/,
    # plugin-env.conf, etc.).
    return ClaudeAgentOptions(
        model=defn.model,
        cli_path=CLAUDE_CLI_PATH,
        system_prompt="",
        allowed_tools=allowed_tools,
        disallowed_tools=list(defn.tools_disallowed),
        permission_mode=defn.permission_mode or "acceptEdits",
        max_turns=200,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks=resolved_hooks,
        cwd="/config",
        resume=None,
        setting_sources=["project"],
        skills="all",  # (f) v0.69.9
        plugins=sdk_plugins,
    )


def build_engagement_resume_options(engagement, session_id: str) -> ClaudeAgentOptions:
    """Rebuild the FULL option set for a resumed engagement, then attach
    ``resume=session_id`` (Finding 2, codex review v0.69.10).

    ``InCasaDriver.resume()`` used to open a BARE ``ClaudeAgentOptions(resume=)``,
    so a resumed interactive specialist/executor lost ``disallowed_tools``
    (Agent/Task — Q-1), the fail-closed ``can_use_tool`` callback, ``hooks``
    (the I-2 settings guard), ``skills``, ``mcp_servers``, and ``cwd`` — running
    on the CLI's broad default surface. Rebuilt from the engagement's CURRENT
    config via the same builder the initial start used.

    Fails CLOSED: if the specialist/executor config is gone (removed while the
    engagement was suspended), raise rather than resume with dropped
    restrictions. §3.8: an executor resumes from its RECORDED plugin artifacts
    (never re-resolving current assignments); a missing recorded artifact
    fails the resume closed (plugin_artifact_missing). Reads registry snapshot
    files — call off the event loop.
    """
    import dataclasses

    kind = getattr(engagement, "kind", "")
    role = getattr(engagement, "role_or_type", "")
    opts: ClaudeAgentOptions | None = None
    if kind == "executor":
        defn = _executor_registry.get(role) if _executor_registry is not None else None
        if defn is not None:
            recorded = getattr(engagement, "plugin_artifacts", None) or ()
            plugin_paths: list[str] = []
            for pa in recorded:
                path = pa.get("path") if isinstance(pa, dict) else None
                if not path or not os.path.isdir(path):
                    raise RuntimeError(
                        f"cannot resume executor engagement role={role!r}: "
                        f"recorded plugin artifact missing "
                        f"(plugin_artifact_missing): {pa!r}")
                plugin_paths.append(path)
            opts = _build_executor_options(
                defn,
                executor_type=role,
                plugin_paths=plugin_paths,
                extra_casa_tools=(
                    "mcp__casa-framework__query_engager",
                    "mcp__casa-framework__emit_completion",
                    # R5 (v0.89.0): a resumed engaged executor keeps `react`.
                    # Scoped to executors (the specialist branch below does NOT
                    # grant it) — matches the plugin-developer definition grant.
                    "mcp__casa-framework__react",
                ),
            )
    else:
        cfg = _specialist_registry.get(role) if _specialist_registry is not None else None
        if cfg is not None:
            # Sol round-3 H7b / round-4: a resumed specialist rebuilds from its
            # RECORDED artifacts (§3.8) — like executors — never re-resolving
            # current assignments. An EMPTY record ([]) is AUTHORITATIVE (started
            # with no plugins → resume with none); only a pre-v0.71.0 record
            # (field absent → None) falls back to a fresh resolve.
            recorded = getattr(engagement, "plugin_artifacts", None)
            if recorded is not None:
                opts = _build_specialist_options(
                    cfg,
                    resolution=_resolution_from_recorded(recorded),
                    extra_casa_tools=(
                        "mcp__casa-framework__query_engager",
                        "mcp__casa-framework__emit_completion",
                    ),
                )
            else:
                opts = _build_specialist_options(
                    cfg,
                    extra_casa_tools=(
                        "mcp__casa-framework__query_engager",
                        "mcp__casa-framework__emit_completion",
                    ),
                )
    if opts is None:
        raise RuntimeError(
            f"cannot rebuild options to resume {kind or 'specialist'} engagement "
            f"role={role!r}: config not found (fail-closed — refusing a bare "
            "resume that would drop Agent/Task denies + the fail-closed callback)"
        )
    return dataclasses.replace(opts, resume=session_id)


def _build_world_state_summary() -> str:
    """Return a short (<=500 tokens) snapshot of Casa's config surface.

    Called at engagement start and interpolated into the executor's
    prompt template as {world_state_summary}. Read-only - does not
    include in-flight session or delegation state.
    """
    lines: list[str] = []
    try:
        specialists = sorted(
            getattr(_specialist_registry, "_configs", {}).keys()
        ) if _specialist_registry else []
    except Exception:  # noqa: BLE001
        specialists = []
    lines.append(f"Enabled specialists:  {', '.join(specialists) or '(none)'}")

    residents: list[str] = []
    agents_dir = "/config/agents"
    try:
        if os.path.isdir(agents_dir):
            for name in sorted(os.listdir(agents_dir)):
                if name in ("specialists", "executors"):
                    continue
                if os.path.isdir(os.path.join(agents_dir, name)):
                    residents.append(name)
    except OSError:
        pass
    lines.append(f"Residents:            {', '.join(residents) or '(none)'}")

    try:
        import agent as agent_mod
        exec_reg = getattr(agent_mod, "active_executor_registry", None)
        exec_types = exec_reg.list_types() if exec_reg else []
    except Exception:  # noqa: BLE001
        exec_types = []
    lines.append(f"Enabled executors:    {', '.join(exec_types) or '(none)'}")

    version = "unknown"
    for candidate in ("/opt/casa/VERSION", "/config/VERSION"):
        try:
            with open(candidate) as fh:
                version = fh.read().strip()
                break
        except OSError:
            continue
    lines.append(f"Addon version:        {version}")

    return "\n".join(lines)


# Specialist memory write-path bg-task anchoring (parity with Agent._bg_tasks
# at agent.py:133). Module-level so it persists across delegate_to_agent calls.
_specialist_bg_tasks: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class DelegatedOutput:
    """Raw text plus the SDK's optional structured result envelope."""

    text: str
    structured_output: Any = None


@dataclass(frozen=True)
class JobActor:
    """Trusted creator identity used for every voice-job lookup/mutation."""

    creator_peer: str
    creator_user_id: str | None
    scope_id: str
    job_control_id: str | None = None


@dataclass
class _PermitHandoff:
    """Shared marker for lexical-to-registry permit ownership transfer."""

    transferred: bool = False


_BACKGROUND_ROUTE_CAPABILITIES = frozenset({
    "background_jobs", "satellite_announce", "voice_handoff",
})
CONCIERGE_ROLE = "concierge"


def requires_voice_handoff(origin: dict, caller_role: str) -> bool:
    """Whether this trusted Concierge call must use WS handoff delivery."""
    return (
        caller_role == CONCIERGE_ROLE
        and origin.get("channel") == "voice"
        and origin.get("voice_transport") == "ws"
        and bool(origin.get("_voice_handoff_reservation"))
    )


def validate_voice_handoff_static(
    agent_name: str, origin: dict, requested_mode: str,
) -> tuple[str, Any | None, dict | None]:
    """Normalize an eligible Concierge voice delegation before any await.

    The policy keys on the stable registered role, not an operator-editable
    display name or model prompt.  It checks only static trusted provenance;
    the existing asynchronous prelaunch pipeline still owns ACL, dependency,
    and concurrency gates.
    """
    caller_role = str(origin.get("execution_role") or origin.get("role") or "")
    if (
        requested_mode != "sync"
        or caller_role != CONCIERGE_ROLE
        or origin.get("role") != CONCIERGE_ROLE
        or origin.get("channel") != "voice"
    ):
        return requested_mode, None, None

    caller_cfg = _agent_role_map.get(caller_role)
    declared = {d.agent for d in (getattr(caller_cfg, "delegates", None) or [])}
    # Preserve the established ACL error and ordering for an undeclared
    # target; capability errors must not disclose target configuration.
    if agent_name not in declared:
        return requested_mode, None, None

    reservation = origin.get("_voice_handoff_reservation")
    if (
        not requires_voice_handoff(origin, caller_role)
        or not callable(getattr(reservation, "reserve", None))
        or not callable(getattr(reservation, "release", None))
        or not callable(getattr(reservation, "commit", None))
        or not background_route_available(origin)
    ):
        return requested_mode, None, _background_delivery_unavailable_result()
    return "async", reservation, None


def background_route_available(origin: dict | None) -> bool:
    """Return whether the trusted turn origin can accept background audio."""
    if not isinstance(origin, dict):
        return False
    if origin.get("channel") != "voice" or origin.get("voice_transport") != "ws":
        return False
    route_id = origin.get("voice_route_id")
    device_id = origin.get("origin_device_id")
    capabilities = origin.get("voice_route_capabilities")
    if not isinstance(route_id, str) or not route_id.strip():
        return False
    if not isinstance(device_id, str) or not device_id.strip():
        return False
    if not isinstance(capabilities, (set, frozenset, list, tuple)):
        return False
    if not _BACKGROUND_ROUTE_CAPABILITIES <= frozenset(
        item for item in capabilities if isinstance(item, str)
    ):
        return False
    # Per-turn capabilities prove how this origin was authenticated, but they
    # are not a perpetual lease. Production wires the live registry here so a
    # launch after the 60-second disconnect grace fails closed. Tests and
    # migration callers without a runtime retain the connection-bound check.
    routes = getattr(_runtime, "voice_route_registry", None)
    if routes is not None:
        return bool(routes.is_recently_capable(route_id.strip()))
    return True


def _background_delivery_unavailable_result() -> dict:
    return _result({
        "status": "error",
        "kind": "background_delivery_unavailable",
        "message": (
            "Background specialist delivery requires a current, "
            "acknowledged voice WebSocket route."
        ),
    })


def _specialist_display_name(cfg: Any, role: str) -> str:
    character = getattr(cfg, "character", None)
    display_name = getattr(character, "name", None)
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    return role


def _job_actor_from_origin(origin: dict | None) -> JobActor | None:
    if not isinstance(origin, dict) or origin.get("channel") != "voice":
        return None
    scope_id = origin.get("chat_id") or origin.get("scope_id")
    if not isinstance(scope_id, (str, int)) or not str(scope_id):
        return None
    raw_user_id = origin.get("user_id")
    user_id = None if raw_user_id is None else str(raw_user_id)
    return JobActor(
        creator_peer="voice",
        creator_user_id=user_id,
        scope_id=str(scope_id),
        job_control_id=(
            str(origin["voice_job_control_id"])
            if isinstance(origin.get("voice_job_control_id"), str)
            and origin["voice_job_control_id"].strip()
            else None
        ),
    )


def _actor_owns_job(actor: JobActor, job: VoiceJob) -> bool:
    if actor.creator_peer != job.creator_peer:
        return False
    if actor.creator_user_id != job.creator_user_id:
        return False
    if job.job_control_id is not None:
        return actor.job_control_id == job.job_control_id
    return actor.scope_id == job.scope_id


def _discard_structured_stderr(_line: str) -> None:
    """Drain structured-job stderr without placing model content in logs."""


async def _run_delegated_agent(
    cfg, task_text: str, context_text: str, resolution=None,
    output_format=None,
) -> DelegatedOutput:
    """Run one ephemeral delegated turn and return text plus structured output.

    ``resolution`` (spec A5): when the caller's requires gate already
    resolved this agent's plugins, that SAME ``ResolutionResult`` is
    passed through to ``_build_specialist_options`` — no second resolve,
    so the gate's decision and what actually launches can never drift.
    ``None`` (the common no-``requires`` case) lets the options builder
    resolve fresh, exactly as before Task 5.
    """
    import agent as agent_mod
    # AR-2: snapshot BEFORE any await — this coroutine outlives the parent
    # turn (async delegations especially), and a pooled client's origin_var
    # holder can be rewritten by the NEXT turn while this one is in flight.
    parent = _snapshot_origin()
    from personality_types import RetainedTurn, SpeakerProvenance

    # Task 10: the caller's REAL identity, read back off the origin snapshot
    # Agent._process now stamps. Absent (a legacy/test ingress that predates this
    # wiring, or the caller is itself an executor/unbound specialist) → the
    # explicit unattributed "system" identity (every field null is a VALID
    # provenance, speaker_provenance.py's system branch). NEVER fabricate a
    # persona for the caller.
    caller_provenance = parent.get("speaker_provenance")
    if not isinstance(caller_provenance, SpeakerProvenance):
        caller_provenance = SpeakerProvenance(speaker_kind="system")
    # The EXECUTING agent's own identity, via the SAME cfg.kind-based fallback
    # policy SessionRegistry.register's callers use (Task 9): a resident (or a
    # bound specialist, once Plan 2 lands) keeps its real persona; an executor
    # gets a stable executor identity; an unbound Plan-1 specialist gets the
    # honest "system" identity — never mislabeled "executor:<slug>". One policy,
    # not two; resolves itself when Plan 2 populates cfg.speaker_provenance.
    executing_provenance = agent_mod.speaker_provenance_for_role(cfg)

    child_origin = {
        **parent,
        "delegation_depth": int(parent.get("delegation_depth", 0)) + 1,
        # Provenance foundation (A:§1, v0.76.0): the delegate's own role,
        # distinct from parent["role"] (the caller) — turn_provenance()
        # compares the two to classify this turn as "delegated".
        "execution_role": cfg.role,
        # Mirrors execution_role: child_origin carries the EXECUTING agent's own
        # provenance forward, not the caller's — so a NESTED delegation's
        # "parent" sees the immediately-enclosing agent's identity.
        "speaker_provenance": executing_provenance,
    }

    # Resolve caller display name; fall back to role.
    caller_role = str(parent.get("role", "")) or "(unknown)"
    caller_name = (
        _agent_registry.role_to_name(caller_role)
        if _agent_registry is not None else caller_role
    )
    originating_channel = str(parent.get("channel", "")) or "(unknown)"
    suggested_register = "voice" if originating_channel == "voice" else "text"

    delegation_context = (
        "<delegation_context>\n"
        f"caller_role: {caller_role}\n"
        f"caller_name: {caller_name}\n"
        f"originating_channel: {originating_channel}\n"
        f"suggested_register: {suggested_register}\n"
        "</delegation_context>"
    )

    if context_text:
        body_tail = (
            f"Task: {task_text}\n\n"
            f"Context from {caller_name}:\n{context_text}"
        )
    else:
        body_tail = f"Task: {task_text}"

    # Specialist memory read on the shared `casa` bank, at the PARENT context's
    # read-clearance (design §3, plan 3). Opt-in via cfg.memory.token_budget > 0.
    memory_block = ""
    if cfg.memory.token_budget > 0:
        unavailable_note = (
            f'<memory_context agent="{cfg.role}" status="unavailable">\n'
            "Long-term memory could not be checked for this task "
            "(backend unavailable). Do not conclude Casa lacks "
            "information on any topic — if asked, say memory could "
            "not be checked.\n"
            "</memory_context>\n\n"
        )
        sem = getattr(agent_mod, "active_semantic_memory", None)
        if sem is None:
            # No backend wired: same unavailability as a failed check — a
            # silent cold turn reads as "no memories exist".
            memory_block = unavailable_note
        else:
            try:
                digest = await delegated_recall(
                    sem, query=task_text,
                    origin_channel=str(parent.get("channel", "")),
                    max_tokens=cfg.memory.token_budget,
                )
            except RecallUnavailable:
                # Delegated turn proceeds cold, but the specialist is TOLD the
                # check failed — a silent cold turn reads as "no memories
                # exist" and the specialist would claim absence to the user.
                digest = ""
                memory_block = unavailable_note
            if digest:
                memory_block = (
                    f'<memory_context agent="{cfg.role}">\n'
                    f"{digest}\n"
                    f"</memory_context>\n\n"
                )

    prompt = f"{delegation_context}\n\n{memory_block}{body_tail}"

    # Off-loop: _build_specialist_options resolves the registry (file IO) —
    # keep it off the shared event loop (H2/M20).
    # Sol #4 residual (documented): this ephemeral delegation client pins the
    # artifacts it resolves here for its (short) lifetime; it is NOT recorded in
    # runtime.agents or the engagement registry, so a plugin_update landing
    # mid-delegation is not disclosed by verify until the delegation ends. This
    # window is transient and self-healing (the next delegation resolves the new
    # artifact); the PERSISTENT stale-binding incident is fully closed. Tracked
    # for a future live-binding registry (docs/ROADMAP-backlog.md).
    options = await asyncio.to_thread(
        _build_specialist_options, cfg, resolution=resolution,
        output_format=output_format)
    text = ""
    result_msg: ResultMessage | None = None
    token = agent_mod.origin_var.set(child_origin)
    try:
        client_options = (
            options if output_format is not None
            else sdk_logging.with_stderr_callback(options, engagement_id=None)
        )
        sdk_log_guard = (
            sdk_logging.suppress_structured_voice_sdk_payload_logs()
            if output_format is not None else nullcontext()
        )
        with sdk_log_guard:
            async with ClaudeSDKClient(client_options) as client:
                await client.query(prompt)
                async for sdk_msg in client.receive_response():
                    if isinstance(sdk_msg, AssistantMessage):
                        for block in getattr(sdk_msg, "content", []):
                            if isinstance(block, TextBlock):
                                text += block.text
                    elif isinstance(sdk_msg, ResultMessage):
                        # Task 6 (spec §4.6): previously discarded — captured
                        # below (in `finally`, so it's recorded even when the
                        # client raises before the loop reaches a ResultMessage).
                        result_msg = sdk_msg
    finally:
        agent_mod.origin_var.reset(token)
        # Task 6 (spec §4.6): aggregate cost/usage from the captured
        # ResultMessage. COUNTING is separate (`record_launch`, done by the
        # caller at ownership transfer) so a delegation that fails during
        # setup — or whose ResultMessage never arrived — is still counted;
        # here we only aggregate cost when a ResultMessage actually arrived.
        if _specialist_telemetry is not None and result_msg is not None:
            cost_usd = float(getattr(result_msg, "total_cost_usd", 0.0) or 0.0)
            usage = extract_usage(result_msg)
            _specialist_telemetry.record_cost(
                cfg.role, cost_usd=cost_usd, usage=usage,
            )

    # Task 6 (spec §4.6): output bounding is applied by the CALLER (see
    # `specialist_limits.truncate_output` at the sync-result / async-
    # DelegationComplete assembly sites) rather than here, so the
    # `output_truncated` flag survives this task's str boundary and reaches
    # the wire. The raw text is returned; memory retain below stores it as
    # exchanged (the bound is a caller-facing surface concern, not a memory
    # one — and token_budget>0 specialists are rare).

    # Specialist write: one explicit tier-classified retain of the exchange to
    # the shared bank, gated by the PARENT channel's write-trust (voice → no
    # write) — design §3, plan 3. Ephemeral specialists have no session
    # registry, so the freshness reaper never sees them; the retain is explicit.
    if cfg.memory.token_budget > 0 and text:
        sem = getattr(agent_mod, "active_semantic_memory", None)
        if sem is not None:
            bg = asyncio.create_task(retain_delegated(
                sem, origin_channel=str(parent.get("channel", "")),
                turns=[
                    RetainedTurn(task_text, caller_provenance),
                    RetainedTurn(text, executing_provenance),
                ],
            ))
            _specialist_bg_tasks.add(bg)
            bg.add_done_callback(_specialist_bg_tasks.discard)

    return DelegatedOutput(
        text=text,
        structured_output=(
            getattr(result_msg, "structured_output", None)
            if result_msg is not None else None
        ),
    )


def _record_launch_safe(agent_name: str) -> None:
    """Count one delegation launch, never raising (spec §4.6).

    Called AFTER permit ownership has already transferred (``owned = None``),
    and guarded so a telemetry/logging failure can never propagate into the
    caller and — worse — never reach the ``owned`` finally to release a
    permit whose task/driver is already live."""
    if _specialist_telemetry is None:
        return
    try:
        _specialist_telemetry.record_launch(agent_name)
    except Exception:  # noqa: BLE001 — telemetry must never affect permit ownership or the launch
        logger.warning(
            "specialist telemetry record_launch raised for %s",
            agent_name, exc_info=True,
        )


def _permit_release_callback(permit) -> "callable":
    """Return an asyncio task done-callback that releases *permit* (spec §4.6).

    This is the SOLE authoritative release for a launched sync/async task —
    the ``SpecialistRegistry`` terminal transitions deliberately do not
    release (that would race the voice teardown's ``cancel_delegation``
    against a still-unwinding task). Attaching it to the task is strictly more
    robust than a ``finally`` inside the coroutine: a task cancelled BEFORE
    its coroutine ever starts running has no coroutine-`finally` to run, but
    its done callbacks still fire. It fires exactly once per task (normal
    completion, exception, or cancellation), and ``Permit.release()`` is
    idempotent so an overlap with the pre-launch ``owned`` guard is a
    safe no-op."""
    def _cb(_task: asyncio.Task) -> None:
        permit.release()
    return _cb


def _attach_completion_callback(
    task: asyncio.Task,
    record: DelegationRecord,
) -> None:
    """Wire the bus NOTIFICATION post on delegation completion.

    Used by the degraded-sync and async paths. Task 7's sync-ok /
    sync-error paths bookkeep inline.

    Task 6 (spec §4.6): the permit release is NOT here — it rides on the
    dedicated `_permit_release_callback` done-callback attached to the same
    task (robust even against a cancelled-before-start task, which has no
    coroutine `finally`). This callback bounds the notified output via
    `truncate_output` and propagates the `output_truncated` flag onto the
    `DelegationComplete`.
    """
    loop = asyncio.get_running_loop()

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            loop.create_task(_specialist_registry.cancel_delegation(record.id))
            return
        complete: DelegationComplete | None = None
        try:
            text = t.result().text
            bounded, output_truncated = specialist_limits.truncate_output(text)
            if output_truncated:
                logger.warning(
                    "delegated agent %s output truncated: %d > %d chars "
                    "(spec §4.6)", record.agent, len(text),
                    specialist_limits._MAX_OUTPUT_CHARS,
                )
            complete = DelegationComplete(
                delegation_id=record.id,
                agent=record.agent,
                status="ok",
                text=bounded,
                origin=record.origin,
                elapsed_s=time.time() - record.started_at,
                output_truncated=output_truncated,
            )
            loop.create_task(_specialist_registry.complete_delegation(record.id))
        except Exception as exc:
            kind = _classify_error(exc).value
            complete = DelegationComplete(
                delegation_id=record.id,
                agent=record.agent,
                status="error",
                kind=kind,
                message=str(exc),
                origin=record.origin,
                elapsed_s=time.time() - record.started_at,
            )
            loop.create_task(_specialist_registry.fail_delegation(record.id, exc))

        if _bus is None or complete is None:
            return
        target_role = record.origin.get("role") or "assistant"
        loop.create_task(_bus.notify(BusMessage(
            type=MessageType.NOTIFICATION,
            source=record.agent,
            target=target_role,
            content=complete,
            channel=record.origin.get("channel", ""),
            context={
                "cid": record.origin.get("cid", "-"),
                "chat_id": record.origin.get("chat_id", ""),
                "delegation_id": record.id,
            },
        )))
    task.add_done_callback(_done)


def _delegation_scope(origin: dict, agent_name: str) -> str:
    """Concurrency scope key for the per-scope specialist cap (spec §4.6).

    The voice channel's per-turn rate limiter (``channels/voice/channel.py``
    ``_resolve_scope_id`` / ``VoiceRateLimiter``) already keys off a
    ``scope_id`` — a per-session identifier — and threads that SAME value
    through as ``origin["chat_id"]`` for every voice-channel turn. Reusing
    it here makes spec §4.6's "one active MTG delegation per voice scope"
    literal: at most one delegation to a GIVEN specialist may be in flight
    per calling session at a time (a session may still run concurrent
    delegations to two DIFFERENT specialists — each gets its own scope key
    — bounded only by the global cap). Non-voice channels' chat_id serves
    the same "one calling session" role, so the same cap applies uniformly.

    Falls back to ``cid`` (the per-turn correlation id) when chat_id is
    empty/missing so an unscoped caller still gets its own bucket instead
    of colliding with every other unscoped caller under one ``"-"`` key.
    """
    chat_id = str((origin or {}).get("chat_id") or "")
    if not chat_id:
        chat_id = str((origin or {}).get("cid") or "-")
    return f"{chat_id}:{agent_name}"


async def _prelaunch(
    agent_name: str, origin: dict, mode: str,
    task_text: str = "", context_text: str = "",
) -> tuple[Any, Any, "specialist_limits.Permit | None", dict | None]:
    """The single unified prelaunch pipeline for delegate_to_agent (spec A4).

    Runs EVERY pre-launch gate, in this order, so no gate is bypassable by
    another and no side effect (topic, engagement/delegation record, task,
    driver start, progress emission) can precede a clean return:

        ACL (Task 1) -> not-initialized -> input-size bounds (Task 6) ->
        depth cap -> mode gate -> target resolution ->
        resident-interactive-compat -> requires (Task 5) ->
        concurrency (Task 6, spec §4.6) -> progress -> launch

    The input-size bounds run AFTER the ACL (so an unauthorized caller is
    denied ``delegation_not_declared`` regardless of payload size, and no
    telemetry is ever keyed on a caller-supplied target name pre-auth) but
    BEFORE target resolution / concurrency / progress (so an oversized
    payload never resolves plugins, acquires a slot, or speaks).

    The mode gate deliberately precedes both target resolution AND the
    resident-interactive-compat check. On voice, interactive is always
    denied as ``mode_unsupported_on_voice``; async without a trusted capable
    route is denied as ``background_delivery_unavailable``. Those denials
    stay independent of what (or whether) the target resolves to.

    Returns ``(cfg, resolution, permit, error)``:
    - ``error`` is a terminal tool-result dict the caller returns
      immediately when a gate denies; ``cfg``/``resolution``/``permit``
      are ``None``. A concurrency (``busy``) denial in particular has NO
      side effects yet — it is checked before progress/record/task, same
      as every earlier gate.
    - On success ``error`` is ``None``, ``cfg`` is the resolved target
      config, ``resolution`` is the plugin ResolutionResult the requires
      gate (spec A5) resolved for ``agent_name`` — ``None`` when
      ``cfg.requires`` is empty (gate skipped, no resolve performed) —
      and ``permit`` is the concurrency slot acquired for this delegation
      (``None`` only when no ``SpecialistLimiter`` is wired at all — see
      ``init_tools``). ``permit`` is ALWAYS acquired when a limiter is
      wired, independent of whether ``resolution`` is ``None`` — a permit
      does not depend on the requires gate having run.
      Callers with a non-``None`` resolution MUST reuse it verbatim
      (``_build_specialist_options(cfg, resolution=resolution)`` and the
      interactive engagement-record binding) rather than re-resolving, so
      the gate's decision and what actually launches never drift. The
      caller (``delegate_to_agent``) owns the permit's lifetime from here.
      Legacy sync/non-voice async tasks use ``_permit_release_callback``;
      accepted async voice jobs transfer the permit to ``JobRegistry`` at
      ``bind_task``; interactive hands it to ``rec.permit`` for release by an
      ``EngagementRegistry`` terminal transition (or
      ``_finalize_engagement``).
    """
    channel = str((origin or {}).get("channel", ""))

    # A1 delegation ACL — the caller's DECLARED delegates are an
    # authorization boundary, enforced FIRST so a missing, unknown, or
    # undeclared caller is denied uniformly with one kind and cannot
    # distinguish existing agents. Caller identity comes ONLY from the
    # trusted origin, never from tool args. Key on execution_role — the
    # agent actually RUNNING this turn — so a delegated specialist is
    # judged by its OWN delegates, not its parent's: on a direct turn
    # execution_role == role (agent.py sets both to self.config.role); on
    # a delegated turn _run_delegated_agent overwrites execution_role with
    # the delegate's role while `role` stays the delegator (read by the
    # v0.76 provenance/authz system — leave it). The InCasaDriver
    # interactive-executor inheritance path (in_casa_driver.py:113) is a
    # tracked residual — unreachable today as no executor grants
    # delegate_to_agent.
    caller_role = str((origin or {}).get("execution_role")
                      or (origin or {}).get("role", ""))
    caller_cfg = _agent_role_map.get(caller_role) if caller_role else None
    declared = {d.agent for d in (getattr(caller_cfg, "delegates", None) or [])}
    if caller_cfg is None or agent_name not in declared:
        return None, None, None, _result({
            "status": "error", "kind": "delegation_not_declared",
            "message": (f"Agent {caller_role or '(unknown)'!r} does not "
                        f"declare {agent_name!r} as a delegate.")})

    if _specialist_registry is None:
        return None, None, None, _result({
            "status": "error",
            "kind": "not_initialized",
            "message": "specialist registry not initialized",
        })

    # Task 6 input bounds (spec §4.6): reject an oversized task/context —
    # AFTER the ACL (an unauthorized caller is already denied above, so we
    # never leak `input_too_large` to an unknown caller, and any denial
    # telemetry keyed on `agent_name` is now safe: the caller is authorized
    # and `agent_name` is one of its DECLARED delegates) but BEFORE target
    # resolution / concurrency / progress (an oversized payload never
    # resolves plugins, acquires a slot, or speaks).
    if len(task_text) > specialist_limits._MAX_TASK_CHARS:
        if _specialist_telemetry is not None:
            _specialist_telemetry.record_denial(agent_name, kind="input_too_large")
        return None, None, None, _result({
            "status": "error", "kind": "input_too_large", "field": "task",
            "agent": agent_name, "length": len(task_text),
            "limit": specialist_limits._MAX_TASK_CHARS,
            "message": (
                f"task ({len(task_text)} chars) exceeds the "
                f"{specialist_limits._MAX_TASK_CHARS}-char limit."
            ),
        })
    if len(context_text) > specialist_limits._MAX_CONTEXT_CHARS:
        if _specialist_telemetry is not None:
            _specialist_telemetry.record_denial(agent_name, kind="input_too_large")
        return None, None, None, _result({
            "status": "error", "kind": "input_too_large", "field": "context",
            "agent": agent_name, "length": len(context_text),
            "limit": specialist_limits._MAX_CONTEXT_CHARS,
            "message": (
                f"context ({len(context_text)} chars) exceeds the "
                f"{specialist_limits._MAX_CONTEXT_CHARS}-char limit."
            ),
        })

    # Depth cap: prevent delegation chains beyond depth=1.
    current_depth = int((origin or {}).get("delegation_depth", 0))
    if current_depth >= _MAX_DELEGATION_DEPTH:
        return None, None, None, _result({
            "status": "error",
            "kind": "delegation_depth_exceeded",
            "message": (
                f"Delegation depth {current_depth} exceeds cap "
                f"{_MAX_DELEGATION_DEPTH}; cannot chain further."
            ),
        })

    # A4 mode gate — BEFORE target resolution and resident-compat. Voice
    # interactive remains unsupported. Async is accepted only when ingress
    # bound a current acknowledged delivery route on the server-owned WS
    # connection; SSE and route-shaped tool/context spoofing fail here before
    # requires/concurrency/progress or any launch side effect.
    if channel == "voice" and mode == "interactive":
        return None, None, None, _result({
            "status": "error",
            "kind": "mode_unsupported_on_voice",
            "message": (
                f"mode={mode!r} is not supported on the voice channel — "
                "interactive engagements require a text channel."
            ),
        })
    if (channel == "voice" and mode == "async"
            and not background_route_available(origin)):
        return None, None, None, _background_delivery_unavailable_result()

    # Resolve target. Look in the merged role map (residents + specialists)
    # first; fall back to the specialist registry for back-compat with any
    # caller still relying on the old wiring.
    cfg = _agent_role_map.get(agent_name) or (
        _specialist_registry.get(agent_name)
        if _specialist_registry is not None else None
    )
    if cfg is None:
        return None, None, None, _result({
            "status": "error",
            "kind": "unknown_agent",
            "message": f"No enabled agent named {agent_name!r}",
        })

    # Resident-interactive-compat — AFTER the mode gate, so a voice
    # interactive delegation to a declared resident still surfaces the
    # voice mode denial (not this one).
    is_resident = bool(getattr(cfg, "channels", []))
    if mode == "interactive" and is_resident:
        return None, None, None, _result({
            "status": "error",
            "kind": "interactive_not_supported",
            "message": (
                f"Cannot open a Telegram engagement for resident "
                f"{agent_name!r} — residents already own their own channels."
            ),
        })

    # A5 requires gate: a delegated agent that declares `requires:` refuses
    # to launch (typed `dependency_unavailable`) unless its required
    # plugins/tools are ACTUALLY resolved for its own tier:role target —
    # never "assume model memory has the tool". Skipped entirely (resolution
    # stays None) when `cfg.requires` is empty, so a delegate with no
    # requires: block behaves exactly as before Task 5. `grants_for_resolution`
    # is SERVER-level (`mcp__plugin_mtg_mtg`), so a tool-level requirement is
    # checked against `declared_tools_for_resolution` (manifest
    # `casa.provides_tools`) AND, for the server prefix, the actual grant —
    # both must hold for the tool to be genuinely usable.
    resolution = None
    req = getattr(cfg, "requires", None)
    if req is not None and (req.plugins or req.tools):
        tier = (_agent_registry.tier_for_role(agent_name)
                if _agent_registry is not None else None) or "specialist"
        resolution = await asyncio.to_thread(
            plugin_registry.resolve_for, f"{tier}:{agent_name}")
        names = {rp.name for rp in resolution.plugins}
        declared = declared_tools_for_resolution(resolution)
        servers = set(grants_for_resolution(resolution))  # server actually attached
        missing_plugins = [p for p in req.plugins if p not in names]
        missing_tools = [
            t for t in req.tools
            if t not in declared or t.rsplit("__", 1)[0] not in servers
        ]
        if missing_plugins or missing_tools or not resolution.registry_valid:
            return None, None, None, _result({
                "status": "error", "kind": "dependency_unavailable",
                "agent": agent_name,
                "missing_plugins": missing_plugins,
                "missing_tools": missing_tools,
                "registry_valid": resolution.registry_valid,
                "message": f"Agent {agent_name!r} launch deps unavailable.",
            })

    # Task 6 concurrency gate (spec §4.6): AFTER requires, BEFORE progress —
    # a denied gate must never speak the "checking" line, and a delegation
    # that fails the requires gate must never occupy a concurrency slot.
    # Acquired regardless of whether `resolution` is None (a permit does
    # NOT depend on the requires gate having run — see docstring). No
    # limiter wired (`_specialist_limiter is None`) means no cap: `permit`
    # stays None and every downstream release is a guarded no-op.
    permit = None
    if _specialist_limiter is not None:
        scope = _delegation_scope(origin, agent_name)
        permit = _specialist_limiter.try_acquire(scope)
        if permit is None:
            if _specialist_telemetry is not None:
                _specialist_telemetry.record_denial(agent_name, kind="busy")
            return None, None, None, _result({
                "status": "error",
                "kind": "busy",
                "agent": agent_name,
                "message": (
                    f"Agent {agent_name!r} is already at its concurrency "
                    "cap — try again shortly."
                ),
            })

    # A4 progress: speak a deterministic "still working" block AFTER every
    # gate above has passed, immediately before the launch side effects —
    # a denied gate must never speak a misleading "checking" line. The
    # sink itself (channels/voice/channel.py) enforces exactly-once-per-
    # turn and suppresses this if the turn already spoke real content.
    #
    # Task 6 (spec §4.6): the progress sink is the ONE await between
    # acquiring the permit above and returning it to the caller. If THIS
    # coroutine is cancelled here (voice barge-in), the caller never
    # receives `permit` to release it — so guard the await and release on
    # any BaseException (CancelledError included) before re-raising. The
    # sink's own errors are still swallowed (best-effort) and do NOT
    # release the permit — a launch still proceeds.
    try:
        if (channel == "voice"
                and not requires_voice_handoff(origin, caller_role)):
            sink = (origin or {}).get("_progress_sink")
            if callable(sink):
                try:
                    await sink("One moment — checking.")
                except Exception:  # noqa: BLE001 — progress is best-effort
                    logger.warning(
                        "voice progress sink raised for delegate_to_agent(%s)",
                        agent_name, exc_info=True,
                    )
    except BaseException:
        if permit is not None:
            permit.release()
        raise

    return cfg, resolution, permit, None


def _voice_wait_from_deadline(raw_deadline: Any, loop) -> float | None:
    """A4: remaining voice budget, recomputed from the ABSOLUTE monotonic
    deadline (``origin["voice_deadline"]``). Returns the wait in seconds,
    or ``None`` when the budget is exhausted, missing, or NON-FINITE —
    the caller then fails closed with ``deadline_exceeded``.

    Must be called at each decision point (pre-register AND post-register)
    because the value goes stale: ``loop.time()`` advances as the handler
    awaits (register_delegation's tombstone lock + I/O), so a wait computed
    before an await can be obsolete after it. A non-finite deadline/wait is
    rejected explicitly — ``min(nan, 60)`` is unreliable and
    ``asyncio.wait(timeout=nan)`` never expires (hang), so NaN must fail
    closed here rather than silently disable the timeout."""
    if raw_deadline is None:
        return None
    try:
        deadline = float(raw_deadline)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(deadline):
        return None
    wait_s = min(
        deadline - loop.time() - _VOICE_FALLBACK_RESERVE_S,
        _SYNC_WAIT_TIMEOUT_S,
    )
    if not math.isfinite(wait_s) or wait_s <= 0:
        return None
    return wait_s


def _deadline_exceeded_result(delegation_id: str, agent_name: str) -> dict:
    """A4: the typed ``deadline_exceeded`` tool result. Shared by the
    pre-launch expiry short-circuit (no task created) and the post-wait
    teardown path (``_voice_deadline_exceeded``)."""
    return _result({
        "status": "error",
        "delegation_id": delegation_id,
        "agent": agent_name,
        "kind": "deadline_exceeded",
        "message": "Voice turn budget exceeded before the specialist finished.",
    })


async def _voice_deadline_exceeded(
    task: asyncio.Task, delegation_id: str, agent_name: str,
) -> dict:
    """A4: voice turn-budget expiry AFTER the task was launched. Cancel it,
    wait a bounded amount for it to actually unwind, and report a typed
    error. Voice never degrades to `pending` — there is no channel to
    deliver a later completion notification on."""
    # Attach the exception-retrieval callback UNCONDITIONALLY, before the
    # cancel — a task that catches CancelledError and raises within the
    # teardown bound lands in `done` (not `pending`) with an exception that
    # would otherwise never be retrieved (asyncio logs "exception was never
    # retrieved"). The callback fires regardless of which set it ends in.
    task.add_done_callback(_retrieve_late_task_exception)
    task.cancel()
    await asyncio.wait({task}, timeout=_VOICE_TEARDOWN_BOUND_S)
    await _specialist_registry.cancel_delegation(delegation_id)
    logger.info(
        "Delegation %s → %s exceeded the voice turn budget — cancelled",
        delegation_id[:8], agent_name,
    )
    return _deadline_exceeded_result(delegation_id, agent_name)


def _retrieve_late_task_exception(t: asyncio.Task) -> None:
    """Done-callback for a cancelled specialist task in the voice teardown
    path — retrieves ``.exception()`` so asyncio never logs it as
    "never retrieved", whether the task finished within the teardown bound
    (landing in ``done`` with an exception it caught then re-raised) or
    survived past it (landing in ``pending`` and finishing later)."""
    if t.cancelled():
        return
    exc = t.exception()
    if exc is not None:
        logger.warning(
            "voice deadline teardown: specialist task raised after "
            "cancellation had already been requested",
        )


# S-2 (block-S live finding 2026-07-15): wall-clock ceiling on every
# launched sync/async delegation task. Async mode (and sync degraded to
# pending at the 60s wait) previously had NO time bound at all — a runaway
# specialist (live: >12 min repetition loop, delegation 07bfeb0b) held its
# per-scope Permit + a global slot until someone killed the CLI subprocess.
# 10× the sync degrade wait: minutes-scale so legitimate long specialist
# turns (the EMIT:2500 sibling run finished after producing 127k chars)
# still complete, but far below the observed indefinite hang. Deliberately
# a module constant, not an add-on option — voice has its own (configurable)
# budget, and the sync/interactive paths have their own bounds; this is a
# runaway backstop, not a tuning knob.
_DELEGATION_CEILING_S: float = 10 * _SYNC_WAIT_TIMEOUT_S
# Bounded wait for the cancelled inner task to unwind (SDK client __aexit__
# terminates the CLI subprocess — its own terminate→kill escalation runs in
# seconds) before the failure is reported. 30s comfortably covers a healthy
# teardown while never letting a wedged one postpone the typed failure
# indefinitely; giving up early is LOUD (see the warning below) because the
# permit then frees while the zombie is still unwinding — a deliberate,
# bounded overshoot of the concurrency cap in the double-failure case only.
_CEILING_TEARDOWN_BOUND_S: float = 30.0


class DelegationCeilingExceeded(asyncio.TimeoutError):
    """A delegated turn exceeded the wall-clock ceiling and was cancelled.

    Subclasses ``asyncio.TimeoutError`` so ``_classify_error`` yields the
    typed kind ``"timeout"`` — the task then FAILS (rather than ending
    cancelled), which routes ``_attach_completion_callback`` into its
    exception branch: ``fail_delegation`` + a ``DelegationComplete`` error
    NOTIFICATION to the engager — the exact path the live kill-subprocess
    probe proved releases the permit and notifies correctly. A bare
    ``task.cancel()`` would instead hit the cancelled-branch, which posts
    NO notification."""


async def _run_delegated_agent_bounded(
    cfg, task_text: str, context_text: str, resolution=None,
    output_format=None,
) -> DelegatedOutput:
    """Run ``_run_delegated_agent`` under ``_DELEGATION_CEILING_S``.

    Transparent wrapper on the happy path (result and exceptions propagate
    unchanged, so error classification is untouched). On ceiling expiry the
    inner task is cancelled, given ``_CEILING_TEARDOWN_BOUND_S`` to unwind
    (the SDK client's ``__aexit__`` terminates the CLI subprocess), and
    ``DelegationCeilingExceeded`` is raised. On cancellation of the OUTER
    task (voice deadline teardown / caller cancel) the inner task is
    cancelled and awaited to completion before re-raising, so the permit —
    released by the outer task's done-callback — is never freed while the
    delegated work is still unwinding (preserves the pre-S-2 release
    timing exactly).

    Both bounds are read off the module at call time so tests can
    monkeypatch them."""
    inner = asyncio.create_task(
        _run_delegated_agent(cfg, task_text, context_text,
                             resolution=resolution,
                             output_format=output_format))
    ceiling = _DELEGATION_CEILING_S
    if not math.isfinite(ceiling) or ceiling <= 0:
        ceiling = 600.0  # fail closed to the shipped default, never hang
    try:
        done, pending = await asyncio.wait({inner}, timeout=ceiling)
    except asyncio.CancelledError:
        # The bounded runner may be abandoned by a voice deadline or caller
        # while the delegated coroutine turns cancellation into an exception.
        # Retrieve that exception without rendering it so private task/result
        # content cannot reach the event loop's exception handler.
        inner.add_done_callback(_retrieve_late_task_exception)
        inner.cancel()
        # Await the actual unwind so the permit (released by the OUTER
        # task's done-callback) never frees while the delegated work is
        # still running — matching the pre-S-2 release timing, where the
        # outer task WAS the delegated coroutine. Tolerates overlapping
        # re-cancellation (Codex review finding 2) up to the teardown
        # bound; the voice teardown's own asyncio.wait bounds how long
        # the CALLER waits regardless.
        if not await _await_task_teardown(inner, _CEILING_TEARDOWN_BOUND_S):
            logger.warning(
                "delegation cancel: delegated task still unwinding after "
                "%.0fs teardown bound — permit will free early (S-2)",
                _CEILING_TEARDOWN_BOUND_S,
            )
        raise
    if pending:
        inner.add_done_callback(_retrieve_late_task_exception)
        inner.cancel()
        if not await _await_task_teardown(inner, _CEILING_TEARDOWN_BOUND_S):
            logger.warning(
                "delegation ceiling: delegated task still unwinding after "
                "%.0fs teardown bound — permit will free early (S-2)",
                _CEILING_TEARDOWN_BOUND_S,
            )
        raise DelegationCeilingExceeded(
            f"delegated turn exceeded the {ceiling:.0f}s wall-clock "
            f"ceiling and was cancelled (S-2 runaway backstop)")
    return inner.result()


async def _await_task_teardown(inner: asyncio.Task, bound_s: float) -> bool:
    """Wait up to *bound_s* for *inner* to actually finish, tolerating
    repeated cancellation of the CALLING task.

    Each re-cancellation of the caller lands here as a CancelledError out
    of ``asyncio.wait``; it is absorbed and the wait re-entered with the
    REMAINING budget, so an overlapping second cancel (voice deadline
    teardown racing a shutdown sweep) cannot end the outer task — and
    thereby free its permit — while the delegated work is still unwinding.
    Absorbing the extra cancels is safe: the cancel-path caller re-raises
    its original CancelledError immediately after this returns, and the
    wait is hard-bounded by *bound_s*. One deliberate consequence (Codex
    round-2 residual, accepted): a cancel that FIRST arrives during the
    ceiling-path teardown is absorbed too, so the task ends with
    ``DelegationCeilingExceeded`` rather than cancelled — bounded by
    *bound_s*, and it routes completion to the NOTIFYING failure branch
    instead of the silent cancelled branch, which is strictly more
    informative for the engager. Returns True when *inner* really ended."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + bound_s
    while not inner.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({inner}, timeout=remaining)
        except asyncio.CancelledError:
            continue
    return inner.done()


def _new_voice_job(
    *,
    job_id: str,
    parent_job_id: str | None,
    cfg: Any,
    specialist_role: str,
    origin: dict,
    task_text: str,
    context_text: str,
    handoff_id: str | None = None,
) -> VoiceJob:
    """Build the durable ACCEPTED row from trusted turn provenance."""
    raw_user_id = origin.get("user_id")
    return VoiceJob(
        id=job_id,
        parent_job_id=parent_job_id,
        creating_role=str(
            origin.get("execution_role") or origin.get("role") or "assistant"
        ),
        specialist_role=specialist_role,
        specialist_display_name=_specialist_display_name(cfg, specialist_role),
        creator_peer="voice",
        creator_user_id=(None if raw_user_id is None else str(raw_user_id)),
        scope_id=str(origin.get("chat_id") or origin.get("scope_id") or ""),
        origin_route_id=str(origin["voice_route_id"]),
        origin_device_id=str(origin["origin_device_id"]),
        task=task_text,
        context=context_text,
        created_at=time.time(),
        started_at=None,
        terminal_at=None,
        expires_at=None,
        execution_state=ExecutionState.ACCEPTED,
        delivery_state=DeliveryState.NONE,
        result=None,
        failure=None,
        awaiting_input=False,
        continuable_until=None,
        delivery_sequence=0,
        delivery_attempt_id=None,
        lease_until=None,
        cancel_pending=False,
        job_control_id=(
            str(origin["voice_job_control_id"])
            if isinstance(origin.get("voice_job_control_id"), str)
            and origin["voice_job_control_id"].strip()
            else None
        ),
        handoff_id=handoff_id,
    )


async def _await_voice_persistence(operation: Awaitable[Any]) -> Any:
    """Finish one terminal write even if lifecycle cancellation arrives."""
    task = asyncio.create_task(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            break
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _persist_voice_terminal(
    operation: Awaitable[Any],
    *,
    registry,
    job_id: str,
    specialist_role: str,
) -> str:
    """Persist a terminal state, then try one metadata-only safe fallback."""
    try:
        await _await_voice_persistence(operation)
        return "primary"
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — never render a persistence exception
        logger.error(
            "Voice job %s role=%s terminal persistence failed; "
            "attempting safe fallback",
            job_id[:8], specialist_role,
        )

    try:
        await _await_voice_persistence(registry.fail_compat(
            job_id,
            JobFailure(
                "persistence_failed",
                "Specialist result could not be saved.",
            ),
        ))
        return "fallback"
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — restart recovery remains authoritative
        logger.error(
            "Voice job %s role=%s safe terminal fallback failed; "
            "scheduling reconciliation",
            job_id[:8], specialist_role,
        )
        try:
            registry.schedule_failure_reconciliation(job_id)
        except Exception:  # noqa: BLE001 — restart recovery remains authoritative
            logger.error(
                "Voice job %s role=%s reconciliation scheduling failed; "
                "restart recovery required",
                job_id[:8], specialist_role,
            )
        return "unpersisted"


async def _run_voice_job_lifecycle(
    *,
    registry,
    job_id: str,
    cfg: Any,
    specialist_role: str,
    task_text: str,
    context_text: str,
    origin: dict,
    resolution: Any,
    started_at: float,
) -> None:
    """Run and persist one job inside the registry-owned task lifetime."""
    try:
        try:
            output = await _run_delegated_agent_bounded(
                cfg,
                task_text,
                context_text,
                resolution=resolution,
                output_format=VOICE_JOB_OUTPUT_FORMAT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — model text stays private
            kind = _classify_error(exc).value
            await _persist_voice_terminal(
                registry.fail_compat(
                    job_id,
                    JobFailure(
                        kind=kind,
                        message="Specialist could not complete the voice job.",
                    ),
                ),
                registry=registry,
                job_id=job_id,
                specialist_role=specialist_role,
            )
            logger.info(
                "Voice job %s role=%s terminal=failed kind=%s elapsed_s=%.2f",
                job_id[:8], specialist_role, kind, time.time() - started_at,
            )
            return

        try:
            voice_result = parse_voice_job_result(output.structured_output)
        except VoiceJobResultError:
            await _persist_voice_terminal(
                registry.fail_compat(
                    job_id,
                    JobFailure(
                        "invalid_specialist_result",
                        "Specialist returned an invalid structured result.",
                    ),
                ),
                registry=registry,
                job_id=job_id,
                specialist_role=specialist_role,
            )
            logger.warning(
                "Voice job %s role=%s terminal=failed "
                "kind=invalid_specialist_result elapsed_s=%.2f",
                job_id[:8], specialist_role, time.time() - started_at,
            )
            return

        # Resolve disclosure without placing either full result or approved
        # spoken text on a Gary-facing surface. Task 4 re-evaluates this
        # durable envelope immediately before delivery.
        spoken = spoken_text_for(
            voice_result,
            prompted=False,
            identity_clearance=voice_identity_clearance(origin),
        )
        durable_result = json.dumps(
            output.structured_output,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        persistence = await _persist_voice_terminal(
            registry.finish_voice_result(
                job_id,
                durable_result,
                awaiting_input=voice_result.awaiting_input,
                delivery_ttl_s=voice_result.delivery_ttl_s,
            ),
            registry=registry,
            job_id=job_id,
            specialist_role=specialist_role,
        )
        logger.info(
            "Voice job %s role=%s terminal=%s status=%s sensitivity=%s "
            "result_chars=%d spoken_chars=%d elapsed_s=%.2f",
            job_id[:8], specialist_role,
            "succeeded" if persistence == "primary" else persistence,
            voice_result.status, voice_result.sensitivity,
            len(durable_result), len(spoken), time.time() - started_at,
        )
    except asyncio.CancelledError:
        # This is also safe after a cancellation arrived during a successful
        # shielded terminal write: fail_compat then observes a terminal row
        # and is an idempotent no-op.
        await _persist_voice_terminal(
            registry.fail_compat(
                job_id,
                JobFailure("cancelled", "Specialist job was cancelled."),
            ),
            registry=registry,
            job_id=job_id,
            specialist_role=specialist_role,
        )
        raise


async def _run_voice_job_after_bind(
    *,
    start_gate: asyncio.Event,
    lifecycle_kwargs: dict[str, Any],
) -> None:
    """Prevent specialist work from starting before RUNNING is durable."""
    await start_gate.wait()
    await _run_voice_job_lifecycle(**lifecycle_kwargs)


def _create_voice_lifecycle_task(
    *,
    start_gate: asyncio.Event,
    lifecycle_kwargs: dict[str, Any],
) -> asyncio.Task:
    """Create the bind-gated lifecycle task without leaking a coroutine."""
    coroutine = _run_voice_job_after_bind(
        start_gate=start_gate,
        lifecycle_kwargs=lifecycle_kwargs,
    )
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


async def _start_voice_async_job(
    *,
    cfg: Any,
    specialist_role: str,
    task_text: str,
    context_text: str,
    origin: dict,
    resolution: Any,
    permit: Any,
    handoff: _PermitHandoff,
    handoff_reservation: Any | None = None,
    parent_job_id: str | None = None,
) -> dict:
    """Persist ACCEPTED, bind RUNNING task/permit ownership, return metadata."""
    registry = _specialist_registry.job_registry
    await registry.load()
    # Apply result TTL before the atomic route-capacity check so an expired
    # READY row cannot consume one of the configured live backlog slots.
    await registry.expire_due()
    job_id = str(uuid.uuid4())
    handoff_id = str(uuid.uuid4()) if handoff_reservation is not None else None
    job = _new_voice_job(
        job_id=job_id,
        parent_job_id=parent_job_id,
        cfg=cfg,
        specialist_role=specialist_role,
        origin=origin,
        task_text=task_text,
        context_text=context_text,
        handoff_id=handoff_id,
    )
    actor = _job_actor_from_origin(origin)
    if parent_job_id is not None and actor is None:
        raise JobAuthorizationError("voice continuation has no actor")
    created = False
    task: asyncio.Task | None = None
    reservation_committed = False
    start_gate = asyncio.Event()
    try:
        try:
            if parent_job_id is None:
                await registry.create(
                    job,
                    max_active_ready_per_route=_voice_job_route_cap,
                )
            else:
                await registry.create_continuation(
                    parent_job_id,
                    job,
                    actor=actor,
                    max_active_ready_per_route=_voice_job_route_cap,
                )
            created = True
        except JobRouteCapacityError:
            if handoff_reservation is not None:
                handoff_reservation.release()
            return _result({
                "status": "error",
                "kind": "route_capacity_reached",
                "message": (
                    f"This voice route already has {_voice_job_route_cap} "
                    "specialist jobs "
                    "awaiting completion or delivery."
                ),
            })
        except BaseException:
            # Atomic persistence can publish and then re-raise cancellation.
            created = registry.get(job_id) is not None
            raise

        started_at = time.time()
        task = _create_voice_lifecycle_task(
            start_gate=start_gate,
            lifecycle_kwargs={
                "registry": registry,
                "job_id": job_id,
                "cfg": cfg,
                "specialist_role": specialist_role,
                "task_text": task_text,
                "context_text": context_text,
                "origin": dict(origin),
                "resolution": resolution,
                "started_at": started_at,
            },
        )
        try:
            await registry.bind_task(job_id, task, permit=permit)
        except BaseException:
            if registry.owns_task(job_id, task):
                handoff.transferred = True
                start_gate.set()
                _record_launch_safe(specialist_role)
            raise
        handoff.transferred = True
        if handoff_reservation is not None:
            pending_job = await registry.mark_handoff_pending(job_id, handoff_id)
            handoff_reservation.commit(pending_job)
            reservation_committed = True
        start_gate.set()
    except BaseException:
        if handoff_reservation is not None and not reservation_committed:
            handoff_reservation.release()
        if handoff.transferred:
            if handoff_reservation is None:
                raise
            if task is not None and not task.done():
                task.cancel()
                await _await_task_teardown(task, _CEILING_TEARDOWN_BOUND_S)
            if created:
                if parent_job_id is None:
                    await registry.cancel(job_id)
                else:
                    await registry.compensate_unbound_continuation(
                        parent_job_id,
                        job_id,
                        actor=actor,
                    )
            raise
        if task is not None and not task.done():
            task.cancel()
            await _await_task_teardown(task, _CEILING_TEARDOWN_BOUND_S)
        if created:
            if parent_job_id is None:
                await registry.cancel(job_id)
            else:
                await registry.compensate_unbound_continuation(
                    parent_job_id,
                    job_id,
                    actor=actor,
                )
        raise

    _record_launch_safe(specialist_role)
    logger.info(
        "Voice job %s role=%s accepted route_bound=true",
        job_id[:8], specialist_role,
    )
    return _result({
        "status": "pending",
        "job_id": job_id,
        "specialist_display_name": job.specialist_display_name,
    })


@tool(
    "delegate_to_agent",
    "Delegate a task to another agent (resident or specialist) and return its result.",
    {"agent": str, "task": str, "context": str, "mode": str},
)
async def delegate_to_agent(args: dict) -> dict:
    """Invoke a Tier 2 specialist via the SDK and return its text.

    Sync mode (default): ``asyncio.wait`` up to 60s, return ok/error
    content; on timeout, attach completion callback and return a
    ``pending`` marker so the delegating resident can narrate
    "still working" and move on.

    Async mode (``mode="async"``): skip the wait, attach callback,
    return ``pending`` immediately.
    """
    # Import lazily — matches the `agent.py` origin_var ContextVar.
    import agent as agent_mod

    agent_name = args.get("agent", "")
    task_text = args.get("task", "")
    context_text = args.get("context", "") or ""
    mode = args.get("mode", "sync") or "sync"

    # AR-2: snapshot at entry — this handler awaits (channel setup,
    # engagement/delegation dispatch) and must not read a holder that a
    # later turn has since rewritten in place.
    origin = _snapshot_origin()

    # Concierge is the sole server-authorized voice handoff role.  Do this
    # synchronously, before `_prelaunch` reaches a plugin/limiter await, so a
    # live voice turn reserves foreground ownership before any work can race
    # ahead.  Task 3 owns durable bind/commit and will consume this object.
    mode, handoff_reservation, static_error = validate_voice_handoff_static(
        agent_name, origin, mode,
    )
    if static_error is not None:
        return static_error
    if handoff_reservation is not None:
        if handoff_reservation.reserve() is False:
            return _result({
                "status": "error",
                "kind": "handoff_after_speech",
                "message": (
                    "A specialist handoff cannot start after this voice turn "
                    "has begun speaking."
                ),
            })

    # A4: THE unified prelaunch pipeline — one call that runs EVERY
    # pre-launch gate (ACL, not-initialized, input-size bounds, depth, mode,
    # target resolution, resident-compat, requires-seam, concurrency,
    # progress) in a fixed order, so no gate is bypassable by another and NO
    # side effect (topic, engagement/delegation record, task, driver start,
    # progress emission) can precede a clean return. It dominates both the
    # interactive branch and the sync/async path below. The input-size bounds
    # live INSIDE _prelaunch (after the ACL) so an unauthorized caller is
    # denied `delegation_not_declared` regardless of payload size and no
    # telemetry is ever keyed on a caller-supplied target pre-auth. `permit`
    # (Task 6, spec §4.6) is the concurrency slot this delegation now owns —
    # the caller owns its lifetime from here: sync/async releases via the
    # task done-callback; interactive hands it to the engagement record.
    try:
        cfg, resolution, permit, prelaunch_error = await _prelaunch(
            agent_name, origin, mode, task_text, context_text)
    except BaseException:
        if handoff_reservation is not None:
            handoff_reservation.release()
        raise
    if prelaunch_error is not None:
        if handoff_reservation is not None:
            handoff_reservation.release()
        return prelaunch_error

    # Task 6 (spec §4.6): lexical ownership guard. `owned` holds the permit
    # from _prelaunch's acquire until launch transfers it. The finally
    # releases on ANY exit before transfer — including a CancelledError
    # raised at an await between here and launch (which `except Exception`
    # would miss), and every early error return. At each true ownership
    # transfer the body sets `owned = None`: for sync/async the moment the
    # task's `_permit_release_callback` done-callback is attached (which then
    # becomes the sole release); for interactive after `driver.start()`
    # succeeds (the engagement record's permit is then released by an
    # `EngagementRegistry` terminal transition). Every release is idempotent,
    # so an overlapping release on the SAME permit is a safe no-op.
    owned = permit
    try:
        if mode == "interactive":
            # Task 6 (spec §4.6): the `owned` lexical ownership guard (try/finally
            # around this whole body) releases `permit` on ANY exit before launch
            # transfer — including a CancelledError raised at any await below and
            # every early error return here — so these paths need no inline
            # release. Ownership transfers to the engagement record only AFTER
            # driver.start() succeeds (see the `owned = None` there).
            # Need telegram channel + supergroup configured.
            if _channel_manager is None:
                return _result({"status": "error", "kind": "no_channel_manager",
                                "message": "channel manager missing"})
            channel = _channel_manager.get(origin.get("channel", "telegram"))
            # E-F (v0.30.0): if supergroup IS configured but
            # engagement_permission_ok is still False, the boot-time setup may
            # have lost a race with a transient network blip. The setup is now
            # wired into _rebuild's tail (self-healing on every reconnect), but
            # in the rare window where the user spawns an engagement before any
            # rebuild has completed, attempt one in-line retry before giving up.
            # Idempotent; cheap on success.
            if (channel is not None
                    and getattr(channel, "engagement_supergroup_id", 0)
                    and not getattr(channel, "engagement_permission_ok", False)):
                try:
                    await channel.setup_engagement_features()  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "engage_executor: in-line setup_engagement_features "
                        "retry failed: %s", exc,
                    )
            if (channel is None
                    or not getattr(channel, "engagement_supergroup_id", 0)
                    or not getattr(channel, "engagement_permission_ok", False)):
                return _engagement_unavailable_result(origin)  # R-2 (v0.69.7)
            # v0.37.1 D-1: U3 title format for specialist engagements too
            # (was legacy `#[<role>] <task> · <id8>`). Bubble carries the
            # role icon via icon_id_for_role; title is `<state> <task>`.
            from channels.state_emoji import (
                STATE_EMOJI, compose_topic_title, concise_task,
            )
            first_line = (task_text or "").splitlines()[0]
            short_task = concise_task(first_line) or "engagement"
            topic_name = compose_topic_title(
                state="active", short_task=short_task,
            )
            try:
                topic_id = await channel.open_engagement_topic(
                    name=topic_name,
                    role=agent_name,
                )
            except Exception as exc:  # noqa: BLE001
                return _result({"status": "error", "kind": "topic_create_failed",
                                "message": str(exc)})
            # §3.8 (Sol #4): record the specialist's plugin binding so verify can
            # disclose this engagement if a later plugin_update supersedes its
            # artifact (informational — mirrors the executor-engagement case). The
            # specialist runs on the same tier:role resolution _build_specialist_
            # options uses below.
            # Sol round-3 H7b: resolve ONCE and feed the SAME result to both the
            # engagement record and the options builder, so a concurrent update can't
            # make the recorded binding disagree with what actually launches.
            # A5: when the requires gate (Task 5) already resolved this agent's
            # plugins (non-empty `cfg.requires`), reuse that SAME ResolutionResult
            # instead of resolving again — the engagement record's binding must
            # never disagree with what the requires gate actually validated.
            # `resolution` (from `_prelaunch`) is None whenever `cfg.requires` is
            # empty, in which case this resolves fresh exactly as before Task 5.
            if resolution is not None:
                _spec_res = resolution
            else:
                _spec_tier = (_agent_registry.tier_for_role(agent_name)
                              if _agent_registry is not None else None) or "specialist"
                _spec_res = plugin_registry.resolve_for(f"{_spec_tier}:{agent_name}")
            _spec_arts = tuple(
                {"name": rp.name, "artifact_id": rp.artifact_id, "path": rp.path}
                for rp in _spec_res.plugins)
            # Create record
            rec = await _engagement_registry.create(
                kind="specialist", role_or_type=agent_name, driver="in_casa",
                task=task_text, origin=dict(origin), topic_id=topic_id,
                plugin_artifacts=_spec_arts,
            )
            # Task 6 (spec §4.6): stash the permit on the engagement record so
            # EVERY registry terminal transition (mark_error, mark_cancelled,
            # mark_completed, try_transition_terminal) releases it — including
            # the direct mark_error routes (resume/orphan failures in
            # channels/telegram.py) that bypass `_finalize_engagement`.
            # `_finalize_engagement` also releases it as an idempotent fallback.
            # Ownership is NOT yet transferred here: `owned` stays set until
            # driver.start() succeeds, so a cancellation/error at any await
            # before that still releases via the outer finally (all releases
            # are idempotent).
            rec.permit = permit
            # Persist initial state emoji so update_topic_state knows
            # whether it needs to edit the title (no-op when state didn't change).
            try:
                await _engagement_registry.set_channel_state(
                    rec.id, current_state_emoji=STATE_EMOJI["active"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("set_channel_state(active) failed: %s", exc)

            # Build options + start driver (off-loop: registry resolve is file IO).
            options = await asyncio.to_thread(
                _build_specialist_options,
                cfg,
                resolution=_spec_res,
                extra_casa_tools=(
                    "mcp__casa-framework__query_engager",
                    "mcp__casa-framework__emit_completion",
                ),
            )

            prompt = (
                f"You are engaged with the user in a Telegram forum topic.\n"
                f"Task: {task_text}\n\n"
                f"Context from Ellen:\n{context_text or '(none)'}\n\n"
                f"When the task is complete, call emit_completion(text=..., "
                f"artifacts=..., next_steps=..., status='ok')."
            )

            driver = getattr(agent_mod, "active_engagement_driver", None)
            if driver is None:
                await _engagement_registry.mark_error(
                    rec.id, kind="no_driver",
                    message="engagement driver not initialized",
                )
                await _abort_engagement_topic(channel, rec.id, topic_id)
                # permit released by mark_error (registry terminal transition)
                # + the outer finally (owned still set) — both idempotent.
                return _result({"status": "error", "kind": "no_driver",
                                "message": "engagement driver not initialized"})
            try:
                await driver.start(rec, prompt=prompt, options=options)
            except Exception as exc:  # noqa: BLE001
                await _engagement_registry.mark_error(rec.id, kind="driver_start_failed",
                                                      message=str(exc))
                await _abort_engagement_topic(channel, rec.id, topic_id)
                # permit released by mark_error + the outer finally (idempotent).
                return _result({"status": "error", "kind": "driver_start_failed",
                                "message": str(exc)})

            # Task 6 (spec §4.6): driver is live — transfer permit ownership to
            # the engagement record (released by an EngagementRegistry terminal
            # transition or _finalize_engagement) by clearing `owned` FIRST, so
            # the following non-raising launch count can never reach the outer
            # finally to release the now-live engagement's permit.
            owned = None  # __TRANSFER_INTERACTIVE__
            _record_launch_safe(agent_name)
            return _result({
                "status": "pending",
                "engagement_id": rec.id,
                "agent": agent_name,
                "mode": "interactive",
                "topic_id": topic_id,
            })

        is_voice = str(origin.get("channel", "")) == "voice"
        if is_voice and mode == "async":
            handoff = _PermitHandoff()
            try:
                result = await _start_voice_async_job(
                    cfg=cfg,
                    specialist_role=agent_name,
                    task_text=task_text,
                    context_text=context_text,
                    origin=origin,
                    resolution=resolution,
                    permit=permit,
                    handoff=handoff,
                    handoff_reservation=handoff_reservation,
                )
                if handoff_reservation is not None:
                    try:
                        launch_status = json.loads(
                            result["content"][0]["text"],
                        ).get("status")
                    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                        launch_status = None
                    if launch_status == "error":
                        # The real launch path releases before returning an
                        # error; preserve that contract at this seam too.
                        handoff_reservation.release()
            finally:
                if handoff.transferred:
                    owned = None  # __TRANSFER_VOICE_JOB__
            return result

        delegation_id = str(uuid.uuid4())

        # A4: synchronous voice turn budget. The deadline is ABSOLUTE
        # (origin["voice_deadline"], a monotonic loop.time() value); remaining
        # wait is recomputed because loop.time() advances across awaits. Async
        # voice returned above and is deliberately not subject to this gate.
        # Non-voice turns use the general 60s ceiling below.
        loop = asyncio.get_running_loop() if is_voice else None
        raw_deadline = origin.get("voice_deadline") if is_voice else None

        if is_voice:
            # Pre-register short-circuit: an already-expired / missing /
            # non-finite deadline never even registers a record (keeps the
            # tombstone store clean for the common "spoke too late" case).
            # Task 6: no task is created on this path — the outer finally
            # (`owned` still set) releases the permit.
            if _voice_wait_from_deadline(raw_deadline, loop) is None:
                logger.info(
                    "Delegation → %s voice budget already exceeded at entry — "
                    "not registered", agent_name,
                )
                return _deadline_exceeded_result(delegation_id, agent_name)

        started_at = time.time()
        record = DelegationRecord(
            id=delegation_id, agent=agent_name, started_at=started_at,
            origin=dict(origin),
        )
        # Task 6 (spec §4.6): stash the permit on the record for
        # observability only. The SOLE release for this legacy path is the
        # `_permit_release_callback` done-callback attached to the task below
        # (robust even if the task is cancelled before its coroutine starts);
        # `SpecialistRegistry` terminal transitions deliberately do NOT
        # release (that would race the voice teardown's `cancel_delegation`
        # against a still-unwinding task).
        record.permit = permit
        await _specialist_registry.register_delegation(record)

        voice_wait_s: float | None = None
        if is_voice:
            # RECOMPUTE after register_delegation — its tombstone lock + I/O
            # consumed wall-clock time, so the pre-register wait is now stale.
            # If registration itself ate the remaining budget, remove the
            # just-registered record (no orphan tombstone) and bail WITHOUT
            # ever launching the specialist task. Task 6: no task created →
            # the outer finally releases the permit (owned still set).
            voice_wait_s = _voice_wait_from_deadline(raw_deadline, loop)
            if voice_wait_s is None:
                await _specialist_registry.cancel_delegation(delegation_id)
                logger.info(
                    "Delegation %s → %s voice budget exhausted during "
                    "registration — specialist not started",
                    delegation_id[:8], agent_name,
                )
                return _deadline_exceeded_result(delegation_id, agent_name)

        # Task 6 (spec §4.6): create the task, then ATOMICALLY (no await between)
        # attach the unconditional permit-release done-callback and transfer
        # ownership (`owned = None`). Attaching the release as a done-callback —
        # not the coroutine's own finally — means even a task cancelled before
        # its coroutine ever runs still releases the slot. Clear `owned` BEFORE
        # counting the launch so a telemetry failure can never reach the outer
        # finally to release the now-live task's permit (the count goes through
        # the non-raising wrapper).
        task = asyncio.create_task(
            _run_delegated_agent_bounded(
                cfg, task_text, context_text, resolution=resolution,
                output_format=(VOICE_JOB_OUTPUT_FORMAT if is_voice else None)))
        if permit is not None:
            task.add_done_callback(_permit_release_callback(permit))
        owned = None  # __TRANSFER_SYNC__
        _record_launch_safe(agent_name)

        if mode == "async":
            _attach_completion_callback(task, record)
            logger.info(
                "Delegation %s → %s (async mode)",
                delegation_id[:8], agent_name,
            )
            return _result({
                "status": "pending",
                "delegation_id": delegation_id,
                "agent": agent_name,
                "mode": "async",
            })

        # mode == "sync"
        if voice_wait_s is not None:
            # A4: voice never degrades to `pending` (async/interactive are
            # already rejected in _prelaunch) — there is no follow-up channel
            # to deliver a LATER completion notification on. Wait only up to
            # the budget computed above (deadline − reserve, capped at 60s); on
            # expiry cancel + bounded teardown + speak the typed error.
            try:
                done, pending = await asyncio.wait({task}, timeout=voice_wait_s)
            except asyncio.CancelledError:
                task.cancel()
                await _specialist_registry.cancel_delegation(delegation_id)
                raise
            if pending:
                return await _voice_deadline_exceeded(task, delegation_id, agent_name)
        else:
            try:
                done, pending = await asyncio.wait({task}, timeout=_SYNC_WAIT_TIMEOUT_S)
            except asyncio.CancelledError:
                task.cancel()
                await _specialist_registry.cancel_delegation(delegation_id)
                raise

            if pending:
                # 60s elapsed; detach and degrade to pending with callback.
                _attach_completion_callback(task, record)
                logger.info(
                    "Delegation %s → %s timed out at 60s — degraded to pending",
                    delegation_id[:8], agent_name,
                )
                return _result({
                    "status": "pending",
                    "delegation_id": delegation_id,
                    "agent": agent_name,
                    "timeout_s": 60,
                    "note": (
                        "Delegation continues in background; you will receive a "
                        "NOTIFICATION when complete."
                    ),
                })

        # Task finished within budget — return ok or error synchronously.
        finished = next(iter(done))
        if finished.exception() is not None:
            exc = finished.exception()
            kind = _classify_error(exc).value
            elapsed = time.time() - started_at
            if is_voice:
                from job_registry import JobFailure
                failure = JobFailure(
                    kind=kind,
                    message="Specialist could not complete the voice job.",
                )
                await _specialist_registry.job_registry.fail_compat(
                    delegation_id, failure)
                logger.info(
                    "Delegation %s → %s failed status=failed kind=%s (%.2fs)",
                    delegation_id[:8], agent_name, kind, elapsed,
                )
                return _result({
                    "status": "error",
                    "delegation_id": delegation_id,
                    "agent": agent_name,
                    "kind": kind,
                    "message": failure.message,
                    "elapsed_s": elapsed,
                })

            await _specialist_registry.fail_delegation(delegation_id, exc)
            logger.info(
                "Delegation %s → %s failed: %s (%s)",
                delegation_id[:8], agent_name, kind, exc,
            )
            return _result({
                "status": "error",
                "delegation_id": delegation_id,
                "agent": agent_name,
                "kind": kind,
                "message": str(exc),
                "elapsed_s": elapsed,
            })

        delegated_output = finished.result()
        if is_voice:
            try:
                voice_result = parse_voice_job_result(
                    delegated_output.structured_output)
            except VoiceJobResultError:
                from job_registry import JobFailure
                failure = JobFailure(
                    kind="invalid_specialist_result",
                    message="Specialist returned an invalid structured result.",
                )
                await _specialist_registry.job_registry.fail_compat(
                    delegation_id, failure)
                elapsed = time.time() - started_at
                logger.warning(
                    "Delegation %s → %s returned an invalid voice result "
                    "status=failed (%.2fs)",
                    delegation_id[:8], agent_name, elapsed,
                )
                return _result({
                    "status": "error",
                    "delegation_id": delegation_id,
                    "agent": agent_name,
                    "kind": failure.kind,
                    "message": failure.message,
                    "elapsed_s": elapsed,
                })

            # The validated structured envelope is durable job data, not Gary
            # context. Persist it once, then resolve the only text allowed onto
            # the voice tool wire from sensitivity + server-bound identity.
            await _specialist_registry.job_registry.finish_voice_result(
                delegation_id,
                json.dumps(delegated_output.structured_output),
                awaiting_input=voice_result.awaiting_input,
                delivery_ttl_s=voice_result.delivery_ttl_s,
            )
            text = spoken_text_for(
                voice_result,
                prompted=False,
                identity_clearance=voice_identity_clearance(origin),
            )
        else:
            text = delegated_output.text
        # Task 6 (spec §4.6): bound the synchronous result + expose the flag on
        # the wire so the narrating resident can disclose a clipped answer.
        original_text_length = len(text)
        text, output_truncated = specialist_limits.truncate_output(text)
        if output_truncated:
            logger.warning(
                "delegated agent %s output truncated: %d > %d chars (spec §4.6)",
                agent_name, original_text_length, specialist_limits._MAX_OUTPUT_CHARS,
            )
        if not is_voice:
            await _specialist_registry.complete_delegation(delegation_id)
        elapsed = time.time() - started_at
        logger.info(
            "Delegation %s → %s ok (%.2fs)",
            delegation_id[:8], agent_name, elapsed,
        )
        return _result({
            "status": "ok",
            "delegation_id": delegation_id,
            "agent": agent_name,
            "elapsed_s": elapsed,
            "text": text,
            "output_truncated": output_truncated,
        })
    finally:
        if owned is not None:
            owned.release()


# ---------------------------------------------------------------------------
# Metadata-only voice job control. Full case/result data never crosses these
# tool envelopes; continuation injects it directly into the specialist child.
# ---------------------------------------------------------------------------


def _job_not_found_result() -> dict:
    return _result({
        "status": "error",
        "kind": "job_not_found",
        "message": "No matching specialist job is available.",
    })


def _no_matching_job_result() -> dict:
    return _result({
        "status": "error",
        "kind": "no_matching_job",
        "message": "No eligible specialist job is available.",
    })


def _ambiguous_job_result(jobs: list[VoiceJob]) -> dict:
    return _result({
        "status": "error",
        "kind": "ambiguous_job",
        "message": "More than one specialist job matches; choose a job ID.",
        "choices": [
            {
                "job_id": job.id,
                "specialist_display_name": job.specialist_display_name,
            }
            for job in jobs
        ],
    })


def _requested_job_id(args: dict) -> str | None:
    raw = args.get("job_id")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip()
    return normalized or None


def _authorized_jobs(registry, actor: JobActor) -> list[VoiceJob]:
    return [job for job in registry.all() if _actor_owns_job(actor, job)]


def _resolve_authorized_job(
    registry,
    actor: JobActor,
    requested_id: str | None,
    *,
    predicate,
    explicit_mismatch_kind: str = "job_not_found",
) -> tuple[VoiceJob | None, dict | None]:
    if requested_id is not None:
        job = registry.get(requested_id)
        if job is None or not _actor_owns_job(actor, job):
            return None, _job_not_found_result()
        if not predicate(job):
            if explicit_mismatch_kind == "job_not_continuable":
                return None, _result({
                    "status": "error",
                    "kind": "job_not_continuable",
                    "message": "That specialist job cannot be continued.",
                })
            return None, _job_not_found_result()
        return job, None

    matches = [
        job for job in _authorized_jobs(registry, actor)
        if predicate(job)
    ]
    if not matches:
        return None, _no_matching_job_result()
    if len(matches) != 1:
        return None, _ambiguous_job_result(matches)
    return matches[0], None


def _job_status_value(job: VoiceJob) -> str:
    if job.execution_state is ExecutionState.ACCEPTED:
        return "pending"
    if job.execution_state is ExecutionState.ORPHANED:
        return "failed"
    return job.execution_state.value.lower()


def _job_metadata(job: VoiceJob) -> dict:
    delivery_status = job.delivery_state.value.lower()
    routes = getattr(_runtime, "voice_route_registry", None)
    connected_route = (
        routes.get_connected(job.origin_route_id)
        if routes is not None and job.origin_route_id else None
    )
    connected_capabilities = getattr(connected_route, "capabilities", ())
    if (
        job.delivery_state is DeliveryState.READY
        and job.origin_route_id
        and routes is not None
        and (
            connected_route is None
            or not _BACKGROUND_ROUTE_CAPABILITIES
            <= frozenset(connected_capabilities)
        )
    ):
        delivery_status = "waiting_for_route"
    return {
        "status": _job_status_value(job),
        "job_id": job.id,
        "specialist_display_name": job.specialist_display_name,
        "awaiting_input": bool(job.awaiting_input),
        "delivery_status": delivery_status,
    }


def _status_candidate(job: VoiceJob) -> bool:
    return (
        job.delivery_state not in {
            DeliveryState.DELIVERED,
            DeliveryState.CANCELLED,
            DeliveryState.EXPIRED,
        }
        or job.execution_state in {
            ExecutionState.ACCEPTED,
            ExecutionState.RUNNING,
        }
    )


def _cancellable_job(job: VoiceJob) -> bool:
    return (
        job.execution_state in {
            ExecutionState.ACCEPTED,
            ExecutionState.RUNNING,
        }
        or job.delivery_state in {
            DeliveryState.READY,
            DeliveryState.CLAIMED,
            DeliveryState.AUTHORIZED,
        }
    )


def _continuable_job(job: VoiceJob, *, now: float) -> bool:
    return (
        job.execution_state is ExecutionState.SUCCEEDED
        and job.awaiting_input
        and job.continuable_until is not None
        and job.continuable_until > now
        and (job.expires_at is None or job.expires_at > now)
        and job.delivery_state not in {
            DeliveryState.CANCELLED,
            DeliveryState.EXPIRED,
        }
        and not job.cancel_pending
        and job.result is not None
    )


def _detail_available_job(job: VoiceJob, *, now: float) -> bool:
    available = (
        job.execution_state is ExecutionState.SUCCEEDED
        and job.result is not None
        and (job.expires_at is None or job.expires_at > now)
        and job.delivery_state not in {
            DeliveryState.CANCELLED,
            DeliveryState.EXPIRED,
        }
        and not job.cancel_pending
    )
    if not available:
        return False
    try:
        result = parse_voice_job_result(json.loads(job.result))
    except (json.JSONDecodeError, TypeError, VoiceJobResultError):
        return False
    return result.status != "needs_clarification"


@tool(
    "voice_job_status",
    "Check specialist job status.",
    {"job_id": str},
)
async def voice_job_status(args: dict) -> dict:
    origin = _snapshot_origin()
    actor = _job_actor_from_origin(origin)
    if actor is None or _specialist_registry is None:
        return _job_not_found_result()
    registry = _specialist_registry.job_registry
    await registry.load()
    await registry.expire_due()
    requested_id = _requested_job_id(args)
    if requested_id is not None:
        job = registry.get(requested_id)
        if job is None or not _actor_owns_job(actor, job):
            return _job_not_found_result()
    else:
        job, error = _resolve_authorized_job(
            registry,
            actor,
            None,
            predicate=_status_candidate,
        )
        if error is not None:
            return error
    return _result(_job_metadata(job))


@tool(
    "cancel_voice_job",
    "Cancel a specialist job.",
    {"job_id": str},
)
async def cancel_voice_job(args: dict) -> dict:
    origin = _snapshot_origin()
    actor = _job_actor_from_origin(origin)
    if actor is None or _specialist_registry is None:
        return _job_not_found_result()
    registry = _specialist_registry.job_registry
    await registry.load()
    await registry.expire_due()
    requested_id = _requested_job_id(args)
    if requested_id is not None:
        job = registry.get(requested_id)
        if job is None or not _actor_owns_job(actor, job):
            return _job_not_found_result()
    else:
        job, error = _resolve_authorized_job(
            registry,
            actor,
            None,
            predicate=_cancellable_job,
        )
        if error is not None:
            return error

    if job.delivery_state is DeliveryState.DELIVERED:
        cancel_status = "already_delivered"
    else:
        try:
            cancel_status = (
                await registry.request_cancel(job.id, actor=actor)
            ).status
        except JobAuthorizationError:
            return _job_not_found_result()
    return _result({
        "status": cancel_status,
        "job_id": job.id,
        "specialist_display_name": job.specialist_display_name,
    })


@tool(
    "continue_voice_job",
    "Continue a specialist job with new input.",
    {"job_id": str, "input": str},
)
async def continue_voice_job(args: dict) -> dict:
    origin = _snapshot_origin()
    actor = _job_actor_from_origin(origin)
    if actor is None or _specialist_registry is None:
        return _job_not_found_result()
    continuation_input = args.get("input")
    if not isinstance(continuation_input, str) or not continuation_input.strip():
        return _result({
            "status": "error",
            "kind": "invalid_arguments",
            "message": "input must be a non-empty string.",
        })
    continuation_input = continuation_input.strip()
    if not background_route_available(origin):
        return _background_delivery_unavailable_result()

    registry = _specialist_registry.job_registry
    await registry.load()
    await registry.expire_due()
    now = time.time()
    requested_id = _requested_job_id(args)
    parent, error = _resolve_authorized_job(
        registry,
        actor,
        requested_id,
        predicate=(
            (lambda job: (
                _continuable_job(job, now=now)
                or _detail_available_job(job, now=now)
            ))
            if requested_id is not None
            else (lambda job: _continuable_job(job, now=now))
        ),
        explicit_mismatch_kind="job_not_continuable",
    )
    if error is not None:
        return error

    if not parent.awaiting_input:
        job_id = str(uuid.uuid4())
        child = _new_voice_job(
            job_id=job_id,
            parent_job_id=parent.id,
            cfg=_agent_role_map.get(parent.specialist_role),
            specialist_role=parent.specialist_role,
            origin=origin,
            task_text=continuation_input,
            context_text="",
        )
        try:
            prompted = await registry.create_prompted_delivery(
                parent.id,
                child,
                actor=actor,
                max_active_ready_per_route=_voice_job_route_cap,
            )
        except JobRouteCapacityError:
            return _result({
                "status": "error",
                "kind": "route_capacity_reached",
                "message": (
                    f"This voice route already has {_voice_job_route_cap} "
                    "specialist jobs "
                    "awaiting completion or delivery."
                ),
            })
        except (JobAuthorizationError, JobTransitionError):
            return _result({
                "status": "error",
                "kind": "job_not_continuable",
                "message": "That specialist job cannot be continued.",
            })
        return _result({
            "status": "pending",
            "job_id": prompted.id,
            "specialist_display_name": prompted.specialist_display_name,
        })

    # The complete prior case/result travels backend-to-specialist only. The
    # tool result below contains opaque metadata, never this private envelope.
    private_context = json.dumps({
        "parent_job_id": parent.id,
        "original_task": parent.task,
        "original_context": parent.context,
        "previous_result": parent.result,
    }, ensure_ascii=False, separators=(",", ":"))

    cfg, resolution, permit, prelaunch_error = await _prelaunch(
        parent.specialist_role,
        origin,
        "async",
        continuation_input,
        private_context,
    )
    if prelaunch_error is not None:
        return prelaunch_error

    owned = permit
    try:
        handoff = _PermitHandoff()
        try:
            try:
                result = await _start_voice_async_job(
                    cfg=cfg,
                    specialist_role=parent.specialist_role,
                    task_text=continuation_input,
                    context_text=private_context,
                    origin=origin,
                    resolution=resolution,
                    permit=permit,
                    handoff=handoff,
                    parent_job_id=parent.id,
                )
            finally:
                if handoff.transferred:
                    owned = None
        except JobAuthorizationError:
            return _job_not_found_result()
        except JobTransitionError:
            return _result({
                "status": "error",
                "kind": "job_not_continuable",
                "message": "That specialist job cannot be continued.",
            })
        return result
    finally:
        if owned is not None:
            owned.release()


# ---------------------------------------------------------------------------
# recall_memory — spec §4.3
# ---------------------------------------------------------------------------


def _recall_surface(channel: str, origin: dict) -> str:
    """Attribution surface for a recall render (Task 11): voice speaks aloud;
    an untrusted webhook_trigger turn is ``restricted_webhook`` (never names a
    person, regardless of clearance); everything else is ``text``."""
    if channel == "voice":
        return "voice"
    if origin.get("_origin_route") == "webhook_trigger":
        return "restricted_webhook"
    return "text"


@tool(
    "recall_memory",
    "Search your long-term memory for facts relevant to a query.",
    {"query": str},
)
async def recall_memory(args: dict) -> dict:
    """On-demand semantic recall against the shared 'casa' bank, filtered by the channel's tier clearance (spec §4.3).
    Voice uses budget=low so the rerank never stalls the turn."""
    import agent as agent_mod

    query = (args.get("query") or "").strip()
    if not query:
        return _result({"status": "error", "kind": "empty_query",
                        "message": "Error: query is required"})
    sem = getattr(agent_mod, "active_semantic_memory", None)
    if sem is None:
        # No backend wired: memory CANNOT be checked — never a fake zero-hit.
        return _result({
            "status": "unavailable",
            "message": (
                "Long-term memory could not be checked (no memory backend). "
                "Do NOT say the information doesn't exist — say memory "
                "couldn't be checked."
            ),
        })

    origin = _snapshot_origin()
    role = origin.get("role", "assistant")
    channel = origin.get("channel", "telegram")
    caller_cfg = _agent_role_map.get(role)

    # Tier clearance — the same read-side gate the turn path uses (design §2.3),
    # re-keyed off the unspoofable origin marker (Release A Layer 2): a
    # webhook_trigger turn reads at its declared clearance (public floor, never
    # private); /invoke stays private; missing/unknown route on the webhook
    # channel fails closed to public.
    from personality_types import SpeakerProvenance
    from sensitivity import clearance_for_origin, readable_tiers
    clearance = clearance_for_origin(
        origin.get("_origin_route"), origin.get("_origin_clearance"), channel,
    )
    tags = readable_tiers(clearance)

    budget = "low" if channel == "voice" else "mid"
    tokens = (
        getattr(getattr(caller_cfg, "memory", None), "token_budget", 2000)
        if caller_cfg else 2000
    )
    # Attribution surface + the recalling agent's own identity (Task 11). The
    # render itself is driven by each hit's recorded provenance; current_speaker
    # is honest metadata only.
    surface = _recall_surface(channel, origin)
    current_speaker = (
        getattr(caller_cfg, "speaker_provenance", None)
        or SpeakerProvenance(speaker_kind="system")
    )
    from hindsight_ids import bank_id
    from recall_health import default_telemetry, observed_recall
    from recall_renderer import render_recall
    try:
        hits = await observed_recall(
            path="direct_tool", telemetry=default_telemetry(),
            operation=lambda: sem.recall_items(
                bank_id("casa"), query, tags=tags, max_tokens=tokens,
                clearance=clearance, budget=budget,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # Three-outcome contract (v0.99.0): a failed recall must NOT report
        # status=ok with an empty digest — the model would then (per doctrine)
        # tell the user Casa has no such information, which is false. Any
        # failure here (typed RecallUnavailable — incl. RecallProtocolError — or
        # unexpected) means memory could not be checked. Only RecallUnavailable's
        # slug reasons are trusted in logs; anything else logs its TYPE (an
        # arbitrary .reason attribute could carry content).
        reason = (
            exc.reason if isinstance(exc, RecallUnavailable)
            else type(exc).__name__
        )
        logger.warning(
            "recall_memory outcome=unavailable role=%r reason=%s", role, reason,
        )
        return _result({
            "status": "unavailable",
            "message": (
                "Long-term memory could not be checked (backend unavailable). "
                "Tell the user memory couldn't be checked right now — do NOT "
                "say the information doesn't exist or that you don't have it."
            ),
        })
    digest = render_recall(
        hits, current_speaker=current_speaker, surface=surface,
        clearance=clearance, token_budget=tokens,
    )
    return _result({"status": "ok", "memory": digest})


# ---------------------------------------------------------------------------
# get_schedule — Phase 3.3
# ---------------------------------------------------------------------------


@tool(
    "get_schedule",
    "Return your upcoming scheduled triggers (interval + cron) within a "
    "time window. Returns a markdown bullet list with name, type, cron/interval "
    "description, and next fire time. Own-role only.",
    {"within_hours": int},
)
async def get_schedule(args: dict) -> dict:
    if _trigger_registry is None:
        return {"content": [{"type": "text",
                             "text": "Error: trigger registry not initialized"}]}

    origin = _snapshot_origin()
    if not origin:
        return {"content": [{"type": "text",
                             "text": "Error: get_schedule called outside a turn context"}]}

    role = origin.get("role") or ""
    if not role:
        return {"content": [{"type": "text",
                             "text": "Error: turn origin has no role"}]}

    raw_hours = args.get("within_hours", 24)
    try:
        within_hours = int(raw_hours) if raw_hours is not None else 24
    except (TypeError, ValueError):
        within_hours = 24
    within_hours = max(1, min(720, within_hours))

    summaries = _trigger_registry.list_jobs_for(
        role=role, within_hours=within_hours,
    )

    if not summaries:
        text = f"(no scheduled triggers in the next {within_hours} hours)"
    else:
        lines = []
        for s in summaries:
            if s.type == "cron":
                desc = f"(cron, `{s.schedule_desc}`)"
            else:
                desc = f"(interval, {s.schedule_desc})"
            lines.append(
                f"- **{s.name}** {desc} — next: "
                f"{s.next_fire.isoformat(timespec='seconds')}"
            )
        text = "\n".join(lines)

    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# config_git_commit - Plan 3 (Tier 3 executor support)
# ---------------------------------------------------------------------------


# Bug 7 (v0.14.6): role guard for the privileged config tools.
# Pre-fix: gated only by each agent's runtime.yaml::tools.allowed,
# meaning a copy-paste error or permissive default in a new resident /
# specialist / executor silently exposed addon-restart and config-commit
# powers. Defense in depth at the tool itself.
_PRIVILEGED_CONFIG_ROLES = frozenset({"configurator"})


def _effective_caller_role() -> str | None:
    """Return the calling agent's role for authorisation checks.

    Inside an active engagement (engagement_var set), the calling role
    IS the engagement's role_or_type — this takes precedence over the
    bus's origin_var.role, which inside in_casa engagements still
    reflects the engager (Ellen's "assistant") because contextvars
    inherit through the same async task.

    Returns None if neither context is bound — caller must refuse rather
    than fall back to a permissive default.
    """
    eng = engagement_var.get(None)
    if eng is not None:
        r = getattr(eng, "role_or_type", None)
        if r:
            return r
    try:
        origin = _snapshot_origin()
        if origin:
            r = origin.get("role")
            if r:
                return r
    except Exception:  # noqa: BLE001 - defensive against import-time issues
        pass
    return None


def _refuse_unprivileged(tool_name: str, caller: str | None) -> dict:
    return _result({
        "status": "error",
        "kind": "not_authorized",
        "message": (
            f"{tool_name} is restricted to roles "
            f"{sorted(_PRIVILEGED_CONFIG_ROLES)}; calling role={caller!r}. "
            "Have a configurator engagement perform this action instead."
        ),
    })


@tool(
    "config_git_commit",
    "Stage and commit all tracked changes under /config/ (tracked: agents/, "
    "policies/, schema/, plugins/registry.json; everything else incl. "
    "plugins/store/, plugins/.staging/, and plugin-env.conf is gitignored). "
    "Returns the commit SHA — empty plus a warning when nothing tracked "
    "changed, which is the expected outcome for gitignored-only writes. "
    "Restricted to the configurator executor role.",
    {"message": str},
)
async def config_git_commit(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("config_git_commit", caller)

    message = args.get("message") or "configurator: commit"
    config_dir = "/config"
    try:
        import config_git
        import agent_loader

        # E-G (v0.31.0): pre-commit schema-validation gate. Refuse the
        # commit if any schema-bearing YAML in the repo would fail
        # boot-time agent_loader validation. Without this, the
        # configurator can write structurally-valid but schema-invalid
        # YAML (e.g., the v0.30.0 ``TRAIT:`` top-level-key repro) and
        # the addon FATALs on next boot. See
        # ``project_eg_configurator_schema_invalid_yaml`` and
        # ``docs/bug-review-2026-05-01-exploration.md`` for the
        # exploration-session repro.
        errors = await asyncio.to_thread(
            agent_loader.validate_config_repo, config_dir,
        )
        if errors:
            return _result({
                "status": "error",
                "kind": "schema_invalid",
                "message": (
                    f"Refusing commit: {len(errors)} schema validation "
                    f"failure(s). Fix the offending YAML and retry."
                ),
                "errors": errors,
            })

        sha = await asyncio.to_thread(
            config_git.commit_config, config_dir, message,
        )
        # G-2 hotfix (v0.33.1): mark this engagement as needing a
        # reload before emit_completion. Drained by casa_reload /
        # casa_reload_triggers; force-honored by emit_completion.
        # `sha` is empty string when nothing actually changed (no-op
        # commit) — only register the pending state when a real commit
        # landed.
        if sha:
            eng = engagement_var.get(None)
            if eng is not None:
                _ENGAGEMENTS_PENDING_RELOAD.add(eng.id)
            return _result({"sha": sha, "message": message})
        # P-3 (v0.69.1): a bare {"sha": ""} left agents looping to reconcile
        # "committed ok" against "file still untracked" when their writes
        # landed on gitignored paths. Say it loudly instead.
        return _result({
            "sha": "", "message": message,
            "warning": (
                "No tracked changes to commit. The config repo tracks ONLY "
                "agents/, policies/, schema/ and plugins/registry.json; every "
                "other path is gitignored by design — plugins/store/, "
                "plugins/.staging/, and plugin-env.conf (a secrets file) must "
                "never enter git history. If you only wrote gitignored paths, "
                "an empty SHA is the expected, correct outcome: report it as "
                "such and do NOT retry the commit."
            ),
        })
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error",
            "kind": "git_error",
            "message": str(exc),
        })


# ---------------------------------------------------------------------------
# casa_reload - Plan 3 (hard reload via Supervisor addon restart)
# ---------------------------------------------------------------------------


@tool(
    "casa_reload",
    "In-process reload of Casa runtime state at a given scope. "
    "Valid scopes: 'agent' (requires role), 'triggers' (requires role), "
    "'policies', 'plugin_env', 'agents', 'executors', 'config_sync', 'full'. Use 'full' "
    "as a catch-all when unsure. Does NOT restart the addon - for that, "
    "see casa_restart_supervised. Restricted to the configurator role.",
    {"scope": str, "role": str, "include_env": bool},
)
async def casa_reload(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_reload", caller)

    scope = (args.get("scope") or "").strip()
    if not scope:
        return _result({
            "status": "error", "kind": "scope_required",
            "message": (
                "casa_reload requires a 'scope' argument. Valid: "
                "'agent', 'triggers', 'policies', 'plugin_env', "
                "'agents', 'executors', 'config_sync', 'full'. See doctrine/reload.md."
            ),
        })

    role = (args.get("role") or "").strip() or None
    include_env = bool(args.get("include_env", False))

    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return _result({
            "status": "error", "kind": "not_initialized",
            "message": "CasaRuntime not bound - boot ordering bug",
        })

    from reload import dispatch
    result = await dispatch(
        scope, runtime=runtime, role=role, include_env=include_env,
    )

    # Drain pending-reload guard if engagement-bound.
    eng = engagement_var.get(None)
    if eng is not None and result.get("status") == "ok":
        _ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)

    return _result(result)


async def _post_supervisor_restart() -> dict:
    """Internal helper used by ``_finalize_engagement`` to honor a
    deferred hard-reload after the bus message + engagement-summary retain have
    landed. Returns a result-shaped dict for logging; never raises.
    """
    import aiohttp
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {
            "status": "error",
            "kind": "no_supervisor_token",
            "message": "SUPERVISOR_TOKEN not set - cannot restart addon",
        }
    headers = {"Authorization": f"Bearer {token}"}
    url = "http://supervisor/addons/self/restart"
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.post(url) as resp:
                return {"supervisor_status": resp.status}
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "kind": "supervisor_error",
            "message": str(exc),
        }


@tool(
    "casa_restart_supervised",
    "Full Supervisor-driven addon restart. Use ONLY when changes require "
    "process-restart semantics (s6 service tree changes, addon "
    "options.json mutations). For routine config edits, use "
    "casa_reload(scope=...) instead. Restricted to the configurator role.",
    {},
)
async def casa_restart_supervised(_: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_restart_supervised", caller)

    eng = engagement_var.get(None)
    if eng is not None:
        # H-1 carry-forward: defer until _finalize_engagement.
        _ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
        _ENGAGEMENTS_DEFERRED_HARD_RELOAD.add(eng.id)
        return _result({
            "supervisor_status": 200,
            "deferred": True,
            "message": (
                "Supervisor restart deferred until engagement finalizes. "
                "Continue with emit_completion."
            ),
        })

    # Out-of-engagement (operator-driven /invoke etc): POST inline.
    return _result(await _post_supervisor_restart())


# ---------------------------------------------------------------------------
# engage_executor — Plan 3 real impl (configurator + future Tier 3 types)
# ---------------------------------------------------------------------------

# P32 (v0.37.10): duplicate-task guard. Refuses a new engage_executor
# spawn whose ``task=`` overlaps with the most-recent engagement in the
# same channel/chat_id within ``_DUPLICATE_TASK_MAX_AGE_S`` seconds at a
# word-level Jaccard >= ``_DUPLICATE_TASK_JACCARD_THRESHOLD``. Guards
# against the cumulative-context bleed pattern observed in
# ``docs/bug-review-2026-05-14-exploration6.md::O-6``: Ellen's
# back-to-back tool calls re-emitting a prior turn's task as a stale
# second engage_executor argument.
_DUPLICATE_TASK_JACCARD_THRESHOLD = 0.5
_DUPLICATE_TASK_MAX_AGE_S = 60.0
_TASK_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _task_tokens(text: str) -> set[str]:
    return set(_TASK_TOKEN_RE.findall((text or "").lower()))


def _jaccard_task_similarity(a: str, b: str) -> float:
    """Word-level Jaccard similarity on lowercased alphanumeric tokens.

    Returns 0.0 for empty inputs. Used by the P32 duplicate-task guard
    at the ``engage_executor`` MCP call site.
    """
    ta, tb = _task_tokens(a), _task_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


async def _fetch_executor_archive(
    *, task: str, origin_channel: str, token_budget: int,
) -> str:
    """Read prior-engagement "lessons" as a SEMANTIC recall against the shared
    ``casa`` bank, keyed on the current ``task`` and filtered to the originating
    engagement's read-clearance (design §3, plan 3). Returns the digest under a
    recognizable header, or "" when the recall is empty or memory could not
    be checked (RecallUnavailable → run cold; never a fabricated header)."""
    import agent as agent_mod
    sem = getattr(agent_mod, "active_semantic_memory", None)
    if sem is None:
        return ""
    try:
        digest = await delegated_recall(
            sem, query=task, origin_channel=origin_channel, max_tokens=token_budget,
            path="executor_archive",
        )
    except RecallUnavailable:
        return ""
    return f"## Prior engagements (lessons learned)\n{digest}" if digest else ""


@tool(
    "engage_executor",
    "Start a Tier 3 Executor engagement (configurator, ha-developer, etc.). "
    "Provide EITHER a plain 'task' string OR a structured 'brief' object "
    "(objective + acceptance_criteria + process_requirements + context + "
    "interaction_required), not both. Returns engagement_id; result arrives "
    "later as a NOTIFICATION.",
    # W3/Sol r3-B8: an explicit JSON Schema so task/brief/context are all
    # schema-OPTIONAL (only executor_type is required). The task-XOR-brief and
    # legacy-top-level-context rules are enforced in the handler — a JSON
    # Schema can't cleanly express the cross-field XOR. The old
    # required-everything shorthand ({"executor_type": str, "task": str,
    # "context": str}) made all three required, so the MCP validator rejected
    # BOTH XOR branches before the handler ever ran.
    {
        "type": "object",
        "properties": {
            "executor_type": {"type": "string"},
            "task": {"type": "string"},
            "context": {"type": "string"},
            # W-R6 (v0.81.0): OPTIONAL short 2-3 word topic title. Normalized +
            # persisted at ingest; absent → a Casa-derived label from task/brief.
            "topic_title": {"type": "string"},
            "brief": {
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "acceptance_criteria": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "process_requirements": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "context": {"type": "string"},
                    "interaction_required": {"type": "boolean"},
                },
                "required": ["objective"],
            },
        },
        "required": ["executor_type"],
    },
)
async def engage_executor(args: dict) -> dict:
    import agent as agent_mod
    # AR-2: snapshot at entry — this handler awaits extensively (topic
    # creation, engagement-registry create, driver dispatch) and reads
    # `origin` well after those awaits; a pooled client's holder rewrite
    # by a later turn must not leak into this in-flight engagement.
    origin = _snapshot_origin()
    if not origin:
        return _result({
            "status": "error", "kind": "no_origin",
            "message": "engage_executor called outside a turn",
        })

    executor_type = args.get("executor_type", "")
    task_text = args.get("task", "") or ""
    context_text = args.get("context", "") or ""

    # W3/Sol B10: task XOR brief. The brief object is the alternative to a
    # plain task string; the handler enforces the cross-field rules a JSON
    # Schema can't. ``brief`` is persisted VERBATIM later — validate but never
    # mutate it here; the normalized (defaults-applied) VIEW drives rendering.
    brief = args.get("brief")
    has_brief = brief is not None
    has_task = bool(task_text)
    normalized_brief: dict | None = None
    if has_brief and has_task:
        return _result({
            "status": "error", "kind": "invalid_arguments",
            "message": "provide EITHER task OR brief, not both",
        })
    if not has_brief and not has_task:
        return _result({
            "status": "error", "kind": "invalid_arguments",
            "message": "provide either a task string or a brief object",
        })
    if has_brief and context_text:
        return _result({
            "status": "error", "kind": "invalid_arguments",
            "message": (
                "legacy top-level 'context' is not allowed alongside 'brief' — "
                "put context inside brief.context"
            ),
        })
    if has_brief:
        _brief_err = validate_brief(brief)
        if _brief_err is not None:
            return _result({
                "status": "error", "kind": "invalid_arguments",
                "message": _brief_err,
            })
        normalized_brief = normalize_brief(brief)
        # Canonical string = the objective — drives concise_task titles, the
        # P32 Jaccard compare, and engagement.task (those call sites below are
        # UNCHANGED; only the value they read is now the objective).
        task_text = normalized_brief["objective"]

    if _executor_registry is None or not _executor_registry.list_types():
        return _result({
            "status": "error", "kind": "no_executor_types",
            "message": (
                "No Tier 3 Executor types registered. "
                "Ship Plan 3/4/5 or enable an executor in its definition.yaml."
            ),
        })

    defn = _executor_registry.get(executor_type)
    if defn is None:
        return _result({
            "status": "error", "kind": "unknown_executor_type",
            "message": (
                f"No enabled executor type named {executor_type!r}. "
                f"Available: {_executor_registry.list_types()}"
            ),
        })

    # §3.5/§3.8/§3.9: resolve this executor's plugin assignment ONCE — the
    # result feeds the launch gate, the recorded binding, AND the in-casa
    # options (one resolve, one binding). Gate BEFORE topic creation so a
    # blocked launch never leaves a dangling topic.
    def _resolve_and_gate():
        """Returns (plugin_resolution, plugin_artifacts, error_result|None)."""
        res = plugin_registry.resolve_for(f"executor:{executor_type}")
        if not res.registry_valid:
            return res, (), _result({
                "status": "error", "kind": "plugin_registry_invalid",
                "message": ("plugin registry is invalid — executor launches are "
                            "blocked until it is repaired "
                            "(see /data/plugin-health.json)"),
            })
        if res.issues:
            detail = [(i.name, i.reason_code) for i in res.issues]
            return res, (), _result({
                "status": "error", "kind": "plugin_unavailable",
                "message": (f"required plugin(s) unavailable for "
                            f"{executor_type!r}: {detail}"),
            })
        # Sol round-3 B4: also gate on CONFIGURED readiness — a resolvable plugin
        # that is not ready (authorization_missing / unresolved secret / missing
        # system requirement / malformed .mcp.json) must NOT launch with
        # --plugin-dir. Check each assigned plugin's executor-target row through
        # the same verification path.
        target = f"executor:{executor_type}"
        not_ready = []
        for rp in res.plugins:
            try:
                v = _tool_verify_plugin_state(plugin_name=rp.name)
            except Exception as exc:  # noqa: BLE001 — Sol round-4 M: fail CLOSED
                logger.warning("launch readiness verify raised for %s: %s",
                               rp.name, exc)
                not_ready.append((rp.name, "verify_error"))
                continue
            row = next((r for r in v.get("targets", [])
                        if r.get("target") == target), None)
            if row is not None and not row.get("ready"):
                not_ready.append((rp.name, (row.get("reasons") or ["not_ready"])[0]))
            elif row is None and v.get("ready") is not True:
                not_ready.append((rp.name, (v.get("reasons") or ["not_ready"])[0]))
        if not_ready:
            return res, (), _result({
                "status": "error", "kind": "plugin_not_ready",
                "message": (f"plugin(s) not ready for {executor_type!r}: "
                            f"{not_ready} (see /data/plugin-health.json)"),
            })
        arts = tuple(
            {"name": rp.name, "artifact_id": rp.artifact_id, "path": rp.path}
            for rp in res.plugins)
        return res, arts, None

    # Sol #6: capture the snapshot generation at resolve so a concurrent
    # plugin_update during the topic-creation await can be detected before the
    # engagement record pins its artifacts (below, just before create()).
    # Sol round-4 M: run the resolve + readiness verification OFF the event loop
    # (it does per-plugin artifact checks + YAML/file reads). This is the
    # PRE-TOPIC gate (fail fast before creating a topic on a known-unavailable
    # plugin); the authoritative record binding is (re)resolved under the lock
    # below, bound to a stable snapshot generation.
    _, _, _gate_err = await asyncio.to_thread(_resolve_and_gate)
    if _gate_err is not None:
        return _gate_err

    if _channel_manager is None:
        return _result({
            "status": "error", "kind": "no_channel_manager",
            "message": "channel manager missing",
        })
    channel = _channel_manager.get(origin.get("channel", "telegram"))
    # E-F (v0.30.0): if supergroup IS configured but
    # engagement_permission_ok is still False, the boot-time setup may
    # have lost a race with a transient first-boot setWebhook NetworkError.
    # The setup is now wired into _rebuild's tail (self-healing on every
    # reconnect), but in the rare window where the user spawns an
    # engagement before any rebuild has completed, attempt one in-line
    # retry before giving up. Idempotent; cheap on success.
    if (channel is not None
            and getattr(channel, "engagement_supergroup_id", 0)
            and not getattr(channel, "engagement_permission_ok", False)):
        try:
            await channel.setup_engagement_features()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "engage_executor: in-line setup_engagement_features "
                "retry failed: %s", exc,
            )
    if (channel is None
            or not getattr(channel, "engagement_supergroup_id", 0)
            or not getattr(channel, "engagement_permission_ok", False)):
        return _engagement_unavailable_result(origin)  # R-2 (v0.69.7)

    # P32 (v0.37.10): refuse duplicate-task spawns. Compare against the
    # most-recent engagement for the same channel/chat_id within the
    # last _DUPLICATE_TASK_MAX_AGE_S seconds; if word-level Jaccard
    # overlap >= _DUPLICATE_TASK_JACCARD_THRESHOLD, return an error
    # envelope. Guards against the cumulative-context bleed pattern
    # observed in bug-review-2026-05-14-exploration6.md::O-6 (back-to-back
    # Ellen turns re-emitting the prior turn's task). isinstance check
    # falls through gracefully when the registry is a MagicMock in unit
    # tests; production callers always pass a real EngagementRegistry.
    if _engagement_registry is not None and hasattr(
        _engagement_registry, "recent_for_origin",
    ):
        try:
            prior = _engagement_registry.recent_for_origin(
                channel=origin.get("channel", "telegram"),
                chat_id=str(origin.get("chat_id", "")),
                max_age_s=_DUPLICATE_TASK_MAX_AGE_S,
            )
        except Exception as exc:  # noqa: BLE001 — defensive in mock-driven tests
            logger.debug("recent_for_origin lookup skipped: %s", exc)
            prior = None
        if isinstance(prior, EngagementRecord):
            sim = _jaccard_task_similarity(prior.task, task_text)
            if sim >= _DUPLICATE_TASK_JACCARD_THRESHOLD:
                age_s = int(time.time() - prior.started_at)
                return _result({
                    "status": "error", "kind": "duplicate_task",
                    "message": (
                        f"engage_executor refused: task overlaps with "
                        f"engagement {prior.id[:8]} "
                        f"({prior.role_or_type}, started {age_s}s ago) "
                        f"at Jaccard {sim:.2f} >= "
                        f"{_DUPLICATE_TASK_JACCARD_THRESHOLD}. "
                        f"You may be re-emitting a prior turn's task. "
                        f"If you mean a new task, narrow the task= text. "
                        f"If you mean to retry, /cancel {prior.id[:8]} first."
                    ),
                })

    # E-12 (v0.37.0) + v0.37.1 D-1: U3 state-encoded topic title.
    # ``<state-emoji> <concise task>`` per spec §6.3 — the role icon
    # is delivered via the bubble (icon_custom_emoji_id from
    # channels.topic_icons.icon_id_for_role), not the title text.
    from channels.state_emoji import (
        STATE_EMOJI, compose_topic_title, concise_task, normalize_topic_title,
    )
    first_line = (task_text or "").splitlines()[0]
    short_task = concise_task(first_line) or "engagement"
    # W-R6 (v0.81.0): normalize ONCE at ingest — an engager-supplied topic_title
    # (rejected + fell back if UNSAFE/blank), else the Casa-derived short label.
    # The SAME persisted value feeds the topic-name state edits AND the live
    # summary title (single source — see EngagementRecord.topic_title).
    persisted_title = normalize_topic_title(args.get("topic_title")) or short_task
    topic_name = compose_topic_title(
        state="active", short_task=persisted_title,
    )
    try:
        topic_id = await channel.open_engagement_topic(
            name=topic_name,
            role=executor_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error", "kind": "topic_create_failed",
            "message": str(exc),
        })

    # Sol #6 / round-3 H6 / round-4 TOCTOU fence: hold the plugin-tools lock
    # across the re-resolve + durable record write so a concurrent plugin_update
    # (which bumps the snapshot generation UNDER THE SAME LOCK during its reload)
    # cannot interleave. plugin_update holds this lock; the manual-edit seam
    # reload_full() does NOT, so we BIND the recorded artifacts to a stable
    # generation: sample → resolve → verify unchanged, retrying if reload_full
    # moved the snapshot mid-resolve. `_gen_at_create` is then the generation the
    # recorded artifacts actually came from, making the post-create recheck sound.
    # The lock covers only this brief section — NOT the topic-creation RT above.
    async with _PLUGIN_TOOLS_LOCK:
        _gate_err = None
        for _attempt in range(5):
            _gen_at_create = plugin_registry.snapshot_generation()
            plugin_resolution, plugin_artifacts, _gate_err = \
                await asyncio.to_thread(_resolve_and_gate)
            if plugin_registry.snapshot_generation() == _gen_at_create:
                break   # artifacts + generation agree
        if _gate_err is not None:
            await _abort_engagement_topic(channel, "engage-abort", topic_id)
            return _gate_err

        # Computed BEFORE create() so it can be persisted onto the record's
        # origin — the claude_code driver reads it (and context_text) back out
        # of engagement.origin when provisioning the workspace CLAUDE.md.
        world_state = _build_world_state_summary()

        # W3 (Task 8): persist the RAW brief VERBATIM on origin (no injected
        # default keys) — the single authoritative source every consumer
        # re-renders from (design §211). W2 is claude_code-ONLY: the same gate
        # sets interaction_state="first_contact_required" at create; in_casa
        # (synchronous configurator, no turn-taking transition path) never
        # enters that state and would otherwise get stuck awaiting.
        _origin_extra = {"context": context_text, "world_state_summary": world_state}
        if brief is not None:
            _origin_extra["brief"] = brief
        _two_phase = bool(
            defn.driver == "claude_code"
            and normalized_brief is not None
            and normalized_brief["interaction_required"]
        )

        rec = await _engagement_registry.create(
            kind="executor", role_or_type=executor_type, driver=defn.driver,
            task=task_text,
            origin={**origin, **_origin_extra},
            topic_id=topic_id,
            tools_allowed=tuple(defn.tools_allowed or ()),
            permission_mode=getattr(defn, "permission_mode", "acceptEdits"),
            plugin_artifacts=plugin_artifacts,      # §3.8 recorded binding
            interaction_state="first_contact_required" if _two_phase else "",
            topic_title=persisted_title,            # W-R6 durable short title
        )

    # Sol round-4: the manual-edit seam `casa_reload(scope="full")` bumps the
    # snapshot generation WITHOUT the plugin-tools lock, so it can move the
    # snapshot while create() awaits. Recheck against the pre-create generation
    # and abort before the driver starts — the record must not launch stale.
    if plugin_registry.snapshot_generation() != _gen_at_create:
        await _engagement_registry.mark_error(
            rec.id, kind="plugin_superseded",
            message="plugin snapshot changed during launch")
        await _abort_engagement_topic(channel, rec.id, topic_id)
        return _result({
            "status": "error", "kind": "plugin_superseded",
            "message": ("plugin registry changed during launch — engagement "
                        "aborted before start; retry")})

    # Persist the initial state emoji so Task 23 ``update_topic_state`` knows
    # whether it needs to edit the title (no-op when state didn't change).
    try:
        await _engagement_registry.set_channel_state(
            rec.id, current_state_emoji=STATE_EMOJI["active"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_channel_state(active) failed: %s", exc)

    # Read + interpolate prompt template (needed by both driver paths —
    # in_casa: options.system_prompt; claude_code: CLAUDE.md body).
    prompt_template = ""
    try:
        with open(defn.prompt_template_path, "r", encoding="utf-8") as fh:
            prompt_template = fh.read()
    except OSError as exc:
        await _engagement_registry.mark_error(
            rec.id, kind="prompt_template_missing", message=str(exc),
        )
        await _abort_engagement_topic(channel, rec.id, topic_id)
        return _result({
            "status": "error", "kind": "prompt_template_missing",
            "message": str(exc),
        })

    # Semantic-recall memory injection (design §3, plan 3): when the executor
    # opts in (defn.memory.enabled=True, off by default), fetch prior-engagement
    # lessons from the shared `casa` bank at the origin channel's read-clearance.
    executor_memory_block = ""
    if defn.memory.enabled:
        executor_memory_block = await _fetch_executor_archive(
            task=task_text,
            origin_channel=origin.get("channel", "telegram"),
            token_budget=defn.memory.token_budget,
        )

    # W3/Sol r5-B8: the {task} value reaches BOTH driver paths through this ONE
    # seam (in_casa options.system_prompt AND claude_code initial FIFO prompt).
    # When a brief is present it becomes the full rendered markdown block
    # (render_brief_task, two-phase gated to claude_code+interaction_required —
    # ``_two_phase`` computed at create above); no brief → the canonical
    # task_text (the else branch is task_text, NOT `task`, which doesn't exist
    # here — using it would break every legacy task= invocation).
    task_for_prompt = (
        render_brief_task(normalized_brief, two_phase=_two_phase)
        if brief is not None else task_text
    )
    prompt = (
        prompt_template
        .replace("{task}", task_for_prompt)
        .replace("{context}", context_text or "(none)")
        .replace("{world_state_summary}", world_state)
        .replace("{executor_memory}", executor_memory_block)
    )

    # Driver dispatch — in_casa uses ClaudeAgentOptions + system_prompt;
    # claude_code uses the ExecutorDefinition + workspace-CLAUDE.md.
    if defn.driver == "claude_code":
        driver = getattr(agent_mod, "active_claude_code_driver", None)
        if driver is None:
            await _engagement_registry.mark_error(
                rec.id, kind="no_driver",
                message="claude_code driver not initialized",
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({
                "status": "error", "kind": "no_driver",
                "message": "claude_code driver not initialized",
            })
        try:
            await driver.start(rec, prompt=prompt, options=defn)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "claude_code driver.start failed for %s", rec.id[:8],
            )
            await _engagement_registry.mark_error(
                rec.id, kind="driver_start_failed", message=str(exc),
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({
                "status": "error", "kind": "driver_start_failed",
                "message": str(exc),
            })
    else:
        # Off-loop: _build_executor_options reads hooks.yaml. §3.9: feed the
        # SAME resolution gated + recorded above (one resolve, one binding).
        options = await asyncio.to_thread(
            _build_executor_options, defn, executor_type=executor_type,
            resolution=plugin_resolution,
            extra_casa_tools=(
                "mcp__casa-framework__query_engager",
                "mcp__casa-framework__emit_completion",
            ),
        )
        options.system_prompt = prompt

        driver = getattr(agent_mod, "active_engagement_driver", None)
        if driver is None:
            await _engagement_registry.mark_error(
                rec.id, kind="no_driver",
                message="engagement driver not initialized",
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({
                "status": "error", "kind": "no_driver",
                "message": "engagement driver not initialized",
            })
        try:
            await driver.start(rec, prompt=prompt, options=options)
        except Exception as exc:  # noqa: BLE001
            await _engagement_registry.mark_error(
                rec.id, kind="driver_start_failed", message=str(exc),
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({
                "status": "error", "kind": "driver_start_failed",
                "message": str(exc),
            })

    return _result({
        "status": "pending",
        "engagement_id": rec.id,
        "executor_type": executor_type,
        "topic_id": topic_id,
    })


def _engagement_supergroup_chat_id(channel: Any | None) -> int | None:
    """Best-effort chat-id resolution for topic-ledger appends [AR-2].

    Prefer the live telegram channel's configured supergroup id; fall back
    to the boot env the channel would have been built from (casa_core), so
    an append still records a chat_id when telegram is momentarily
    unwired. None when neither is available — the ledger keeps such
    entries but never auto-deletes them.
    """
    chat_id = getattr(channel, "engagement_supergroup_id", None)
    if chat_id:
        return chat_id
    try:
        return int(
            os.environ.get("TELEGRAM_ENGAGEMENT_SUPERGROUP_ID", "0") or 0,
        ) or None
    except (TypeError, ValueError):
        return None


async def _abort_engagement_topic(
    channel: Any, engagement_id: str, topic_id: int | None,
) -> None:
    """Best-effort: flip a just-created topic to 'failed' and close it when
    an engagement dies before its driver started. Never raises.

    Do NOT route these failures through _finalize_engagement — it would
    double-notify Ellen over the bus (the tool already returns the error
    envelope synchronously), overwrite the specific error kind with
    'emit_completion_error', and run memory-retention side effects.
    """
    if topic_id is None:
        return
    # Topic-retention ledger (2026-07-10 design): an aborted engagement's
    # topic is today's most orphan-prone — record it for the retention
    # sweep even when the channel is gone (gate only on topic_id, like the
    # finalize funnel). Own try/except: this function never raises.
    try:
        import topic_ledger
        await topic_ledger.append(
            engagement_id=engagement_id,
            chat_id=_engagement_supergroup_chat_id(channel),
            topic_id=topic_id,
            outcome="error",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "abort topic %s: topic ledger append failed: %s", topic_id, exc,
        )
    if channel is None:
        return
    if hasattr(channel, "update_topic_state"):
        try:
            await channel.update_topic_state(
                engagement_id=engagement_id, new_state="failed",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "abort topic %s: update_topic_state failed: %s", topic_id, exc,
            )
    try:
        await channel.close_topic(thread_id=topic_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("abort topic %s: close_topic failed: %s", topic_id, exc)


# ---------------------------------------------------------------------------
# _finalize_engagement — shared funnel for completion + cancel
# ---------------------------------------------------------------------------


class FinalizeResult(enum.Enum):
    """G4 D5 (v0.96.0): typed outcome of ``_finalize_engagement``. Truthiness
    preserves the historical bool contract for existing callers — only a WON
    finalize is truthy (Sol g4-r1-6: an untyped falsy return conflated
    "already terminal" with "persist rolled back, record still LIVE", and
    emit_completion acked the latter as success)."""
    FINALIZED = "finalized"
    ALREADY_TERMINAL = "already_terminal"
    PRECONDITION_FAILED = "precondition_failed"   # D2 (unread inbound) — v0.96.0
    PERSIST_FAILED = "persist_failed"

    def __bool__(self) -> bool:
        return self is FinalizeResult.FINALIZED


async def _finalize_engagement(
    engagement: EngagementRecord,
    *,
    outcome: str,                       # "completed" | "cancelled" | "error"
    text: str,
    artifacts: list[str],
    next_steps: list[dict],
    driver: Any | None,
    stale_before: float | None = None,
    output_truncated: bool = False,     # Task 6 (spec §4.6)
    inbound_gate: bool = False,         # G4 D1/D2 (v0.96.0): emit/completed only
) -> "FinalizeResult":
    """End an engagement: update registry, close topic, NOTIFY Ellen,
    retain a tier-classified engagement summary on the shared ``casa`` bank.

    Never raises on channel/memory side-effects — logs warnings and continues
    so the registry always reaches a terminal state.

    ``stale_before`` (reap only): the terminal transition wins ONLY if the
    record's ``last_user_turn_ts`` is still older than this cutoff — a record
    revived by a user turn since the reap snapshot is left live.

    Returns a ``FinalizeResult``; only ``FINALIZED`` (truthy) means this
    call won the terminal transition and ran the finalize side-effects.
    ``ALREADY_TERMINAL`` covers already-terminal AND a lost ``stale_before``
    guard; ``PERSIST_FAILED`` means the tombstone write rolled back and the
    record is STILL LIVE (retryable).
    """
    now = time.time()

    # 0. Pre-close spool drain (v0.79.0 §3): flush any pending inbound receipts
    #    / eviction notices BEFORE the terminal commit + topic close, while the
    #    topic is still open. Best-effort — the terminal boot-reconciliation
    #    owner covers a crash-after-commit or a drain-send failure. Idempotent
    #    (at-least-once), so running it ahead of the win/lose gate is safe.
    if driver is not None and hasattr(driver, "drain_inbound_spool"):
        try:
            await driver.drain_inbound_spool(engagement)
        except Exception as exc:  # noqa: BLE001 — never abort finalize
            logger.warning(
                "finalize engagement %s: inbound spool drain failed: %s",
                engagement.id[:8], exc,
            )

    # 1. Registry transition — atomic and authoritative. Only the first
    #    caller to flip the record terminal runs the finalize side effects
    #    below (L75/L24: guards against a concurrent /cancel racing this
    #    call across a real suspension point, e.g. the G-2 forced-reload
    #    await, which the naive check-then-act in emit_completion cannot).
    #    v0.79.0 (§3, Sol r6-2): STRICT persistence — a tombstone-write failure
    #    rolls the record back (full-field) and re-raises rather than leaving a
    #    closed topic with no terminal record; treat that as "did not win" so
    #    the record stays live for a retry / boot replay instead of a torn close.
    # G4 D2/D4 (v0.96.0): SYNCHRONOUS terminal hook, evaluated inside the
    # registry's terminal critical section (no suspension between check and
    # flip). Composes the completion inbound GATE (inbound_gate=True: veto on
    # unread depth or a pending ingress reservation) with the no-silent-loss
    # SNAPSHOT of queued-unseen texts (all callers — surfaced to the topic
    # after the flip). Accessor failures fail OPEN with a warning: a driver
    # bug must not wedge termination forever.
    unread_snapshot: list[str] = []

    def _terminal_hook():
        if driver is None or not hasattr(driver, "inbound_unread_texts"):
            return None
        try:
            texts = list(driver.inbound_unread_texts(engagement.id))
            texts = [t for t in texts if isinstance(t, str)]
            _hr = (driver.inbound_reservations(engagement.id)
                   if hasattr(driver, "inbound_reservations") else 0)
            resv = _hr if type(_hr) is int else 0
        except Exception:  # noqa: BLE001 — fail open
            logger.warning(
                "finalize engagement %s: inbound accessors failed — "
                "gate skipped", engagement.id[:8], exc_info=True)
            return None
        unread_snapshot[:] = list(texts)
        if inbound_gate and (texts or resv):
            return (f"unread_inbound depth={len(texts)} "
                    f"reservations={resv}")
        return None

    if _engagement_registry is not None:
        try:
            won = await _engagement_registry.try_transition_terminal(
                engagement.id, outcome,
                completed_at=now if outcome == "completed" else None,
                error_kind="emit_completion_error", error_message=text,
                stale_before=stale_before,
                strict=True,
                terminal_hook=_terminal_hook,
            )
        except TerminalPreconditionFailed as exc:
            logger.info(
                "Engagement %s completion vetoed by inbound gate: %s",
                engagement.id[:8], exc)
            return FinalizeResult.PRECONDITION_FAILED
        except Exception as exc:  # noqa: BLE001 — strict persist failed + rolled back
            logger.warning(
                "Engagement %s terminal transition failed to persist "
                "(rolled back, left live): %s",
                engagement.id[:8], exc,
            )
            return FinalizeResult.PERSIST_FAILED
        if not won:
            logger.info(
                "Engagement %s not finalized — already terminal or revived "
                "since snapshot (outcome=%s)",
                engagement.id[:8], outcome,
            )
            return FinalizeResult.ALREADY_TERMINAL

    # Task 6 (spec §4.6): THIS call just won the terminal flip — release
    # the specialist concurrency permit (if any; executor engagements and
    # any engagement created before a SpecialistLimiter was wired never
    # have one) unconditionally, before any of the best-effort side effects
    # below that might raise. Idempotent — safe even if something else
    # already released it via the pre-finalize failure paths in
    # delegate_to_agent's interactive branch (which also null it out).
    if engagement.permit is not None:
        engagement.permit.release()

    # r5-B6: the instant THIS call won the terminal flip, cancel pending
    # broker requests so a late ask/permission tap can't be answered
    # post-terminal, and DRAIN finish-hooks so their keyboard "expired"
    # edits land while the topic is STILL OPEN (below closes it). Must
    # precede the topic ops, not follow driver teardown — the old placement
    # let a pending ask resolve after the terminal flip and scheduled its
    # edit only after topic closure.
    try:
        from verdict_broker import BROKER
        for ns in ("permission", "engagement_ask"):
            BROKER.cancel_scope(namespace=ns, scope=engagement.id,
                                reason="engagement_terminal")
        await BROKER.drain_hooks()      # flush keyboard edits BEFORE close_topic
    except Exception:  # noqa: BLE001
        pass

    # v0.79.0 (§5): terminal summary flush — set the pinned summary's absolute
    # terminal status (✅/🛑/⚠️) and cancel its elapsed tick while the topic is
    # STILL OPEN (before the U3 title flip + close_topic below).
    if driver is not None and hasattr(driver, "finalize_summary"):
        try:
            await driver.finalize_summary(engagement, outcome)
        except Exception as exc:  # noqa: BLE001 — never abort finalize
            logger.warning(
                "finalize engagement %s: summary finalize failed: %s",
                engagement.id[:8], exc,
            )

    # v0.83.0 (§A3(b), consumer c): SETTLE every remaining open-question anchor
    # while the topic is STILL OPEN — closing the latent gap where /cancel or
    # /complete with a live free-text anchor left it visually open forever. Also
    # clears the engagement's re-anchor latch + cancels its retry owner. Driver-
    # methodized + getattr-tolerant (in_casa / legacy drivers simply don't
    # engage). Never aborts the finalize funnel.
    if driver is not None and hasattr(driver, "settle_all_open_questions"):
        try:
            await driver.settle_all_open_questions(engagement, outcome)
        except Exception as exc:  # noqa: BLE001 — never abort finalize
            logger.warning(
                "finalize engagement %s: open-question settle failed: %s",
                engagement.id[:8], exc,
            )

    # [AR-4] Topic-retention ledger (2026-07-10 design): record the topic
    # for the retention sweep the moment the record flips terminal — both
    # drivers, all outcomes, regardless of whether close_topic below
    # succeeds. Gated ONLY on topic_id, NOT on channel-manager presence:
    # telegram may be momentarily unwired and the append must still land.
    # Own try/except: a ledger failure must never abort this funnel — the
    # idempotency guard above makes a partial finalize unretryable.
    if engagement.topic_id is not None:
        try:
            import topic_ledger
            ledger_ch = (_channel_manager.get("telegram")
                         if _channel_manager is not None else None)
            await topic_ledger.append(
                engagement_id=engagement.id,
                chat_id=_engagement_supergroup_chat_id(ledger_ch),
                topic_id=engagement.topic_id,
                outcome=outcome,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: topic ledger append failed: %s",
                engagement.id[:8], exc,
            )

    # L68/L17: drop per-engagement observer bookkeeping now that the
    # engagement is terminal — keeps _interjection_counts/_silenced from
    # growing unbounded over the process lifetime.
    try:
        import agent as agent_mod
        _obs = getattr(agent_mod, "active_observer", None)
        if _obs is not None:
            _obs.forget(engagement.id)
    except Exception:  # noqa: BLE001
        pass

    # 2. Post completion message into the topic (if any), flip U3 state, close.
    if engagement.topic_id is not None and _channel_manager is not None:
        tch = _channel_manager.get(engagement.origin.get("channel", "telegram"))
        if tch is not None:
            try:
                summary_text = (
                    f"Engagement {outcome}. Summary:\n{text}"
                    if text else f"Engagement {outcome}."
                )
                # W2/Sol B9 (Task 7): surface a mutating tool-use taken while
                # the engagement was awaiting_operator — set by the
                # claude_code driver's _on_stream_event mutating_tool seam.
                if engagement.origin.get("interaction_violated"):
                    summary_text += (
                        "\n\n⚠️ This engagement took an action before you "
                        "responded — please review."
                    )
                # G4 D4 (v0.96.0): surface operator messages that were NEVER
                # read (queued at terminalization — cancel/reap/error paths;
                # a gated completion cannot reach here with unread input).
                # TOPIC-ONLY (never in `text`, which flows to the bus
                # notification and semantic memory) and BOUNDED to one
                # message: excerpts + count, full texts stay in the durable
                # spool file.
                if unread_snapshot:
                    _budget = 2800
                    _parts = []
                    for _t in unread_snapshot:
                        _ex = _t if len(_t) <= 400 else _t[:400] + "…"
                        if _budget - len(_ex) < 0:
                            break
                        _budget -= len(_ex)
                        _parts.append(f"• {_ex}")
                    _more = len(unread_snapshot) - len(_parts)
                    summary_text += (
                        f"\n\n⚠️ {len(unread_snapshot)} operator "
                        "message(s) were never read by this engagement:\n"
                        + "\n".join(_parts)
                        + (f"\n…and {_more} more (kept in the engagement's "
                           "inbound spool file)." if _more > 0 else "")
                    )
                # v0.79.0 (§2 F1(c)): for a claude_code engagement, DRAIN the
                # sequencer (settle pending narration + parked/armed intents)
                # BEFORE posting the completion text THROUGH the single writer —
                # completion may not overtake its causal block. The driver hook
                # returns True when it posted (skip the direct send); a
                # non-claude_code driver / no live sequencer falls back to the
                # pre-v0.79 direct send.
                posted_via_sequencer = False
                if driver is not None and hasattr(
                        driver, "finalize_completion_post"):
                    try:
                        posted_via_sequencer = await driver.finalize_completion_post(
                            engagement, summary_text)
                    except Exception as exc:  # noqa: BLE001 — never abort finalize
                        logger.warning(
                            "finalize engagement %s: sequencer completion post "
                            "failed: %s", engagement.id[:8], exc,
                        )
                if not posted_via_sequencer:
                    await tch.send_to_topic(
                        engagement.topic_id,
                        summary_text,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "finalize engagement %s: send_to_topic failed: %s",
                    engagement.id[:8], exc,
                )
            # E-12 (v0.37.0) Task 23: U3 terminal state — flip the topic title
            # to <state-emoji>·<role-emoji> <task> before closing so the
            # closed-topic sidebar carries the outcome at a glance.
            terminal_state = {
                "completed": "completed",
                "cancelled": "cancelled",
                "error": "failed",
                "failed": "failed",
            }.get(outcome)
            if terminal_state is not None and hasattr(tch, "update_topic_state"):
                try:
                    await tch.update_topic_state(
                        engagement_id=engagement.id, new_state=terminal_state,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "finalize engagement %s: U3 state update failed: %s",
                        engagement.id[:8], exc,
                    )
            try:
                await tch.close_topic(thread_id=engagement.topic_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "finalize engagement %s: close_topic failed: %s",
                    engagement.id[:8], exc,
                )
            # R5 (v0.89.0): drop the react target (map hygiene). Best-effort —
            # a missed clear is harmless (react's non-live gate rejects it).
            if hasattr(tch, "clear_inbound"):
                try:
                    tch.clear_inbound(engagement.id)
                except Exception:  # noqa: BLE001
                    pass

    # 3. Tear down driver client
    if driver is not None:
        try:
            await driver.cancel(engagement)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: driver.cancel failed: %s",
                engagement.id[:8], exc,
            )

    # Drop the permission-verdict queue for this engagement (leak guard).
    # Lazy import matches this function's existing style and avoids cycles.
    try:
        from channels.channel_handlers import PERMISSION_QUEUES
        PERMISSION_QUEUES.pop(engagement.id, None)
    except Exception:  # noqa: BLE001
        pass

    # 4. NOTIFY Ellen (via existing DelegationComplete-shaped pathway)
    if _bus is not None:
        target_role = engagement.origin.get("role") or "assistant"
        complete = DelegationComplete(
            delegation_id=engagement.id,
            agent=engagement.role_or_type,
            status="ok" if outcome == "completed" else "error",
            text=text,
            kind="" if outcome == "completed" else outcome,
            message=text,
            origin=dict(engagement.origin),
            elapsed_s=now - engagement.started_at,
            output_truncated=output_truncated,  # Task 6 (spec §4.6)
        )
        try:
            await _bus.notify(BusMessage(
                type=MessageType.NOTIFICATION,
                source=engagement.role_or_type,
                target=target_role,
                content=complete,
                channel=engagement.origin.get("channel", ""),
                context={
                    "cid": engagement.origin.get("cid", "-"),
                    "chat_id": engagement.origin.get("chat_id", ""),
                    "engagement_id": engagement.id,
                    "next_steps": next_steps,
                },
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: bus.notify failed: %s",
                engagement.id[:8], exc,
            )

    # 5. Retain a structured engagement summary on the shared `casa` bank,
    #    tier-classified and gated by the origin channel's write-trust (voice →
    #    nothing) — design §3, plan 3. The post-back NOTIFICATION above is the
    #    durable record for the engager; this is the structured one-shot the
    #    maintainer chose to keep.
    # L33: retain_delegated internally runs an LLM tier-classification per item
    # (a claude-CLI subprocess spawn + round trip) — off the turn's critical
    # path per tier_classifier's own doctrine. Run both retains as background
    # tasks (mirroring _run_delegated_agent) so emit_completion / cancel return
    # promptly; the deferred-reload path below drains them first (H-1).
    retain_tasks: list[asyncio.Task] = []
    import agent as agent_mod
    from personality_types import RetainedTurn, SpeakerProvenance
    # Task 10: the structured summary is a platform record authored on behalf of
    # the finalized engagement. Attribute it to the identity on the current
    # origin snapshot when one is present; otherwise the honest, unattributed
    # "system" identity — NEVER a fabricated persona.
    _summary_prov = _snapshot_origin().get("speaker_provenance")
    if not isinstance(_summary_prov, SpeakerProvenance):
        _summary_prov = SpeakerProvenance(speaker_kind="system")
    sem = getattr(agent_mod, "active_semantic_memory", None)
    if sem is not None:
        summary = json.dumps({
            "kind": "engagement_summary",
            "engagement_id": engagement.id,
            "specialist_or_type": engagement.role_or_type,
            "task": engagement.task,
            "status": outcome,
            "started_at": engagement.started_at,
            "completed_at": now,
            "duration_s": now - engagement.started_at,
            "text": text,
            "artifacts": artifacts,
            "next_steps": next_steps,
        })
        bg = asyncio.create_task(retain_delegated(
            sem, origin_channel=str(engagement.origin.get("channel", "")),
            turns=[RetainedTurn(summary, _summary_prov)],
        ))
        _specialist_bg_tasks.add(bg)
        bg.add_done_callback(_specialist_bg_tasks.discard)
        retain_tasks.append(bg)

    # Plan 4a.1 §8.4: update .casa-meta.json with terminal status + retention_until.
    if engagement.driver == "claude_code":
        try:
            from drivers.workspace import load_casa_meta, write_casa_meta
            ws = os.path.join(_ENGAGEMENTS_ROOT, engagement.id)
            if os.path.isdir(ws):
                meta = load_casa_meta(ws) or {}
                finished_iso = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now),
                )
                retention_iso = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(now + _WORKSPACE_RETENTION_DAYS * 24 * 3600),
                )
                final_status = ("COMPLETED" if outcome == "completed"
                                else "CANCELLED" if outcome == "cancelled"
                                else "ERROR")
                write_casa_meta(
                    workspace_path=ws,
                    engagement_id=engagement.id,
                    executor_type=engagement.role_or_type,
                    status=final_status,
                    created_at=meta.get("created_at") or finished_iso,
                    finished_at=finished_iso,
                    retention_until=retention_iso,
                    # §3.8: the immutable binding survives the terminal rewrite.
                    plugin_artifacts=meta.get("plugin_artifacts"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: .casa-meta update failed: %s",
                engagement.id[:8], exc,
            )

    # Per-executor-type structured summary (only kind=executor), retained
    # tier-tagged on the shared bank. Its content differs from the engagement
    # summary above, so its content-addressed document_id (Task 10) is distinct
    # and the two never clobber each other.
    # `sem` is the active_semantic_memory resolved in step 5 above.
    if engagement.kind == "executor" and sem is not None:
        type_summary = json.dumps({
            "kind": "executor_engagement_summary",
            "engagement_id": engagement.id,
            "executor_type": engagement.role_or_type,
            "started_at": engagement.started_at,
            "finished_at": now,
            "duration_s": now - engagement.started_at,
            "terminal_state": outcome,
            "engager": engagement.origin.get("role") or "assistant",
            "task": engagement.task,
            "last_text": text,
            "artifacts": artifacts,
        })
        bg = asyncio.create_task(retain_delegated(
            sem, origin_channel=str(engagement.origin.get("channel", "")),
            turns=[RetainedTurn(type_summary, _summary_prov)],
        ))
        _specialist_bg_tasks.add(bg)
        bg.add_done_callback(_specialist_bg_tasks.discard)
        retain_tasks.append(bg)

    # H-1 (v0.34.0): honor deferred hard-reload now that all bus +
    # retain writes have landed. Only on outcome=completed — per
    # ``completion.md`` doctrine line 61, a cancelled engagement does
    # NOT need a reload (artifact is operator-pending). On
    # outcome=error the engagement bailed; the reload decision is the
    # operator's, not the platform's. Drain the marker on every path
    # to prevent stale state from haunting an idempotent re-call or
    # a follow-up engagement reusing the (very short) id slice.
    deferred_pending = engagement.id in _ENGAGEMENTS_DEFERRED_HARD_RELOAD
    _ENGAGEMENTS_DEFERRED_HARD_RELOAD.discard(engagement.id)
    if outcome == "completed" and deferred_pending:
        # H-1: the Supervisor container-kill must be sequenced AFTER the retain
        # writes have landed. Since L33 moved the retains to background tasks,
        # drain them here (only on this rare deferred-reload path) so the
        # invariant "all retain writes have landed" still holds before restart.
        if retain_tasks:
            await asyncio.gather(*retain_tasks, return_exceptions=True)
        result = await _post_supervisor_restart()
        if result.get("status") == "error":
            logger.warning(
                "finalize engagement %s: deferred Supervisor "
                "restart failed: %s",
                engagement.id[:8], result.get("message"),
            )
        else:
            logger.info(
                "finalize engagement %s: deferred Supervisor restart "
                "POSTed (supervisor_status=%s); container kill arrives "
                "asynchronously, the bus message is already on disk.",
                engagement.id[:8], result.get("supervisor_status"),
            )

    # G-4 (v0.33.0): surface the cause when outcome=error so operators
    # have a starting point for triage. Pre-fix the only log line for
    # this path was an unconditional `logger.info(... outcome=error)`
    # with no reason — exploration2 found a configurator engagement
    # finalized error 24s after system_init with zero log evidence of
    # *why*. Upgrade to WARNING and pull whatever reason fields exist
    # off the registry origin (mark_error stashes kind/message there)
    # plus the text that the emit_completion caller (or the cancel
    # path) passed in.
    if outcome == "error":
        error_kind = engagement.origin.get("error_kind") or "unknown"
        error_message = engagement.origin.get("error_message") or ""
        reason_from_text = (text or "").strip()
        # Prefer registry-stored kind/message (set by mark_error before
        # finalize), then fall back to the text the model emitted.
        composite_reason = (
            error_message or reason_from_text or "no_reason_provided"
        )
        logger.warning(
            "Engagement %s finalized outcome=error kind=%s reason=%s",
            engagement.id[:8], error_kind, composite_reason,
        )
    else:
        logger.info(
            "Engagement %s finalized outcome=%s",
            engagement.id[:8], outcome,
        )
    return FinalizeResult.FINALIZED


# ---------------------------------------------------------------------------
# Stale-engagement reap (D-4, v0.69.0)
# ---------------------------------------------------------------------------


_ENGAGEMENT_REAP_DAYS_DEFAULT = 7.0


def _engagement_reap_days() -> float:
    """Reap TTL in days from the ``engagement_reap_days`` add-on option
    (env ``ENGAGEMENT_REAP_DAYS``); 0 disables the reap."""
    raw = os.environ.get("ENGAGEMENT_REAP_DAYS", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _ENGAGEMENT_REAP_DAYS_DEFAULT


def _resolve_engagement_driver(rec: Any) -> Any | None:
    """Resolve the driver that owns ``rec``'s engagement lifecycle.

    ``claude_code`` executors run as s6-managed subprocesses that only the
    claude_code driver stops/removes; everything else runs in-casa. Shared by
    ``emit_completion``, ``cancel_engagement`` and ``reap_stale_engagements``
    so all three tear down the RIGHT process. (D-4 fix v0.69.6: the reap used
    the in-casa driver for every record, so reaping a claude_code executor
    closed the topic but leaked its subprocess + workspace.)"""
    import agent as agent_mod
    attr = ("active_claude_code_driver" if rec.driver == "claude_code"
            else "active_engagement_driver")
    return getattr(agent_mod, attr, None)


async def reap_stale_engagements(*, ttl_days: float | None = None) -> int:
    """Cancel engagements with no user turn for ``ttl_days`` (D-4, v0.69.0).

    Interrupted/abandoned engagements used to linger active/idle forever
    (25-day-stale engagement, 2026-07-10; restart orphan reaped manually,
    2026-07-11). Runs in the daily engagement sweep BEFORE the idle-reminder
    pass so a to-be-reaped record doesn't get a pointless reminder in the
    same run. Goes through ``_finalize_engagement`` — the same funnel as a
    manual cancel — so the topic is closed + ledger-recorded, the RIGHT
    driver stops the process, Ellen is notified, and the summary retain
    lands. ``stale_before=cutoff`` makes the staleness check part of the
    locked terminal transition, so a record revived by a user turn between
    the snapshot below and the transition is NOT cancelled. Returns the
    number actually reaped.
    """
    if _engagement_registry is None:
        return 0
    if ttl_days is None:
        ttl_days = _engagement_reap_days()
    if ttl_days <= 0:
        return 0
    now = time.time()
    cutoff = now - ttl_days * 86400
    reaped = 0
    for rec in list(_engagement_registry.active_and_idle()):
        if rec.last_user_turn_ts >= cutoff:
            continue
        idle_days = int((now - rec.last_user_turn_ts) // 86400)
        logger.info(
            "reaping stale engagement %s (%s/%s, idle %dd > ttl %gd)",
            rec.id[:8], rec.kind, rec.role_or_type, idle_days, ttl_days,
        )
        try:
            won = await _finalize_engagement(
                rec, outcome="cancelled",
                text=(
                    f"Auto-closed after {idle_days} days with no activity "
                    f"(reap TTL {ttl_days:g}d). Start a new engagement to "
                    "continue this work."
                ),
                artifacts=[], next_steps=[],
                driver=_resolve_engagement_driver(rec),
                stale_before=cutoff,
            )
            if won:
                reaped += 1
        except Exception:  # noqa: BLE001 — one bad record must not stop the sweep
            logger.warning("reap of engagement %s failed", rec.id[:8], exc_info=True)
    return reaped


# ---------------------------------------------------------------------------
# emit_completion — called by the engaged agent
# ---------------------------------------------------------------------------


# B-3 (v0.69.3): the doctrine's status vocabulary (completion.md:31), each
# mapped to its TRUE registry outcome. "error" kept as a legacy alias — the
# tool historically treated any non-ok status as error, so agents may pass it.
_COMPLETION_STATUS_TO_OUTCOME = {
    "ok": "completed",
    "partial": "completed",   # objectives partly met — text carries the marker
    "failed": "error",
    "error": "error",
    "cancelled": "cancelled",
}
_COMPLETION_TEXT_MAX = 8000

# A.2 (v0.74.0): executor types whose ok-completions carry release artifacts
# that MUST pass the mechanical release-identity gate before finalization.
_COMPLETION_GUARDED_EXECUTOR_TYPES = frozenset({"plugin-developer"})


@tool(
    "emit_completion",
    "Mark this engagement complete. Ellen receives the summary. Must be called "
    "from inside an active engagement. status: 'ok' | 'partial' | 'failed' | "
    "'cancelled'.",
    {"text": str, "artifacts": list, "next_steps": list, "status": str},
)
async def emit_completion(args: dict) -> dict:
    engagement = engagement_var.get(None)
    if engagement is None:
        return _result({
            "status": "error",
            "kind": "not_in_engagement",
            "message": "emit_completion called outside an engagement",
        })

    # Bug 9 (v0.14.6): idempotency. Re-emitting completion (e.g. SDK
    # retry, hook misfire) used to re-run _finalize_engagement, which
    # double-closes the topic, double-NOTIFYs Ellen, and double-retains
    # the engagement summary on the shared `casa` bank. Re-read the live registry
    # state so we catch transitions that happened on another in-flight
    # turn since this engagement_var snapshot was taken.
    if _engagement_registry is not None:
        live = _engagement_registry.get(engagement.id)
        if live is not None and live.status in (
            "completed", "cancelled", "error",
        ):
            return _result({
                "status": "acknowledged",
                "kind": "already_terminal",
                "message": (
                    f"engagement is already {live.status!r}; "
                    "emit_completion is a no-op."
                ),
            })

    # B-3 (v0.69.3): validate BEFORE any side effect. The old mapping sent
    # EVERY status other than exactly "ok" — including the doctrine's own
    # "partial"/"cancelled", or a model writing "success" — into a terminal
    # outcome=error kind=emit_completion_error, failing fully-successful
    # engagements (2026-07-12 00:14Z incident). A malformed call now comes
    # back as a TOOL error the agent can correct; the engagement stays live.
    status_in = args.get("status", "ok") or "ok"
    if not isinstance(status_in, str) or status_in not in _COMPLETION_STATUS_TO_OUTCOME:
        return _result({
            "status": "error", "kind": "invalid_status",
            "message": (
                f"status={status_in!r} is not a valid completion status; use "
                "'ok' | 'partial' | 'failed' | 'cancelled' (completion.md). "
                "The engagement is still active — call emit_completion again "
                "with a valid status."
            ),
        })
    text = args.get("text", "") or ""
    if not isinstance(text, str):
        return _result({
            "status": "error", "kind": "invalid_args",
            "message": ("text must be a string (got "
                        f"{type(text).__name__}). The engagement is still "
                        "active — call emit_completion again."),
        })
    artifacts = args.get("artifacts") or []
    if isinstance(artifacts, str):
        artifacts = [artifacts]  # a bare SHA is obviously one artifact
    if not isinstance(artifacts, list):
        return _result({
            "status": "error", "kind": "invalid_args",
            "message": ("artifacts must be a list of strings (got "
                        f"{type(artifacts).__name__}). The engagement is "
                        "still active — call emit_completion again."),
        })
    next_steps = args.get("next_steps") or []
    if not isinstance(next_steps, list):
        return _result({
            "status": "error", "kind": "invalid_args",
            "message": ("next_steps must be a list (got "
                        f"{type(next_steps).__name__}). The engagement is "
                        "still active — call emit_completion again."),
        })
    # Task 6 (spec §4.6): the interactive output bound. This ALSO records
    # the `output_truncated` flag that flows to the DelegationComplete
    # notification, so a clipped engagement answer is disclosed to the
    # narrating resident (mirrors the sync/async truncation flag).
    output_truncated = len(text) > _COMPLETION_TEXT_MAX
    if output_truncated:
        logger.warning(
            "emit_completion text truncated (%d > %d chars) for engagement %s",
            len(text), _COMPLETION_TEXT_MAX, engagement.id[:8],
        )
        text = text[:_COMPLETION_TEXT_MAX] + " … [truncated]"
    if status_in == "partial":
        text = f"[partial] {text}" if text else "[partial]"
    outcome = _COMPLETION_STATUS_TO_OUTCOME[status_in]

    # A.2 (v0.74.0): producer completion guard — the enforcement seam that
    # makes forgetting the release ritual impossible. For a plugin-developer
    # ok-completion, EVERY casa_plugin_repo artifact must carry a verified
    # release identity (annotated vX.Y.Z tag pushed to the remote, peeling
    # to exactly the completion's `revision`, matching the REMOTE
    # plugin.json.version and the completion's own `version`). Rejection is
    # a TOOL error BEFORE finalization — the engagement stays live so the
    # producer can fix the release (or retry a transient remote-visibility
    # lag) and emit again. Guard crashes fail CLOSED: an unverifiable
    # release must never finalize as ok.
    if (status_in == "ok"
            and getattr(engagement, "kind", "") == "executor"
            and getattr(engagement, "role_or_type", "")
            in _COMPLETION_GUARDED_EXECUTOR_TYPES):
        import plugin_completion_guard
        try:
            guard_failures = await asyncio.to_thread(
                plugin_completion_guard.validate_completion_artifacts,
                artifacts)
        except Exception as exc:  # noqa: BLE001 — fail closed, stay live
            logger.exception("completion guard crashed for engagement %s",
                             engagement.id[:8])
            guard_failures = [{"index": None, "reason_code": "guard_error",
                               "message": str(exc)[:200]}]
        if guard_failures:
            return _result({
                "status": "error", "kind": "completion_rejected",
                "failures": guard_failures,
                "message": (
                    "completion rejected: casa_plugin_repo artifact(s) "
                    "failed release-identity validation. Required: an "
                    "ANNOTATED tag named 'v' + plugin.json.version, pushed "
                    "atomically with the branch, peeling on the remote to "
                    "the completion's `revision`. Fix the release (or retry "
                    "shortly if GitHub was transiently unavailable) and "
                    "call emit_completion again — the engagement is still "
                    "active."),
            })

    # Driver is discovered via the agent singleton accessible through the
    # agent module (plan-1 pattern).
    driver = None
    try:
        import agent as agent_mod  # noqa: F401
        if engagement.driver == "claude_code":
            driver = getattr(agent_mod, "active_claude_code_driver", None)
        else:
            driver = getattr(agent_mod, "active_engagement_driver", None)
    except Exception:
        pass

    # G-2 hotfix (v0.33.1): defensive reload guard. If this engagement
    # committed a real change via config_git_commit but never invoked
    # casa_reload / casa_reload_triggers, force-call casa_reload now.
    # The doctrine-only fix in v0.33.0 didn't change model behavior
    # (verify cid `a9313680` 2026-05-01 11:39:57Z); this guard makes
    # post-commit activation a platform invariant rather than a
    # model-compliance contract. Force-call BEFORE _finalize_engagement
    # so the bus message lands after the addon has been told to
    # restart, mirroring the doctrine's own commit-reload-emit order.
    if outcome == "completed" and engagement.id in _ENGAGEMENTS_PENDING_RELOAD:
        logger.warning(
            "Engagement %s emit_completion called with outstanding "
            "reload obligation — config_git_commit landed but no "
            "casa_reload(_triggers) was invoked. Force-calling "
            "casa_reload to honor the post-commit activation contract "
            "(G-2 v0.33.1 defensive guard).",
            engagement.id[:8],
        )
        try:
            # casa_reload is wrapped by @tool — call the underlying
            # handler so we don't pay the SDK envelope-decoding round
            # trip from inside Casa's own code path.
            forced = await casa_reload.handler({})
            logger.info(
                "Engagement %s forced casa_reload result: %s",
                engagement.id[:8],
                json.loads(forced["content"][0]["text"])
                if isinstance(forced, dict)
                and isinstance(forced.get("content"), list)
                and forced["content"]
                else forced,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Engagement %s forced casa_reload raised: %s; "
                "the artifact may remain INERT until manual reload",
                engagement.id[:8], exc,
            )
        finally:
            _ENGAGEMENTS_PENDING_RELOAD.discard(engagement.id)

    # G4 D1 (v0.96.0): completion inbound gate — the ask gate's contract
    # (`unread_inbound` ⇒ end your turn) extended to completion. Gates ONLY
    # successful outcomes; failed/error exits stay ungated (a broken
    # engagement must be able to die) and rely on the D4 annotation.
    # Placed BEFORE consumption-debt registration so a refusal leaves no
    # sequencer debt (Sol g4-r1-7).
    if (outcome == "completed" and driver is not None
            and hasattr(driver, "inbound_unread_depth")):
        try:
            _d = driver.inbound_unread_depth(engagement.id)
            _r = (driver.inbound_reservations(engagement.id)
                  if hasattr(driver, "inbound_reservations") else 0)
        except Exception:  # noqa: BLE001 — fail open, never wedge completion
            _d = _r = 0
        # STRICT int typing (fail open otherwise): a duck/mock driver whose
        # accessors return non-ints must read as "no unread input", never as
        # a fabricated depth (int(MagicMock()) == 1 bit us in the gate).
        _depth = _d if type(_d) is int else 0
        _resv = _r if type(_r) is int else 0
        if _depth > 0 or _resv > 0:
            try:
                _rn = (driver.record_completion_refusal(engagement.id)
                       if hasattr(driver, "record_completion_refusal") else 1)
            except Exception:  # noqa: BLE001
                _rn = 1
            _n = _rn if type(_rn) is int else 1
            # D3: from the 2nd consecutive refusal, force a turn boundary so
            # the queued envelope actually pumps (delivery re-arms only at
            # spawn) instead of livelocking on doctrine alone.
            if _n >= 2 and hasattr(driver, "force_completion_turn_boundary"):
                try:
                    await driver.force_completion_turn_boundary(engagement)
                except Exception:  # noqa: BLE001 — escalation is best-effort
                    logger.warning(
                        "completion-gate forced boundary failed for %s",
                        engagement.id[:8], exc_info=True)
            return _result({
                "status": "error", "kind": "unread_inbound",
                "retryable": True,
                "message": (
                    "an operator message is waiting unread — END YOUR TURN "
                    "NOW; the message is delivered at your next turn start; "
                    "read it, then call emit_completion again"),
            })

    # v0.79.0 (§2 F1(c)): register the emit_completion CONSUMPTION DEBT (identity
    # hash over the raw args) so the relay silently consumes the emit_completion
    # tool_use block instead of emitting stray narration below the completion.
    # Best-effort — no live sequencer ⇒ no-op.
    if driver is not None and hasattr(driver, "register_completion_consumption"):
        try:
            driver.register_completion_consumption(engagement.id, args)
        except Exception:  # noqa: BLE001 — defensive, never block completion
            logger.debug("register_completion_consumption failed", exc_info=True)

    fin = await _finalize_engagement(
        engagement,
        outcome=outcome,
        text=text,
        artifacts=artifacts,
        next_steps=next_steps,
        driver=driver,
        output_truncated=output_truncated,  # Task 6 (spec §4.6)
        inbound_gate=(outcome == "completed"),   # G4 D1/D2
    )
    # G4 D2 (v0.96.0): the registry-internal gate vetoed the flip (a message
    # landed between the handler gate above and the terminal critical
    # section). Roll back the consumption debt registered above (Sol
    # g4-r1-7) and refuse with the same retryable contract.
    if fin is FinalizeResult.PRECONDITION_FAILED:
        if driver is not None and hasattr(driver, "cancel_send_intent"):
            try:
                driver.cancel_send_intent(
                    engagement.id, f"emit_completion:{engagement.id}")
            except Exception:  # noqa: BLE001 — best-effort debt rollback
                logger.debug("completion debt rollback failed", exc_info=True)
        if (driver is not None
                and hasattr(driver, "record_completion_refusal")):
            try:
                driver.record_completion_refusal(engagement.id)
            except Exception:  # noqa: BLE001
                pass
        return _result({
            "status": "error", "kind": "unread_inbound", "retryable": True,
            "message": (
                "an operator message arrived while completing — END YOUR "
                "TURN NOW; read it at your next turn start, then call "
                "emit_completion again"),
        })
    # G4 D5 (v0.96.0): a persist rollback leaves the record LIVE — surface a
    # retryable error instead of acking a completion that did not happen.
    if fin is FinalizeResult.PERSIST_FAILED:
        return _result({
            "status": "error", "kind": "finalize_persist_failed",
            "retryable": True,
            "message": ("completion could not be persisted; the engagement "
                        "is still active — call emit_completion again"),
        })
    # Drain on terminal paths (e.g., outcome=error or
    # already-terminal short-circuit above) — the engagement is gone.
    _ENGAGEMENTS_PENDING_RELOAD.discard(engagement.id)
    return _result({"status": "acknowledged"})


# ---------------------------------------------------------------------------
# query_engager — retrieval + bounded synthesis
# ---------------------------------------------------------------------------


_QUERY_ENGAGER_SYSTEM = (
    "You answer factually using ONLY the provided context. If the context "
    "does not answer the question, reply with exactly: UNKNOWN"
)


async def _synthesize_answer(
    question: str, context: str, max_tokens: int,
) -> str:
    """Run a constrained Anthropic pass via the SDK. Returns the synthesized
    answer, or the literal string 'UNKNOWN' if the context is insufficient.

    Uses SECONDARY_AGENT_MODEL (env-resolved). No tools. No streaming — the
    caller needs a single string.
    """
    import os
    model = os.environ.get("SECONDARY_AGENT_MODEL", "haiku")
    # The pinned claude-agent-sdk's ClaudeAgentOptions has no
    # max_tokens/max_output_tokens field; cap output via the documented
    # Claude Code CLI env knob instead (env merges over the inherited
    # environment for this one CLI subprocess only).
    options = ClaudeAgentOptions(
        model=model,
        cli_path=CLAUDE_CLI_PATH,
        system_prompt=_QUERY_ENGAGER_SYSTEM,
        max_turns=1,
        mcp_servers={},
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max(1, max_tokens))},
    )
    prompt = (
        f"Context:\n{context}\n\nQuestion: {question}\n\n"
        f"Answer concisely, in at most about {max_tokens} tokens."
    )
    out = ""
    eng = engagement_var.get(None)
    eng_id = eng.id[:8] if eng is not None else None
    async with ClaudeSDKClient(
        sdk_logging.with_stderr_callback(options, engagement_id=eng_id),
    ) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in getattr(msg, "content", []):
                    if isinstance(b, TextBlock):
                        out += b.text
    out = out.strip()
    # Belt-and-braces hard stop in case the CLI/model still overshoots.
    from tokens import estimate_tokens
    if estimate_tokens(out) > max_tokens:
        out = out[: max_tokens * 4].rstrip()
    return out


@tool(
    "query_engager",
    "Ask the engaging agent a question. status=ok returns the answer "
    "synthesized from the engager's clearance-filtered memory; status=unknown "
    "means the memory was searched and holds nothing relevant; "
    "status=unavailable means memory could not be checked (do not conclude "
    "the information doesn't exist). Callable only from inside an active "
    "engagement.",
    {"question": str, "max_tokens": int},
)
async def query_engager(args: dict) -> dict:
    engagement = engagement_var.get(None)
    if engagement is None:
        return _result({"status": "error", "kind": "not_in_engagement",
                        "message": "query_engager called outside an engagement"})
    question = args.get("question", "") or ""
    max_tokens = max(1, min(int(args.get("max_tokens") or 500), 4000))

    # Retrieve engager-side context: a semantic recall against the shared `casa`
    # bank at the engagement origin's read-clearance (design §3, plan 3).
    import agent as agent_mod
    sem = getattr(agent_mod, "active_semantic_memory", None)
    context = ""
    memory_unavailable = False
    if sem is None:
        # No backend wired: the engager's memory cannot be checked.
        memory_unavailable = True
    else:
        try:
            context = await delegated_recall(
                sem, query=question,
                origin_channel=str(engagement.origin.get("channel", "")),
                max_tokens=2000, path="query_engager",
            )
        except RecallUnavailable:
            # Distinct from status=unknown (a genuine zero-hit): the engager's
            # memory could not be checked at all.
            memory_unavailable = True

    # Publish bus event so observer can see the query
    if _bus is not None:
        try:
            await _bus.notify(BusMessage(
                type=MessageType.NOTIFICATION,
                source=engagement.role_or_type, target="observer",
                content={
                    "event": "query_engager",
                    "engagement_id": engagement.id,
                    "question": question,
                    "status": "pending",
                },
                context={"engagement_id": engagement.id},
            ))
        except Exception:
            pass

    if memory_unavailable:
        return _result({
            "status": "unavailable", "text": "",
            "message": (
                "The engager's memory could not be checked (backend "
                "unavailable). Do not conclude the information doesn't exist."
            ),
        })
    if not context:
        return _result({"status": "unknown", "text": ""})
    answer = await _synthesize_answer(question, context, max_tokens)
    if answer.strip().upper().startswith("UNKNOWN"):
        return _result({"status": "unknown", "text": ""})
    return _result({"status": "ok", "text": answer})


@tool(
    "cancel_engagement",
    "Cancel an in-flight engagement by id. Closes the topic and NOTIFIES the engager.",
    {"engagement_id": str},
)
async def cancel_engagement(args: dict) -> dict:
    engagement_id = args.get("engagement_id", "") or ""
    if _engagement_registry is None:
        return _result({"status": "error", "kind": "not_initialized",
                        "message": "engagement registry not initialized"})
    rec = _engagement_registry.get(engagement_id)
    if rec is None:
        return _result({"status": "error", "kind": "unknown_engagement",
                        "message": f"no engagement named {engagement_id!r}"})
    if rec.status in ("completed", "cancelled", "error"):
        # L75/L24: a late cancel against an engagement that already
        # finalized (e.g. it raced emit_completion and lost) gets a
        # truthful reply instead of a silent no-op / duplicate finalize.
        return _result({"status": "acknowledged", "kind": "already_terminal",
                        "message": f"engagement is already {rec.status!r}"})

    driver = None
    try:
        import agent as agent_mod  # noqa: F401
        if rec.driver == "claude_code":
            driver = getattr(agent_mod, "active_claude_code_driver", None)
        else:
            driver = getattr(agent_mod, "active_engagement_driver", None)
    except Exception:
        pass

    await _finalize_engagement(
        rec, outcome="cancelled", text="Engagement cancelled.",
        artifacts=[], next_steps=[], driver=driver,
    )
    return _result({"status": "ok", "engagement_id": engagement_id})


# ---------------------------------------------------------------------------
# casa_reload_triggers - back-compat shim for Plan 3 soft-reload (now via dispatch)
# ---------------------------------------------------------------------------


@tool(
    "casa_reload_triggers",
    "Re-register triggers for one agent in-process (no addon restart). "
    "Use when ONLY <role>/triggers.yaml changed. For other config "
    "edits, use casa_reload(scope=...). Restricted to the configurator role.",
    {"role": str},
)
async def casa_reload_triggers(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_reload_triggers", caller)

    role = args.get("role")
    if not role:
        return _result({
            "status": "error", "kind": "role_required",
            "message": "casa_reload_triggers requires 'role'",
        })

    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return _result({
            "status": "error", "kind": "not_initialized",
            "message": "CasaRuntime not bound - boot ordering bug",
        })

    from reload import dispatch
    result = await dispatch("triggers", runtime=runtime, role=role)
    if result.get("status") == "ok":
        result.setdefault("role", role)
        # Back-compat: emit registered=[trigger_names] from runtime.role_configs / specialists
        try:
            cfg = runtime.role_configs.get(role)
            if cfg is None:
                cfg = runtime.specialist_registry.all_configs().get(role)
            if cfg is not None and getattr(cfg, "triggers", None):
                result["registered"] = [t.name for t in cfg.triggers]
        except Exception:  # noqa: BLE001 — best-effort surfacing
            pass
    return _result(result)


# ---------------------------------------------------------------------------
# Plan 4a.1: workspace inspection tools
# ---------------------------------------------------------------------------


def _scan_engagement_workspaces(root: str, status_filter: str | None) -> list[dict]:
    """Blocking scan of /data/engagements — must run via asyncio.to_thread.

    L27: computes a du-style recursive size per workspace (os.walk + per-file
    os.stat). claude_code workspaces can hold cloned repos / node_modules with
    tens of thousands of files, so this runs off the shared event loop.
    """
    from drivers.workspace import load_casa_meta
    entries: list[dict] = []
    for ent in sorted(os.scandir(root), key=lambda e: e.name):
        if not ent.is_dir():
            continue
        meta = load_casa_meta(ent.path) or {}
        if status_filter and meta.get("status") != status_filter:
            continue
        size_bytes = 0
        for dirpath, _dirs, files in os.walk(ent.path):
            for f in files:
                try:
                    size_bytes += os.stat(os.path.join(dirpath, f)).st_size
                except OSError:
                    pass
        entries.append({
            "engagement_id": ent.name,
            "executor_type": meta.get("executor_type"),
            "status": meta.get("status"),
            "created_at": meta.get("created_at"),
            "finished_at": meta.get("finished_at"),
            "retention_until": meta.get("retention_until"),
            "size_bytes": size_bytes,
        })
    return entries


@tool(
    "list_engagement_workspaces",
    "List engagement workspaces under /data/engagements with status + size. "
    "Optional status filter. Truncates at 100 entries.",
    {"status": str},
)
async def list_engagement_workspaces(args: dict) -> dict:
    status_filter = (args.get("status") or "").strip() or None
    root = _ENGAGEMENTS_ROOT

    if not os.path.isdir(root):
        return _result({"workspaces": [], "truncated": False, "total": 0})

    entries = await asyncio.to_thread(_scan_engagement_workspaces, root, status_filter)

    total = len(entries)
    truncated = total > 100
    return _result({
        "workspaces": entries[:100],
        "truncated": truncated,
        "total": total,
    })


_LIVE_ENGAGEMENT_STATES = frozenset({"active", "idle"})


@tool(
    "delete_engagement_workspace",
    "Delete /data/engagements/<id>/ and cancel+finalize the engagement if "
    "still active or idle. Requires force=true to act on a live engagement.",
    {"engagement_id": str, "force": bool},
)
async def delete_engagement_workspace(args: dict) -> dict:
    import shutil

    engagement_id = (args.get("engagement_id") or "").strip()
    force = bool(args.get("force", False))

    if not engagement_id:
        return _result({
            "status": "error", "kind": "bad_request",
            "message": "engagement_id is required",
        })
    if _engagement_registry is None:
        return _result({
            "status": "error", "kind": "not_initialized",
            "message": "engagement registry not wired",
        })

    rec = _engagement_registry.get(engagement_id)
    if rec is None:
        return _result({
            "status": "error", "kind": "unknown_engagement",
            "message": f"no engagement named {engagement_id!r}",
        })

    # Bug 12 (v0.14.6): treat ``idle`` the same as ``active``. An idle
    # engagement is the SDK-suspended-after-24h state — its s6 service
    # and workspace are still live and the driver may resume on the
    # next user turn. Pre-fix the guard only checked ``active`` and
    # quietly yanked an idle workspace out from under a still-running
    # service.
    if rec.status in _LIVE_ENGAGEMENT_STATES and not force:
        return _result({
            "status": "error", "kind": "refused",
            "message": (
                f"engagement is {rec.status!r} (still live); "
                "pass force=true to cancel + delete"
            ),
        })

    if rec.status in _LIVE_ENGAGEMENT_STATES and force:
        # Finalize as cancelled before pulling the workspace.
        driver = None
        try:
            import agent as agent_mod
            driver = (getattr(agent_mod, "active_claude_code_driver", None)
                      if rec.driver == "claude_code"
                      else getattr(agent_mod, "active_engagement_driver", None))
        except Exception:
            pass
        await _finalize_engagement(
            rec, outcome="cancelled",
            text="Workspace deletion forced",
            artifacts=[], next_steps=[],
            driver=driver,
        )

    ws = os.path.join(_ENGAGEMENTS_ROOT, engagement_id)
    if os.path.isdir(ws):
        try:
            shutil.rmtree(ws)
        except OSError as exc:
            return _result({
                "status": "error", "kind": "rmtree_failed",
                "message": f"rmtree {ws}: {exc}",
            })
    # v0.64.0: the per-engagement s6-log dir follows the workspace on this
    # caller-managed path too — once the workspace is gone, the retention
    # sweep can never map to the log dir again.
    from drivers.workspace import engagement_log_dir
    log_dir = engagement_log_dir(engagement_id)
    try:
        if os.path.isdir(log_dir):
            shutil.rmtree(log_dir)
    except OSError as exc:
        logger.warning(
            "delete_engagement_workspace: log dir rmtree %s failed: %s",
            log_dir, exc,
        )
    return _result({
        "status": "ok", "engagement_id": engagement_id,
        "workspace_removed": os.path.isdir(ws) is False,
    })


_PEEK_MAX_DEFAULT = 65_536
_PEEK_MAX_HARD = 524_288


@tool(
    "peek_engagement_workspace",
    "Read-only inspection of /data/engagements/<id>/. Empty path returns a "
    "3-deep tree listing; otherwise reads file contents up to max_bytes "
    "(default 64KB, hard cap 512KB). Path-traversal guarded.",
    {"engagement_id": str, "path": str, "max_bytes": int},
)
async def peek_engagement_workspace(args: dict) -> dict:
    from pathlib import Path as _Path

    engagement_id = (args.get("engagement_id") or "").strip()
    if not engagement_id:
        return _result({"status": "error", "kind": "bad_request",
                        "message": "engagement_id is required"})

    # Security (H15): engagement_id must be a bare workspace name. Real ids
    # are uuid4().hex; reject anything containing path separators or dots so
    # it cannot re-root the workspace via '..' (traversal) or '/config'
    # (absolute join). Without this the traversal guard on `path` below is
    # useless — it anchors on the (already re-rooted) ws_root.resolve().
    if not re.fullmatch(r"[A-Za-z0-9_-]+", engagement_id):
        return _result({"status": "error", "kind": "bad_request",
                        "message": f"invalid engagement_id {engagement_id!r}"})

    ws_root = _Path(_ENGAGEMENTS_ROOT) / engagement_id
    # Defense in depth: the resolved workspace must sit DIRECTLY under the
    # engagements root — never above it or re-rooted elsewhere (also blocks
    # symlink tricks and any future id class).
    if ws_root.resolve().parent != _Path(_ENGAGEMENTS_ROOT).resolve():
        return _result({"status": "error", "kind": "unknown_workspace",
                        "message": f"no workspace for {engagement_id!r}"})
    if not ws_root.is_dir():
        return _result({"status": "error", "kind": "unknown_workspace",
                        "message": f"no workspace for {engagement_id!r}"})

    path_arg = (args.get("path") or "").strip()
    if not path_arg:
        tree = _walk_workspace_tree(ws_root, max_depth=3)
        return _result({"status": "ok", "tree": tree})

    full = (ws_root / path_arg).resolve()
    ws_resolved = ws_root.resolve()
    try:
        full.relative_to(ws_resolved)
    except ValueError:
        return _result({"status": "error", "kind": "path_outside_workspace",
                        "message": f"path {path_arg!r} escapes the workspace"})

    if not full.is_file():
        return _result({"status": "error", "kind": "not_a_file",
                        "message": f"{path_arg!r} is not a regular file"})

    max_bytes = int(args.get("max_bytes") or _PEEK_MAX_DEFAULT)
    if max_bytes > _PEEK_MAX_HARD:
        max_bytes = _PEEK_MAX_HARD
    if max_bytes < 1:
        max_bytes = _PEEK_MAX_DEFAULT

    def _read_prefix() -> str:
        # M26: read only the capped byte prefix — never load the whole file
        # into RAM (a multi-GB workspace log would OOM the container). Cap is
        # in BYTES; a multibyte char split at the boundary decodes to a
        # trailing U+FFFD, which is acceptable for a peek tool.
        with open(full, "rb") as fh:
            data = fh.read(max_bytes)
        return data.decode("utf-8", errors="replace")

    contents = await asyncio.to_thread(_read_prefix)
    return _result({"status": "ok", "path": path_arg, "contents": contents})


def _walk_workspace_tree(root, *, max_depth: int) -> list[dict]:
    out: list[dict] = []
    def _walk(d, depth):
        if depth > max_depth:
            return []
        children: list[dict] = []
        try:
            for e in sorted(os.scandir(d), key=lambda e: e.name):
                entry = {"name": e.name,
                         "type": "dir" if e.is_dir() else "file"}
                if e.is_dir() and depth < max_depth:
                    entry["children"] = _walk(e.path, depth + 1)
                children.append(entry)
        except OSError:
            pass
        return children
    out = _walk(str(root), 1)
    return out


_TOPIC_CLEANUP_SCOPES = ("due", "all_terminal")


@tool(
    "cleanup_engagement_topics",
    "Delete finished engagements' Telegram forum topics recorded in the "
    "topic ledger. scope='due' (default) deletes only entries past the "
    "7-day retention window; 'all_terminal' purges every ledger entry "
    "immediately and is configurator-only. Deletion is irreversible — pass "
    "dry_run=true first to preview what would be deleted.",
    {"scope": str, "dry_run": bool},
)
async def cleanup_engagement_topics(args: dict) -> dict:
    """Configurator-owned on-demand topic cleanup [AR-7] — ledger-only.

    Deletes ONLY topics recorded in the terminal-engagement ledger
    (``/data/topic-ledger.json``): never guesses topic ids, never touches
    active/idle engagements (they are not in the ledger). Deletion is
    IRREVERSIBLE — it removes the topic and all its messages for every
    member — so prefer a ``dry_run=true`` pass first and confirm the
    counts before purging for real (configurator doctrine:
    architecture.md "Engagement-topic cleanup"). The result echoes
    ``dry_run`` and lists the affected topics in ``targets``
    (``{engagement_id, topic_id}`` pairs — would-be deletions under
    dry_run, resolved deletions otherwise) alongside the counts.
    Per-entry telegram failures are classified inside the sweep ([AR-5])
    and reported in ``failures``; entries are retained for retry, never
    dropped on an unrecognized error.
    """
    import topic_ledger

    scope = (args.get("scope") or "due").strip()
    if scope not in _TOPIC_CLEANUP_SCOPES:
        return _result({
            "status": "error", "kind": "bad_scope",
            "message": (
                f"scope must be one of {_TOPIC_CLEANUP_SCOPES}, "
                f"got {scope!r}"
            ),
        })
    # v0.69.12: Ellen (assistant) holds a due-ONLY variant (X2 resolved →
    # webhook trust = authenticated, so the AR-7 deferral is cleared). The
    # irreversible `all_terminal` purge (deletes EVERY ledger topic + all its
    # messages immediately, for all members) stays configurator-only; a
    # non-privileged caller requesting it is refused with a nudge to `due`.
    if scope == "all_terminal" and _effective_caller_role() not in _PRIVILEGED_CONFIG_ROLES:
        return _result({
            "status": "error", "kind": "not_authorized",
            "message": (
                "scope='all_terminal' (immediate purge of every engagement "
                "topic) is restricted to the configurator; use scope='due' to "
                "delete only topics past the retention window."
            ),
        })
    dry_run = bool(args.get("dry_run", False))

    channel = (_channel_manager.get("telegram")
               if _channel_manager is not None else None)
    if channel is None or not getattr(channel, "engagement_supergroup_id", None):
        return _result({
            "status": "error", "kind": "telegram_not_configured",
            "message": ("telegram engagement supergroup is not configured "
                        "— there are no topics to clean up"),
        })

    result = await topic_ledger.sweep_topics(
        channel,
        chat_id=channel.engagement_supergroup_id,
        scope=scope,
        dry_run=dry_run,
    )
    return _result({"status": "ok", **result})


# ---------------------------------------------------------------------------
# Plan 4b §7.1: marketplace_* Configurator MCP tools
# ---------------------------------------------------------------------------


# H16: serialize the mutating plugin/marketplace tools once their blocking
# bodies move off the loop via asyncio.to_thread. On the single event loop
# these handlers were previously mutually exclusive for free (they never
# awaited mid-body); the lock preserves that invariant for concurrent
# marketplace-file / manifest writes. Read-only vault helpers don't take it.
_PLUGIN_TOOLS_LOCK = asyncio.Lock()

# ---------------------------------------------------------------------------
# Unified plugin architecture (§3.9/§3.13): registry-mutating tools.
# ---------------------------------------------------------------------------

_PLUGIN_HEALTH_PATH = "/data/plugin-health.json"


def _regenerate_plugin_health(extra_issues: list) -> None:
    """§3.10/R2-4 + Sol #13 + D2/B3 (v0.74.0): rewrite the health report from
    the CURRENT resolver state PLUS the RUNTIME verify state of EVERY
    registered plugin — not just the plugin this mutation touched. Otherwise
    a successful mutation of plugin B would rewrite health from resolver
    issues + B's (empty) extras and ERASE plugin A's still-active runtime
    failure. D2: readiness derives from THIS regeneration's OWN fresh pass —
    a caller-supplied stage="verify" extra row is DROPPED when the fresh
    pass can REDISCOVER it (its plugin is registered AND its target is None
    or still among that entry's targets): a still-true failure re-lands
    fresh, a transient one (e.g. a pre-v0.74.0 torn-read reload_required)
    must not linger. Carried forward verbatim: stage="reload" rows (a failed
    reconstruction is not rediscoverable by verify), rows for unregistered
    plugins, and rows whose target was since UNASSIGNED (the fresh pass no
    longer grades that target — B3)."""
    import plugin_health
    from plugin_registry import PluginIssue, load_registry
    res = plugin_registry.resolve_all()
    reg = load_registry()
    entry_targets: dict = {}
    if reg.valid:
        for e in reg.entries:
            entry_targets[e.get("name")] = list(e.get("targets") or [])

    def _rediscoverable(issue) -> bool:
        if getattr(issue, "stage", None) != "verify":
            return False
        nm = getattr(issue, "name", None)
        if nm not in entry_targets:
            return False
        tgt = getattr(issue, "target", None)
        return tgt is None or tgt in entry_targets[nm]

    extra_issues = [e for e in extra_issues if not _rediscoverable(e)]
    seen = {(getattr(e, "name", None), getattr(e, "target", None),
             getattr(e, "reason_code", None)) for e in extra_issues}
    runtime_issues: list = []
    def _add(name, target, reason):
        key = (name, target, reason)
        if key not in seen:
            seen.add(key)
            runtime_issues.append(PluginIssue(
                name=name, target=target, stage="verify", reason_code=reason))

    if reg.valid:
        for entry in reg.entries:
            name = entry.get("name")
            try:
                verify = _tool_verify_plugin_state(plugin_name=name)
            except Exception:  # noqa: BLE001
                # Sol round-3 H13: a verifier crash is ITSELF a health problem —
                # surface it, never silently drop the plugin from the report.
                _add(name, None, "verify_exception")
                continue
            rows = verify.get("targets") or []
            for row in rows:
                if not row.get("ready"):
                    _add(name, row.get("target"),
                         (row.get("reasons") or ["not_ready"])[0])
            # Sol round-3 H13: a top-level not-ready with NO target rows (e.g. an
            # unassigned plugin with a missing secret / mcp_invalid) would else be
            # erased — surface it against the plugin itself.
            if verify.get("ready") is not True and not rows:
                for reason in (verify.get("reasons") or ["not_ready"]):
                    _add(name, None, reason)
    # Release B: plugin-trigger issues are a RECOMPUTABLE input — derived
    # fresh on EVERY regeneration (never passed as one-shot extras), so an
    # unrelated health refresh can never erase trigger_pending_ack /
    # trigger_channel_missing. current_issues() never raises.
    import trigger_reconcile
    trigger_issues = trigger_reconcile.current_issues()
    plugin_health.write_report(
        issues=(list(res.issues) + list(extra_issues) + runtime_issues
                + list(trigger_issues)),
        warnings=list(res.warnings),
        path=_PLUGIN_HEALTH_PATH,
    )


async def _notify_plugin_health_if_possible() -> None:
    if _bus is None:
        return
    try:
        import casa_core
        await casa_core.notify_plugin_health(_bus, path=_PLUGIN_HEALTH_PATH)
    except Exception:  # noqa: BLE001 — never fail a mutation on notify
        logger.debug("plugin health notify skipped", exc_info=True)


def _stale_absent_targets(targets: list, name: str, runtime) -> list[str]:
    """The concrete resident/specialist targets that STILL bind `name` after
    an absent-expectation mutation (unassign/remove) — read from the live
    agents' coherent binding property (D3, v0.74.0: concrete targets, never
    a registry-wide null)."""
    if runtime is None:
        return []
    agents = getattr(runtime, "agents", {}) or {}
    stale: list[str] = []
    for target in targets:
        tier, _, role = target.partition(":")
        if tier in ("resident", "specialist"):
            agent = agents.get(role)
            if agent is not None and name in getattr(
                    agent, "active_plugin_binding", {}):
                stale.append(target)
    return stale


def _postcondition_holds(verify: dict, targets: list, *, expect: str,
                         name: str | None = None, runtime=None) -> bool:
    """§3.9 mutation postcondition. 'present' (add/update/assign): every in-casa
    target row must be ready. 'absent' (unassign/remove): no RECONSTRUCTED
    in-casa agent still binds `name` — delegates to _stale_absent_targets so
    failures carry concrete targets (D3, v0.74.0)."""
    if expect == "present":
        # Sol #9: a plugin with zero target rows (e.g. fully unassigned, or an
        # update whose new artifact has unresolved secrets) made all([]) == True
        # → false-green. Require the TOP-LEVEL readiness, which folds in the
        # artifact/tools/secrets checks AND every target row's readiness — so an
        # empty-rows verify no longer passes vacuously.
        return verify.get("ready") is True
    if runtime is None or name is None:
        return True
    return not _stale_absent_targets(targets, name, runtime)


def _issues_from_mutation(name: str, *, reload_errors: list, verify: dict,
                          expect: str, postcondition_ok: bool,
                          stale_absent_targets: "tuple | list" = (),
                          snapshot_raced: bool = False) -> list:
    """R2-4 + D3 (v0.74.0): translate reload/verify/postcondition failures
    into structured PluginIssues. postcondition_failed(target=None) is
    emitted ONLY when nothing concrete (reload error / not-ready target row /
    top-level verify reason / snapshot race) already explains the failure —
    pre-v0.74.0 it duplicated reload_required registry-wide, warning EVERY
    resident (the operator's original symptom). Absent-case failures name
    their concrete stale targets; a snapshot race is its own reason."""
    from plugin_registry import PluginIssue
    issues: list = []
    for err in reload_errors:
        issues.append(PluginIssue(
            name=name, target=err.get("target"), stage="reload",
            reason_code="reload_failed"))
    row_failures = [row for row in (verify.get("targets") or [])
                    if not row.get("ready")]
    for row in row_failures:
        reasons = row.get("reasons") or ["not_ready"]
        issues.append(PluginIssue(
            name=name, target=row.get("target"), stage="verify",
            reason_code=reasons[0]))
    if snapshot_raced:
        issues.append(PluginIssue(
            name=name, target=None, stage="verify",
            reason_code="snapshot_raced"))
        return issues
    if postcondition_ok or reload_errors:
        return issues
    if expect == "absent" and stale_absent_targets:
        for t in stale_absent_targets:
            issues.append(PluginIssue(
                name=name, target=t, stage="verify",
                reason_code="postcondition_failed"))
    elif not row_failures and not (verify.get("reasons") or []):
        issues.append(PluginIssue(
            name=name, target=None, stage="verify",
            reason_code="postcondition_failed"))
    return issues


async def _reload_and_verify_targets(name: str, targets: list,
                                     *, expect: str) -> dict:
    """§3.9 mutation sequencing — THE ordering that kills the incident. The
    atomic registry write already happened; now: reload the resolver snapshot
    FIRST, reconstruct affected in-casa agents, desired==active verify, then
    regenerate + notify health. Order is load-bearing (stale-snapshot hazard)."""
    await asyncio.to_thread(plugin_registry.reload_snapshot)   # 1. FIRST
    import agent as agent_mod
    import reload as reload_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    reloaded: list = []
    reload_errors: list = []
    for target in targets:
        tier, _, role = target.partition(":")
        if tier in ("resident", "specialist") and runtime is not None:
            res = await reload_mod.dispatch("agent", runtime=runtime, role=role)
            if res.get("status") == "ok":
                reloaded.append(target)
            else:
                reload_errors.append({"target": target, **res})
        # executors: nothing to reconstruct (per-launch resolution).
    # Sol round-3 B2a + D2 (v0.74.0): reconstruction leaves the new Agent's
    # binding snapshot lazy (None until its first turn) — verify would then
    # classify a live, registered agent as "dormant" and green it BEFORE its
    # binding is captured. Force resolution, then enforce the D2 generation
    # fence: every reloaded target's snapshot must carry the CURRENT
    # post-reload generation, and the global generation must not move across
    # the verify. A mismatch means an intervening reload — retry ONCE with a
    # real re-dispatch (a cached snapshot would short-circuit a bare
    # re-resolve), then fail EXPLICITLY (kind=snapshot_raced), never grading
    # against a stale generation.
    agents = getattr(runtime, "agents", {}) or {}
    verify: dict = {}
    snapshot_raced = False
    resolve_failed: set = set()
    for _verify_attempt in range(2):
        gen_now = plugin_registry.snapshot_generation()
        for target in reloaded:
            _, _, role = target.partition(":")
            agent = agents.get(role)
            if agent is not None and getattr(
                    agent, "plugin_binding_snapshot", None) is None:
                try:
                    await agent._get_plugin_resolution()
                except Exception as exc:  # noqa: BLE001
                    # Sol round-4 M: a fail-OPEN resolve would leave the
                    # snapshot None → verify classifies the live agent as
                    # dormant → green. Record a reload error instead.
                    logger.warning(
                        "post-reconstruct resolve failed for %s: %s",
                        role, exc)
                    if target not in resolve_failed:
                        resolve_failed.add(target)
                        reload_errors.append(
                            {"target": target, "status": "error",
                             "kind": "resolve_failed"})
        stale_gen = []
        for t in reloaded:
            _, _, _role = t.partition(":")
            _snap = getattr(agents.get(_role), "plugin_binding_snapshot", None)
            if _snap is not None and _snap.generation != gen_now:
                stale_gen.append(t)
        verify = await asyncio.to_thread(
            _tool_verify_plugin_state, plugin_name=name)
        raced = (bool(stale_gen)
                 or plugin_registry.snapshot_generation() != gen_now)
        if not raced:
            snapshot_raced = False
            break
        snapshot_raced = True
        if _verify_attempt == 0:
            for t in (stale_gen or reloaded):
                _, _, role = t.partition(":")
                try:
                    await reload_mod.dispatch("agent", runtime=runtime,
                                              role=role)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("generation-race re-reload failed for "
                                   "%s: %s", role, exc)
    stale_absent = (_stale_absent_targets(targets, name, runtime)
                    if expect == "absent" else [])
    # Release B: reconcile plugin-declared webhook triggers LAST — after the
    # snapshot reload, agent reconstruction, and verify — so the overlay
    # derives from the POST-mutation resolver state (every one of the 5
    # lifecycle mutations funnels through this sequencer). Its issues land in
    # the health regen below via trigger_reconcile.current_issues() (a
    # recomputable input, never a one-shot extra). A reconcile failure keeps
    # the old overlay (fail-safe for ingress) and never fails the mutation.
    try:
        import trigger_reconcile
        await trigger_reconcile.reconcile_from_runtime(runtime)
    except Exception:  # noqa: BLE001
        logger.warning("plugin-trigger reconcile failed", exc_info=True)
    ok = (not snapshot_raced and not reload_errors
          and _postcondition_holds(verify, targets, expect=expect,
                                   name=name, runtime=runtime))
    mutation_issues = _issues_from_mutation(
        name, reload_errors=reload_errors, verify=verify,
        expect=expect, postcondition_ok=ok,
        stale_absent_targets=stale_absent, snapshot_raced=snapshot_raced)
    await asyncio.to_thread(_regenerate_plugin_health, mutation_issues)
    await _notify_plugin_health_if_possible()
    result = {
        # Spec §E pinned payload: activation_committed = the registry pin
        # landed (this sequencer only runs post-commit); runtime_ready =
        # reload + verify agree desired==active; ok = both; kind is present
        # on EVERY payload (None on success). is_error is NOT a payload
        # field — _result derives the outer MCP flag from ok:false.
        "ok": ok,
        "kind": None,
        "activation_committed": True,
        "runtime_ready": ok,
        "reloaded": reloaded, "reload_errors": reload_errors,
        "verify": verify,
    }
    if not ok:
        result["kind"] = ("snapshot_raced" if snapshot_raced
                          else "reload_failed" if reload_errors
                          else "postcondition_failed")
    return result


def _safe_remove_manifest(name: str) -> None:
    """Sol round-4: remove the plugin's sysreq manifest row, tolerating failure
    (unwritable/malformed manifest) so a cleanup error never bypasses the
    mandatory reload/verify sequencing that follows a registry mutation."""
    try:
        remove_manifest(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin sysreq manifest cleanup for %s failed "
                       "(non-fatal): %s", name, exc)


def _install_plugin_sysreqs(name: str, manifest: dict) -> dict | None:
    """§3.3: install a plugin's system requirements BEFORE registry activation.
    Returns an error envelope on failure (registry left unchanged), else None."""
    reqs = plugin_store.manifest_sysreqs(manifest)
    if not reqs:
        # Sol round-3 M: an update to a manifest with NO requirements must clear
        # any stale row (add_plugin_entry replaces by name on the has-reqs path,
        # so only this branch leaks). No-op for a brand-new plugin. Sol round-4:
        # cleanup failure is non-fatal — never break the mutation sequence.
        _safe_remove_manifest(name)
        return None
    try:
        outcomes = install_requirements(
            plugin_name=name, requirements=reqs,
            tools_root=Path("/config/tools"))
    except OrchestrationError as exc:
        return {"ok": False, "kind": "system_requirements_failed",
                "detail": str(exc)}
    for outcome in outcomes:
        add_manifest(outcome.manifest_entry(name))
    return None


def _resolve_and_guard(*, repo: str, ref: str,
                       expected_revision: str | None) -> "str | dict":
    """C.2 steps 1-2 (v0.74.0): resolve ref -> sha, then the
    expected_revision guard. Returns the 40-hex commit on success, else a
    failure envelope dict. resolve_unavailable envelopes carry the
    resolver's structured retry_after_s when known (C.3)."""
    try:
        commit = plugin_store.resolve_ref(repo, ref)
    except plugin_store.RefNotFound:
        return {"ok": False, "kind": "ref_not_found"}
    except plugin_store.ResolveAuthFailed:
        return {"ok": False, "kind": "resolve_auth_failed"}
    except plugin_store.SourceEmpty:
        return {"ok": False, "kind": "source_empty"}
    except plugin_store.ResolveUnavailable as exc:
        env = {"ok": False, "kind": "resolve_unavailable"}
        if getattr(exc, "retry_after_s", None) is not None:
            env["retry_after_s"] = exc.retry_after_s
        return env
    except plugin_store.StoreError as exc:
        return {"ok": False, "kind": getattr(exc, "reason_code", "store_error")}
    if expected_revision is not None:
        want = plugin_store.normalize_revision(expected_revision)
        if want is None:
            return {"ok": False, "kind": "invalid_expected_revision",
                    "expected_revision": expected_revision}
        if want != commit:
            # A tag that MOVED between the producer's build and this pin
            # (spec C.2 step 2) — hard abort, nothing mutated.
            return {"ok": False, "kind": "revision_mismatch",
                    "expected_revision": want, "resolved_revision": commit}
    return commit


def _tag_version_guard(ref: str, manifest: dict) -> dict | None:
    """C.2 step 4 (v0.74.0): a release-tag ref must equal 'v' + the FETCHED
    manifest's version. None when the guard passes (or ref is not a release
    tag). Runs BEFORE sysreq install and registry mutation."""
    if not plugin_store.RELEASE_TAG_RE.match(ref or ""):
        return None
    version = str((manifest or {}).get("version", ""))
    if ref != f"v{version}":
        return {"ok": False, "kind": "tag_version_mismatch",
                "ref": ref, "manifest_version": version}
    return None


def _plugin_add_sync(*, name: str, repo: str, ref: str, subdir: str = "",
                     targets: list,
                     expected_revision: str | None = None) -> dict:
    """Blocking core of plugin_add. C.2 (v0.74.0) enforcement ORDER — all
    identity guards run before any sysreq install or registry mutation:
    resolve -> revision-check -> manifest-fetch (publish) -> tag/version-check
    -> sysreqs -> activate. Registry stays byte-identical on any
    pre-activation failure (FR2)."""
    if not plugin_registry.NAME_RE.match(name or ""):
        return {"ok": False, "kind": "invalid_name", "name": name}
    targets = list(targets or [])
    # Sol round-3 M: a non-string target must not raise TypeError out of the
    # envelope (TARGET_RE.match(1) throws) — reject it as an invalid target.
    bad = [t for t in targets
           if not isinstance(t, str) or not plugin_registry.TARGET_RE.match(t)]
    if bad or not targets:
        return {"ok": False, "kind": "invalid_target", "invalid": bad}
    # Sol round-3 M: validate subdir here so `../x` returns an envelope instead
    # of an uncaught ValueError from normalize_subdir inside publish.
    try:
        plugin_registry.normalize_subdir(subdir or "")
    except ValueError:
        return {"ok": False, "kind": "invalid_subdir", "subdir": subdir}
    data = plugin_registry.load_registry()                     # from DISK
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    if any(isinstance(e, dict) and e.get("name") == name
           for e in data.raw.get("plugins", [])):
        return {"ok": False, "kind": "plugin_exists", "name": name}
    guarded = _resolve_and_guard(repo=repo, ref=ref,
                                 expected_revision=expected_revision)
    if isinstance(guarded, dict):
        return guarded
    try:
        result = plugin_store.publish(name=name, repo=repo, ref=ref,
                                      subdir=subdir, commit=guarded)
    except plugin_store.RefNotFound:
        return {"ok": False, "kind": "ref_not_found"}
    except plugin_store.ResolveUnavailable:
        return {"ok": False, "kind": "resolve_unavailable"}
    except plugin_store.StoreError as exc:
        return {"ok": False, "kind": getattr(exc, "reason_code", "store_error"),
                **getattr(exc, "detail", {})}
    err = _tag_version_guard(ref, result.manifest)             # BEFORE sysreqs
    if err is not None:
        return err
    err = _install_plugin_sysreqs(name, result.manifest)       # BEFORE activate
    if err is not None:
        return err
    data.raw.setdefault("plugins", []).append({
        "name": name,
        "source": {"type": "github", "repo": repo, "ref": ref,
                   "revision": result.revision, "subdir": subdir},
        "artifact_id": result.artifact_id, "version": result.version,
        "targets": targets,
    })
    plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "targets": targets,
            "artifact_id": result.artifact_id, "version": result.version,
            "revision": result.revision, "path": result.path}


def _plugin_update_sync(*, name: str, new_ref: str,
                        expected_revision: str | None = None) -> dict:
    """Blocking core of plugin_update. C.2 (v0.74.0) enforcement ORDER — all
    identity guards run before any sysreq install or registry mutation:
    resolve -> revision-check -> manifest-fetch (publish) -> tag/version-check
    -> sysreqs -> registry repoint. Version DERIVED from the fetched manifest
    (FR5). Old artifact retained."""
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = next((e for e in data.raw.get("plugins", [])
                  if isinstance(e, dict) and e.get("name") == name), None)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    # A:§3.3 (r1-B8): capture the OLD artifact_id BEFORE the mutation — the
    # caller invalidates its grants/challenges only after this commits.
    old_artifact_id = entry.get("artifact_id")
    src = entry.get("source") or {}
    repo, subdir = src.get("repo", ""), src.get("subdir", "")
    guarded = _resolve_and_guard(repo=repo, ref=new_ref,
                                 expected_revision=expected_revision)
    if isinstance(guarded, dict):
        return guarded
    try:
        result = plugin_store.publish(name=name, repo=repo, ref=new_ref,
                                      subdir=subdir, commit=guarded)
    except plugin_store.RefNotFound:
        return {"ok": False, "kind": "ref_not_found"}
    except plugin_store.ResolveUnavailable:
        return {"ok": False, "kind": "resolve_unavailable"}
    except plugin_store.StoreError as exc:
        return {"ok": False, "kind": getattr(exc, "reason_code", "store_error"),
                **getattr(exc, "detail", {})}
    err = _tag_version_guard(new_ref, result.manifest)         # BEFORE sysreqs
    if err is not None:
        return err
    err = _install_plugin_sysreqs(name, result.manifest)       # BEFORE repoint
    if err is not None:
        return err
    entry["source"]["ref"] = new_ref
    entry["source"]["revision"] = result.revision
    entry["artifact_id"] = result.artifact_id
    entry["version"] = result.version
    plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "targets": list(entry.get("targets") or []),
            "artifact_id": result.artifact_id, "version": result.version,
            "revision": result.revision, "path": result.path,
            "old_artifact_id": old_artifact_id}


def _invalidate_lifecycle(*, artifact_id: "str | None" = None,
                          roles: "list[str] | None" = None) -> None:
    """Purge authorization grants + cancel pending challenges for an
    invalidated artifact and/or role (A:§3.3/§3.4, r1-B8/r2-B5). Call sites
    invoke this AFTER the registry mutation COMMITS and BEFORE the first
    post-commit await/reload — an ABORTED mutation (validation failure)
    must invalidate NOTHING. A stale challenge referencing the invalidated
    artifact/role must never survive to be approved (an obsolete keyboard
    dispatching an unusable continuation is a broken lifecycle even though
    artifact/role binding already blocks consumption). ``roles`` entries may
    be tier-qualified plugin targets (``"specialist:finance"``) — normalized
    via :func:`authz_grants.normalize_role` before purge/cancel, since
    ``GrantKey.enforcement_role`` is always plain."""
    if artifact_id:
        GRANTS.purge_artifact(artifact_id)
        CHALLENGES.cancel_matching(artifact=artifact_id)
        # Release B: an artifact change/removal drops its webhook-trigger
        # consents (the ack identity is artifact-bound) and retires the
        # per-trigger secrets — live + .next + rotation state — BEFORE any
        # re-approval can mint replacements, so a new artifact never
        # inherits the old one's credentials. cancel_matching above already
        # killed any pending consent keyboard for the old artifact
        # (TriggerConsentKey carries artifact_id). Never fatal: the grant
        # purge invariants must hold even if the ack sweep fails.
        try:
            import trigger_acks
            import trigger_reconcile
            import webhook_auth
            for rec in trigger_acks.ACKS.revoke_artifact(artifact_id):
                webhook_auth.retire_secret(
                    rec.get("effective") or "",
                    secrets_dir=trigger_reconcile.SECRETS_DIR)
        except Exception:  # noqa: BLE001
            logger.warning("trigger-ack artifact revoke failed (%s)",
                           artifact_id, exc_info=True)
    for target in roles or ():
        role = normalize_role(target)
        GRANTS.purge_role(role)
        CHALLENGES.cancel_matching(role=role)


def _resolved_observability(name: str) -> dict:
    """granted_tools + required_env_vars for the freshly-activated plugin (best
    effort — reads the just-reloaded snapshot; resolve_all finds it regardless
    of target)."""
    for rp in plugin_registry.resolve_all().plugins:
        if rp.name == name:
            return {"granted_tools": grants_for_resolved(rp),
                    "required_env_vars": required_env_vars_for_resolved(rp)}
    return {"granted_tools": [], "required_env_vars": []}


@tool(
    "plugin_add",
    "Add a plugin to the registry: publish its pinned artifact, install any "
    "system requirements, assign it to targets, then reload + verify. Version "
    "is derived from the plugin manifest (never supplied).",
    # Sol #15: an explicit JSON Schema — the shorthand {key: type} form marks
    # EVERY key required, so a root-plugin call omitting `subdir` (defaulted by
    # the handler) is rejected by the MCP input validator before the handler
    # runs. `subdir` is the only optional field.
    {"type": "object",
     "properties": {
         "name": {"type": "string"},
         "repo": {"type": "string"},
         "ref": {"type": "string"},
         "subdir": {"type": "string"},
         # C.2 (v0.74.0): the producer's handed-off revision — a tag that
         # moved after the build aborts before activation.
         "expected_revision": {"type": "string"},
         # Sol round-3 M: constrain items so a non-string target (`[1]`) is
         # rejected by the MCP validator, not by an uncaught TARGET_RE.match.
         "targets": {"type": "array", "items": {"type": "string"}}},
     "required": ["name", "repo", "ref", "targets"]},
)
async def plugin_add(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(
            _plugin_add_sync, name=args["name"], repo=args["repo"],
            ref=args["ref"], subdir=args.get("subdir", ""),
            targets=args.get("targets") or [],
            expected_revision=args.get("expected_revision"))
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(core)
        seq = await _reload_and_verify_targets(
            core["name"], core["targets"], expect="present")
        core.update(seq)
        core.update(_resolved_observability(core["name"]))
        return _result(core)


@tool(
    "plugin_update",
    "Update a registered plugin to a new ref: re-publish, install new system "
    "requirements, repoint the registry, reload + verify. Version derives "
    "from the fetched manifest. Pass expected_revision (the producer's "
    "handed-off sha) so a tag that moved after the build aborts before "
    "activation.",
    {"type": "object",
     "properties": {
         "name": {"type": "string"},
         "new_ref": {"type": "string"},
         "expected_revision": {"type": "string"}},
     "required": ["name", "new_ref"]},
)
async def plugin_update(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(
            _plugin_update_sync, name=args["name"], new_ref=args["new_ref"],
            expected_revision=args.get("expected_revision"))
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(core)
        # A:§3.3 (r1-B8): the artifact just changed under this plugin —
        # invalidate the OLD artifact's grants/challenges right after the
        # mutation commits, before the first post-commit await (reload).
        _invalidate_lifecycle(artifact_id=core.get("old_artifact_id"))
        seq = await _reload_and_verify_targets(
            core["name"], core["targets"], expect="present")
        core.update(seq)
        core.update(_resolved_observability(core["name"]))
        return _result(core)


def _find_entry(data, name: str) -> dict | None:
    return next((e for e in data.raw.get("plugins", [])
                 if isinstance(e, dict) and e.get("name") == name), None)


def _plugin_assign_sync(*, name: str, target: str) -> dict:
    if not plugin_registry.TARGET_RE.match(target or ""):
        return {"ok": False, "kind": "invalid_target", "target": target}
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    targets = entry.setdefault("targets", [])
    was_assigned = target in targets
    if not was_assigned:
        targets.append(target)
        plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "target": target,
            "targets": list(targets), "was_assigned": was_assigned}


def _plugin_unassign_sync(*, name: str, target: str) -> dict:
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    targets = entry.get("targets") or []
    was_assigned = target in targets
    if was_assigned:
        entry["targets"] = [t for t in targets if t != target]
        plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "target": target,
            "was_assigned": was_assigned, "targets": entry.get("targets") or []}


def _plugin_remove_sync(*, name: str) -> dict:
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    targets = list(entry.get("targets") or [])
    # A:§3.3 (r1-B8): capture the artifact_id + targets BEFORE the mutation —
    # the caller invalidates grants/challenges by artifact AND by each
    # former target's role only after this commits.
    artifact_id = entry.get("artifact_id")
    data.raw["plugins"] = [
        e for e in data.raw.get("plugins", [])
        if not (isinstance(e, dict) and e.get("name") == name)]
    # §3.1 no-resurrection: seeded_defaults is INTENTIONALLY left untouched, so
    # a removed default stays removed across upgrades. Artifact left for GC.
    plugin_registry.save_registry(data)
    # Sol round-3 M: drop the plugin's system-requirement manifest row too, so a
    # removed plugin leaves no stale reconciliation burden. Sol round-4: this is
    # NON-FATAL — a raise here (unwritable manifest) must not bypass the mandatory
    # reload/verify tail that runs after the sync core returns ok.
    _safe_remove_manifest(name)
    return {"ok": True, "name": name, "targets": targets,
            "artifact_retained": True, "artifact_id": artifact_id}


def _tool_plugin_list() -> dict:
    data = plugin_registry.load_registry()
    seeded = set(data.raw.get("seeded_defaults") or [])
    plugins = []
    for e in data.raw.get("plugins", []):
        if not isinstance(e, dict):
            continue
        name, aid = e.get("name"), e.get("artifact_id")
        store_dir = plugin_registry.STORE_ROOT / str(name) / str(aid)
        plugins.append({
            "name": name, "version": e.get("version"),
            "revision": (e.get("source") or {}).get("revision"),
            "targets": e.get("targets") or [], "artifact_id": aid,
            "artifact_present": store_dir.is_dir(),
            "seeded_default": name in seeded,
        })
    return {
        "registry_valid": data.valid, "plugins": plugins,
        "issues": [{"name": i.name, "reason_code": i.reason_code}
                   for i in data.entry_issues],
    }


@tool(
    "plugin_assign",
    "Assign a registered plugin to a target (resident:/specialist:/executor:).",
    {"name": str, "target": str},
)
async def plugin_assign(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(
            _plugin_assign_sync, name=args["name"], target=args["target"])
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(core)
        seq = await _reload_and_verify_targets(
            core["name"], [core["target"]], expect="present")
        core.update(seq)
        return _result(core)


@tool(
    "plugin_unassign",
    "Remove a plugin's assignment to one target (the plugin stays registered).",
    {"name": str, "target": str},
)
async def plugin_unassign(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(
            _plugin_unassign_sync, name=args["name"], target=args["target"])
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(core)
        # A:§3.3 (r2-B5): invalidate by the (normalized) unassigned role only
        # — the plugin/artifact stays valid for its OTHER targets. A NO-OP
        # unassign (plugin was never assigned to this target) invalidates
        # NOTHING.
        if core.get("was_assigned"):
            _invalidate_lifecycle(roles=[core["target"]])
        seq = await _reload_and_verify_targets(
            core["name"], [core["target"]], expect="absent")
        core.update(seq)
        return _result(core)


@tool(
    "plugin_remove",
    "Remove a plugin from the registry entirely (artifact retained for GC).",
    {"name": str},
)
async def plugin_remove(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(_plugin_remove_sync, name=args["name"])
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(core)
        # A:§3.3 (r1-B8): the plugin is gone entirely — invalidate by its
        # (retained-for-GC) artifact AND by every former target's role.
        _invalidate_lifecycle(artifact_id=core.get("artifact_id"),
                              roles=core.get("targets"))
        seq = await _reload_and_verify_targets(
            core["name"], core["targets"], expect="absent")
        core.update(seq)
        return _result(core)


@tool(
    "trigger_ack_revoke",
    "Revoke the operator's webhook-trigger consent for a plugin, unroute "
    "its /webhook/plg-<plugin>--… endpoints IMMEDIATELY (they 404 before "
    "this returns), and retire its per-trigger secrets. A later re-approval "
    "mints fresh secrets, so the plugin's setup tool must re-provision the "
    "external service; the consent DM re-prompts on the next plugin "
    "mutation or casa_reload_triggers.",
    {"type": "object", "properties": {"name": {"type": "string"}},
     "required": ["name"]},
)
async def trigger_ack_revoke(args: dict) -> dict:
    """Release B operator off-switch. Runs under _PLUGIN_TOOLS_LOCK like the
    lifecycle mutations; the reconcile inside is the SYNCHRONOUS unroute —
    the overlay swap completes before the tool returns, so an inbound
    request can only race the 404, never a half-revoked route."""
    async with _PLUGIN_TOOLS_LOCK:
        import agent as agent_mod
        import trigger_acks
        import trigger_reconcile
        name = args["name"]
        # Terra shipB-r1 P1-1: kill any PENDING consent keyboard for this
        # plugin FIRST (synchronous broker cancel — later taps read
        # "expired"), so a stale Approve can never re-ack past the revoke.
        CHALLENGES.cancel_matching(plugin=name)
        removed = await asyncio.to_thread(
            trigger_acks.ACKS.revoke_plugin, name)
        runtime = getattr(agent_mod, "active_runtime", None)
        registry = getattr(runtime, "trigger_registry", None)
        # Effective names are injective (plg-<plugin>--…), so a prefix
        # match is exact — used for BOTH the overlay sweep and the
        # filesystem secret retirement below.
        prefix = f"plg-{name}--"
        if registry is not None:
            # Fail-closed DIRECT sweep first — the immediate-404 guarantee
            # must not depend on resolver health. Match on the entry's OWN
            # plugin attribution (Sol shipB-r2 P1-3), with the name prefix
            # as belt-and-braces for any entry lacking it.
            registry.replace_plugin_overlay({
                eff: entry
                for eff, entry in registry.plugin_overlay_snapshot().items()
                if entry.get("plugin", "") != name
                and not eff.startswith(prefix)})
        try:
            await trigger_reconcile.reconcile_from_runtime(
                runtime, prompt=False)
        except Exception:  # noqa: BLE001 — sweep above already unrouted
            logger.warning("post-revoke trigger reconcile failed",
                           exc_info=True)
        # Sol shipB-r1 P1-4: retire the plugin's per-trigger secrets from
        # the FILESYSTEM inventory (prefix glob — never from the ack
        # records this tool just deleted). Keeping them would let the NEXT
        # artifact inherit the old credential: a later plugin_update's
        # revoke_artifact would find no records and retire nothing.
        import webhook_auth
        retired = await asyncio.to_thread(
            webhook_auth.retire_secrets_with_prefix, prefix,
            secrets_dir=trigger_reconcile.SECRETS_DIR)
        # Sol shipB-r2 P1-1: cancel AGAIN after our reconcile. Prompts fire
        # under _RECONCILE_LOCK, and our reconcile above serialized behind
        # any in-flight one — so a keyboard posted by that in-flight
        # reconcile is REGISTERED by now and this cancel provably kills it.
        # (Reconciles triggered by LATER mutations legitimately re-prompt —
        # that is the documented re-consent path, not a survival.)
        CHALLENGES.cancel_matching(plugin=name)
        # Refresh health (trigger_pending_ack reappears via the recomputable
        # input) WITHOUT the operator DM — they just did this deliberately.
        await asyncio.to_thread(_regenerate_plugin_health, [])
        return _result({
            "ok": True, "name": name, "revoked": len(removed),
            "unrouted": sorted(r.get("effective") or "" for r in removed),
            "secrets_retired": retired,
        })


@tool(
    "plugin_list",
    "List every registered plugin with its version, revision, targets, "
    "artifact presence, and seeded-default status.",
    {},
)
async def plugin_list(args: dict) -> dict:
    return _result(await asyncio.to_thread(_tool_plugin_list))


# ---------------------------------------------------------------------------
# Plan 4b §7.4–7.6: uninstall + verify_plugin_state + vault helper tools
# ---------------------------------------------------------------------------


def _tool_verify_plugin_state(
    *,
    plugin_name: str,
    _tools_bin: Path | None = None,
    _registry_path=None,
    _store_root=None,
) -> dict:
    """Tier-aware verification (§3.9): does the RUNNING state agree with the
    registry's DESIRED state? Constructed residents/specialists must have the
    desired artifact bound (else reload_required); dormant targets report
    configured readiness only; executors also need their plugin MCP namespaces
    authorized; running engagements on a previous artifact are informational.
    Verification can never report active agreement while a running consumer
    executes different code (FR3).

    FR3 readiness rule (spec 2026-07-13 §D1 — verbatim):

    Readiness describes the artifact a target can execute on its next new
    turn, not whether it is busy at verification time. A current dispatchable
    resident or specialist Agent is ready only when it is unresolved and will
    resolve the current registry snapshot before use, or its coherently
    recorded binding equals the desired artifact. A stale binding remains
    `reload_required` while idle because that Agent can reuse it on its next
    bus or trigger turn. Artifacts retained only by already-started, pinned,
    or draining engagements are informational, provided no new turn can enter
    them.
    """
    import agent as agent_mod
    import cc_tool_pattern
    from plugin_env_conf import read_entries
    from plugin_registry import ResolvedPlugin, STORE_ROOT, load_registry
    from system_requirements.manifest import read_manifest

    reg = (load_registry(_registry_path) if _registry_path is not None
           else load_registry())
    if not reg.valid:
        return {"ready": False, "reasons": ["registry_invalid"],
                "targets": []}
    # Sol #8: verify MUST agree with the resolver. An entry that resolve_for()
    # drops (duplicate_name) or skips (entry_invalid) is unavailable to every
    # agent/executor — verify must not green it off the raw list. Select from
    # the VALIDATED entries and surface the resolver's own rejection reason.
    # Sol round-3 M-shadow: PREFER a validated resolved entry. A rejection issue
    # (entry_invalid / duplicate_name) only blocks when NO validated entry of
    # this name survived — otherwise an unrelated same-name invalid entry would
    # shadow the valid one the resolver actually serves.
    entry = next((e for e in reg.entries
                  if isinstance(e, dict) and e.get("name") == plugin_name), None)
    if entry is None:
        bad = next((i for i in reg.entry_issues if i.name == plugin_name), None)
        if bad is not None:
            return {"ready": False, "reasons": [bad.reason_code], "targets": []}
        return {"ready": False, "reasons": ["not_registered"], "targets": []}

    store_root = _store_root if _store_root is not None else STORE_ROOT
    artifact_id = entry.get("artifact_id")
    src = entry.get("source") or {}
    revision = src.get("revision", "")
    path = Path(store_root) / plugin_name / str(artifact_id)
    reasons: list[str] = []
    provenance_warning = revision.startswith("legacy-content:")
    present = path.is_dir()
    checksum_valid = False
    if not present:
        reasons.append("artifact_missing")
    else:
        verdict = plugin_store.artifact_verdict(
            path, name=plugin_name, repo=src.get("repo", ""),
            revision=revision, subdir=src.get("subdir", ""),
            artifact_id=str(artifact_id))
        if verdict is None:
            checksum_valid = True
        else:
            reasons.append(verdict)          # artifact_invalid | corrupt_artifact

    manifest: dict = {}
    try:
        manifest = json.loads((path / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    rp = ResolvedPlugin(name=plugin_name, artifact_id=str(artifact_id),
                        path=str(path), version=str(entry.get("version", "")),
                        manifest=manifest)
    # Sol #16: a PRESENT-but-malformed .mcp.json silently degrades grants/secrets
    # to [] (indistinguishable from skill-only), so a broken MCP server would
    # otherwise verify ready. Treat it as a blocking reason.
    if checksum_valid and mcp_json_malformed(rp):
        reasons.append("mcp_invalid")
    # P1 (2026-07-18 self-containment plan): static launch-reference
    # resolvability — the gmail-v0.2.0 class (command points at a gitignored
    # venv absent from the artifact). Detects resolvable command/artifact-file
    # references only, NOT "the server can spawn"; unjudgeable shapes are
    # "unchecked" and never block. Appended AFTER artifact/mcp_invalid so the
    # single-reason health rollup (reasons[0]) keeps integrity reasons first.
    command_status = (plugin_store.mcp_command_verdicts(
        path / ".mcp.json", path) if checksum_valid else [])
    if any(c["status"] == "missing" for c in command_status):
        reasons.append("mcp_command_missing")
    # G6 corrected (2026-07-19): a server env self-declaring a CLI-reserved
    # var (CLAUDE_PLUGIN_DATA/CLAUDE_PLUGIN_ROOT) shadows the CLI's native
    # per-plugin value with a literal — blocking, same tier as mcp_invalid.
    reserved_env = (plugin_store.reserved_env_violations(path / ".mcp.json")
                    if checksum_valid else [])
    if reserved_env:
        reasons.append("mcp_reserved_env")
    granted = grants_for_resolved(rp) if checksum_valid else []
    # A:§3.7 (B7): eyeball-checkable declared protected tools. A malformed
    # field already surfaced as a blocking "protected_tools_invalid" reason
    # above (via artifact_verdict); this is purely informational disclosure
    # of the declared list, tolerant of that same malformed case.
    # v0.78.0 W1: manifest_protected_tools now returns a NORMALIZED
    # [{"name", "summary"}] list. `protected_tools` KEEPS its existing
    # machine-readable names-only contract; the new `protected_tool_summaries`
    # field (below) surfaces the declared templates for entries that have one,
    # so the trusted template text is eyeball-checkable too.
    try:
        protected_entries = (plugin_store.manifest_protected_tools(manifest)
                             if checksum_valid else [])
    except plugin_store.StoreError:
        protected_entries = []
    protected_tools = [e["name"] for e in protected_entries]
    protected_tool_summaries = {
        e["name"]: e["summary"] for e in protected_entries if e.get("summary")
    }

    # Tools (system-requirements — verify_bin presence). Sol #11: check BOTH the
    # INSTALLED manifest rows AND every requirement the ARTIFACT declares, so a
    # plugin declaring a sysreq with no installed row can't pass on all([]).
    data = read_manifest()
    tool_entries = [p for p in data["plugins"] if p["name"] == plugin_name]
    tools_bin = _tools_bin if _tools_bin is not None else Path("/config/tools/bin")
    tools_status = []
    for t in tool_entries:
        vb = t.get("verify_bin", "")
        ready_bin = (tools_bin / vb).is_file()   # follows symlinks (M23)
        tools_status.append({
            # Sol CI-review: a hand-corrupted manifest row may lack winning_strategy
            # (read_manifest keeps any dict with a name) — access defensively.
            "requirement": t.get("winning_strategy", "unknown"), "verify_bin": vb,
            "status": "ready" if ready_bin else "missing",
            **({} if ready_bin else {"reason": f"{vb} not in tools/bin"})})
    # Declared-but-not-installed requirements (Sol #11): every requirement the
    # artifact's manifest declares must have a corresponding installed entry.
    installed_bins = {t.get("verify_bin", "") for t in tool_entries}
    for req in (plugin_store.manifest_sysreqs(manifest) if checksum_valid else []):
        vb = req.get("verify_bin", "")
        if vb and vb not in installed_bins:
            tools_status.append({
                "requirement": req.get("type", "declared"), "verify_bin": vb,
                "status": "missing",
                "reason": f"declared requirement {vb!r} has no installed entry"})

    # Secrets from the ARTIFACT's .mcp.json (no version-dir guessing).
    required = set(required_env_vars_for_resolved(rp)) if checksum_valid else set()
    env_conf = read_entries()
    secrets_status = []
    for var in sorted(required):
        conf_val = env_conf.get(var)
        # Sol round-4: a secret counts as resolved only if it is present in the
        # EFFECTIVE environment (boot sourced plugin-env.conf into os.environ,
        # resolving op:// refs). A config entry whose op:// resolution FAILED at
        # boot leaves os.environ unset — configuration presence alone must not
        # mark it resolved, or a broken MCP plugin passes the launch gate.
        effective = os.environ.get(var)
        # Sol r4-10 (rotation stale-green): a PLAIN configured value that
        # differs from the effective environment means the reload hasn't run
        # yet — the old value would be inherited by the next MCP spawn.
        # (op:// refs can't be compared: conf holds the ref, environ the
        # resolved secret.)
        if (effective and conf_val is not None
                and not conf_val.startswith("op://")
                and effective != conf_val):
            secrets_status.append({
                "var": var, "source": "plain", "status": "unresolved",
                "reason": ("configured value not yet applied to the "
                           "effective environment (reload pending)")})
        elif effective and not effective.startswith("op://"):
            source = "op" if (conf_val or "").startswith("op://") else "plain"
            secrets_status.append({"var": var, "source": source,
                                   "status": "resolved"})
        elif conf_val is not None:
            secrets_status.append({
                "var": var,
                "source": "op" if conf_val.startswith("op://") else "plain",
                "status": "unresolved",
                "reason": "configured but not resolved in the effective environment"})
        else:
            secrets_status.append({"var": var, "source": "missing",
                                   "status": "unresolved",
                                   "reason": "not in plugin-env.conf"})

    tools_ready = all(t["status"] == "ready" for t in tools_status)
    secrets_ready = all(s["status"] == "resolved" for s in secrets_status)
    configured_ready = (not reasons) and tools_ready and secrets_ready

    runtime = getattr(agent_mod, "active_runtime", None)
    agents = getattr(runtime, "agents", {}) or {}
    exec_reg = getattr(runtime, "executor_registry", None)

    target_rows = []
    stale_targets = []
    for target in entry.get("targets", []):
        tier, _, role = target.partition(":")
        row = {"target": target, "ready": configured_ready, "reasons": []}
        if not configured_ready:
            row["reasons"] = list(reasons) or ["not_ready"]
        if tier == "specialist" and (
                spec_reg := getattr(runtime, "specialist_registry", None)
        ) is not None and getattr(spec_reg, "is_disabled",
                                  lambda r: False)(role) is True:
            # (`is True`: a MagicMock registry in unit stand-ins returns a
            # truthy Mock — never let that read as "disabled".)
            # v0.74.1 (live finding 2026-07-13): the specialist-tier analogue
            # of the v0.71.1 disabled-executor rule. A DISABLED specialist is
            # dormant-by-config: reload tears it down (never registered), so
            # no new turn can enter it (FR3: informational, not blocking) —
            # its BINDING is never graded. Grading the tier-missed empty
            # binding produced the §1.4 registry-wide "Plugin degraded"
            # amplification. Sol v0.74.1-B2: configured readiness (secrets/
            # sysreqs/artifact/mcp) is PRESERVED from the row init — being
            # disabled must not mask a broken configuration. Re-checked for
            # real the moment the operator enables the specialist.
            row["state"] = "disabled"
        elif tier in ("resident", "specialist"):
            # D2 (v0.74.0): read the ONE frozen snapshot — never the legacy
            # attribute pair — and guard against racing a reload swap: the
            # object we grade must still BE runtime.agents[role] after the
            # snapshot read (verify runs on a worker thread). Bounded
            # re-read; if the mapping won't stabilize, fail EXPLICITLY
            # rather than grade a replaced object.
            agent = None
            snap = None
            stable = False
            for _ in range(5):
                agent = agents.get(role)
                snap = (getattr(agent, "plugin_binding_snapshot", None)
                        if agent is not None else None)
                if agent is agents.get(role):
                    stable = True
                    break
            if not stable:
                row["ready"] = False
                row["reasons"] = ["verify_unstable"]
                row["state"] = "unstable"
            elif agent is not None and snap is not None:
                row["state"] = "active"
                active_aid = snap.binding.get(plugin_name)
                row["active_artifact_id"] = active_aid
                # D2 disclosure: which resolver generation this binding was
                # computed against. Grading stays FR3 — binding equality is
                # authoritative (an unrelated mutation bumps the generation
                # for agents it never reconstructs; the MUTATION path is
                # where a generation mismatch hard-fails, spec D2 —
                # provisional pending the B1 spec decision, Sol r3/r4).
                row["resolution_generation"] = snap.generation
                row["generation_stale"] = (
                    snap.generation != plugin_registry.snapshot_generation())
                if active_aid != artifact_id:
                    # FR3: a stale binding is BLOCKING even while idle — the
                    # persistent Agent reuses it on its next bus/trigger turn.
                    row["ready"] = False
                    row["reasons"] = ["reload_required"]
                    stale_targets.append(target)
            else:
                row["state"] = "dormant"
        elif tier == "executor":
            # A DISABLED executor (enabled: false) is absent from the registry's
            # get() by design, so its tools.allowed would read empty and every
            # derived grant would look unauthorized — a false authorization_missing
            # + operator DM on every boot. A plugin assigned to a disabled executor
            # is dormant-by-config, NOT a health failure: report state="disabled"
            # and never flag it not-ready. Its authorization is still recorded
            # (validated against the disabled defn) and is re-checked for real the
            # moment the operator enables the executor.
            disabled = (exec_reg is not None and exec_reg.is_disabled(role))
            row["state"] = "disabled" if disabled else "dormant"
            defn = (exec_reg.definition_any(role) if disabled
                    else (exec_reg.get(role) if exec_reg is not None else None))
            allowed = list(getattr(defn, "tools_allowed", []) or []) if defn else []
            missing = [g for g in granted
                       if not cc_tool_pattern.matches_any(allowed, g, {})]
            row["authorization"] = {"required": granted, "missing": missing}
            if missing and not disabled:
                row["ready"] = False
                row["reasons"] = ["authorization_missing"]
        target_rows.append(row)

    # Running executor engagements on a PREVIOUS artifact — informational.
    sessions = []
    if _engagement_registry is not None:
        try:
            running = _engagement_registry.active_and_idle()
        except Exception:  # noqa: BLE001
            running = []
        for rec in running:
            for pa in getattr(rec, "plugin_artifacts", ()) or ():
                if (pa.get("name") == plugin_name
                        and pa.get("artifact_id") != artifact_id):
                    sessions.append({"engagement_id": rec.id,
                                     "artifact_id": pa.get("artifact_id")})

    # Draining resident/specialist turns still on the PREVIOUS artifact —
    # informational (Sol #4): after a reload swaps an agent, its in-flight turn
    # keeps executing the old artifact until aclose drains it (≤ pool drain
    # timeout). verify discloses it rather than silently implying it's gone.
    for d in getattr(runtime, "draining", None) or []:
        aid = (d.get("binding") or {}).get(plugin_name)
        if aid is not None and aid != artifact_id:
            sessions.append({"draining_role": d.get("role"),
                             "artifact_id": aid})

    top_ready = (configured_ready
                 and all(r["ready"] for r in target_rows))
    return {
        "ready": top_ready,
        "reasons": reasons,
        "desired": {"artifact_id": artifact_id,
                    "version": entry.get("version"),
                    "revision": revision, "targets": entry.get("targets", [])},
        "artifact": {"present": present, "checksum_valid": checksum_valid,
                     "provenance_warning": provenance_warning},
        "tools": tools_status,
        "secrets": secrets_status,
        "mcp_commands": command_status,
        "reserved_env": reserved_env,
        "granted_tools": granted,
        "protected_tools": sorted(protected_tools),
        "protected_tool_summaries": protected_tool_summaries,
        "targets": target_rows,
        "stale_targets": stale_targets,
        "sessions_on_previous_artifact": sessions,
    }


def _tool_verify_plugin_secrets(*, plugin_name: str) -> dict:
    """Back-compat shim (one release only)."""
    state = _tool_verify_plugin_state(plugin_name=plugin_name)
    return {"secrets": state["secrets"]}


def _tool_set_plugin_env_reference(
    *,
    plugin: str,
    var_name: str,
    op_ref_or_value: str,
) -> dict:
    from plugin_env_conf import set_entry as _set_env_entry_local
    _set_env_entry_local(var_name, op_ref_or_value)
    return {"ok": True}


def _tool_list_vault_items(*, query: str = "", vault: str = "") -> dict:
    cmd = ["op", "item", "list", "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"error": r.stderr.strip()}
    items = json.loads(r.stdout)
    if query:
        items = [i for i in items if query.lower() in (i.get("title", "")).lower()]
    return {"items": [{"name": i.get("title"), "id": i.get("id"),
                       "category": i.get("category"),
                       "updated_at": i.get("updated_at")} for i in items]}


def _tool_get_item_fields(*, item: str, vault: str = "") -> dict:
    cmd = ["op", "item", "get", item, "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"error": r.stderr.strip()}
    data = json.loads(r.stdout)
    return {"fields": [{"label": f.get("label"),
                        "section": (f.get("section") or {}).get("label", ""),
                        "type": f.get("type")}
                       for f in data.get("fields", [])]}


@tool(
    "verify_plugin_state",
    "Check tool readiness, secret resolution, and MCP cache status for a plugin.",
    {"plugin_name": str},
)
async def verify_plugin_state(args: dict) -> dict:
    return _result(_tool_verify_plugin_state(plugin_name=args["plugin_name"]))


@tool(
    "verify_plugin_secrets",
    "Back-compat shim: check secret resolution for a plugin (use verify_plugin_state instead).",
    {"plugin_name": str},
)
async def verify_plugin_secrets(args: dict) -> dict:
    return _result(_tool_verify_plugin_secrets(plugin_name=args["plugin_name"]))


@tool(
    "set_plugin_env_reference",
    "Upsert a VAR=value or VAR=op://... line in plugin-env.conf.",
    {"plugin": str, "var_name": str, "op_ref_or_value": str},
)
async def set_plugin_env_reference(args: dict) -> dict:
    return _result(_tool_set_plugin_env_reference(
        plugin=args["plugin"],
        var_name=args["var_name"],
        op_ref_or_value=args["op_ref_or_value"],
    ))


@tool(
    "list_vault_items",
    "List 1Password vault items, optionally filtered by query string and/or vault name.",
    {"query": str, "vault": str},
)
async def list_vault_items(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_list_vault_items,
        query=args.get("query", ""),
        vault=args.get("vault", ""),
    ))


@tool(
    "get_item_fields",
    "Get field labels and types for a 1Password item (does not return secret values).",
    {"item": str, "vault": str},
)
async def get_item_fields(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_get_item_fields,
        item=args["item"],
        vault=args.get("vault", ""),
    ))


# --- Personality Phase A, Task 8: resident persona swap / reset -------------
#
# Both tools STAGE a desired instance tuple (InstanceDir.stage_desired) rather
# than committing directly — the actual `active := desired` commit happens at
# the NEXT BOOT's reconcile_resident_binding (agent_loader), which re-validates
# against the then-current role checksum before committing. This keeps exactly
# one commit code path (boot-time reconciliation), so a personality-identity
# change is always restart-to-swap. `active_runtime` is the established
# module-level accessor (mirrors active_semantic_memory), set by casa_core as
# `agent.active_runtime = runtime`.

_PERSONA_ROOTS = (Path("/config/personas"), Path("/opt/casa/defaults/personas"))


def _resolve_local_persona(ref: str):
    """Load an ALREADY LOCALLY PRESENT persona pack by exact ref (this plan does
    not fetch a bare persona from a remote repo). Persona bytes are installed
    under /config/personas/<ns>/<slug>/<version>/ (or the image defaults) by the
    same out-of-band means as any other locally-staged content; this only loads
    and validates what is already there."""
    from persona_pack import load_persona_pack

    persona_id, _, version = ref.partition("@")
    for root in _PERSONA_ROOTS:
        pack_dir = root / persona_id / version / "pack"
        manifest_path = root / persona_id / version / "manifest.json"
        if pack_dir.is_dir() and manifest_path.is_file():
            return load_persona_pack(pack_dir, manifest_path)
    raise ValueError(f"persona {ref!r} is not present under any configured persona root")


def _stage_and_report(role_id: str, slot: str, binding) -> dict:
    from personality_binding import InstanceDir, InstanceTuple

    # Resolve the bindings root through the SAME seam agent_loader's boot-time
    # reconcile uses (agent_loader._resident_bindings_root — honors an explicit
    # arg, then CASA_BINDINGS_DIR, then /config/bindings). This tool and the
    # next boot's reconcile_resident_binding() MUST agree on the on-disk
    # directory for a given slot, or a staged swap/reset is silently never
    # picked up (see tests/test_reconcile_resident_binding.py's round-trip
    # regression test).
    import agent_loader

    instance_dir = InstanceDir(agent_loader._resident_bindings_root(None) / f"resident-{slot}")
    active = instance_dir.active()
    root = binding.override_source or binding.image_default_root or ""
    instance_dir.stage_desired(InstanceTuple(
        root=root, binding=binding, config_snapshot={}, config_digest=binding.effective_config_digest,
    ))
    return {
        "ok": True, "role": role_id, "persona": f"{binding.persona_id}@{binding.persona_version}",
        "activation": "restart_required",
        "prior_persona": (f"{active.binding.persona_id}@{active.binding.persona_version}" if active else None),
    }


def _resolve_resident_role(role_id: str):
    """Resolve a fixed resident slot's live RoleSlot from the Task 8
    ``active_runtime.role_slots`` registry, or a structured-error result."""
    from role_slot import FIXED_RESIDENT_SLOTS

    slot = role_id.split(":", 1)[1] if ":" in role_id else ""
    if slot not in FIXED_RESIDENT_SLOTS:
        return None, slot, _result({"ok": False, "kind": "invalid_role"})
    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return None, slot, _result({"ok": False, "kind": "runtime_unavailable"})
    role = runtime.role_slots.get(role_id)
    if role is None:
        return None, slot, _result({"ok": False, "kind": "role_not_loaded"})
    return role, slot, None


@tool(
    "resident_persona_swap",
    "Stage an OVERRIDE persona binding for a fixed resident slot (assistant/"
    "butler/concierge) from an already locally-present persona pack. Validates "
    "role/persona compatibility BEFORE staging anything. Takes effect only after "
    "casa_restart_supervised (personality-identity changes are restart-to-swap, "
    "never hot-reloaded).",
    {"type": "object", "properties": {
        "role": {"enum": ["resident:assistant", "resident:butler", "resident:concierge"]},
        "persona_ref": {"type": "string"},
    }, "required": ["role", "persona_ref"]},
)
async def resident_persona_swap(args: dict) -> dict:
    from personality_binding import check_persona_requirements, materialize_override_binding

    role_id = args["role"]
    role, slot, err = _resolve_resident_role(role_id)
    if err is not None:
        return err
    try:
        persona = _resolve_local_persona(args["persona_ref"])
        check_persona_requirements(role.normalized, persona)  # rejects BEFORE any staging
    except ValueError as exc:
        return _result({"ok": False, "kind": "incompatible_or_missing_persona", "detail": str(exc)})
    binding = materialize_override_binding(
        role=role, persona=persona, override_source=f"operator:{args['persona_ref']}",
    )
    return _result(_stage_and_report(role_id, slot, binding))


@tool(
    "resident_persona_reset",
    "Stage a reset for a fixed resident slot's binding back to the CURRENT "
    "in-image default persona, undoing any override (spec §2.3/§4.4's "
    "always-available reset). Takes effect only after casa_restart_supervised.",
    {"type": "object", "properties": {
        "role": {"enum": ["resident:assistant", "resident:butler", "resident:concierge"]},
    }, "required": ["role"]},
)
async def resident_persona_reset(args: dict) -> dict:
    from personality_binding import (
        IMAGE_DEFAULT_PERSONA_BY_SLOT,
        check_persona_requirements,
        materialize_image_default_binding,
    )

    role_id = args["role"]
    role, slot, err = _resolve_resident_role(role_id)
    if err is not None:
        return err
    default_ref = IMAGE_DEFAULT_PERSONA_BY_SLOT[slot]
    # Run the SAME resolve-then-validate-before-staging sequence as
    # resident_persona_swap — a missing/uninstalled default persona blob or a
    # compatibility failure is returned as a structured error, never an
    # unhandled ValueError out of the handler, and nothing is staged before
    # validation passes.
    try:
        persona = _resolve_local_persona(default_ref)
        check_persona_requirements(role.normalized, persona)  # rejects BEFORE any staging
    except ValueError as exc:
        return _result({"ok": False, "kind": "incompatible_or_missing_persona", "detail": str(exc)})
    binding = materialize_image_default_binding(role=role, persona=persona, image_default_root=default_ref)
    return _result(_stage_and_report(role_id, slot, binding))


# Module-level tool registry — iterated by create_casa_tools() for the SDK
# path and by the MCP HTTP bridge (mcp_bridge._build_tool_dispatch) for
# real `claude` CLI engagements. Adding a tool here exposes it on both
# transports automatically.
CASA_TOOLS: tuple = (
    send_message,
    send_media,
    react,
    ask_user,
    delegate_to_agent,
    voice_job_status,
    cancel_voice_job,
    continue_voice_job,
    recall_memory,                 # §4.3 — shared-bank semantic recall (tier-clearance filtered)
    get_schedule,
    engage_executor,
    emit_completion,
    cancel_engagement,
    query_engager,
    config_git_commit,
    casa_reload,
    casa_restart_supervised,            # NEW — Task D.2
    casa_reload_triggers,
    list_engagement_workspaces,
    delete_engagement_workspace,
    peek_engagement_workspace,
    cleanup_engagement_topics,     # v0.65.0 [AR-7] — configurator-only grant
    # Unified plugin architecture (§3.13) — registry tools.
    plugin_add,
    plugin_update,
    plugin_assign,
    plugin_unassign,
    plugin_remove,
    trigger_ack_revoke,            # Release B — plugin-trigger consent off-switch
    plugin_list,
    verify_plugin_state,
    verify_plugin_secrets,
    set_plugin_env_reference,
    list_vault_items,
    get_item_fields,
    # Personality Phase A, Task 8 — configurator-only resident persona control.
    resident_persona_swap,
    resident_persona_reset,
)


def select_casa_tools(
    allowed_tools: frozenset[str] | None = None,
) -> tuple[SdkMcpTool, ...]:
    """Return the Casa tools granted to one resolved MCP server."""
    selected = list(CASA_TOOLS)
    if allowed_tools is not None and "mcp__casa-framework" not in allowed_tools:
        selected = [
            candidate for candidate in CASA_TOOLS
            if f"mcp__casa-framework__{candidate.name}" in allowed_tools
        ]
    return tuple(selected)


def create_casa_tools(
    allowed_tools: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Create and return the casa-framework MCP server config."""
    selected = select_casa_tools(allowed_tools)
    server = create_sdk_mcp_server(
        name="casa-framework",
        tools=list(selected),
    )
    # S-1 (block-S live finding 2026-07-15): opt the framework server out of
    # Claude Code's ToolSearch deferral. Since CLI ~2.1.69 ALL MCP tool
    # schemas are deferred by default (allow-listing does not exempt them);
    # cold voice sessions burned 1-4 ToolSearch model round-trips (4.7→27.5s,
    # escalating) resolving delegate_to_agent before they could call it —
    # eating the 27s voice budget. `alwaysLoad` (CLI ≥2.1.121; our pin
    # 2.1.150) eager-loads every tool of THIS server at session start; the
    # SDK transport forwards all non-`instance` keys of an sdk-type server
    # config to --mcp-config verbatim. Plugin/HA/n8n servers deliberately
    # keep deferral — this is the small always-needed core surface only.
    server["alwaysLoad"] = True
    return server
