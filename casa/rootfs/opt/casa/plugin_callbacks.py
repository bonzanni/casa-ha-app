"""Plugin-declared authorization callbacks — manifest parsing + intrinsic
validation.

Leaf module (stdlib only). A plugin's ``plugin.json`` may carry
``casa.callbacks`` — a peer of ``casa.triggers`` naming redirect endpoints an
external OAuth-style provider deposits an authorization result at
(``GET /callback/<effective>``). This module turns that block into
normalized callback dicts and collects intrinsic-validation errors (shape,
naming) that are knowable WITHOUT deployment state. Contextual validity
(operator consent, plugin existence) is decided later at reconcile time.

Unlike a trigger, a callback grants no turn into a role and no memory
access: an entry carries no ``target``, no ``clearance``, no ``auth``
block — just the declared name. The deposited state is consumer-minted.

Intrinsic validation is fail-closed and per-plugin all-or-nothing: any error
in the set means the whole plugin's callback declaration is rejected by
callers.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_CALLBACKS = 4
# Not the trigger cap (64): bundled plugins' registry names are
# `slug.manifest_name` (up to 73 chars) and callbacks have no secret files
# to bound, so the cap is looser.
MAX_EFFECTIVE_LEN = 128

_CALLBACK_KEYS = {"name"}


def effective_name(plugin: str, declared: str) -> str:
    """The routed callback name for a plugin-declared callback: ``plg-<plugin>--<declared>``."""
    return f"plg-{plugin}--{declared}"


def declaration_digest(entry: dict[str, Any]) -> str:
    """sha256 hex over the canonical JSON of a callback's normalized declaration.

    Bound only to the declared name — a callback carries no target,
    clearance, or auth policy to fold in. One input to ``ack_identity``: a
    routine plugin upgrade that leaves the declaration unchanged produces
    the same digest (and therefore keeps its consent), unlike artifact-bound
    trigger acks.
    """
    body = json.dumps(
        {"name": entry["declared"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def ack_identity(plugin: str, effective: str, declaration_digest: str) -> str:
    """The consent identity: sha256 over ``(plugin, effective, declaration_digest)``.

    Deliberately excludes the artifact id (operator decision): a callback
    ack authorizes only "an unauthenticated GET may deposit a query blob
    into this plugin's spool" — no turn, no memory access — so a plugin
    upgrade that leaves the declaration unchanged keeps its ack. Any
    operator-visible change (rename, new fields later) yields a different
    ``declaration_digest`` and therefore a new identity.
    """
    body = json.dumps(
        [plugin, effective, declaration_digest],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def parse_and_validate(
    plugin: str, manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(callbacks, errors)`` for a plugin's ``casa.callbacks``.

    ``callbacks`` are the normalized (best-effort) parsed entries; a
    non-empty ``errors`` means callers reject the WHOLE set (per-plugin
    all-or-nothing). An absent/malformed ``casa`` or ``casa.callbacks`` is
    ``([], [])`` (no callbacks declared, not an error).
    """
    errs: list[str] = []
    casa = manifest.get("casa")
    if not isinstance(casa, dict):
        return [], []
    raw = casa.get("callbacks")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["casa.callbacks must be a list"]

    # Plugin name itself must be injective-safe (feeds the effective name).
    # Same rails as plugin_triggers.py:124-134 — with both dash edges
    # banned, the FIRST '--' in an effective name is always exactly the
    # separator (unique decomposition).
    if "--" in plugin:
        errs.append(f"plugin name {plugin!r} may not contain '--'")
    if plugin.endswith("-"):
        errs.append(f"plugin name {plugin!r} may not end with '-' "
                    "(ambiguous callback-name separator)")

    if len(raw) > MAX_CALLBACKS:
        errs.append(f"too many callbacks ({len(raw)} > {MAX_CALLBACKS})")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"casa.callbacks[{i}]"
        if not isinstance(entry, dict):
            errs.append(f"{where}: must be an object")
            continue
        unknown = set(entry) - _CALLBACK_KEYS
        if unknown:
            errs.append(f"{where}: unknown key(s) {sorted(unknown)}")
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name or ""):
            errs.append(f"{where}: name must match [a-zA-Z0-9_-]+")
            name = None
        elif "--" in name:
            errs.append(f"{where}: name {name!r} may not contain '--'")
        elif name.startswith("-"):
            errs.append(f"{where}: name {name!r} may not start with '-' "
                        "(ambiguous plugin-name separator)")
        elif name.startswith("plg-"):
            errs.append(f"{where}: name may not start with the reserved 'plg-' prefix")
        else:
            if name in seen:
                errs.append(f"{where}: duplicate declared name {name!r}")
            seen.add(name)
            eff = effective_name(plugin, name)
            if len(eff) > MAX_EFFECTIVE_LEN:
                errs.append(f"{where}: effective name {eff!r} too long "
                            f"(>{MAX_EFFECTIVE_LEN})")
        if name:
            out.append({"declared": name, "effective": effective_name(plugin, name)})
    return out, errs
