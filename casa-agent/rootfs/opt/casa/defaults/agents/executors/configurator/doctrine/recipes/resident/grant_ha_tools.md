# Recipe: grant a resident Home Assistant tools

When a resident agent needs to control HA devices (lights, climate, locks, media, sensors), grant the **whole HA Assist tool surface in one line**: server-level `mcp__homeassistant`. Per-tool enumeration is unnecessary.

The default Casa setup wires this for `butler` (Tina). Use this recipe to add it to any other resident.

## Prerequisite — HA-side configuration

The user must, in Home Assistant:

1. Enable **Settings → Devices & Services → Add Integration → "Model Context Protocol Server"**.
2. Expose every entity the resident should control to the **default Assist pipeline** (Settings → Voice Assistants → Expose).

Without these, `http://supervisor/core/api/mcp` returns 404 and tool calls fail at the transport layer.

## Step 1 — Server-level grant in `runtime.yaml`

Edit `/config/agents/<role>/runtime.yaml`:

```yaml
tools:
  allowed:
    - Read
    - Skill
    - mcp__homeassistant   # ← grants every HA tool, present and future
```

Bare `mcp__<server>` (no `__<tool>` suffix) is a server-level wildcard. As the user adds new exposed entities to Assist, the resident gets access automatically; no Casa restart needed beyond the next session pool turn.

## Step 2 — Confirm `homeassistant` in `mcp_server_names`

Same file:

```yaml
mcp_server_names:
  - homeassistant
  - casa-framework
```

Without this, the allow-list grant points at nothing. casa_core only registers the homeassistant server when SUPERVISOR_TOKEN is set (always true on a real HA install).

## Step 3 — Teach the resident HOW in `prompts/system.md`

Append a `## Home Assistant tools` section that names the conventional intents (`HassTurnOn`, `HassTurnOff`, `HassLightSet`, `HassClimateSetTemperature`, `GetLiveContext`, …) and points the model at `GetLiveContext` first when it doesn't know what's exposed. See butler's `prompts/system.md` for the reference shape.

## Verify

```
/ha-prod-console:restart c071ea9c_casa-agent
```

Then ask the resident a control question via its primary channel ("turn off the kitchen lights"). Check the addon logs for an `mcp__homeassistant__HassTurnOff` call.

## Common pitfalls

- **HA integration not enabled** — addon log shows `404 Not Found` against `/core/api/mcp`. Fix: enable the integration in HA.
- **Entity not exposed to Assist** — model gets back `entity not found`. Fix: expose the entity in Voice Assistants settings.
- **Agent has the tool but doesn't call it** — usually a prompt issue. Make sure the resident's system prompt actually mentions the HA tools; the model needs to know they're available.
- **Wrong Casa version** — the bare `mcp__homeassistant` grant requires v0.15.1+. Earlier versions used per-tool entries.
