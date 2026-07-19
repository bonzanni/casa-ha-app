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
            if not acks.is_acked(ident):
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
                "clearance": t["clearance"], "auth": t["auth"]}

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
            got = webhook_auth.ensure_secret(eff, owner="casa",
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


async def reconcile_plugin_triggers(
    *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any = None, acks: Any = None,
    secrets_dir: "Path | None" = None, prompt: bool = True,
    resolver: "Callable[[str | None], Any] | None" = None,
    global_secret_ok: "Callable[[], bool] | None" = None,
) -> list:
    """Compute + apply: swap the complete desired overlay into the registry,
    mint eager secrets, fire consent prompts. Returns the trigger issues."""
    acks = acks if acks is not None else _default_acks()
    # Resolved at CALL time from the module attribute (not a def-time default)
    # so there is one source of truth for the secrets location.
    secrets_dir = SECRETS_DIR if secrets_dir is None else secrets_dir

    def _compute_and_mint() -> DesiredTriggers:
        desired = compute_desired(
            role_configs=role_configs, acks=acks, resolver=resolver,
            global_secret_ok=global_secret_ok)
        _mint_secrets(desired, Path(secrets_dir))
        return desired

    async with _RECONCILE_LOCK:
        try:
            desired = await asyncio.to_thread(_compute_and_mint)
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

    if prompt and desired.pending:
        _fire_consent_prompts(
            desired.pending, trigger_registry=trigger_registry,
            role_configs=role_configs, channel_manager=channel_manager,
            acks=acks, secrets_dir=secrets_dir, resolver=resolver,
            global_secret_ok=global_secret_ok)
    return desired.issues


def _fire_consent_prompts(
    pending: list[dict], *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any, acks: Any, secrets_dir: Path,
    resolver: Any, global_secret_ok: Any,
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
            global_secret_ok=global_secret_ok)

    for p in pending:
        try:
            trigger_consent.prompt_trigger_consent(
                coordinator=authz_grants.CHALLENGES, channel=channel,
                chat_id=chat_id, operator_id=operator_id, acks=acks,
                reconcile_cb=_reconcile_again, **p)
        except Exception:  # noqa: BLE001 — a prompt failure never breaks
            # the mutation; pending_ack stays in health and re-prompts later.
            logger.exception("trigger consent prompt failed (plugin=%s)",
                             p.get("plugin"))


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
