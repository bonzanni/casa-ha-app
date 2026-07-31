#!/usr/bin/env python3
"""The code-derived coverage ledger.

The corpus can only claim completeness against a surface list that comes from the code
itself — a hand-maintained list rots the day after it is written. This script enumerates,
mechanically:

* every ``.py`` of ≥100 lines under ``casa/rootfs/opt/casa/`` (the substantial modules),
* every ``options:`` / ``schema:`` key in ``casa/config.yaml``,
* every s6 unit directory under ``casa/rootfs/etc/s6-overlay/s6-rc.d/``,
* every tool in ``tools.py``'s ``CASA_TOOLS`` tuple,
* every HTTP route registration (``add_get``/``add_post``/``add_route``/``add_routes``
  call sites, by AST, so comments and docstrings cannot fake one).

``docs/coverage.yaml`` must map every enumerated item to the corpus document that covers
it, or exclude it with a one-line reason. The check is bidirectional, like the manifest:
an enumerated item absent from the ledger fails, a ledger item no longer enumerated
fails, and a ``doc:`` the manifest does not know fails.

Usage:
    python3 scripts/coverage_ledger.py enumerate [repo_root]
    python3 scripts/coverage_ledger.py check [repo_root]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

CODE_ROOT = "casa/rootfs/opt/casa"
S6_ROOT = "casa/rootfs/etc/s6-overlay/s6-rc.d"
SCRIPTS_ROOT = "casa/rootfs/etc/s6-overlay/scripts"
SCHEMA_ROOT = "casa/rootfs/opt/casa/defaults/schema"
DOCKERFILE = "casa/Dockerfile"
CONFIG_YAML = "casa/config.yaml"
TOOLS_MODULE = "tools.py"
# Every module counts — a size floor made ~40 small modules invisible to the
# ledger, and small is not the same as uninteresting (webhook_auth.py is tiny).
MIN_MODULE_LINES = 0

# aiohttp registration spellings. ``add_route(method, path)`` carries its method as the
# first argument; ``add_routes([web.get(path, h), …])`` nests them in a list.
DIRECT_METHODS = {"add_get": "GET", "add_post": "POST", "add_put": "PUT",
                  "add_delete": "DELETE", "add_patch": "PATCH", "add_head": "HEAD"}
ROUTEDEF_NAMES = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
                  "patch": "PATCH", "head": "HEAD", "route": "ROUTE"}


def _expr_text(node: ast.AST) -> str:
    """A path literal yields its value; anything else yields its source text — a dynamic
    path is still a surface, and skipping it would let a whole route family go
    unledgered."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ast.unparse(node)


def enumerate_modules(repo_root: Path) -> list[str]:
    root = repo_root / CODE_ROOT
    out = []
    for path in sorted(root.rglob("*.py")):
        try:
            loc = len(path.read_text(errors="replace").splitlines())
        except OSError:
            continue
        if loc >= MIN_MODULE_LINES:
            out.append(str(path.relative_to(repo_root)))
    return out


def enumerate_options(repo_root: Path) -> list[str]:
    try:
        data = yaml.safe_load((repo_root / CONFIG_YAML).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    keys: set[str] = set()
    for section in ("options", "schema"):
        value = data.get(section)
        if isinstance(value, dict):
            keys.update(value)
    return [f"option:{key}" for key in sorted(keys)]


def enumerate_s6(repo_root: Path) -> list[str]:
    root = repo_root / S6_ROOT
    if not root.is_dir():
        return []
    return [f"s6:{p.name}" for p in sorted(root.iterdir()) if p.is_dir()]


def enumerate_tools(repo_root: Path) -> list[str]:
    """The identifiers in the CASA_TOOLS tuple. They are bare function names, not
    ``name=`` keywords — read from the AST, so a commented-out entry does not count."""
    path = repo_root / CODE_ROOT / TOOLS_MODULE
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        if not any(t.id == "CASA_TOOLS" for t in targets):
            continue
        value = node.value
        if not isinstance(value, ast.Tuple):
            return []
        names = []
        for element in value.elts:
            if isinstance(element, ast.Name):
                names.append(f"tool:{element.id}")
            else:
                names.append(f"tool:{ast.unparse(element)}")
        return sorted(set(names))
    return []


def enumerate_routes(repo_root: Path) -> list[str]:
    root = repo_root / CODE_ROOT
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(repo_root))
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            name = node.func.attr
            if name in DIRECT_METHODS and node.args:
                found.add(f"route:{rel}:{DIRECT_METHODS[name]}:{_expr_text(node.args[0])}")
            elif name == "add_route" and len(node.args) >= 2:
                method = _expr_text(node.args[0]).upper()
                found.add(f"route:{rel}:{method}:{_expr_text(node.args[1])}")
            elif name == "add_routes" and node.args:
                container = node.args[0]
                elements = container.elts if isinstance(container, (ast.List, ast.Tuple)) else []
                for element in elements:
                    if (
                        isinstance(element, ast.Call)
                        and isinstance(element.func, ast.Attribute)
                        and element.func.attr in ROUTEDEF_NAMES
                        and element.args
                    ):
                        method = ROUTEDEF_NAMES[element.func.attr]
                        found.add(f"route:{rel}:{method}:{_expr_text(element.args[0])}")
                    else:
                        found.add(f"route:{rel}:?:{ast.unparse(element)}")
    return sorted(found)


def enumerate_env_reads(repo_root: Path) -> list[str]:
    """Every environment variable the code reads by literal name, by AST —
    ``os.environ.get("X")``, ``os.environ["X"]`` and ``os.getenv("X")``. Env
    vars are the classic undocumented surface: a tunable nobody wrote down."""
    root = repo_root / CODE_ROOT
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                func = node.func
                base = func.value
                is_env_get = (
                    func.attr == "get"
                    and isinstance(base, ast.Attribute) and base.attr == "environ"
                )
                is_getenv = (
                    func.attr == "getenv"
                    and isinstance(base, ast.Name) and base.id == "os"
                )
                if (
                    (is_env_get or is_getenv)
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    names.add(node.args[0].value)
            elif isinstance(node, ast.Subscript):
                value = node.value
                if (
                    isinstance(value, ast.Attribute) and value.attr == "environ"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                ):
                    names.add(node.slice.value)
    return [f"env:{name}" for name in sorted(names)]


def enumerate_scripts(repo_root: Path) -> list[str]:
    root = repo_root / SCRIPTS_ROOT
    if not root.is_dir():
        return []
    return [f"script:{p.name}" for p in sorted(root.iterdir()) if p.is_file()]


def enumerate_schemas(repo_root: Path) -> list[str]:
    root = repo_root / SCHEMA_ROOT
    if not root.is_dir():
        return []
    return [f"schema:{p.name}" for p in sorted(root.iterdir()) if p.is_file()]


def enumerate_dockerfile(repo_root: Path) -> list[str]:
    return [DOCKERFILE] if (repo_root / DOCKERFILE).is_file() else []


def enumerate_items(repo_root: Path) -> list[str]:
    return (
        enumerate_modules(repo_root)
        + enumerate_options(repo_root)
        + enumerate_s6(repo_root)
        + enumerate_tools(repo_root)
        + enumerate_routes(repo_root)
        + enumerate_env_reads(repo_root)
        + enumerate_scripts(repo_root)
        + enumerate_schemas(repo_root)
        + enumerate_dockerfile(repo_root)
    )


# --- the check ------------------------------------------------------------------------

def _load_ledger(repo_root: Path) -> tuple[list[dict], list[str]]:
    path = repo_root / "docs" / "coverage.yaml"
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError:
        return [], ["docs/coverage.yaml is missing — the coverage ledger is mandatory"]
    except yaml.YAMLError as exc:
        return [], [f"docs/coverage.yaml is not valid YAML: {exc}"]
    if not isinstance(raw, list):
        return [], ["docs/coverage.yaml must be a list of entries"]
    entries, problems = [], []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or not isinstance(entry.get("item"), str):
            problems.append(f"coverage entry {index} is not a mapping with a string `item`")
            continue
        entries.append(entry)
    return entries, problems


def _manifest_docs(repo_root: Path) -> set[str]:
    try:
        raw = yaml.safe_load((repo_root / "docs" / "manifest.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {e["doc"] for e in raw if isinstance(e, dict) and isinstance(e.get("doc"), str)}


def check(repo_root: Path) -> list[str]:
    """Return every coverage problem. Empty list means every surface is accounted for."""
    entries, problems = _load_ledger(repo_root)
    if problems and not entries:
        return problems
    manifest = _manifest_docs(repo_root)
    enumerated = set(enumerate_items(repo_root))

    seen: set[str] = set()
    for entry in entries:
        item = entry["item"]
        if item in seen:
            problems.append(f"coverage: {item!r} is listed twice")
        seen.add(item)
        doc, excluded = entry.get("doc"), entry.get("excluded")
        if (doc is None) == (excluded is None):
            problems.append(
                f"coverage: {item!r} must carry exactly one of `doc` or `excluded`"
            )
            continue
        if doc is not None and doc not in manifest:
            problems.append(
                f"coverage: {item!r} is assigned to {doc!r}, which is not in the manifest"
            )
        if excluded is not None and (not isinstance(excluded, str) or not excluded.strip()):
            problems.append(
                f"coverage: {item!r} is excluded without a reason — every exclusion "
                f"states one"
            )

    for item in sorted(enumerated - seen):
        problems.append(
            f"coverage: {item!r} exists in the code but is not in docs/coverage.yaml — "
            f"assign it to a document or exclude it with a reason"
        )
    for item in sorted(seen - enumerated):
        problems.append(
            f"coverage: {item!r} is in the ledger but no longer enumerated from the "
            f"code — remove the stale entry"
        )
    return problems


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("enumerate", "check"):
        print(__doc__)
        return 2
    root = Path(args[1] if len(args) > 1 else ".").resolve()
    if args[0] == "enumerate":
        for item in enumerate_items(root):
            print(item)
        return 0
    problems = check(root)
    for problem in problems:
        print(f"✗ {problem}")
    if problems:
        print(f"\n{len(problems)} coverage problem(s).")
        return 1
    print("✓ coverage ledger verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
