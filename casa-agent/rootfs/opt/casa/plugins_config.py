"""Loader + dataclass for defaults/agents/<role>/plugins.yaml (Plan 4b §B.1).

Shape validated against schema/plugins.v1.json. Returns a dataclass; callers
iterate .plugins or use .iter_refs() to get "<name>@<marketplace>" strings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

logger = logging.getLogger(__name__)

_ALLOWED_MARKETPLACES = {"casa-plugins-defaults", "casa-plugins"}


class PluginsConfigError(ValueError):
    """Raised when plugins.yaml fails schema validation."""


@dataclass(frozen=True)
class PluginEntry:
    name: str
    marketplace: str
    version: str | None = None

    def ref(self) -> str:
        return f"{self.name}@{self.marketplace}"


@dataclass(frozen=True)
class PluginsConfig:
    plugins: list[PluginEntry]

    def iter_refs(self) -> Iterator[str]:
        for p in self.plugins:
            yield p.ref()


def load_plugins_yaml(path: Path) -> PluginsConfig:
    if not path.is_file():
        return PluginsConfig(plugins=[])

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if raw.get("schema_version") != 1:
        raise PluginsConfigError(
            f"{path}: unsupported schema_version {raw.get('schema_version')!r} (expected 1)"
        )

    entries: list[PluginEntry] = []
    for item in raw.get("plugins", []) or []:
        if not isinstance(item, dict):
            raise PluginsConfigError(f"{path}: plugin entry must be mapping, got {type(item).__name__}")
        name = item.get("name")
        marketplace = item.get("marketplace")
        version = item.get("version")
        if not isinstance(name, str) or not name:
            raise PluginsConfigError(f"{path}: plugin entry missing 'name'")
        if marketplace not in _ALLOWED_MARKETPLACES:
            raise PluginsConfigError(
                f"{path}: plugin {name!r}: marketplace must be one of "
                f"{sorted(_ALLOWED_MARKETPLACES)}, got {marketplace!r}"
            )
        if version is not None and not isinstance(version, str):
            raise PluginsConfigError(f"{path}: plugin {name!r}: version must be string")
        entries.append(PluginEntry(name=name, marketplace=marketplace, version=version))

    return PluginsConfig(plugins=entries)
