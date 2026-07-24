"""Tests for specialist_export.py — N2's production-export tooling (spec §4.5).

Post-cutover (Step 9): finance's and mtg's role artifacts no longer exist
under the image's defaults/roles/specialist/ tree — Step 9 removed both
(finance permanently; mtg was always staging-only, present only long enough
for export_mtg_component to read it once). These tests therefore build a
SYNTHETIC defaults_root holding the exact finalized role.yaml/doctrine.md
content each export was validated against pre-cutover, so the export
tool + the real role_artifact/specialist_component loaders it drives are
still exercised end-to-end. The alex/judge persona packs are copied from
the still-bundled real image tree — Step 9 does not remove personas, only
the finance/mtg role directories (and finance's legacy agent directory).
"""
import json
import shutil
from pathlib import Path

from specialist_export import (
    export_finance_component,
    validate_export_bundle_self_consistency,
    write_export_bundle,
)

_FINANCE_ROLE_YAML = """\
api_version: casa.role/v1
id: specialist:finance
kind: specialist
slot: finance
mission: Retrieve and explain household financial records using deterministic arithmetic.
enabled: false
model: {source: fixed, value: sonnet}
tools:
  allowed: [Read, Skill, mcp__casa-framework__get_schedule, mcp__casa-framework__send_media, mcp__casa-framework__ask_user]
  disallowed: [Bash, Write, Edit]
  permission_mode: acceptEdits
  max_turns: 10
  skills: all
  voice_guard: none
mcp_servers: [n8n-workflows, casa-framework]
channels: []
memory: {token_budget: 4000, read_strategy: per_turn}
session: {strategy: ephemeral, idle_timeout_seconds: 0}
disclosure: {policy: delegated, overrides: {}}
delegates: []
executors: []
triggers: []
hooks: {pre_tool_use: []}
tts: {tag_dialect: none, error_phrases: {}}
response:
  text: {register: precise, max_status_sentences: 3}
  voice: {register: spoken, max_status_sentences: 2}
  restricted_webhook: {register: plain, max_status_sentences: 2}
persona:
  policy: optional-but-bound
  compatibility: ["casa/alex@>=0.1.0 <1.0.0"]
requires: {plugins: [], tools: []}
doctrine_file: doctrine.md
"""

_FINANCE_DOCTRINE_MD = """\
# Core doctrine

Answer only finance-scoped delegations. Retrieve source records through assigned tools, route every arithmetic operation through the deterministic finance calculation path, distinguish source data from conclusions, and return a precise task-focused result. Treat recalled material as attributed prior evidence, not personal recollection.

## Text projection

Use concise prose and tables only when they make the figures easier to audit.

## Voice projection

Lead with the result, then give at most the essential supporting figures.

## Restricted webhook projection

Do not expose financial records or persona identity.
"""

_MTG_ROLE_YAML = """\
api_version: casa.role/v1
id: specialist:mtg
kind: specialist
slot: mtg
mission: Ground every Magic — The Gathering rules question in the offline CR/oracle corpus via the mtg plugin tools, emitting the structured evidence contract.
enabled: true
model: {source: fixed, value: sonnet}
tools:
  allowed: []
  disallowed: [Bash, Write, Edit, Read, Glob, Grep, WebFetch, WebSearch, NotebookEdit, Agent, Task]
  permission_mode: dontAsk
  max_turns: 8
  skills: none
  voice_guard: none
mcp_servers: []
channels: []
memory: {token_budget: 0, read_strategy: per_turn}
session: {strategy: ephemeral, idle_timeout_seconds: 0}
disclosure: {policy: delegated, overrides: {}}
delegates: []
executors: []
triggers: []
hooks: {pre_tool_use: []}
tts: {tag_dialect: none, error_phrases: {}}
response:
  text: {register: spoken, max_confirmation_sentences: 2, max_status_sentences: 3}
  voice: {register: spoken, max_confirmation_sentences: 2, max_status_sentences: 2}
  restricted_webhook: {register: plain, max_status_sentences: 2}
persona:
  policy: required
  compatibility: ["casa/judge@>=0.1.0 <1.0.0"]
requires:
  plugins: [mtg]
  tools: [mcp__plugin_mtg_mtg__lookup_rule, mcp__plugin_mtg_mtg__lookup_card]
doctrine_file: doctrine.md
"""

_MTG_DOCTRINE_MD = """\
# Core doctrine

Invoke the mtg-judge procedure for EVERY question: identify cards (`lookup_card`, language-aware for
non-English names), classify the interaction, gather rules (`lookup_rule`/`search_rules`/
`lookup_term`) and rulings (`get_rulings`) only when a specifically named card's rulings could
materially change the answer, then emit the structured YAML result contract as the entire final
message. No citation ⇒ status tentative, never answered. At most one clarification, only on a
material fork. Scope is casual-game rules and current Oracle text — tournament policy, format
legality, and banlists are out of scope. If corpus tools fail or are missing, status
dependency_unavailable (`not_found` is reserved for a corpus lookup miss, not a tool outage). Treat
recalled material as attributed prior evidence, never first-person recollection.

## Text projection

Answer in the structured result contract exactly as specified — no additional prose.

## Voice projection

Keep `answer` to at most 4 short lines; `spoken_summary` at most 2 sentences, colloquial, no rule
numbers, in the question's language. Latency discipline: voice callers wait under 20 seconds — make
the fewest corpus calls that ground the ruling, typically 1–3; never re-verify what a tool result
already told you.

## Restricted webhook projection

Emit only the structured result contract; no persona voice, no conversational framing.
"""


def _build_synthetic_defaults_root(tmp_path: Path) -> Path:
    real_repo_root = Path(__file__).resolve().parents[1]
    real_defaults_root = real_repo_root / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"

    root = tmp_path / "synthetic-defaults"
    finance_role_dir = root / "roles" / "specialist" / "finance"
    finance_role_dir.mkdir(parents=True)
    (finance_role_dir / "role.yaml").write_text(_FINANCE_ROLE_YAML, encoding="utf-8")
    (finance_role_dir / "doctrine.md").write_text(_FINANCE_DOCTRINE_MD, encoding="utf-8")

    mtg_role_dir = root / "roles" / "specialist" / "mtg"
    mtg_role_dir.mkdir(parents=True)
    (mtg_role_dir / "role.yaml").write_text(_MTG_ROLE_YAML, encoding="utf-8")
    (mtg_role_dir / "doctrine.md").write_text(_MTG_DOCTRINE_MD, encoding="utf-8")

    for persona_slug in ("alex", "judge"):
        src = real_defaults_root / "personas" / "casa" / persona_slug / "0.1.0"
        dst = root / "personas" / "casa" / persona_slug / "0.1.0"
        shutil.copytree(src, dst)

    return root


def test_export_finance_component_produces_a_self_consistent_bundle(tmp_path: Path) -> None:
    defaults_root = _build_synthetic_defaults_root(tmp_path)
    bundle = export_finance_component(defaults_root=defaults_root)
    assert bundle.slug == "finance"
    assert "manifest.json" in bundle.files
    assert "role/role.yaml" in bundle.files
    assert "role/doctrine.md" in bundle.files
    assert "persona/pack/persona.yaml" in bundle.files
    manifest = json.loads(bundle.files["manifest.json"])
    assert manifest["default_persona"]["ref"].startswith("casa/alex@")


def test_export_finance_component_bundle_writes_and_self_validates(tmp_path: Path) -> None:
    defaults_root = _build_synthetic_defaults_root(tmp_path)
    bundle = export_finance_component(defaults_root=defaults_root)
    write_export_bundle(bundle, tmp_path / "finance-export")
    validate_export_bundle_self_consistency(bundle)  # raises on any inconsistency — no exception here


def _build_mtg_plugin_root(tmp_path: Path) -> Path:
    """A real (if minimal) plugin tree — the operator-supplied dir
    `export_mtg_component` now receives directly (it no longer trusts a
    caller-supplied checksum it never had bytes to back up)."""
    plugin_root = tmp_path / "mtg-plugin-source"
    plugin_dir = plugin_root / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "mtg", "version": "1.0.0"}), encoding="utf-8")
    (plugin_root / "skills").mkdir()
    (plugin_root / "skills" / "lookup.md").write_text(
        "# lookup skill\n", encoding="utf-8")
    return plugin_root


def test_export_mtg_component_bundles_role_persona_and_corpus(tmp_path: Path) -> None:
    from specialist_export import export_mtg_component, validate_export_bundle_self_consistency

    corpus = tmp_path / "corpus-source"
    corpus.mkdir()
    (corpus / "cr.txt").write_text("702.1 Some rule text.\n", encoding="utf-8")
    plugin_root = _build_mtg_plugin_root(tmp_path)

    defaults_root = _build_synthetic_defaults_root(tmp_path)
    bundle = export_mtg_component(
        defaults_root=defaults_root, corpus_source=corpus,
        mtg_plugin_root=plugin_root,
    )
    assert bundle.slug == "mtg"
    assert "corpus/mtg-rules-corpus/cr.txt" in bundle.files
    assert "plugins/mtg/.claude-plugin/plugin.json" in bundle.files
    assert "plugins/mtg/skills/lookup.md" in bundle.files
    manifest = json.loads(bundle.files["manifest.json"])
    kinds = {d["kind"] for d in manifest["dependencies"]}
    assert kinds == {"persona", "corpus/data", "plugin/implementation"}
    plugin_dep = next(d for d in manifest["dependencies"]
                       if d["kind"] == "plugin/implementation")
    assert plugin_dep == {
        "kind": "plugin/implementation", "identifier": "mtg",
        "digest": plugin_dep["digest"],
        "source": {"type": "bundled", "path": "plugins/mtg"},
    }
    assert plugin_dep["digest"].startswith("sha256:")
    validate_export_bundle_self_consistency(bundle)

    # The digest is over the COPIED tree exactly as it lands in the bundle
    # (normalized: bytecode stripped) — recompute it independently from the
    # bundle's own `plugins/mtg/...` entries to prove the write side and the
    # read side (resolve_dependency_closure) will always agree.
    import plugin_store

    reconstructed = tmp_path / "reconstructed-plugin-tree"
    reconstructed.mkdir()
    for key, content in bundle.files.items():
        if not key.startswith("plugins/mtg/"):
            continue
        rel = key[len("plugins/mtg/"):]
        target = reconstructed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    assert plugin_dep["digest"] == "sha256:" + plugin_store.content_checksum(reconstructed)


def test_export_mtg_component_strips_bytecode_from_the_plugin_dependency(tmp_path: Path) -> None:
    """The bundled plugin tree is normalized (bytecode stripped) BEFORE the
    digest is computed and BEFORE it is copied into the bundle — a __pycache__
    directory or stray .pyc in the operator-supplied source tree must never
    reach the exported bundle or perturb the digest."""
    from specialist_export import export_mtg_component

    corpus = tmp_path / "corpus-source"
    corpus.mkdir()
    (corpus / "cr.txt").write_text("702.1 Some rule text.\n", encoding="utf-8")
    plugin_root = _build_mtg_plugin_root(tmp_path)
    pycache = plugin_root / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.cpython-311.pyc").write_bytes(b"\x00\x01\x02")

    defaults_root = _build_synthetic_defaults_root(tmp_path)
    bundle = export_mtg_component(
        defaults_root=defaults_root, corpus_source=corpus,
        mtg_plugin_root=plugin_root,
    )
    assert not any(k.startswith("plugins/mtg/__pycache__") for k in bundle.files)
    assert not any(k.endswith(".pyc") for k in bundle.files)


def test_clean_image_install_of_the_exported_finance_bundle_succeeds(tmp_path: Path, monkeypatch) -> None:
    """Proves inspect_specialist_repo's check_slug_uniqueness passes for the
    exported finance bundle against the image's ACTUAL role slots — post-Step-9,
    that is simply the real, current _discover_image_role_slots() result (finance
    is genuinely gone now), so no synthetic clean-roles patch is needed anymore."""
    from specialist_export import export_finance_component, write_export_bundle
    from specialist_install import inspect_specialist_repo
    from specialist_registry import InstalledSpecialistIndex

    defaults_root = _build_synthetic_defaults_root(tmp_path)
    bundle = export_finance_component(defaults_root=defaults_root)
    fetched_repo = tmp_path / "fetched-finance-repo"
    write_export_bundle(bundle, fetched_repo)

    def _fake_resolve_and_fetch(repo, ref, subdir, dest, *, expected_revision=None):
        shutil.copytree(fetched_repo, dest)
        return "0" * 40

    monkeypatch.setattr("specialist_install.resolve_and_fetch", _fake_resolve_and_fetch)

    result = inspect_specialist_repo(
        "casa-org/casa-specialist-finance", "v0.1.0",
        staging_root=tmp_path / "staging", installed_index=InstalledSpecialistIndex(
            specialists_dir=str(tmp_path / "specialists")),
        receipts_dir=tmp_path / "receipts",
    )
    assert result.slug == "finance"  # no SpecialistInstallError("slug_collision", ...) raised
