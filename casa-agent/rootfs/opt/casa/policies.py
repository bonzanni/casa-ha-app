"""Disclosure-policy loader + resolver + renderer.

One policy file (``policies/disclosure.yaml``) is loaded once at boot.
Each resident's ``disclosure.yaml`` references a policy by name and may
apply overrides at the top level. The resolved policy is rendered into
a ``### Disclosure`` section appended to the agent's system prompt.
"""

from __future__ import annotations

import os
from typing import Any

import yaml
import jsonschema


class PolicyError(Exception):
    """Raised on any policy load/resolve failure."""


class PolicyLibrary:
    def __init__(self, policies: dict[str, dict[str, Any]]) -> None:
        self._policies = policies

    def names(self) -> list[str]:
        return list(self._policies.keys())

    def resolve(self, name: str, overrides: dict[str, Any]) -> dict[str, Any]:
        """Return the policy dict with top-level keys overridden.

        Overrides are shallow — a caller that supplies
        ``{"safe_on_any_channel": [...]}`` replaces the whole list, not
        the items inside. Spec design §policies/disclosure.yaml says
        overrides are "applying to the resolved policy before rendering";
        shallow semantics are the simplest faithful interpretation and
        keep rendering deterministic.
        """
        base = self._policies.get(name)
        if base is None:
            raise PolicyError(
                f"unknown policy {name!r}; available: {sorted(self._policies)}"
            )
        resolved = dict(base)
        resolved.update(overrides or {})
        return resolved


def _load_schema() -> dict[str, Any]:
    """Read the on-disk schema for policy files.

    Ships at ``/opt/casa/defaults/schema/policy-disclosure.v1.json`` in
    the container. Tests monkeypatch ``POLICY_SCHEMA_PATH`` when needed.
    """
    with open(POLICY_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        import json
        return json.load(fh)


# Resolved at import time. Tests override with monkeypatch.setattr.
POLICY_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "defaults", "schema", "policy-disclosure.v1.json",
)


def load_policies(path: str) -> PolicyLibrary:
    """Load and validate the policy file at *path*. Raises on any error."""
    if not os.path.exists(path):
        raise PolicyError(f"policy file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"could not parse {path}: {exc}") from exc

    schema = _load_schema()
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        # jsonschema surfaces "Additional properties are not allowed"
        # for unknown fields; the test asserts on "additional" casefold.
        raise PolicyError(
            f"{path}: schema violation: {exc.message.casefold()}"
        ) from exc

    return PolicyLibrary(data["policies"])


def render_disclosure_section(resolved: dict[str, Any]) -> str:
    """Render a resolved policy dict as a system-prompt section.

    Deterministic: categories emitted in insertion order; deflection
    patterns likewise. The composed text is appended verbatim to the
    agent's system prompt by :mod:`agent_loader`.
    """
    lines: list[str] = ["### Disclosure", ""]

    cats = resolved.get("categories") or {}
    if cats:
        lines.append("Confidential on untrusted channels:")
        for cat_name, cat in cats.items():
            examples = ", ".join(cat.get("examples") or [])
            lines.append(f"- {cat_name.capitalize()} ({examples})")
        lines.append("")

    safe = resolved.get("safe_on_any_channel") or []
    if safe:
        lines.append("Safe on any channel: " + ", ".join(safe) + ".")
        lines.append("")

    deflections = resolved.get("deflection_patterns") or {}
    if deflections:
        lines.append("Deflection patterns:")
        for key, text in deflections.items():
            lines.append(f"- {key}: \"{text}\"")

    return "\n".join(lines).rstrip() + "\n"
