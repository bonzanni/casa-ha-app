"""Reminder entries as data (#396).

A reminder IS a trigger. This module owns the parts of that which are about
*data*: generating and recognising reminder names, deriving a schedule from a
resolved instant plus a repeat rule, reading/appending/removing reminder
entries in a role's ``reminders.yaml``, and answering which reminders are
overdue. It knows about files and time; it knows nothing about APScheduler or
MCP — so ``trigger_registry`` never learns to write YAML and ``tools`` never
learns cron.

Two design points worth keeping in view:

* **Cron has no year field.** A dated one-shot written as cron (``55 7 3 8 *``)
  is an ANNUAL trigger with a self-delete instruction stapled on. ``type:
  date`` exists to remove that trap.
* **Presence is the ledger.** A one-shot reminder still sitting in
  ``reminders.yaml`` with a past fire time *is* the record that delivery is
  owed. Delivery removes the entry. There is no second store to keep in sync.
* **The file is separate on purpose.** ``config_sync`` resolves an edited
  image-owned file against a changed shipped default as "image wins", so
  reminders kept in ``triggers.yaml`` would be deleted wholesale by the first
  update touching its default. See :func:`reminders_path`.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta

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


def new_reminder_name(taken: "set[str] | None" = None) -> str:
    """A fresh reminder trigger name, matching the schema's name pattern.

    *taken* is the set of names already in use. A collision would make
    registration fail on a duplicate job id, and the caller's rollback would
    then delete the PRE-EXISTING reminder of the same name along with its
    own — losing a reminder the user had already been promised. Avoiding the
    collision outright is the fix; the widened entropy just makes retries
    vanishingly rare.
    """
    taken = taken or set()
    for _ in range(10):
        name = f"{REMINDER_PREFIX}{secrets.token_hex(4)}"
        if name not in taken:
            return name
    raise ValueError("could not generate an unused reminder name")


def existing_names(path: str) -> set[str]:
    """Every trigger name currently in the reminder store at *path*."""
    try:
        return {t.get("name", "") for t in _load(path)["triggers"]}
    except (OSError, ValueError):
        return set()


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


def validate_recurring(at: datetime, repeat: str, tz=None) -> None:
    """Raise ValueError if *at* cannot be honoured EXACTLY as *repeat*.

    Refusing beats approximating. A cron expression has minute resolution and
    a fixed day-of-month, so a sub-minute anchor or a day that does not exist
    in every month can only be delivered by silently changing what the user
    asked for — and then the time they were told is not the time that fires.
    The caller surfaces this so the agent can ask for something expressible.
    """
    if repeat == "none":
        return
    if at.second or at.microsecond:
        raise ValueError(
            "a repeating reminder must fall on a whole minute; "
            f"{at.isoformat()} has seconds"
        )
    local = at.astimezone(tz) if tz is not None else at
    if repeat == "weekdays" and local.weekday() >= 5:
        raise ValueError(
            "a weekdays reminder cannot start on a Saturday or Sunday: the "
            "first occurrence would silently be the following Monday. Give "
            "the first weekday occurrence instead."
        )
    if repeat == "monthly" and local.day > 28:
        raise ValueError(
            f"a monthly reminder cannot fall on day {local.day}: that day is "
            "missing from some months, so the reminder would skip them. Use "
            "day 28 or earlier, or ask the user for a different day."
        )


def derive_schedule(at: datetime, repeat: str, tz=None) -> dict[str, str]:
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

    # This function does NO rounding and NO clamping. Three review rounds
    # produced a finding here every time — truncating seconds, then rounding
    # them up, then mapping day>28 to end-of-month — each fix creating the
    # next defect, because approximating a request silently makes the time the
    # user was told differ from the time that fires. Anything a cron cannot
    # express EXACTLY is refused by the caller instead (see
    # ``validate_recurring``), so what is promised is always what happens.
    #
    # The one transformation that remains is a conversion, not an
    # approximation: the wall-clock fields are read in the SCHEDULER's
    # timezone. The caller's offset pins which instant is meant; the cron is
    # evaluated in the scheduler's zone, so deriving the fields from the
    # caller's offset would misschedule by the difference whenever the two
    # disagree — and would drift across a DST boundary.
    local = at.astimezone(tz) if tz is not None else at

    minute, hour = local.minute, local.hour
    if repeat == "daily":
        schedule = f"{minute} {hour} * * *"
    elif repeat == "weekdays":
        schedule = f"{minute} {hour} * * mon-fri"
    elif repeat == "weekly":
        schedule = f"{minute} {hour} * * {_DOW_BY_WEEKDAY[local.weekday()]}"
    else:  # monthly
        schedule = f"{minute} {hour} {local.day} * *"
    # ``at`` is the FIRST occurrence and becomes the scheduler's start_date.
    # Without it, "every Thursday from the 20th" set on the 3rd would fire on
    # the 6th and 13th — two occurrences the user never asked for. It does NOT
    # drive recurrence: the cron fields above do, evaluated in the scheduler's
    # timezone, which is what keeps the series DST-correct.
    #
    # Callers must report THIS value back to the user rather than the one they
    # passed in: the two differ whenever the caller's offset and the
    # scheduler's timezone render different wall-clock times.
    return {"type": "cron", "schedule": schedule, "at": local.isoformat()}


# ---------------------------------------------------------------------------
# The entry store — reminder entries inside a role's reminders.yaml
# ---------------------------------------------------------------------------


def reminders_path(agents_dir: str, role: str) -> str:
    """Absolute path to *role*'s reminders.yaml. Residents only.

    Reminders deliberately do NOT live in ``triggers.yaml``, even though they
    are ordinary triggers and the loader merges them into the same list.
    ``config_sync``'s three-way reconcile treats an edited ``triggers.yaml``
    as a conflict once the image ships a changed default and resolves it
    "image wins" — which would silently delete every pending reminder on such
    an update, exactly the failure this whole feature exists to prevent.
    ``reminders.yaml`` is not in the defaults tree, so reconcile adopts it and
    never rewrites it.
    """
    return os.path.join(agents_dir, role, "reminders.yaml")


def _load(path: str) -> dict:
    """Read the store, folding every parse failure into ``ValueError``.

    ``load_yaml_no_aliases`` raises ``yaml.YAMLError`` or ``RecursionError``,
    neither of which is a ``ValueError`` — and its docstring says callers are
    expected to fold both into their own fail-closed error. Without that, a
    malformed store would escape every ``except (OSError, ValueError)`` here
    and abort the whole sweep, so later roles' overdue reminders would go
    undelivered until a pass that happened to avoid the bad file.
    """
    if not os.path.exists(path):
        return {"schema_version": 1, "triggers": []}
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        doc = load_yaml_no_aliases(text) or {}
    except (Exception, RecursionError) as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot parse: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: reminders.yaml is not a mapping")
    doc.setdefault("schema_version", 1)
    if not isinstance(doc.get("triggers"), list):
        doc["triggers"] = []
    # Normalize ONCE, here, so every consumer is inherently safe. Guarding
    # per-function does not work: a pass that hardened `past_due` alone left
    # `remove_entry` calling .get() on the same bad item, which aborted the
    # sweep right after a delivery — so the reminder was redelivered every
    # pass and later roles were skipped entirely. A non-mapping entry is
    # corruption in an agent-owned file; dropping it is also what makes the
    # file writable again.
    kept = [e for e in doc["triggers"] if isinstance(e, dict)]
    if len(kept) != len(doc["triggers"]):
        logger.warning(
            "reminders: %s contains %d non-mapping entr(ies); ignoring them",
            path, len(doc["triggers"]) - len(kept),
        )
        doc["triggers"] = kept
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


def all_entries(path: str) -> "list[dict] | None":
    """Every reminder entry in the store at *path*.

    Returns ``None`` when the store cannot be read — NOT an empty list. An
    empty list means "the store is empty", which authorises reverse
    reconciliation to drop every reminder job; a transient read error must
    never be allowed to say that, or one bad read would unschedule every
    recurring reminder until the next successful sweep.
    """
    try:
        return [e for e in _load(path)["triggers"]
                if is_reminder_name(e.get("name", ""))]
    except (OSError, ValueError):
        logger.warning("reminders: cannot read %s; skipping reconciliation",
                       path, exc_info=True)
        return None


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
        # A date trigger is one-shot BY DEFINITION, so membership is decided
        # on the type alone. Requiring the ``one_shot`` flag here as well
        # would mean an entry that somehow lacked it was skipped at
        # registration (past) AND skipped by the sweep — silently never
        # delivered. The schema forbids that shape; this does not depend on it.
        if entry.get("type") != "date":
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


# ---------------------------------------------------------------------------
# The sweep — INV-TRIG-008
# ---------------------------------------------------------------------------


async def sweep_reminders(runtime, now: datetime) -> int:
    """Deliver every overdue one-shot reminder and remove it. Returns the
    number delivered.

    This is the backstop for the gap ``docs/architecture/triggers.md`` names
    outright: the scheduler is configured with no persistent job store, so an
    occurrence whose fire time fell while the process was down was never
    recorded anywhere and is otherwise simply lost. The misfire grace period
    bounds lateness for a RUNNING process; it cannot resurrect what was never
    recorded.

    Delivery happens BEFORE removal, so a removal failure redelivers on the
    next pass. That is at-least-once by choice (spec §8) — a duplicate nudge
    is a far better failure than a missed reminder. Conversely a FAILED
    delivery must not remove the entry: the reminder is still owed.
    """
    from bus import BusMessage, MessageType
    from log_cid import new_cid

    registry = getattr(runtime, "trigger_registry", None)

    delivered = 0
    for role in list(getattr(runtime, "role_configs", {}) or {}):
        path = reminders_path(runtime.agents_dir, role)
        for entry in past_due(path, now):
            name = entry["name"]

            # Exclusive ownership. If the scheduler still holds a live job for
            # this reminder it WILL deliver it, so the sweep must not: for a
            # reminder whose time has only just passed, both are otherwise
            # eligible and the user gets it twice. After a restart there is no
            # job (they are memory-only and a past-dated one is never
            # registered), which is exactly when the sweep should act.
            if registry is not None and registry.has_job(role, name):
                continue

            # The sweep delivers the stored prompt verbatim — it has no
            # agent_dir to resolve a prompt_file against. The schema forbids
            # that combination, so an empty prompt here means a hand-edited or
            # corrupt entry. Refuse rather than send an empty message and
            # delete the evidence: a loud no-op beats silent loss.
            content = (entry.get("prompt") or "").strip()
            if not content:
                logger.warning(
                    "reminder sweep: %s has no prompt; leaving it in place "
                    "rather than delivering an empty message", name,
                )
                continue

            logger.info(
                "reminder sweep: delivering overdue %s for %s (due %s)",
                name, role, entry.get("at"),
            )
            try:
                await runtime.bus.send(BusMessage(
                    type=MessageType.SCHEDULED,
                    source="reminder-sweep",
                    target=role,
                    content=content,
                    channel=entry.get("channel", ""),
                    context={
                        "chat_id": f"date-{name}",
                        "trigger": name,
                        "cid": new_cid(),
                        "late": True,
                    },
                ))
            except Exception:  # noqa: BLE001
                # Still owed — leave the entry for the next pass.
                logger.warning(
                    "reminder sweep: delivery of %s failed; leaving it queued",
                    name, exc_info=True,
                )
                continue
            delivered += 1
            try:
                remove_entry(path, name)
            except (OSError, ValueError):
                logger.warning(
                    "reminder sweep: could not remove %s after delivery; it "
                    "will be redelivered next pass", name, exc_info=True,
                )

        _reconcile_registrations(runtime, registry, role, path, now)

    return delivered


def _reconcile_registrations(runtime, registry, role: str, path: str,
                             now: datetime) -> None:
    """Re-register any reminder in the store that has no live job.

    The store is the truth; the scheduler is a cache of it. Anything that can
    make the two diverge — a reload re-registering a role from a snapshot
    taken before a reminder was written, a registration that failed
    transiently — is healed here rather than needing its own lock. Without
    this a recurring reminder lost that way would never fire again until the
    next restart, because only one-shots are recoverable by delivery.
    """
    if registry is None:
        return
    from config import TriggerSpec

    channels = list(getattr(
        getattr(runtime, "role_configs", {}).get(role), "channels", []) or [])
    if not channels:
        return

    entries = all_entries(path)
    if entries is None:
        # Store unreadable: neither direction is safe. Dropping would
        # unschedule live reminders; registering would work from nothing.
        return

    # Direction 1: a job with no entry left in the store must go. A
    # cancellation that raced a reload — which re-registers the role from a
    # snapshot taken before the cancellation — would otherwise leave the
    # reminder firing forever, even though cancel_reminder reported success.
    live_names = {e.get("name", "") for e in entries}
    # Only jobs the registry recorded as coming FROM THIS STORE are candidates
    # for removal. Provenance is carried as data on the spec, not inferred
    # from the name or by re-reading the operator's file — the schema requires
    # every date trigger to carry the reminder prefix, so an operator may
    # legitimately author one, and three rounds of inference each found a new
    # way to delete a live operator trigger.
    try:
        registered = registry.reminder_job_names(role)
    except Exception:  # noqa: BLE001 - older registry without the accessor
        registered = []
    for name in registered:
        if name not in live_names:
            logger.info(
                "reminder sweep: dropping job %s for %s — no longer in the "
                "store", name, role,
            )
            registry.remove_job_for(role, name)

    # Direction 2: an entry with no job must be registered.
    for entry in entries:
        name = entry.get("name", "")
        if registry.has_job(role, name):
            continue
        if entry.get("type") == "date":
            # A past-dated one-shot is the sweep's to deliver, not to
            # register; a future one genuinely needs a job.
            try:
                if parse_at(entry.get("at", "")) <= now:
                    continue
            except ValueError:
                continue
        try:
            registry.register_agent(role, [TriggerSpec(
                name=name, type=entry.get("type", ""),
                schedule=entry.get("schedule", ""), at=entry.get("at", ""),
                one_shot=bool(entry.get("one_shot", False)),
                channel=entry.get("channel", ""),
                prompt=entry.get("prompt", ""),
                from_reminder_store=True,
            )], channels)
            logger.info(
                "reminder sweep: re-registered %s for %s (no live job)",
                name, role,
            )
        except Exception:  # noqa: BLE001 - one bad entry must not stop the rest
            logger.warning(
                "reminder sweep: could not re-register %s for %s",
                name, role, exc_info=True,
            )
