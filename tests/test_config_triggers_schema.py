"""triggers schema v2: per-trigger auth + clearance, path removal (Release A, Task 1)."""
from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

SCHEMA = json.loads(pathlib.Path(
    "casa/rootfs/opt/casa/defaults/schema/triggers.v1.json").read_text())


def _doc(trigger: dict, version: int = 2) -> dict:
    return {"schema_version": version, "triggers": [trigger]}


def test_v2_static_header_ok():
    jsonschema.validate(_doc({
        "name": "vm", "type": "webhook",
        "auth": {"mode": "static_header", "header": "X-API-Key"}}), SCHEMA)


def test_v2_timestamped_hmac_provider_ok():
    jsonschema.validate(_doc({
        "name": "vm", "type": "webhook",
        "auth": {"mode": "timestamped_hmac", "secret_owner": "provider",
                 "tolerance_secs": 300}}), SCHEMA)


def test_v2_webhook_no_auth_ok():
    # auth optional → synthesized as hmac_body by the loader.
    jsonschema.validate(_doc({"name": "vm", "type": "webhook"}), SCHEMA)


def test_v2_clearance_public_ok_private_rejected():
    jsonschema.validate(_doc({"name": "vm", "type": "webhook",
                              "clearance": "family"}), SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({"name": "vm", "type": "webhook",
                                  "clearance": "private"}), SCHEMA)


def test_v2_provider_requires_timestamped_hmac():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({
            "name": "vm", "type": "webhook",
            "auth": {"mode": "static_header", "secret_owner": "provider"}}), SCHEMA)


def test_v2_path_rejected():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({"name": "vm", "type": "webhook",
                                  "path": "/hooks/x"}), SCHEMA)


def test_v1_path_still_ok():
    jsonschema.validate(_doc({"name": "vm", "type": "webhook",
                              "path": "/hooks/x"}, version=1), SCHEMA)


def test_v1_webhook_without_path_rejected():
    # v1 semantics unchanged: webhook requires path.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({"name": "vm", "type": "webhook"}, version=1),
                            SCHEMA)


def test_tolerance_bounds():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({
            "name": "vm", "type": "webhook",
            "auth": {"mode": "timestamped_hmac", "tolerance_secs": 10}}), SCHEMA)


def test_interval_trigger_still_validates():
    jsonschema.validate(_doc({
        "name": "hb", "type": "interval", "minutes": 5,
        "channel": "telegram", "prompt": "ping"}), SCHEMA)


def test_user_trigger_name_cannot_use_reserved_plg_prefix():
    # Release B: 'plg-' is reserved for plugin-declared triggers; a user
    # trigger claiming it would collide in the shared webhook namespace.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({"name": "plg-x", "type": "webhook",
                                  "auth": {"mode": "static_header"}}), SCHEMA)


def test_user_trigger_normal_name_still_ok():
    jsonschema.validate(_doc({"name": "plgx", "type": "webhook",
                              "auth": {"mode": "static_header"}}), SCHEMA)
    jsonschema.validate(_doc({"name": "my-plg", "type": "webhook",
                              "auth": {"mode": "static_header"}}), SCHEMA)


class TestNameFitsTheProvenancePeerBound:
    """#204 (Sol, review r1): the trigger name becomes the automation peer
    ``webhook:{name}``, which must fit the ``user_peer`` provenance bound (256
    scalars). Without a schema cap, a long-but-legal name registered fine,
    authenticated fine, and then failed identity stamping on EVERY request —
    turning a config mistake into a permanently broken webhook discovered only
    in production. The cap belongs at config time, where the operator sees it.
    """

    def test_a_name_that_would_overflow_the_peer_bound_is_rejected(self):
        # 249 is the first length that cannot fit: "webhook:" (8) + 249 = 257,
        # one past the 256-scalar user_peer bound.
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                _doc({"name": "n" * 249, "type": "webhook"}), SCHEMA)

    def test_the_cap_is_the_exact_derived_bound_not_a_round_number(self):
        # Sol, re-review r2: an arbitrarily tighter cap would reject names
        # that are in fact stampable. The schema cap must BE the constraint.
        import sys

        sys.path.insert(0, "casa/rootfs/opt/casa")
        import ingress_identity as ii

        cap = SCHEMA["properties"]["triggers"]["items"][
            "properties"]["name"]["maxLength"]
        assert cap == ii._PEER_MAX_SCALARS - len(ii._WEBHOOK_PEER_PREFIX)

    def test_every_schema_legal_name_can_be_stamped(self):
        # The property that matters: schema-legal implies stampable. Pins the
        # schema cap and the provenance bound together so they cannot drift
        # apart silently.
        import sys

        sys.path.insert(0, "casa/rootfs/opt/casa")
        from ingress_identity import ingress_identity

        longest = "n" * SCHEMA["properties"]["triggers"]["items"][
            "properties"]["name"]["maxLength"]
        jsonschema.validate(_doc({"name": longest, "type": "webhook"}), SCHEMA)
        origin = ingress_identity("webhook_trigger", webhook_name=longest)
        assert origin.user_peer == "webhook:" + longest


# --- #396: point-in-time reminders (type=date, at, one_shot) ---------------


def test_date_trigger_validates():
    jsonschema.validate(_doc({
        "name": "reminder-a1b2c3", "type": "date",
        "at": "2026-08-03T08:00:00+02:00", "one_shot": True,
        "channel": "telegram",
        "prompt": 'Send this exact message via telegram: "Bins."'}, 1), SCHEMA)


def test_date_trigger_requires_at():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({
            "name": "reminder-a1b2c3", "type": "date",
            "channel": "telegram", "prompt": "x"}, 1), SCHEMA)


def test_date_trigger_requires_channel():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({
            "name": "reminder-a1b2c3", "type": "date",
            "at": "2026-08-03T08:00:00+02:00", "prompt": "x"}, 1), SCHEMA)


def test_date_trigger_rejects_both_prompt_and_prompt_file():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({
            "name": "reminder-a1b2c3", "type": "date",
            "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
            "prompt": "x", "prompt_file": "prompts/x.md"}, 1), SCHEMA)


def test_cron_trigger_may_carry_one_shot():
    jsonschema.validate(_doc({
        "name": "reminder-a1b2c3", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "telegram", "prompt": "x"}, 1), SCHEMA)


def test_date_trigger_valid_under_schema_version_2():
    jsonschema.validate(_doc({
        "name": "reminder-a1b2c3", "type": "date",
        "at": "2026-08-03T08:00:00+02:00", "one_shot": True,
        "channel": "telegram", "prompt": "x"}, 2), SCHEMA)


# --- #396 round 2: one_shot <=> date, and date names are reminder-bound ----


def test_date_trigger_requires_one_shot_true():
    """A date trigger without one_shot would be skipped at registration when
    past AND skipped by the sweep — silently never delivered."""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({
            "name": "reminder-a1b2c3", "type": "date",
            "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
            "prompt": "x"}, 1), SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({
            "name": "reminder-a1b2c3", "type": "date", "one_shot": False,
            "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
            "prompt": "x"}, 1), SCHEMA)


def test_a_date_trigger_name_need_not_carry_the_reminder_prefix():
    """#398 release 2: the ``^reminder-`` requirement is DELETED.

    It only ever existed because post-fire cleanup was prefix-bounded. Cleanup
    is now bounded by ``managed_by``, so the prefix constrains nothing — and
    requiring it was actively harmful: it forced an operator's own dated
    one-shot to look agent-owned, which is the shape three rounds of #396
    findings came from.
    """
    jsonschema.validate(_doc({
        "name": "garbage-day", "type": "date", "one_shot": True,
        "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
        "prompt": "x"}, 1), SCHEMA)


def test_date_trigger_forbids_prompt_file():
    """The sweep delivers the stored prompt verbatim; it has no agent_dir to
    resolve a file against, so a prompt_file reminder would send nothing."""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc({
            "name": "reminder-a1b2c3", "type": "date", "one_shot": True,
            "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
            "prompt_file": "prompts/x.md"}, 1), SCHEMA)


def test_one_shot_true_is_rejected_on_cron_and_interval():
    """one_shot on a non-date trigger promises a self-removal that cannot
    happen: the entry survives and re-fires after every restart."""
    for trig in (
        {"name": "reminder-a1b2c3", "type": "cron", "schedule": "0 7 * * thu",
         "one_shot": True, "channel": "telegram", "prompt": "x"},
        {"name": "reminder-a1b2c3", "type": "interval", "minutes": 60,
         "one_shot": True, "channel": "telegram", "prompt": "x"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_doc(trig, 1), SCHEMA)


# --- #398 release 2: managed_by is provenance as DATA ----------------------
#
# The whole reason this field exists: with reminders folded into triggers.yaml,
# "agent-owned" can no longer be derived from which file an entry came from,
# and it must not be derived from the entry's own content either. The schema's
# job is to make the field expressible and to stop constraining the name.


class TestManagedBy:
    _BASE = {
        "name": "reminder-a1b2c3", "type": "date", "one_shot": True,
        "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
        "prompt": "x",
    }

    @pytest.mark.parametrize("version", [1, 2])
    def test_agent_is_accepted_on_a_date_trigger(self, version):
        jsonschema.validate(
            _doc({**self._BASE, "managed_by": "agent"}, version), SCHEMA)

    def test_agent_is_accepted_on_cron_and_interval(self):
        # A RECURRING reminder is a cron trigger, so the field cannot be
        # date-only. Nothing ties managed_by to one_shot either.
        jsonschema.validate(_doc({
            "name": "reminder-a1b2c3", "type": "cron",
            "schedule": "0 7 * * thu", "channel": "telegram", "prompt": "x",
            "managed_by": "agent"}, 1), SCHEMA)
        jsonschema.validate(_doc({
            "name": "reminder-a1b2c3", "type": "interval", "minutes": 60,
            "channel": "telegram", "prompt": "x",
            "managed_by": "agent"}, 1), SCHEMA)

    def test_any_other_owner_is_refused(self):
        # The enum is deliberately a single value. "operator" as a marker would
        # invert the default: an entry with no field at all is the operator's,
        # and that must stay the only reading.
        for bad in ("operator", "casa", "", "Agent"):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(
                    _doc({**self._BASE, "managed_by": bad}, 1), SCHEMA)

    def test_a_non_string_is_refused(self):
        for bad in (True, 1, None, ["agent"], {"who": "agent"}):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(
                    _doc({**self._BASE, "managed_by": bad}, 1), SCHEMA)

    def test_absent_is_still_valid(self):
        """The change is purely ADDITIVE — no version bump, no migration.

        Every document that validated before this release still validates, and
        an entry with no ``managed_by`` is the operator's by default.
        """
        jsonschema.validate(_doc(self._BASE, 1), SCHEMA)

    def test_every_shipped_default_still_validates(self):
        """Additive means additive: the image's own files are the regression
        surface, and a tightening here would be boot-fatal (spec §7.1)."""
        import glob

        import yaml
        found = glob.glob(
            "casa/rootfs/opt/casa/defaults/agents/*/triggers.yaml")
        assert found, "no shipped triggers.yaml found — the guard is inert"
        for path in found:
            with open(path, encoding="utf-8") as fh:
                jsonschema.validate(yaml.safe_load(fh), SCHEMA)

    def test_the_field_does_not_relax_the_shape_rules(self):
        """managed_by is provenance, not a permission to be malformed."""
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_doc({
                "name": "reminder-a1b2c3", "type": "date", "one_shot": False,
                "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
                "prompt": "x", "managed_by": "agent"}, 1), SCHEMA)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_doc({
                "name": "reminder-a1b2c3", "type": "date", "one_shot": True,
                "channel": "telegram", "prompt": "x",
                "managed_by": "agent"}, 1), SCHEMA)
