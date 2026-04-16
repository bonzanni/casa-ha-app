# Local Testing

Test the Casa add-on locally without Home Assistant.

## Build

```bash
docker build -f test-local/Dockerfile.test -t casa-test .
```

## Run

```bash
docker run --rm -p 8080:8080 casa-test
```

## What it does

- Runs a mock Supervisor API that serves `options.json` on port 80 inside the container
- Bashio reads config from this mock API instead of the real HA Supervisor
- All s6 init scripts and services start normally
- nginx serves on port 8080 (mapped to host)

## Test endpoints

- `http://localhost:8080/healthz` -- aiohttp health check
- `http://localhost:8080/terminal/` -- ttyd web terminal (if enable_terminal is true)

## Customize

Edit `test-local/options.json` to change add-on options (agent names, tokens, etc.).
