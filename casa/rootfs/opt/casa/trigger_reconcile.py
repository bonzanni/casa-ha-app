"""Release B — the plugin-trigger reconciler (runtime seam).

The ONE writer of :class:`trigger_registry.TriggerRegistry`'s plugin overlay.
Wired into: casa_core boot (after resident triggers register), every plugin
lifecycle mutation (``tools._reload_and_verify_targets``, reconcile-LAST
after verify), every trigger-affecting reload scope (``reload.dispatch``),
the consent approve path, and the ``trigger_ack_revoke`` tool. All entry
points serialize on ``_RECONCILE_LOCK``.

Semantics (spec §2 Release B, r2):

* **Complete desired overlay, atomic swap.** Every reconcile derives the FULL
  set of routable plugin triggers from the CURRENT resolver snapshot and
  swaps it in one operation — a removed / unresolved / revoked / corrupt
  plugin's ingress is swept by absence (handler 404s), and readers never see
  a partial overlay.
* **Assignment authority is target-scoped.** A plugin trigger routes to
  ``resident:<role>`` ONLY when target-scoped resolution
  (``plugin_registry.resolve_for``) includes that plugin for that target —
  unassigned / specialist-only plugins route nothing.
* **Fail-closed, per-plugin all-or-nothing.** A plugin routes only when EVERY
  declared trigger is intrinsically valid, targets an existing resident that
  declares the ``webhook`` channel, is assigned, has its secret backing
  (global ``WEBHOOK_SECRET`` for ``hmac_body``), and carries an operator
  consent ack for its full identity. Any gap ⇒ the plugin's whole set is
  unrouted plus a ``stage="triggers"`` ``PluginIssue``.
* **Eager secrets.** Casa-owned per-trigger secrets (``static_header`` /
  ``timestamped_hmac``) are minted at reconcile time — BEFORE any traffic —
  so the plugin's setup tool can read
  ``/data/webhook_secrets/plg-<plugin>--<name>`` right after consent.
* **Recomputable health.** :func:`current_issues` recomputes the contextual
  trigger issues fresh from the live runtime — folded into EVERY
  ``_regenerate_plugin_health`` pass so an unrelated health refresh can never
  erase ``trigger_pending_ack``/``trigger_channel_missing``. Prompting is a
  separate side effect of :func:`reconcile_plugin_triggers` only.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import plugin_triggers
from plugin_triggers import ack_identity

logger = logging.getLogger(__name__)

SECRETS_DIR = Path("/data/webhook_secrets")
_GLOBAL_SECRET_PATH = Path("/data/webhook_secret")

# Serializes every overlay writer (boot, mutations, reloads, consent approve,
# revoke) so each swap derives from a self-consistent compute.
_RECONCILE_LOCK = asyncio.Lock()

# Per-trigger auth modes backed by a casa-minted per-trigger secret file.
_PER_TRIGGER_SECRET_MODES = ("static_header", "timestamped_hmac")


# -- injectable defaults (module functions so tests can monkeypatch) ---------


def _default_resolver() -> Callable[[str | None], Any]:
    import plugin_registry

    def resolve(target: "str | None") -> Any:
        if target is None:
            return plugin_registry.resolve_all()
        return plugin_registry.resolve_for(target)

    return resolve


def _default_acks() -> Any:
    from trigger_acks import ACKS

    return ACKS


def _default_global_secret_ok() -> Callable[[], bool]:
    def ok() -> bool:
        if os.environ.get("WEBHOOK_SECRET", ""):
            return True
        try:
            return bool(_GLOBAL_SECRET_PATH.read_text(encoding="utf-8").strip())
        except OSError:
            return False

    return ok


@dataclass
class DesiredTriggers:
    """The pure compute result: what SHOULD route right now."""

    overlay: dict[str, dict] = field(default_factory=dict)
    issues: list = field(default_factory=list)
    # Consent prompts to fire — only for triggers whose ONLY gap is the ack
    # (approving a trigger that still could not route is a broken promise).
    pending: list[dict] = field(default_factory=list)


def compute_desired(
    *, role_configs: dict, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    global_secret_ok: "Callable[[], bool] | None" = None,
) -> DesiredTriggers:
    """Side-effect-free derivation of the complete desired plugin overlay +
    the contextual trigger issues. Never raises for bad plugin data."""
    from plugin_registry import PluginIssue

    acks = acks if acks is not None else _default_acks()
    resolver = resolver if resolver is not None else _default_resolver()
    global_secret_ok = (global_secret_ok if global_secret_ok is not None
                        else _default_global_secret_ok())

    out = DesiredTriggers()
    all_res = resolver(None)
    if not getattr(all_res, "registry_valid", False):
        # Fail-closed: an invalid registry routes NO plugin ingress (its own
        # registry-stage issues surface via the resolver / health pass).
        return out

    # Assignment authority (target-scoped): plugin p may route to
    # resident:<role> only when resolve_for("resident:<role>") includes it.
    assigned: dict[str, set[str]] = {}
    for role in role_configs:
        res = resolver(f"resident:{role}")
        assigned[role] = ({rp.name for rp in res.plugins}
                          if getattr(res, "registry_valid", False) else set())

    for rp in all_res.plugins:
        triggers, errs = plugin_triggers.parse_and_validate(rp.name, rp.manifest)
        if errs:
            # Intrinsically invalid declaration (pre-published artifacts can
            # carry one — the publish gate is younger than the store).
            out.issues.append(PluginIssue(
                name=rp.name, target=None, stage="triggers",
                reason_code="trigger_invalid", artifact_id=rp.artifact_id))
            continue
        if not triggers:
            continue

        entries: dict[str, dict] = {}
        plugin_pending: list[dict] = []
        nonconsent_gap = False
        for t in triggers:
            target = t["target"]
            role = target.partition(":")[2]
            cfg = role_configs.get(role)
            if cfg is None or "webhook" not in (getattr(cfg, "channels", None) or []):
                out.issues.append(PluginIssue(
                    name=rp.name, target=target, stage="triggers",
                    reason_code="trigger_channel_missing",
                    artifact_id=rp.artifact_id))
                nonconsent_gap = True
                continue
            if rp.name not in assigned.get(role, set()):
                out.issues.append(PluginIssue(
                    name=rp.name, target=target, stage="triggers",
                    reason_code="trigger_unassigned_target",
                    artifact_id=rp.artifact_id))
                nonconsent_gap = True
                continue
            if t["auth"]["mode"] == "hmac_body" and not global_secret_ok():
                out.issues.append(PluginIssue(
                    name=rp.name, target=target, stage="triggers",
                    reason_code="trigger_secret_missing",
                    artifact_id=rp.artifact_id))
                nonconsent_gap = True
                continue
            ident = ack_identity(
                plugin=rp.name, artifact_id=rp.artifact_id,
                effective=t["effective"], target=target, auth=t["auth"])
            ack_rec = acks.get(ident)
            if ack_rec is None:
                out.issues.append(PluginIssue(
                    name=rp.name, target=target, stage="triggers",
                    reason_code="trigger_pending_ack",
                    artifact_id=rp.artifact_id))
                plugin_pending.append({
                    "plugin": rp.name, "artifact_id": rp.artifact_id,
                    "effective": t["effective"], "target": target,
                    "auth": t["auth"], "clearance": t["clearance"]})
                continue
            entries[t["effective"]] = {
                "plugin": rp.name, "role": role,
                "clearance": t["clearance"], "auth": t["auth"],
                # the (consent identity, approval generation) this route was
                # approved under — the mint binds the secret to the PAIR, so
                # a re-approval after a revoke (new gen) rekeys even for an
                # identical tuple (Sol shipB-r3)
                "identity": f"{ident}#{ack_rec.get('gen', '')}"}

        # Per-plugin all-or-nothing: any gap unroutes the whole set.
        if not plugin_pending and not nonconsent_gap:
            out.overlay.update(entries)
        elif not nonconsent_gap:
            out.pending.extend(plugin_pending)
    return out


def _mint_secrets(desired: DesiredTriggers, secrets_dir: Path) -> None:
    """Eagerly mint casa-owned per-trigger secrets for the routed set; a mint
    failure fail-closes the OWNING PLUGIN's whole set (all-or-nothing)."""
    import webhook_auth
    from plugin_registry import PluginIssue

    failed_plugins: set[str] = set()
    for eff, entry in desired.overlay.items():
        if entry["auth"].get("mode") not in _PER_TRIGGER_SECRET_MODES:
            continue
        try:
            # Identity-bound (Terra shipB-r2): a surviving secret minted
            # under a DIFFERENT consent identity is rekeyed here — the old
            # credential can never carry into a new approval even when an
            # earlier retirement silently failed.
            got = webhook_auth.ensure_secret_for_identity(
                eff, identity=entry.get("identity", ""),
                secrets_dir=secrets_dir)
        except Exception:  # noqa: BLE001 — Terra shipB-r1 P1-2: one plugin's
            # mint blow-up (fs error) must fail-close THAT plugin, never
            # abort the whole reconcile (which would retain every stale
            # route, including a just-unassigned plugin's).
            logger.exception("per-trigger secret mint failed (%s)", eff)
            got = None
        if not got:
            failed_plugins.add(entry.get("plugin", ""))
            desired.issues.append(PluginIssue(
                name=entry.get("plugin", ""), target=f"resident:{entry['role']}",
                stage="triggers", reason_code="trigger_secret_missing"))
            continue
        try:
            import log_redact
            log_redact.register_secret(got.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — redaction is best-effort
            pass
    if failed_plugins:
        desired.overlay = {
            eff: entry for eff, entry in desired.overlay.items()
            if entry.get("plugin") not in failed_plugins}


async def _regen_health_safe() -> None:
    """Regenerate the plugin-health report (no operator notify) so a
    just-acked trigger's stale ``trigger_pending_ack`` clears immediately
    (v0.98.2 P2 follow-up) instead of lingering until the next plugin
    mutation/boot. ``current_issues()`` recomputes fresh from the persisted
    acks + resolver, so the routed trigger drops out of the report. Never
    raises — a health-refresh failure must not break the reconcile."""
    try:
        import tools
        await asyncio.to_thread(tools._regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("post-consent plugin-health regen failed", exc_info=True)


async def reconcile_plugin_triggers(
    *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any = None, acks: Any = None,
    secrets_dir: "Path | None" = None, prompt: bool = True,
    resolver: "Callable[[str | None], Any] | None" = None,
    global_secret_ok: "Callable[[], bool] | None" = None,
    regen_health: bool = False,
) -> list:
    """Compute + apply: swap the complete desired overlay into the registry,
    mint eager secrets, fire consent prompts. Returns the trigger issues.

    ``regen_health`` (set by the consent-approve reconcile) additionally
    rewrites plugin-health after the swap so a freshly-acked trigger's stale
    ``trigger_pending_ack`` clears at once. The mutation/boot/reload paths
    leave it False — they regenerate health themselves — so there is no
    double-regen."""
    acks = acks if acks is not None else _default_acks()
    # Resolved at CALL time from the module attribute (not a def-time default)
    # so there is one source of truth for the secrets location.
    secrets_dir = SECRETS_DIR if secrets_dir is None else secrets_dir

    def _compute_and_mint() -> "tuple[DesiredTriggers, list[dict], bool, list[dict] | None]":
        desired = compute_desired(
            role_configs=role_configs, acks=acks, resolver=resolver,
            global_secret_ok=global_secret_ok)
        _mint_secrets(desired, Path(secrets_dir))
        # The CALLBACK half of the union membership and the setup-candidate
        # sweep are derived here, in the SAME worker thread — both read
        # plugin.json for every resolved plugin, which must never run on the
        # event loop under the reconcile lock. Both are computed strictly
        # before any keyboard posts (sealing and prompting happen below, after
        # this returns).
        #
        # #451: the callback half is computed whenever ``prompt`` is set, NOT
        # only when this pass has pending triggers. Sealing a ZERO-member
        # verdict — the positive statement that an artifact needs no consent —
        # requires knowing that the callback half is empty too, and a plugin
        # can have a pending callback consent while no trigger consent pends.
        # NOT gated on ``prompt``: the obligation sweep and the verdict sealing
        # must run on every reconcile, including the prompt=False BOOT pass.
        # Boot is the only pass that follows a crash between a durable registry
        # publish and its lifecycle reconcile — precisely when the level-
        # triggered sweep is the thing that recovers the missing obligation.
        # Only the KEYBOARDS depend on `prompt`.
        union_ok, union = _callback_pending_for_union(
            role_configs=role_configs, resolver=resolver)
        cand_ok, cand = setup_candidates(resolver=resolver)
        candidates = cand if cand_ok else None
        return desired, union, union_ok, candidates

    async with _RECONCILE_LOCK:
        try:
            (desired, callback_pending, callback_ok,
             setup_cands) = await asyncio.to_thread(_compute_and_mint)
        except Exception:
            # Terra shipB-r1 P1-2: a compute failure must not RETAIN the old
            # overlay (a just-unassigned/revoked plugin's routes would stay
            # live behind a swallowed warning). Fail closed to NO plugin
            # ingress — resident triggers are untouched — then propagate so
            # the caller logs/surfaces it; the next successful reconcile
            # restores the valid set.
            trigger_registry.replace_plugin_overlay({})
            raise
        trigger_registry.replace_plugin_overlay(desired.overlay)
        # v0.112.0 (impl r5, Terra): the overlay is now live — wake the
        # setup-episode worker so any pending episode gated on a
        # previously-down route dispatches. This fires on EVERY reconcile
        # (consent-driven or a plain casa_reload_triggers heal), not just the
        # consent finish hook — otherwise an episode whose approval-time
        # reconcile failed would wait indefinitely for a later heal to notice
        # it. Cheap (an Event.set); the worker re-checks routes_live itself.
        try:
            import plugin_setup_episodes
            plugin_setup_episodes.kick()
        except Exception:  # noqa: BLE001 — never break a reconcile on this
            pass
        # Prompts fire INSIDE the lock (Sol shipB-r2 P1-1): keyboard
        # registration is then ordered BEFORE any later reconcile can
        # acquire the lock — so trigger_ack_revoke's final
        # cancel_matching(plugin=…), which runs after ITS reconcile,
        # provably catches every keyboard an in-flight reconcile posted.
        # register_challenge is synchronous (the Telegram post happens on
        # an owned background driver), so this adds no IO under the lock.
        # #451: SEAL BEFORE the operator-reachability gate, and on EVERY pass
        # including prompt=False. Sealing used to live inside
        # _fire_consent_prompts, AFTER its `channel is None` / `op is None`
        # early returns — so with no DM reachable nothing was sealed at all,
        # and a round could first seal on a later ordinary reload, long after a
        # mutation had already reported which runner owned setup. Sealing here
        # means an unreachable operator yields a members-bearing verdict and
        # the obligation correctly HOLDS.
        nonce_by_identity = seal_setup_state(
            trigger_pending=desired.pending,
            callback_pending=callback_pending,
            pending_complete=callback_ok,
            candidates=setup_cands)
        try:
            import plugin_setup_episodes
            plugin_setup_episodes.kick()   # a zero-member verdict releases
        except Exception:  # noqa: BLE001
            pass
        if prompt and desired.pending:
            _fire_consent_prompts(
                desired.pending, trigger_registry=trigger_registry,
                role_configs=role_configs,
                channel_manager=channel_manager,
                acks=acks, secrets_dir=secrets_dir, resolver=resolver,
                global_secret_ok=global_secret_ok,
                nonce_by_identity=nonce_by_identity)
    if regen_health:
        # After the lock: the overlay + persisted ack are already live, so the
        # fresh health pass sees the routed (no-longer-pending) state.
        await _regen_health_safe()
    return desired.issues


def _fire_consent_prompts(
    pending: list[dict], *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any, acks: Any, secrets_dir: Path,
    resolver: Any, global_secret_ok: Any, nonce_by_identity: dict[str, str],
) -> None:
    import authz_grants
    import trigger_consent

    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None:
        return  # no DM reachable — pending_ack stands; re-prompted later
    op = trigger_consent.operator_identity(channel)
    if op is None:
        return
    chat_id, operator_id = op

    async def _reconcile_again() -> None:
        # Captures THIS reconcile's inputs. If a reload_full rebinds the
        # runtime registries before the tap lands, the swap goes to the old
        # registry object — harmless: the ack is persisted, so the next
        # lifecycle reconcile routes it on the live one.
        await reconcile_plugin_triggers(
            trigger_registry=trigger_registry, role_configs=role_configs,
            channel_manager=channel_manager, acks=acks,
            secrets_dir=secrets_dir, prompt=False, resolver=resolver,
            global_secret_ok=global_secret_ok, regen_health=True)

    # The setup-round membership was SEALED by the caller, before this
    # function's reachability gate (#451) and therefore before any keyboard
    # posts — a fast Approve on the first keyboard can never settle a round
    # still registering members.
    _ack_identity = ack_identity  # module-level import (plugin_triggers)

    for p in pending:
        try:
            ident = _ack_identity(
                plugin=p["plugin"], artifact_id=p["artifact_id"],
                effective=p["effective"], target=p["target"], auth=p["auth"])
            trigger_consent.prompt_trigger_consent(
                coordinator=authz_grants.CHALLENGES, channel=channel,
                chat_id=chat_id, operator_id=operator_id, acks=acks,
                reconcile_cb=_reconcile_again,
                setup_nonce=nonce_by_identity.get(ident, ""), **p)
        except Exception:  # noqa: BLE001 — a prompt failure never breaks
            # the mutation; pending_ack stays in health and re-prompts later.
            logger.exception("trigger consent prompt failed (plugin=%s)",
                             p.get("plugin"))


def trigger_pending_for_union(
    *, role_configs: dict, resolver: Any = None,
) -> tuple[bool, list[dict]]:
    """The pending TRIGGER consents, for the callback reconciler's union
    sealing (the mirror of ``callback_reconcile.callback_pending_for_union``).
    Side-effect free and never raises.

    Returns ``(ok, pending)``. #451: the success flag is LOAD-BEARING — this
    used to degrade a failure to ``[]``, which is indistinguishable from
    "genuinely nothing pending". Under-reporting pending consents would let a
    verdict be sealed over a subset (a fast Approve then settles a round the
    other consent never joined), and would let a ZERO-member verdict — a
    positive statement that an artifact needs no consent — be sealed when the
    truth is unknown. The caller seals nothing unless both halves report ok.

    NOTE the ack store: this reads the module DEFAULT (``compute_desired``
    resolves ``_default_acks`` when none is passed), because the caller is the
    OTHER reconciler and does not hold this kind's store. Correct in
    production, where the default IS the live store — but a caller injecting a
    non-default store for one kind must inject BOTH, or this half reports an
    already-acked consent as still pending and the verdict it seals never
    settles.
    """
    try:
        return True, compute_desired(role_configs=role_configs,
                                     resolver=resolver).pending
    except Exception:  # noqa: BLE001
        logger.exception("trigger union-member compute failed")
        return False, []


def _callback_pending_for_union(
    *, role_configs: dict, resolver: Any = None,
) -> tuple[bool, list[dict]]:
    """``(ok, pending)`` — see :func:`trigger_pending_for_union` for why the
    flag matters. A raising lookup reports ``ok=False``, never empty-as-none."""
    try:
        import callback_reconcile
        return callback_reconcile.callback_pending_for_union(
            role_configs=role_configs, resolver=resolver)
    except Exception:  # noqa: BLE001
        logger.exception("callback union-member lookup failed")
        return False, []


def setup_candidates(*, resolver: Any = None) -> tuple[bool, list[dict]]:
    """Every resolved plugin declaring ``casa.setupTool``, as
    ``(ok, [{"plugin", "artifact_id"}])`` (#451).

    These are the plugins Casa owes a setup run for. Reads plugin.json for
    every resolved plugin, so it runs in the reconcile's WORKER THREAD, never
    on the event loop under the reconcile lock. ``ok=False`` on any sweep
    failure — the caller then creates no obligations and seals no verdicts,
    leaving every existing obligation to hold until a later pass.

    Reads through the SAME ``resolver`` seam as ``compute_desired``, so the
    obligations created and the consent verdict sealed in one pass describe one
    registry snapshot. Resolving independently here would let an artifact
    change between the two reads and seal a verdict against the wrong
    artifact — for which the obligation would then hold forever.
    """
    try:
        import plugin_store
        resolver = resolver if resolver is not None else _default_resolver()
        res = resolver(None)
        if not getattr(res, "registry_valid", False):
            # An invalid registry is not an empty one: creating no obligations
            # is right, but reporting ok would seal "needs no consent" verdicts
            # for plugins we simply could not see.
            return False, []
        out: list[dict] = []
        for rp in getattr(res, "plugins", None) or ():
            try:
                setup = plugin_store.manifest_setup_tool(rp.manifest)
            except Exception:  # noqa: BLE001 — verify already refused it; a
                # malformed declaration is not a candidate.
                continue
            if setup:
                # rp.name — the REGISTRY name, matching the ledger key used by
                # the pending rows, by callback identities and by
                # casa_core's _resolve_registry_entry seam. runtime_name
                # (manifest_name for an owned plugin) would key a
                # specialist-bundled plugin's obligation differently from its
                # consent verdict, and it would never release.
                out.append({"plugin": rp.name,
                            "artifact_id": rp.artifact_id})
        return True, out
    except Exception:  # noqa: BLE001
        logger.exception("setup-candidate sweep failed")
        return False, []


def seal_setup_state(*, trigger_pending: list[dict],
                     callback_pending: list[dict], pending_complete: bool,
                     candidates: "list[dict] | None") -> dict[str, str]:
    """Create the setup obligations Casa owes, and seal ONE positive consent
    verdict per ``(plugin, artifact_id)`` — in one yield-free batch per plugin
    BEFORE any keyboard posts, so a fast Approve on the first keyboard can
    never settle a round still registering its other members.

    Membership is the UNION of the supplied pending trigger and callback
    consent identities. A plugin that owes setup and has NO pending consent is
    sealed with an EMPTY membership: a positive statement that this artifact
    needs no consent, which releases its obligation. That is deliberately
    distinct from sealing nothing, which means "no verdict yet" and holds.

    ``pending_complete`` must be True — both pending computes succeeded — or
    this seals NOTHING and returns ``{}``. An incomplete union cannot be
    distinguished from a complete one, and either kind of seal over a subset
    releases setup early (#451, attempt 1's mechanism). ``candidates=None``
    (the sweep failed) likewise creates no obligations.

    Returns ``{identity: nonce}`` — each caller threads its own prompts'
    nonces into their decision callbacks (stale-expiry fencing). Never raises:
    a ledger failure leaves the prompts unfenced, never blocked.
    """
    import plugin_setup_episodes

    # The pending identities come FIRST, because whether a consent is pending
    # for an artifact is an input to the obligation decision (a terminal row
    # plus a pending consent means setup is owed again — see
    # ``ensure_obligation``). Membership is sealed for every pending identity
    # regardless of whether setup is owed: the round is also what fences the
    # consent keyboards.
    by_plugin: dict[tuple, list[str]] = {}
    for p in trigger_pending:
        ident = ack_identity(
            plugin=p["plugin"], artifact_id=p["artifact_id"],
            effective=p["effective"], target=p["target"], auth=p["auth"])
        by_plugin.setdefault((p["plugin"], p["artifact_id"]), []).append(ident)
    for c in callback_pending:
        # The callback identity is precomputed by its own compute (it binds
        # the declaration digest, which only that module derives).
        by_plugin.setdefault(
            (c["plugin"], c["artifact_id"]), []).append(c["identity"])

    # A candidate that awaits a verdict joins the sealing pass with (possibly)
    # zero members. One already dispatched, refused or failed with NO pending
    # consent reports False and is left alone — no verdict churn on every
    # reconcile for a plugin whose setup is long settled.
    for cand in candidates or ():
        key = (cand["plugin"], cand["artifact_id"])
        try:
            if plugin_setup_episodes.ensure_obligation(
                    plugin=cand["plugin"], artifact_id=cand["artifact_id"],
                    # Only a COMPLETE pending set may re-arm: an under-reported
                    # one would leave a terminal row terminal for the wrong
                    # reason, and an incomplete one cannot be told from it.
                    consent_pending=bool(pending_complete
                                         and by_plugin.get(key))):
                by_plugin.setdefault(key, [])
        except Exception:  # noqa: BLE001 — never break a reconcile on this
            logger.exception("setup obligation ensure failed (plugin=%s)",
                             cand.get("plugin"))
    if not pending_complete:
        # Sealing a verdict now would either under-report membership or assert
        # "no consent needed" without knowing. Hold instead; the obligations
        # created above stay pending and this runs again on the next reconcile.
        logger.info("setup verdicts not sealed: a pending-consent compute "
                    "failed this pass")
        return {}

    nonce_by_identity: dict[str, str] = {}
    for (plg, art), idents in by_plugin.items():
        try:
            nonce_by_identity.update(plugin_setup_episodes.open_round(
                plugin=plg, artifact_id=art, identities=idents))
        except Exception:  # noqa: BLE001 — unfenced, never blocking
            logger.exception("setup-round open failed (plugin=%s)", plg)
    return nonce_by_identity


async def reconcile_from_runtime(runtime: Any, *, prompt: bool = True) -> list:
    """Convenience seam for tools/reload callers holding a CasaRuntime."""
    if runtime is None or getattr(runtime, "trigger_registry", None) is None:
        return []
    return await reconcile_plugin_triggers(
        trigger_registry=runtime.trigger_registry,
        role_configs=getattr(runtime, "role_configs", None) or {},
        channel_manager=getattr(runtime, "channel_manager", None),
        prompt=prompt)


def current_issues() -> list:
    """Fresh, side-effect-free trigger issues for health regeneration —
    recomputed on EVERY ``_regenerate_plugin_health`` pass so they survive
    unrelated refreshes. Never raises (health must always regenerate)."""
    try:
        import agent as agent_mod

        runtime = getattr(agent_mod, "active_runtime", None)
        if runtime is None:
            return []
        role_configs = getattr(runtime, "role_configs", None)
        if not role_configs:
            return []
        return compute_desired(role_configs=role_configs).issues
    except Exception:  # noqa: BLE001 — a trigger-compute crash must never
        # take down the whole health pass; log and degrade to no extras.
        logger.exception("trigger issue recompute failed")
        return []
