# Casa Agent — contributor & AI-assistant guide

Casa is a **Home Assistant add-on**: a fleet of Claude-powered agents reachable over
Telegram and a voice (SSE/WebSocket) channel, packaged as an HA add-on. Python +
`aiohttp`, built on the **Claude Agent SDK**, Honcho memory, the **MCP** protocol, and
**APScheduler**, all **s6-overlay**–supervised inside the container.

## Where things are
- **Application code:** `casa-agent/rootfs/opt/casa/` (~45 modules — this is the deep
  HA-rootfs path; the add-on copies `rootfs/` into the image root).
- **Add-on manifest:** `casa-agent/config.yaml` (version lives here). User-facing add-on
  docs: `casa-agent/DOCS.md`. Add-on changelog: `casa-agent/CHANGELOG.md`.
- **Tests:** `tests/` (173 files). Container/e2e harness: `test-local/`. CI: `.github/workflows/qa.yml`.
- **Internal engineering docs:** `docs/` — see the boundary note below.

## Build & test
Run once on a fresh checkout:
```bash
make setup        # builds a WSL/Linux venv at venv_test/ + installs the git hooks
```
Then:
```bash
make test-unit    # fast unit tests:  pytest -m "unit and not docker and not slow"
make test-docker  # docker-backed unit tests
```
CI runs four tiers (tier1-smoke, tier2-functional, baseline-runtime, tier3-hardening);
tier2 is the unit gate. Test markers: `unit`, `docker`, `slow` (`pytest.ini`).
The `tests/conftest.py` auto-adds the code root to `sys.path`.

## Release flow
1. Branch `feat/vX.Y.Z-<desc>` off `master`.
2. Bump `version:` in `casa-agent/config.yaml` and prepend a `casa-agent/CHANGELOG.md` entry.
3. Commit `release: vX.Y.Z (<summary>)`, push, open a PR, **squash-merge** once CI is green.

## Environment (WSL)
- Develop on **WSL2 on the native ext4 filesystem** (not `/mnt/c`) — needed for perf and
  correct exec bits.
- **Container-bound files must be LF.** `.gitattributes` enforces `eol=lf` on `*.sh`,
  `Dockerfile`, and all of `casa-agent/rootfs/**` — CRLF breaks shebangs and s6. Don't
  fight it; new `rootfs/**` or `*.sh` files must be LF.
- `ls` may show `-rwxr-xr-x` on plain files — that's a WSL mount display artifact; git
  tracks `100644`. Don't "fix" it or commit mode changes.
- `venv_test/` must be a **Linux venv** (run `make setup`); any pre-existing Windows-layout
  venv (`Scripts/`, `Activate.ps1`) won't run under WSL.

## The `docs/` boundary — READ THIS
`docs/` is an **intentionally-private, separate inner git repo** (its own `docs/.git` with
a private remote). It is **gitignored by this public add-on repo and is NEVER shipped
here.** It holds internal engineering docs (roadmaps, design specs, the canonical
current-state spec).

- **Never `git add -f docs/` or otherwise commit `docs/` into this repo.** A
  `.githooks/pre-commit` guard refuses it; `make setup` installs that guard.
- Do **not** `git commit` inside `docs/` unless explicitly asked — it has its own history.
- If you have the private `docs/` tree checked out, the **canonical current-state
  reference** is `docs/current-state-spec.md` (code is the source of truth — when that doc
  and the code disagree, the code wins). For internal guidance see `docs/CLAUDE.md`.

## Working norms
- **Verify against whole files, not thin grep slices** — read around a symbol before
  asserting behaviour.
- Don't commit or push unless asked; if on `master`, branch first.
