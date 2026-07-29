# Working on Casa — agent entry card

Casa is a Home Assistant app: Claude-powered agents reachable over Telegram and a voice
channel, packaged as an HA app. Python + `aiohttp`, s6-overlay-supervised inside the
container.

This card is tool-neutral. `CLAUDE.md` carries the same norms for Claude Code, and the
build and test commands there are identical for every tool.

## Before you change anything

**Code is the source of truth.** Any documentation is a map; when they disagree, the code
wins — and the doc gets fixed in the same change.

**Verify against whole files, not grep slices.** Most false "discrepancies" in this
codebase came from reading a narrow slice; open the file, read around the symbol, then
assert.

## Before you commit anything

Internal engineering material — design specs, plans, roadmaps, reviews, captured
transcripts — is never committed here. The rule for what may go in is one line:

> A fact belongs in this repo only if it is verifiable from the public commit alone: no
> operator, no production box, no private repository.

An agent-facing corpus is being published under `docs/`. Until it lands, treat that
directory as tracked and public: anything committed there is published.

A pre-commit guard, a pre-push gate and a CI sweep enforce this; `make setup` installs the
hooks. **The first push of anything is irreversible** — objects stay fetchable by SHA
afterwards, whatever happens to the branch. If a check refuses your change, the fix is
almost never to loosen the check.

## House rules that are easy to get wrong

- Branch first; never commit to `main`. Delete merged branches with `git branch -D` — a
  squash merge leaves the tip un-merged as far as `-d` is concerned.
- Container-bound files are LF. `.gitattributes` enforces `eol=lf` for `*.sh`,
  `Dockerfile` and everything under `casa/rootfs/**`; CRLF breaks shebangs and s6.
- The unit gate is opt-out: unmarked tests run, `docker` and `slow` are excluded.
- Never patch `<module>.asyncio.sleep`. It rebinds the shared module attribute, so any
  `while True: await asyncio.sleep(...)` loop elsewhere in the process spins at CPU speed
  under an `AsyncMock` whose `call_args_list` grows without bound.
- Removing an app option is two edits: delete the schema key *and* append it to the
  deprecated-keys list, or the stored value survives and the host keeps warning.
