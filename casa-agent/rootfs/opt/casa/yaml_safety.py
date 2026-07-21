"""Shared hardened YAML loader for authored (operator- or image-owned)
content: persona packs (persona_pack.py) and role artifacts
(role_artifact.py).

Both loaders parse an adversarial trust boundary with plain
``yaml.safe_load``, which admits YAML aliases (``&anchor`` / ``*alias``).
An anchor with no alias is harmless (it is just a label; it produces no
extra reuse), but an alias lets an authored document build a DAG that is
tiny on disk and shallow (well under any depth cap) yet EXPANDS to an
astronomically large tree once walked node-by-node — e.g. chaining
``&a1 [*a0, *a0]`` through ``&a30`` yields ~2^30 leaves. `canonical_bytes`'s
path-local guards (``assert_json_safe``'s cycle check, ``deep_freeze``'s
depth bound) correctly allow ordinary DAGs and reject true cycles, but they
still have to WALK — and in `deep_freeze`'s case, MATERIALIZE — every one
of those expanded nodes, which is a CPU + memory DoS by the time the walk
reaches them.

Canonical role/persona content is authored as a tree; aliases have no
legitimate authoring use and are the only way YAML can express reuse or a
cycle. `_NoAliasSafeLoader` forbids aliases outright at parse time, making
the DAG-expansion and self-referential-cycle attacks both impossible
before any of the downstream guards ever run.
"""

from __future__ import annotations

import yaml
from yaml.events import AliasEvent


class _NoAliasSafeLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that raises on any YAML alias (`*name`) node.

    Anchors (`&name`) are left alone — an anchor with no alias is inert,
    so rejecting only the alias (the actual reuse mechanism) is sufficient
    and avoids over-rejecting harmless authored YAML.
    """

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            event = self.peek_event()
            raise yaml.constructor.ConstructorError(
                None, None,
                "YAML aliases are not permitted in authored content",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def load_yaml_no_aliases(text: str):
    """Parse *text* with `_NoAliasSafeLoader`.

    Raises `yaml.YAMLError` (via `_NoAliasSafeLoader`'s
    `yaml.constructor.ConstructorError`, a `yaml.YAMLError` subclass) if
    *text* contains a YAML alias, and may raise `yaml.YAMLError` or
    `RecursionError` for any other malformed/pathological input — callers
    parsing untrusted content are expected to wrap this call and fold both
    into their own generic fail-closed error (see role_artifact.py,
    persona_pack.py: foundation review r3, F-B/F-C)."""
    return yaml.load(text, Loader=_NoAliasSafeLoader)
