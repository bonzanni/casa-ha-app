"""triggers schema v2: per-trigger auth + clearance, path removal (Release A, Task 1)."""
from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

SCHEMA = json.loads(pathlib.Path(
    "casa-agent/rootfs/opt/casa/defaults/schema/triggers.v1.json").read_text())


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
