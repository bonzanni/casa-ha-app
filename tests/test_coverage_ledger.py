"""The code-derived coverage ledger.

Enumeration comes from the code, never from a hand list; the ledger maps every
enumerated surface to the corpus document that covers it, or records an explicit
exclusion. Both directions are checked, like the manifest.
"""
import importlib.util
from pathlib import Path

# Loaded by explicit path for the same reason as test_verify_docs.py: the app code root
# on sys.path contains its own scripts/ directory which shadows the repo-root one.
_spec = importlib.util.spec_from_file_location(
    "casa_coverage_ledger",
    Path(__file__).resolve().parents[1] / "scripts" / "coverage_ledger.py",
)
coverage_ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coverage_ledger)

BIG_MODULE = "# module\n" + "x = 1\n" * 120
SMALL_MODULE = "x = 1\n"

TOOLS_PY = (
    "def send_message():\n    pass\n\n"
    "def react():\n    pass\n\n"
    "CASA_TOOLS: tuple = (\n    send_message,\n    react,\n)\n"
    + "# padding\n" * 120
)

CONFIG_YAML = """
options:
  log_level: info
schema:
  log_level: str
  new_key: str
"""

ROUTES_PY = (
    "def build(app, dynamic_path):\n"
    "    app.router.add_get('/healthz', h)\n"
    "    app.router.add_post('/invoke/{agent}', h)\n"
    "    app.router.add_post(dynamic_path, h)\n"
    + "# padding\n" * 120
)

MANIFEST = """
- doc: manifest.yaml
  kind: meta
  summary: Allowlist.
- doc: architecture/thing.md
  summary: A doc.
  when_changing: things
"""


def _repo(tmp_path: Path, ledger: str | None = None) -> Path:
    code = tmp_path / "casa" / "rootfs" / "opt" / "casa"
    code.mkdir(parents=True)
    (code / "big.py").write_text(BIG_MODULE)
    (code / "small.py").write_text(SMALL_MODULE)
    (code / "tools.py").write_text(TOOLS_PY)
    (code / "routes_mod.py").write_text(ROUTES_PY)
    (tmp_path / "casa" / "config.yaml").write_text(CONFIG_YAML)
    s6 = tmp_path / "casa" / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d"
    (s6 / "svc-casa").mkdir(parents=True)
    (s6 / "init-setup").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manifest.yaml").write_text(MANIFEST)
    if ledger is not None:
        (tmp_path / "docs" / "coverage.yaml").write_text(ledger)
    return tmp_path


def _full_ledger_for(root: Path) -> str:
    lines = []
    for item in coverage_ledger.enumerate_items(root):
        lines.append(f"- item: {item}\n  doc: architecture/thing.md\n")
    return "".join(lines)


# --- enumeration is mechanical, from code ---------------------------------------------

def test_enumeration_covers_every_surface_kind(tmp_path):
    items = coverage_ledger.enumerate_items(_repo(tmp_path))
    assert "casa/rootfs/opt/casa/big.py" in items          # every module —
    assert "casa/rootfs/opt/casa/small.py" in items        # no size floor
    assert "option:log_level" in items                     # options: key
    assert "option:new_key" in items                       # schema:-only key still counts
    assert "s6:svc-casa" in items and "s6:init-setup" in items
    assert "tool:send_message" in items and "tool:react" in items
    assert "route:casa/rootfs/opt/casa/routes_mod.py:GET:/healthz" in items
    assert "route:casa/rootfs/opt/casa/routes_mod.py:POST:/invoke/{agent}" in items


def test_enumeration_covers_env_scripts_schemas_and_dockerfile(tmp_path):
    """Env reads are AST-enumerated (a commented read does not count), boot
    scripts, schema files and the Dockerfile are surfaces too."""
    root = _repo(tmp_path)
    code = root / "casa" / "rootfs" / "opt" / "casa"
    (code / "envuser.py").write_text(
        "import os\n"
        'A = os.environ.get("CASA_PROBE_A")\n'
        'B = os.environ["CASA_PROBE_B"]\n'
        'C = os.getenv("CASA_PROBE_C")\n'
        '# os.environ.get("CASA_PROBE_COMMENTED")\n'
        "def _env_int(name, default):\n"
        "    return int(os.environ.get(name, default))\n"
        'D = _env_int("CASA_PROBE_HELPER", 3)\n'
        "class settings:\n"
        "    environ = {}\n"
        'E = settings.environ.get("CASA_PROBE_DECOY")\n'
        'F = settings.environ["CASA_PROBE_DECOY2"]\n'
        "def reader(env=os.environ):\n"
        '    return env.get("CASA_PROBE_PARAM")\n'
        "def cond_reader(env=None):\n"
        "    env = env if env is not None else os.environ\n"
        '    return env["CASA_PROBE_BOUND"]\n'
        "def other(mapping={}):\n"
        '    return mapping.get("CASA_PROBE_NOT_ENV")\n'
        'raw = os.environ.get("CASA_PROBE_A")\n'
        "raw = {}\n"
        'G = raw.get("CASA_PROBE_REUSED_NAME")\n'
    )
    scripts = root / "casa" / "rootfs" / "etc" / "s6-overlay" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "setup-probe.sh").write_text("#!/bin/sh\n")
    schema = code / "defaults" / "schema"
    schema.mkdir(parents=True)
    (schema / "probe.v1.json").write_text("{}")
    (root / "casa" / "Dockerfile").write_text("FROM scratch\n")

    items = coverage_ledger.enumerate_items(root)
    assert "env:CASA_PROBE_A" in items
    assert "env:CASA_PROBE_B" in items
    assert "env:CASA_PROBE_C" in items
    assert "env:CASA_PROBE_COMMENTED" not in items
    assert "env:CASA_PROBE_HELPER" in items       # read via an _env_* helper
    assert "env:CASA_PROBE_DECOY" not in items    # not os.environ
    assert "env:CASA_PROBE_DECOY2" not in items
    assert "env:CASA_PROBE_PARAM" in items        # param defaulted to os.environ
    assert "env:CASA_PROBE_BOUND" in items        # name bound from os.environ
    assert "env:CASA_PROBE_NOT_ENV" not in items  # unrelated mapping
    assert "env:CASA_PROBE_REUSED_NAME" not in items  # value-bind, name reused
    assert "script:setup-probe.sh" in items
    assert "schema:probe.v1.json" in items
    assert "casa/Dockerfile" in items


def test_a_dynamic_route_path_is_enumerated_not_skipped(tmp_path):
    """A registration whose path is a variable is still a surface; skipping it would let
    a whole route family go unledgered."""
    items = coverage_ledger.enumerate_items(_repo(tmp_path))
    assert any(
        i.startswith("route:casa/rootfs/opt/casa/routes_mod.py:POST:") and "dynamic_path" in i
        for i in items
    )


# --- the check bites, both directions --------------------------------------------------

def test_a_fully_assigned_ledger_passes(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "coverage.yaml").write_text(_full_ledger_for(root))
    assert coverage_ledger.check(root) == []


def test_an_unassigned_enumerated_item_is_refused(tmp_path):
    root = _repo(tmp_path)
    ledger = _full_ledger_for(root).replace(
        "- item: option:log_level\n  doc: architecture/thing.md\n", ""
    )
    (root / "docs" / "coverage.yaml").write_text(ledger)
    assert any("option:log_level" in p for p in coverage_ledger.check(root))


def test_a_stale_ledger_entry_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "coverage.yaml").write_text(
        _full_ledger_for(root) + "- item: tool:vanished\n  doc: architecture/thing.md\n"
    )
    assert any("tool:vanished" in p for p in coverage_ledger.check(root))


def test_an_exclusion_with_a_reason_passes(tmp_path):
    root = _repo(tmp_path)
    ledger = _full_ledger_for(root).replace(
        "- item: option:log_level\n  doc: architecture/thing.md\n",
        '- item: option:log_level\n  excluded: "PHASE-3: reference/operator-options.md"\n',
    )
    (root / "docs" / "coverage.yaml").write_text(ledger)
    assert coverage_ledger.check(root) == []


def test_an_exclusion_without_a_reason_is_refused(tmp_path):
    root = _repo(tmp_path)
    ledger = _full_ledger_for(root).replace(
        "- item: option:log_level\n  doc: architecture/thing.md\n",
        '- item: option:log_level\n  excluded: ""\n',
    )
    (root / "docs" / "coverage.yaml").write_text(ledger)
    assert any("option:log_level" in p and "reason" in p for p in coverage_ledger.check(root))


def test_a_doc_not_in_the_manifest_is_refused(tmp_path):
    root = _repo(tmp_path)
    ledger = _full_ledger_for(root).replace(
        "- item: option:log_level\n  doc: architecture/thing.md\n",
        "- item: option:log_level\n  doc: architecture/ghost.md\n",
    )
    (root / "docs" / "coverage.yaml").write_text(ledger)
    assert any("architecture/ghost.md" in p for p in coverage_ledger.check(root))


def test_a_duplicate_ledger_item_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "coverage.yaml").write_text(
        _full_ledger_for(root) + "- item: option:log_level\n  doc: architecture/thing.md\n"
    )
    assert any("option:log_level" in p and "twice" in p for p in coverage_ledger.check(root))


def test_a_missing_ledger_is_a_finding_not_a_traceback(tmp_path):
    root = _repo(tmp_path)
    assert any("coverage.yaml" in p for p in coverage_ledger.check(root))
