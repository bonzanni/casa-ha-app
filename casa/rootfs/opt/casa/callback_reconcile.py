"""The authorization-callback reconciler (runtime seam).

The ONE writer of :class:`trigger_registry.TriggerRegistry`'s CALLBACK
overlay, and the owner of the spool's advisory files (``ready.json`` and the
``.index`` discovery entries). Wired into the same call sites as the
trigger reconciler: casa_core boot, every plugin lifecycle mutation, the
trigger-affecting reload scopes, the consent approve path and the revoke tool.
All entry points serialize on ``_RECONCILE_LOCK``.

Semantics:

* **Complete desired overlay, atomic swap.** Every reconcile derives the FULL
  set of routable plugin callbacks from the CURRENT resolver snapshot and
  swaps it in one operation — a removed / unresolved / revoked / re-declared
  plugin's callback ingress is swept by absence (the handler 404s), and
  readers never see a partial overlay.
* **Gates, in order.** Intrinsic validity of the declaration
  (``callback_invalid``) → the plugin is assigned to at least one role
  (``callback_no_target``) → a persisted operator ack for the exact consent
  identity (``callback_pending_ack``). Unlike a trigger there is no target,
  no clearance and no secret to gate on: a callback grants no turn and no
  memory access, so the pass is the trigger pass with the assignment check
  generalized to "any role" and the secret stage dropped.
* **Fail-closed, per-plugin all-or-nothing.** Any gap in a plugin's set keeps
  its WHOLE set dark plus a ``stage="callbacks"`` ``PluginIssue`` (the mirror
  of INV-TRIG-003). A non-consent gap additionally SUPPRESSES prompting —
  approving a callback that still could not route is a broken promise.
* **Asymmetric file ordering.** Routing swaps the overlay FIRST and only then
  creates the readiness marker + index entry; unrouting deletes them BEFORE
  the swap. The marker can therefore never be falsely positive, and it stays
  advisory: the overlay alone decides what the endpoint serves, so a stale
  marker cannot open a route.
* **Consent survives a routine upgrade.** The consent identity binds the
  DECLARATION digest, not the artifact — an update that leaves ``casa.callbacks``
  untouched keeps its ack (no dark window, no re-tap). Identities no longer
  computable from any installed declaration are pruned opportunistically, and
  only on a CLEAN pass (a resolution hiccup must never vaporize consent).
* **Recomputable health.** :func:`current_issues` recomputes the contextual
  callback issues fresh from the live runtime, so an unrelated health refresh
  can never erase ``callback_pending_ack`` / ``callback_no_target``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import plugin_callbacks

logger = logging.getLogger(__name__)

# Serializes every callback-overlay writer (boot, mutations, reloads, consent
# approve, revoke) so each swap derives from a self-consistent compute.
_RECONCILE_LOCK = asyncio.Lock()

# Distinguishes "caller passed no spool" from "caller passed None on purpose"
# (the latter is a wiring gap the reconcile must SURFACE, not silently paper
# over with the process singleton).
_UNSET: Any = object()

# Assignment targets a callback's delivery nudge can actually reach. An
# executor-only plugin has no agent to collect the code it accepted, so it is
# `callback_no_target` — the same rule plugin_setup_episodes._compose applies
# when it picks a dispatch target.
_RESIDENT_PREFIX = "resident:"
_SPECIALIST_PREFIX = "specialist:"


# -- injectable defaults (module functions so tests can monkeypatch) ---------


def _default_resolver() -> Callable[[str | None], Any]:
    import plugin_registry

    def resolve(target: "str | None") -> Any:
        if target is None:
            return plugin_registry.resolve_all()
        return plugin_registry.resolve_for(target)

    return resolve


def _default_entries() -> Callable[[], list[dict]]:
    """The registry ENTRIES seam — assignment authority for callbacks.

    A resolved plugin carries no targets, and the callback gate is "assigned
    to at least one role" (any resident/specialist, not one scoped target), so
    the entries of the same snapshot the resolver reads are the natural
    source. Keeping it a seam keeps the compute pure and testable."""
    import plugin_registry

    def entries() -> list[dict]:
        return list(plugin_registry.snapshot_registry().entries)

    return entries


def _default_acks() -> Any:
    from callback_acks import ACKS

    return ACKS


def _default_spool() -> Any:
    """The process-wide spool, or ``None`` before boot wired one."""
    import callback_spool

    return callback_spool.get_spool()


def _base_url() -> str | None:
    """The public base URL the redirect URIs are built from.

    Delegates to ``callback_urls.validated_base`` (full origin
    validation: absolute https, no userinfo/path/query/fragment, host not an
    IP literal) rather than only the bashio ``"null"``/``"None"`` guard
    casa_core uses for ``PUBLIC_URL``. ``None`` means the facility is
    unavailable: consent still works, but no readiness marker or index entry
    is written, and every routed plugin reports
    ``callback_base_url_invalid``. Kept as a module-function seam (not an
    inlined call) so tests can keep monkeypatching ``cr._base_url`` directly.
    """
    import callback_urls

    return callback_urls.validated_base()


def _redirect_uri(base: str, effective: str) -> str:
    """The redirect URI a consumer registers with its provider — the
    urllib-based join in ``callback_urls``, never string concat."""
    import callback_urls

    return callback_urls.redirect_uri(base, effective)


@dataclass
class RoutedCallbacks:
    """One routed plugin's published set (the ready/index payload's source)."""

    plugin: str
    artifact_id: str
    path: str
    callbacks: list[dict] = field(default_factory=list)


@dataclass
class DesiredCallbacks:
    """The pure compute result: what SHOULD route right now."""

    overlay: dict[str, dict] = field(default_factory=dict)
    issues: list = field(default_factory=list)
    # Consent prompts to fire — only for callbacks whose ONLY gap is the ack.
    pending: list[dict] = field(default_factory=list)
    routed: list[RoutedCallbacks] = field(default_factory=list)
    # Every identity computable from a currently-installed declaration (the
    # prune's keep-set), and whether this pass is clean enough to prune at all.
    valid_identities: set[str] = field(default_factory=set)
    prunable: bool = False
    base_url: "str | None" = None


def compute_desired(
    *, role_configs: dict, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    entries: "Callable[[], list[dict]] | None" = None,
) -> DesiredCallbacks:
    """Side-effect-free derivation of the complete desired callback overlay +
    the contextual callback issues. Never raises for bad plugin data."""
    import plugin_store
    from plugin_registry import PluginIssue

    acks = acks if acks is not None else _default_acks()
    resolver = resolver if resolver is not None else _default_resolver()
    entries = entries if entries is not None else _default_entries()

    out = DesiredCallbacks(base_url=_base_url())
    all_res = resolver(None)
    if not getattr(all_res, "registry_valid", False):
        # Fail-closed: an invalid registry routes NO callback ingress (its own
        # registry-stage issues surface via the resolver / health pass), and
        # nothing is pruned — a membership set derived from a failed load
        # would drop every consent.
        return out
    # Opportunistic prune only on a CLEAN pass: an artifact checksum hiccup or
    # an unreadable manifest drops that plugin from the resolution, and
    # treating its absence as "the declaration is gone" would silently discard
    # the operator's consent. The next clean reconcile prunes instead.
    out.prunable = not list(getattr(all_res, "issues", ()) or ())

    # Assignment authority: the registry entry's OWN declared targets, read
    # once from the same snapshot the resolver reads.
    targets_by_name: dict[str, list] = {}
    for entry in entries():
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            targets_by_name[entry["name"]] = list(entry.get("targets") or [])
    live_roles = {f"{_RESIDENT_PREFIX}{role}" for role in role_configs}

    for rp in all_res.plugins:
        try:
            callbacks = plugin_store.manifest_callbacks(rp.manifest, rp.name)
        except Exception:  # noqa: BLE001 — StoreError("callbacks_invalid"),
            # or any other read failure on a pre-published artifact: the
            # publish gate is younger than the store, so an invalid
            # declaration is a state to SURFACE, never a reconcile crash.
            out.issues.append(PluginIssue(
                name=rp.name, target=None, stage="callbacks",
                reason_code="callback_invalid", artifact_id=rp.artifact_id))
            # An unparseable declaration contributes NO identities,
            # so pruning this pass would destroy the operator's consent for
            # this plugin's still-valid callbacks (all-or-nothing rejects the
            # set, it does not delete it). We cannot read the declaration, so
            # we cannot know any ack is stale — suppress the whole prune until
            # a pass that can.
            out.prunable = False
            continue
        if not callbacks:
            continue

        # The prune's keep-set is about the DECLARATION existing, not about
        # routing: an unassigned (or still-unacked) plugin's consent must
        # survive, so these identities are collected BEFORE any later gate.
        declared = [
            (cb, digest, plugin_callbacks.ack_identity(
                rp.name, cb["effective"], digest))
            for cb, digest in ((cb, plugin_callbacks.declaration_digest(cb))
                               for cb in callbacks)]
        out.valid_identities.update(ident for _cb, _d, ident in declared)

        # Gate 2 — assignment. Per PLUGIN (a callback declares no target):
        # the delivery nudge needs an agent to hand the waiting result to, and
        # routing an unreachable plugin would accept short-lived codes nobody
        # can ever collect.
        if not _reachable(targets_by_name.get(rp.name) or [], live_roles):
            out.issues.append(PluginIssue(
                name=rp.name, target=None, stage="callbacks",
                reason_code="callback_no_target", artifact_id=rp.artifact_id))
            # A non-consent gap: no prompt, and the whole set stays dark.
            continue

        entries_for_plugin: dict[str, dict] = {}
        plugin_pending: list[dict] = []
        for cb, digest, identity in declared:
            if acks.get(identity) is None:
                out.issues.append(PluginIssue(
                    name=rp.name, target=None, stage="callbacks",
                    reason_code="callback_pending_ack",
                    artifact_id=rp.artifact_id))
                plugin_pending.append({
                    "plugin": rp.name, "artifact_id": rp.artifact_id,
                    "declared": cb["declared"], "effective": cb["effective"],
                    "declaration_digest": digest, "identity": identity})
                continue
            entries_for_plugin[cb["effective"]] = {
                "plugin": rp.name, "declared": cb["declared"],
                # Carry the effective name in the value too (it is already the
                # key): the callback handler records it in the result and logs
                # it, and reading it from the entry keeps that on one shape
                # instead of a routed-name fallback.
                "effective": cb["effective"], "path": rp.path}

        # Per-plugin all-or-nothing: any gap unroutes the whole set.
        if plugin_pending:
            out.pending.extend(plugin_pending)
            continue
        out.overlay.update(entries_for_plugin)
        out.routed.append(RoutedCallbacks(
            plugin=rp.name, artifact_id=rp.artifact_id, path=rp.path,
            callbacks=[dict(cb) for cb in callbacks]))
        if out.base_url is None:
            # Consent and routing stand; the facility is simply unavailable
            # until the operator sets a usable public_url — nothing can be
            # published for the consumer to read.
            out.issues.append(PluginIssue(
                name=rp.name, target=None, stage="callbacks",
                reason_code="callback_base_url_invalid",
                artifact_id=rp.artifact_id))
    return out


def _reachable(targets: list, live_roles: set[str]) -> bool:
    """A target the delivery nudge can reach: a LIVE resident role, or any
    specialist (specialists are not in ``role_configs``; the nudge reaches
    them through assistant delegation, exactly as setup episodes do)."""
    for t in targets:
        if not isinstance(t, str):
            continue
        if t in live_roles or t.startswith(_SPECIALIST_PREFIX):
            return True
    return False


# ---------------------------------------------------------------------------
# the spool file phase (ready.json + .index)
# ---------------------------------------------------------------------------


def _ready_payload(base_url: str, routed: RoutedCallbacks) -> dict:
    return {
        "v": 1,
        "base_url": base_url,
        "callbacks": {
            cb["declared"]: {
                "effective": cb["effective"],
                "redirect_uri": _redirect_uri(base_url, cb["effective"]),
            }
            for cb in routed.callbacks
        },
    }


def _spool_issue(issues: list, plugin: str, artifact_id: str | None) -> None:
    """One ``callback_spool_error`` per plugin per pass: an unwired spool
    would otherwise emit a row for every file operation of every plugin, and
    the health report's fingerprint dedup is not a licence to spam it."""
    from plugin_registry import PluginIssue
    if any(getattr(i, "name", None) == plugin
           and getattr(i, "reason_code", None) == "callback_spool_error"
           for i in issues):
        return
    issues.append(PluginIssue(
        name=plugin, target=None, stage="callbacks",
        reason_code="callback_spool_error", artifact_id=artifact_id))


def _retire_orphaned_markers(spool: Any, desired: DesiredCallbacks) -> None:
    """Reconcile the DURABLE on-disk marker inventory against the desired
    routed set, retiring any published ``ready.json`` / index entry whose
    plugin (or artifact key) the desired set no longer routes.

    Why this exists alongside :func:`_pre_swap_files`: that pass compares the
    desired set against the IN-MEMORY previous overlay, which is empty across a
    restart — so a marker for a plugin that has since dropped out of the routed
    set (ack lost, declaration changed, base URL invalid, removed, artifact
    path changed) would survive a reboot and keep advertising a route the
    overlay no longer serves. A consumer that read such a stale marker would
    mint a state and be sent to a route the overlay 404s — a dead flow that
    looks alive. Reading the spool's OWN published inventory makes the
    retirement survive a restart, where the in-memory diff cannot.

    A still-ROUTED plugin whose on-disk payload no longer matches the desired
    one (a callback dropped, or the redirect base changed, while the process
    was down) is caught here too: the in-memory diff cannot see it across a
    restart, so its old marker would keep advertising a removed callback or an
    obsolete redirect URI until the post-swap rewrite — and PERMANENTLY if that
    rewrite then fails. Retiring it BEFORE the swap makes the post-swap rewrite
    the sole writer: a failed rewrite then leaves the marker ABSENT
    (fail-closed — the consumer reads "not approved yet, wait") rather than
    stale. An UNCHANGED marker is left exactly as it is (no retire, no churn).

    Gated on ``desired.prunable`` — the SAME double-gate the stale-ack prune
    uses. A wholesale compute failure (invalid registry, or a resolution
    hiccup) leaves ``prunable`` False, so this never nukes every marker (the
    availability blast radius); a plugin simply ABSENT from a trustworthy
    computation does have its stale marker retired. Runs BEFORE the overlay
    swap — the same delete-before-swap ordering as the in-memory path.
    """
    if spool is None or not desired.prunable:
        return
    import callback_spool

    if desired.base_url:
        routed_by_plugin = {r.plugin: r for r in desired.routed}
        keys = {callback_spool.index_key(r.path) for r in desired.routed}
    else:
        routed_by_plugin, keys = {}, set()
    published = set(routed_by_plugin)
    try:
        on_disk_plugins = list(spool.published_plugins())
    except Exception:  # noqa: BLE001 — a read failure degrades to leaving the
        on_disk_plugins = []           # markers in place, never a reconcile crash
    for plugin in on_disk_plugins:
        if plugin not in published:
            _retire_marker(spool, "delete_ready", plugin)
    try:
        on_disk_keys = list(spool.index_keys())
    except Exception:  # noqa: BLE001
        on_disk_keys = []
    for key in on_disk_keys:
        if key not in keys:
            _retire_marker(spool, "delete_index_key", key)

    # Still-routed but payload-changed: retire the stale pair before the swap
    # so the post-swap rewrite is the sole writer (fail-closed on rewrite
    # failure). Both files carry the same payload, so both retire together —
    # keeping ready.json (keyed by plugin) and the .index entry (keyed by
    # artifact realpath) consistent, exactly as the write path pairs them.
    for plugin, routed in routed_by_plugin.items():
        if _marker_is_stale(spool, desired.base_url, routed):
            _retire_marker(spool, "delete_ready", plugin)
            _retire_marker(spool, "delete_index_entry", routed.path)


def _read_marker(spool: Any, op: str, arg: str) -> "dict | None":
    """Read one on-disk marker payload, total: any failure ⇒ ``None``
    (treated as ABSENT — the write path restores it, and an absent marker is
    never falsely positive)."""
    try:
        return getattr(spool, op)(arg)
    except Exception:  # noqa: BLE001
        return None


def _marker_is_stale(spool: Any, base_url: "str | None",
                     routed: RoutedCallbacks) -> bool:
    """True when a PRESENT on-disk marker for this still-routed plugin differs
    from the desired payload — the retire-before-swap trigger. An ABSENT marker
    is not stale (the post-swap write creates it); only an existing, differing
    one must retire so a failed rewrite leaves it absent rather than stale."""
    ready, index = _desired_marker_payloads(base_url, routed)
    on_ready = _read_marker(spool, "read_ready", routed.plugin)
    on_index = _read_marker(spool, "read_index_entry", routed.path)
    return ((on_ready is not None and on_ready != ready)
            or (on_index is not None and on_index != index))


def _desired_marker_payloads(base_url: "str | None",
                             routed: RoutedCallbacks) -> "tuple[dict, dict]":
    """The ready.json and .index payloads the post-swap write would publish —
    the single source both the stale-check and the skip-if-unchanged write use,
    so the two can never drift."""
    ready = _ready_payload(base_url, routed)
    return ready, dict(ready, plugin_dir=routed.plugin)


def _retire_marker(spool: Any, op: str, arg: str) -> None:
    """Best-effort retirement of one orphaned marker/index entry. A failure is
    logged, not surfaced as a per-plugin health issue: the entry is orphaned
    (its plugin is not in the desired routed set), so there is no routed plugin
    to attribute a ``callback_spool_error`` to, and the overlay — the sole
    routing authority — already 404s the deposit regardless."""
    try:
        getattr(spool, op)(arg)
    except Exception:  # noqa: BLE001
        logger.warning("callback orphan-marker retire %s failed", op,
                       exc_info=True)


def _pre_swap_files(spool: Any, desired: DesiredCallbacks,
                    previous: dict[str, dict]) -> None:
    """Delete the markers/index entries of everything that is about to stop
    being published — BEFORE the swap that unroutes it, so a crash mid-unroute
    can only leave the route closed with the marker already gone, never the
    reverse. Covers four cases with one rule: unrouted plugins, plugins that
    stay routed but can no longer be published (no base URL), routed plugins
    whose ARTIFACT PATH changed (the index key is the artifact path, so the
    old key must retire in the same pass), and routed plugins
    that DROP any previously published callback: "never falsely positive" holds
    per FILE, not per overlay entry, so a marker still advertising a dropped
    callback is exactly the stale-marker case, both during the swap window and
    persistently if the post-swap rewrite then fails. Both published files
    carry the same ``callbacks`` map, so both retire together on that
    condition: a failed rewrite then leaves them ABSENT (the consumer
    reads "facility unavailable") rather than stale."""
    published = {r.plugin: r for r in desired.routed} if desired.base_url \
        else {}
    stale_pairs: set[tuple[str, str]] = set()
    previous_effectives: dict[str, set[str]] = {}
    for effective, entry in previous.items():
        plugin = entry.get("plugin") or ""
        if not plugin:
            continue
        stale_pairs.add((plugin, entry.get("path") or ""))
        previous_effectives.setdefault(plugin, set()).add(effective)
    for plugin, path in sorted(stale_pairs):
        keep = published.get(plugin)
        dropped = keep is not None and _dropped_any(
            previous_effectives.get(plugin, set()), keep)
        retire_index = path and (keep is None or keep.path != path or dropped)
        if keep is None or dropped:
            _guard(spool, desired, plugin,
                   keep.artifact_id if keep is not None else None,
                   "delete_ready", plugin)
        if retire_index:
            # An EMPTY path is never handed to the spool: the index key is
            # sha256(realpath(path)), and realpath("") is the process CWD —
            # a malformed overlay entry must not make us delete a key derived
            # from wherever casa happens to be running.
            _guard(spool, desired, plugin,
                   keep.artifact_id if keep is not None else None,
                   "delete_index_entry", path)


def _dropped_any(previous: set[str], keep: RoutedCallbacks) -> bool:
    """True when the desired set DROPS anything the last published files
    advertised — whatever it adds in the same pass.

    A strict-subset test missed the mixed transition: rename one callback and
    add another, and the marker kept naming the dropped one. Additions are
    irrelevant to the property being protected — a file naming a callback the
    overlay no longer routes is falsely positive regardless of what else it
    names. A pure GROWTH (nothing dropped) only ever under-advertises, which is
    fail-closed, so it must not churn the files at all.
    """
    now = {cb["effective"] for cb in keep.callbacks}
    return bool(previous - now)


def _post_swap_files(spool: Any, desired: DesiredCallbacks) -> None:
    """Publish the readiness marker + discovery index entry for every routed
    plugin — only AFTER the overlay swap, so the marker can never advertise a
    route that is not live.

    A plugin whose on-disk payload ALREADY matches the desired one is skipped:
    the marker is advisory and only reflects the base URL + callbacks map, so
    rewriting an identical payload every reconcile is pure churn (a temp +
    rename + fsync per pass). Anything that changed was retired before the swap
    (:func:`_retire_orphaned_markers` / :func:`_pre_swap_files`), so a stale
    marker is never left behind by this skip."""
    if desired.base_url is None:
        return
    for routed in desired.routed:
        ready, index = _desired_marker_payloads(desired.base_url, routed)
        if not _marker_needs_write(spool, routed, ready, index):
            continue
        if not _guard(spool, desired, routed.plugin, routed.artifact_id,
                      "ensure_plugin_dirs", routed.plugin):
            continue
        if not _guard(spool, desired, routed.plugin, routed.artifact_id,
                      "write_ready", routed.plugin, ready):
            continue
        _guard(spool, desired, routed.plugin, routed.artifact_id,
               "write_index_entry", routed.path, index)


def _marker_needs_write(spool: Any, routed: RoutedCallbacks,
                        ready: dict, index: dict) -> bool:
    """True unless BOTH on-disk files already equal the desired payloads. An
    absent or unreadable marker reads back as ``None`` ⇒ needs writing; a
    matching pair is left untouched (no churn)."""
    on_ready = _read_marker(spool, "read_ready", routed.plugin)
    on_index = _read_marker(spool, "read_index_entry", routed.path)
    return on_ready != ready or on_index != index


def _guard(spool: Any, desired: DesiredCallbacks, plugin: str,
           artifact_id: "str | None", op: str, *args) -> bool:
    """Run one spool operation, converting any failure into a health issue.

    A spool failure never unroutes: the overlay is the authority and the
    published files are advisory, so the fail-closed direction here is "the
    consumer cannot discover its redirect URI", which the operator sees as
    ``callback_spool_error`` rather than a silently dead endpoint."""
    if spool is None:
        _spool_issue(desired.issues, plugin, artifact_id)
        return False
    try:
        getattr(spool, op)(*args)
        return True
    except Exception:  # noqa: BLE001 — a marker failure must never break the
        # reconcile for OTHER plugins (or leave the overlay half-swapped).
        logger.warning("callback spool %s failed (plugin=%s)", op, plugin,
                       exc_info=True)
        _spool_issue(desired.issues, plugin, artifact_id)
        return False


async def _regen_health_safe() -> None:
    """Regenerate the plugin-health report (no operator notify) so a
    just-acked callback's stale ``callback_pending_ack`` clears immediately
    instead of lingering until the next plugin mutation/boot. Never raises."""
    try:
        import tools
        await asyncio.to_thread(tools._regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("post-consent plugin-health regen failed", exc_info=True)


async def reconcile_plugin_callbacks(
    *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any = None, acks: Any = None, spool: Any = _UNSET,
    resolver: "Callable[[str | None], Any] | None" = None,
    entries: "Callable[[], list[dict]] | None" = None,
    prompt: bool = True, regen_health: bool = False,
) -> list:
    """Compute + apply: retire the files of everything about to stop being
    published, swap the complete desired overlay, publish the routed set's
    files, prune stale acks, fire consent prompts. Returns the callback
    issues."""
    acks = acks if acks is not None else _default_acks()
    spool = _default_spool() if spool is _UNSET else spool

    def _compute() -> "tuple[DesiredCallbacks, list[dict]]":
        # The union-membership compute reads plugin.json for every
        # resolved plugin, so it belongs in the SAME worker thread as the main
        # compute — never on the event loop under the reconcile lock. It still
        # runs strictly before any keyboard posts (the prompts fire below,
        # after this returns).
        computed = compute_desired(
            role_configs=role_configs, acks=acks, resolver=resolver,
            entries=entries)
        union: list[dict] = []
        if prompt and computed.pending:
            import trigger_reconcile
            union = trigger_reconcile.trigger_pending_for_union(
                role_configs=role_configs, resolver=resolver)
        return computed, union

    async with _RECONCILE_LOCK:
        try:
            desired, union_pending = await asyncio.to_thread(_compute)
        except Exception:
            # A compute failure must not RETAIN the old overlay (a
            # just-revoked plugin's callback would stay open behind a
            # swallowed warning). Fail closed to NO callback ingress, then
            # propagate so the caller logs/surfaces it; the next successful
            # reconcile restores the valid set. The spool files are left
            # untouched — they are advisory and the closed overlay already
            # 404s every deposit.
            trigger_registry.replace_callback_overlay({})
            raise

        previous = trigger_registry.callback_overlay_snapshot()
        await asyncio.to_thread(_retire_orphaned_markers, spool, desired)
        await asyncio.to_thread(_pre_swap_files, spool, desired, previous)
        trigger_registry.replace_callback_overlay(desired.overlay)
        await asyncio.to_thread(_post_swap_files, spool, desired)

        if desired.prunable:
            try:
                removed = await asyncio.to_thread(
                    acks.prune_stale, desired.valid_identities)
                if removed:
                    logger.info("pruned %d stale callback ack(s)", len(removed))
            except Exception:  # noqa: BLE001 — an opportunistic prune must
                # never break the reconcile; the next pass retries.
                logger.warning("callback ack prune failed", exc_info=True)

        try:
            import plugin_setup_episodes
            plugin_setup_episodes.kick()
        except Exception:  # noqa: BLE001 — never break a reconcile on this
            pass

        # Prompts fire INSIDE the lock (the trigger-reconcile discipline):
        # keyboard registration is then ordered BEFORE any later reconcile can
        # acquire the lock, so a revoke's final cancel_matching(plugin=…)
        # provably catches every keyboard an in-flight reconcile posted.
        if prompt and desired.pending:
            _fire_consent_prompts(
                desired.pending, trigger_registry=trigger_registry,
                role_configs=role_configs, channel_manager=channel_manager,
                acks=acks, spool=spool, resolver=resolver, entries=entries,
                union_pending=union_pending)
    if regen_health:
        await _regen_health_safe()
    return desired.issues


def _fire_consent_prompts(
    pending: list[dict], *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any, acks: Any, spool: Any, resolver: Any,
    entries: Any, union_pending: list[dict],
) -> None:
    import authz_grants
    import callback_consent
    import trigger_reconcile

    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None:
        return  # no DM reachable — pending_ack stands; re-prompted later
    op = callback_consent.operator_identity(channel)
    if op is None:
        return
    chat_id, operator_id = op

    async def _reconcile_again() -> None:
        # Captures THIS reconcile's inputs. If a reload rebinds the runtime
        # registries before the tap lands, the swap goes to the old registry
        # object — harmless: the ack is persisted, so the next lifecycle
        # reconcile routes it on the live one.
        await reconcile_plugin_callbacks(
            trigger_registry=trigger_registry, role_configs=role_configs,
            channel_manager=channel_manager, acks=acks, spool=spool,
            resolver=resolver, entries=entries, prompt=False,
            regen_health=True)

    # SEAL the plugin's setup-round membership as the UNION of its pending
    # TRIGGER and CALLBACK consents, in one yield-free batch BEFORE any
    # keyboard posts: the two reconcilers run as a pair at every call
    # site, and whichever prompts first must open the complete membership —
    # otherwise a fast Approve on this keyboard could settle a round whose
    # other kind has not registered yet, running the plugin's setup tool while
    # a consent is still open. ``union_pending`` (the trigger half) was
    # computed off-loop with this pass's desired set.
    nonce_by_identity = trigger_reconcile.seal_setup_rounds(
        trigger_pending=union_pending, callback_pending=pending)

    for p in pending:
        try:
            callback_consent.prompt_callback_consent(
                coordinator=authz_grants.CHALLENGES, channel=channel,
                chat_id=chat_id, operator_id=operator_id, acks=acks,
                reconcile_cb=_reconcile_again,
                setup_nonce=nonce_by_identity.get(p["identity"], ""),
                plugin=p["plugin"], artifact_id=p["artifact_id"],
                declared=p["declared"], effective=p["effective"],
                declaration_digest=p["declaration_digest"])
        except Exception:  # noqa: BLE001 — a prompt failure never breaks the
            # mutation; pending_ack stays in health and re-prompts later.
            logger.exception("callback consent prompt failed (plugin=%s)",
                             p.get("plugin"))


def callback_pending_for_union(
    *, role_configs: dict, resolver: Any = None,
) -> list[dict]:
    """The pending CALLBACK consents, for the trigger reconciler's union
    sealing. Side-effect free and never raises: a failure here degrades the
    union to trigger-only membership (the callback reconcile that follows
    seals its own), it must never break trigger prompting."""
    try:
        return compute_desired(role_configs=role_configs,
                               resolver=resolver).pending
    except Exception:  # noqa: BLE001
        logger.exception("callback union-member compute failed")
        return []


async def reconcile_from_runtime(runtime: Any, *, prompt: bool = True) -> list:
    """Convenience seam for tools/reload callers holding a CasaRuntime."""
    if runtime is None or getattr(runtime, "trigger_registry", None) is None:
        return []
    return await reconcile_plugin_callbacks(
        trigger_registry=runtime.trigger_registry,
        role_configs=getattr(runtime, "role_configs", None) or {},
        channel_manager=getattr(runtime, "channel_manager", None),
        prompt=prompt)


def current_issues() -> list:
    """Fresh, side-effect-free callback issues for health regeneration —
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
    except Exception:  # noqa: BLE001 — a callback-compute crash must never
        # take down the whole health pass; log and degrade to no extras.
        logger.exception("callback issue recompute failed")
        return []
