# Casa Agent — contributor & AI-assistant guide

Casa is a **Home Assistant app** (formerly "add-on"): a fleet of Claude-powered agents
reachable over Telegram and a voice (SSE/WebSocket) channel, packaged as an HA app.
Python + `aiohttp`, built on the **Claude Agent SDK**, Hindsight memory, the **MCP**
protocol, and **APScheduler**, all **s6-overlay**–supervised inside the container.

## Where things are
- **Application code:** `casa-agent/rootfs/opt/casa/` (~45 modules — this is the deep
  HA-rootfs path; the add-on copies `rootfs/` into the image root).
- **App manifest:** `casa-agent/config.yaml` (version lives here). User-facing app
  docs: `casa-agent/DOCS.md`. App changelog: `casa-agent/CHANGELOG.md`.
- **Tests:** `tests/` (173 files). Container/e2e harness: `test-local/`. CI: `.github/workflows/qa.yml`.
- **Internal engineering docs:** `docs/` — see the boundary note below.

## Build & test
Run once on a fresh checkout:
```bash
make setup        # builds a WSL/Linux venv at venv_test/ + installs the git hooks
```
Then:
```bash
make test-unit    # fast unit tests:  pytest -m "not docker and not slow" (opt-out gate)
make test-docker  # docker-backed unit tests
```
CI runs four tiers (tier1-smoke, tier2-functional, baseline-runtime, tier3-hardening);
tier2 is the unit gate. The gate is **opt-out** (v0.64.2): unmarked tests run by
default; mark `docker` or `slow` to exclude (`unit` is legacy/optional). Markers
in `pytest.ini`.
The `tests/conftest.py` auto-adds the code root to `sys.path`.

> **⚠️ Memory cage for pytest (2026-07-11, leak FIXED in v0.66.0 — cap stays):**
> a ~23 GB pytest blow-up OOM-killed the entire WSL VM twice. Root cause:
> `patch("retry.asyncio.sleep", …)` patches the **global** `asyncio.sleep`, which
> made the SDK-pool sweeper's `while True: await asyncio.sleep(...)` spin at CPU
> speed under an AsyncMock (unbounded `call_args_list`). Fixed by scoping those
> patches to retry's module-local `asyncio` (see `patch_retry_sleep` in
> `tests/test_agent_process.py`). Two standing rules: (1) **never patch
> `<module>.asyncio.sleep`** — it is the shared module attribute, not a local;
> (2) keep running pytest under the hard cap as belt-and-suspenders, since a cap
> kills only the runaway pytest, not the VM:
> `systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=2G venv_test/bin/pytest …`

## Release flow
1. Branch `feat/vX.Y.Z-<desc>` off `master`.
2. Bump `version:` in `casa-agent/config.yaml` and prepend a `casa-agent/CHANGELOG.md` entry.
3. Commit `release: vX.Y.Z (<summary>)`, push, open a PR, **squash-merge** once CI is green.
4. Merging to master auto-publishes the GHCR images and creates the `vX.Y.Z` tag +
   GitHub Release from the changelog entry (`deploy.yml`) — no manual tagging.
- **Removing an app option?** Also append its key to `DEPRECATED_OPTION_KEYS` in
  `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh` (the `deprecated-options-prune`
  block) so the stale stored value is deleted on boot and HA stops warning.

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

## Repo hygiene & publishing norms
This public repo is the storefront for the Casa app (HA renamed "add-ons" → "apps"
mid-2026). Keep it publish-ready at all times:
- **Green master.** Ship-fast doesn't wait for CI, but after pushing check the previous
  QA run (`gh run list --workflow qa.yml --branch master --limit 1`); a red master is
  stop-the-line before the next release — the e2e tiers cover what the local unit gate
  can't (see the v0.52–v0.57 red streak).
- **Branches die on merge.** GitHub auto-deletes the remote head; delete the local
  branch too. No stray branches on origin.
- **Every release**: bump `casa-agent/config.yaml` version + a user-facing CHANGELOG
  entry (keepachangelog tone; deep engineering detail belongs in the PR body) + a
  `translations/en.yaml` entry for any new/changed option + DOCS.md accuracy.
- **Nothing internal on any pushed ref**: no `docs/` content, no audit/diagnosis
  ledgers, no `.claude/`. Internal artifacts live in `docs/` (the private inner repo).
  Force-pushed-away commits stay fetchable by SHA on GitHub — prevention is the only
  cure.
- **Public identity**: commits AND public-facing files (repository.yaml maintainer,
  README) use `3899230+bonzanni@users.noreply.github.com`, never a personal address.
- **Clean tree between sessions**: commit or revert stragglers. New files have a home
  (eval scripts → `test-local/eval/`, e2e → `test-local/e2e/`, internal specs →
  `docs/`); nothing parked at the repo root.
- **AI attribution**: commits end with `Assisted-by: Claude Code` (kernel/Fedora-style
  disclosure — not `Co-Authored-By`, which implies authorship and suffers GitHub
  email-squatting); PR bodies carry no vendor footer. Configured in
  `.claude/settings.json` (`attribution`); the README's "AI-assisted development" note
  is the canonical disclosure. Never strip or rewrite historical trailers.
