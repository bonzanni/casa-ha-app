# Local Testing

Test the Casa add-on locally in Docker. The real Claude Agent SDK is swapped
for an offline mock so runtime tests need no OAuth token.

## Quick start

```bash
make -C test-local test          # smoke + migration + runtime, ~3 min
make -C test-local clean         # remove leftover containers
```

Windows Git Bash typically ships without GNU Make. Either install it
(`scoop install make` or `choco install make`) or run the scripts
directly — each is self-contained and builds the image on demand:

```bash
bash test-local/e2e/test_smoke.sh
bash test-local/e2e/test_migration.sh
bash test-local/e2e/test_invoke_sessions.sh
bash test-local/e2e/test_concurrency.sh
```

## What the E2E suite covers

| Script | Covers |
|---|---|
| `e2e/test_smoke.sh` | build, `/healthz`, dashboard startup race (BUG-D1) |
| `e2e/test_migration.sh` | role-based rename, CRLF handling (BUG-M1/M2), re-run idempotency |
| `e2e/test_invoke_sessions.sh` | per-invoke session isolation (BUG-I1) |
| `e2e/test_concurrency.sh` | parallel invokes with simulated SDK latency |

## Manually running with real credentials

Copy `options.json.example` to `options.json` and fill in real tokens. The
file is gitignored. Build and run without the mock by using the production
Dockerfile directly:

```bash
docker build -f casa-agent/Dockerfile -t casa-live .
docker run --rm -p 8080:8080 -v $(pwd)/test-local/options.json:/data/options.json casa-live
```

Endpoints:

- `http://localhost:8080/healthz` — aiohttp health check
- `http://localhost:8080/` — status dashboard
- `http://localhost:8080/terminal/` — ttyd web terminal (if enabled)

## Editing options

Edit `test-local/options.json` (untracked) to change agent names, tokens, etc.
