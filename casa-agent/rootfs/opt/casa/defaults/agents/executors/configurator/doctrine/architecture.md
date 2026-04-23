# Casa architecture (what you're configuring)

## Directory layout

Everything you edit lives under `/addon_configs/casa-agent/`:

    /addon_configs/casa-agent/
    agents/
      <resident-role>/               # flat - tier 1 (e.g. assistant, butler)
        character.yaml
        runtime.yaml
        delegates.yaml
        disclosure.yaml
        response_shape.yaml
        voice.yaml
        triggers.yaml              # optional
        hooks.yaml                 # optional
        prompts/
          system.md
          <trigger-name>.md        # one per scheduled/webhook trigger
      specialists/
        <role>/                    # tier 2 (e.g. finance)
          character.yaml
          runtime.yaml
          response_shape.yaml
          voice.yaml
          hooks.yaml               # optional
          prompts/system.md
      executors/
        <type>/                    # tier 3 (e.g. configurator) - this is you
          definition.yaml
          prompt.md
          hooks.yaml               # optional
          observer.yaml            # optional
          doctrine/                # your own knowledge base
    policies/
      scopes.yaml
      disclosure.yaml
    schema/
      *.v1.json                    # READ-ONLY - editing breaks loaders
    workspace/                     # scratch dir you can use for non-tracked work

Read-only to you (hook-blocked): `/data/**` (runtime state), `/addon_configs/casa-agent/schema/**`, `/opt/casa/**`.

## Tier taxonomy

| Tier | Name | What it is | Where it lives |
|---|---|---|---|
| 1 | Resident | Long-lived agent owning a channel (Ellen=telegram+voice, Tina=voice). Has scopes, memory budget, delegates. | agents/<role>/ |
| 2 | Specialist | Role-keyed helper (e.g. finance/Alex). Called by residents via delegate_to_specialist. No channel, no scopes_owned, ephemeral session. | agents/specialists/<role>/ |
| 3 | Executor | Task-bounded, ephemeral agent (e.g. you - configurator). Engaged via engage_executor. Runs in a dedicated Telegram topic. | agents/executors/<type>/ |

Ellen is the only agent allowed to invoke specialists or executors.

## Key files per tier

| File | Resident | Specialist | Executor |
|---|---|---|---|
| character.yaml | required | required | forbidden (uses definition.yaml) |
| runtime.yaml | required | required | forbidden (fields in definition.yaml) |
| delegates.yaml | required | forbidden | forbidden |
| disclosure.yaml | required | forbidden | forbidden |
| response_shape.yaml | required | required | forbidden |
| voice.yaml | required | required | forbidden |
| triggers.yaml | optional | forbidden | forbidden |
| hooks.yaml | optional | optional | optional |
| prompts/system.md | required | required | forbidden (uses prompt.md) |
| prompts/<name>.md | per-trigger | - | - |
| definition.yaml | forbidden | forbidden | required |
| prompt.md | forbidden | forbidden | required |
| observer.yaml | forbidden | forbidden | optional |

agent_loader.py enforces these rules. Adding a forbidden file or removing a required file makes the agent fail to load.

## MCP service topology (v0.14.0)

The `casa-framework` MCP server runs as its own s6-supervised service
called `svc-casa-mcp`, NOT inside casa-main. It listens on
`127.0.0.1:8100` and forwards every tool call and hook decision to
casa-main over a Unix socket at `/run/casa/internal.sock`.

This means:
- An engagement subprocess's MCP TCP connection survives a casa-main
  restart (addon update, in-container respawn). Mid-restart tool calls
  return JSON-RPC `-32000 casa_temporarily_unavailable` (a recoverable
  error the model handles), not a connection drop.
- Casa-main's public port 8099 still serves `/mcp/casa-framework` and
  `/hooks/resolve` as a back-compat fallback for pre-v0.14.0 workspaces;
  these routes will be removed in v0.14.2 or later.
- New engagement workspaces have `.mcp.json` pointing at port 8100;
  pre-v0.14.0 workspaces still point at 8099 and continue to function
  via the fallback.

You (Configurator) do NOT need to touch any of this — workspace
provisioning + hook proxying are framework concerns. If a user asks why
their engagement survived a Casa restart cleanly, this is the reason.
