"""Reminder entries as data (#396).

A reminder IS a trigger. This module owns the parts of that which are about
*data*: generating and recognising reminder names, deriving a schedule from a
resolved instant plus a repeat rule, reading/appending/removing reminder
entries in a role's ``triggers.yaml``, and answering which reminders are
overdue. It knows about files and time; it knows nothing about APScheduler or
MCP — so ``trigger_registry`` never learns to write YAML and ``tools`` never
learns cron.

Two design points worth keeping in view:

* **Cron has no year field.** A dated one-shot written as cron (``55 7 3 8 *``)
  is an ANNUAL trigger with a self-delete instruction stapled on. ``type:
  date`` exists to remove that trap.
* **Presence is the ledger.** A one-shot reminder still sitting in
  ``triggers.yaml`` with a past fire time *is* the record that delivery is
  owed. Delivery removes the entry. There is no second store to keep in sync.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime

import yaml

from atomic_io import atomic_write_text
from yaml_safety import load_yaml_no_aliases

logger = logging.getLogger(__name__)

REMINDER_PREFIX = "reminder-"

REPEATS: tuple[str, ...] = ("none", "daily", "weekdays", "weekly", "monthly")

# datetime.weekday(): 0=Monday. Cron day NAMES are emitted deliberately — a
# numeric day-of-week reintroduces #343 (standard cron numbers Sunday 0,
# APScheduler 3.x numbers Monday 0), which trigger_registry._translate_cron_dow
# exists to defuse. Deriving the name from the already-resolved date also means
# the weekday can never disagree with the time the agent read back to the user.
_DOW_BY_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def new_reminder_name() -> str:
    """A fresh reminder trigger name, matching the schema's name pattern."""
    return f"{REMINDER_PREFIX}{secrets.token_hex(3)}"


def is_reminder_name(name: str) -> bool:
    """True iff *name* is one the reminder tools are allowed to touch.

    This is the whole bound between "a resident managing its own reminders"
    and "a resident editing operator configuration".
    """
    return bool(name) and name.startswith(REMINDER_PREFIX)


# ---------------------------------------------------------------------------
# Time and schedule derivation
# ---------------------------------------------------------------------------


def parse_at(value: str) -> datetime:
    """Parse an ISO-8601 instant that MUST carry a UTC offset.

    A naive datetime is refused rather than assumed to be local: "08:00" with
    no offset is not a point in time, and guessing one is how a reminder
    quietly fires an hour out.
    """
    if not value:
        raise ValueError("reminder time is required")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not an ISO-8601 time: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"reminder time must carry a UTC offset; got {value!r}"
        )
    return dt


def derive_schedule(at: datetime, repeat: str) -> dict[str, str]:
    """Trigger fields for *repeat* anchored at *at*.

    ``repeat="none"`` keeps the absolute instant — a single point in time has
    no recurrence to drift, and the offset is what pins it.

    Every recurring rule persists DERIVED WALL-CLOCK fields and DISCARDS the
    offset, so a reminder set at 07:00 in summer still fires at 07:00 local in
    winter (spec §7.1). APScheduler evaluates the cron expression in the
    scheduler's own timezone; persisting the supplied ``+02:00`` and applying
    it literally would shift the reminder by an hour across a DST boundary.
    """
    if repeat not in REPEATS:
        raise ValueError(f"repeat must be one of {REPEATS}; got {repeat!r}")
    if repeat == "none":
        return {"type": "date", "at": at.isoformat()}

    minute, hour = at.minute, at.hour
    if repeat == "daily":
        schedule = f"{minute} {hour} * * *"
    elif repeat == "weekdays":
        schedule = f"{minute} {hour} * * mon-fri"
    elif repeat == "weekly":
        schedule = f"{minute} {hour} * * {_DOW_BY_WEEKDAY[at.weekday()]}"
    else:  # monthly
        schedule = f"{minute} {hour} {at.day} * *"
    return {"type": "cron", "schedule": schedule}


# ---------------------------------------------------------------------------
# The entry store — reminder entries inside a role's triggers.yaml
# ---------------------------------------------------------------------------


def triggers_path(agents_dir: str, role: str) -> str:
    """Absolute path to *role*'s triggers.yaml. Residents only."""
    return os.path.join(agents_dir, role, "triggers.yaml")


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {"schema_version": 1, "triggers": []}
    with open(path, encoding="utf-8") as fh:
        doc = load_yaml_no_aliases(fh.read()) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: triggers.yaml is not a mapping")
    doc.setdefault("schema_version", 1)
    if not isinstance(doc.get("triggers"), list):
        doc["triggers"] = []
    return doc


def _save(path: str, doc: dict) -> None:
    atomic_write_text(
        path, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
    )


def append_entry(path: str, entry: dict) -> None:
    """Append a reminder *entry*, leaving every other trigger untouched."""
    if not is_reminder_name(entry.get("name", "")):
        raise ValueError(
            f"refusing to write a non-reminder entry: {entry.get('name')!r}"
        )
    doc = _load(path)
    doc["triggers"].append(entry)
    _save(path, doc)


def remove_entry(path: str, name: str) -> bool:
    """Remove the reminder called *name*. True if something was removed.

    Refuses any name lacking the reserved prefix — this is what keeps the
    writer from deleting operator-authored triggers.
    """
    if not is_reminder_name(name):
        raise ValueError(f"not a reminder name: {name!r}")
    doc = _load(path)
    kept = [t for t in doc["triggers"] if t.get("name") != name]
    if len(kept) == len(doc["triggers"]):
        return False
    doc["triggers"] = kept
    _save(path, doc)
    return True


def past_due(path: str, now: datetime) -> list[dict]:
    """One-shot reminder entries whose instant has passed and which are still
    present. Presence IS the record that delivery is owed.

    An unparseable ``at`` is skipped with a warning rather than raised: one
    corrupt entry must not stop the sweep delivering the others.
    """
    out: list[dict] = []
    try:
        entries = _load(path)["triggers"]
    except (OSError, ValueError):
        logger.warning("reminders: cannot read %s; skipping", path,
                       exc_info=True)
        return out
    for entry in entries:
        if entry.get("type") != "date" or not entry.get("one_shot"):
            continue
        if not is_reminder_name(entry.get("name", "")):
            continue
        try:
            when = parse_at(entry.get("at", ""))
        except ValueError:
            logger.warning("reminder %s has an unparseable 'at'; skipping",
                           entry.get("name"))
            continue
        if when <= now:
            out.append(entry)
    return out
