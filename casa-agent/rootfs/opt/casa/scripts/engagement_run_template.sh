#!/command/with-contenv sh
# Casa claude_code engagement run script.
# Substitutions performed by drivers.workspace.render_run_script():
#   {ID}              — engagement id (hex uuid)
#   {PERMISSION_MODE} — permission-mode flag value
#   {ADD_DIR_FLAGS}   — space-joined --add-dir <path> flags
#   {PLUGIN_DIR_FLAGS}— space-joined --plugin-dir <path> flags (pinned artifacts)
#   {EXTRA_UNSET}     — additional space-separated env var names to unset

set -e

exec 2>&1

# Sensitive env vars — never forwarded to the subprocess.
# Update this list in the same commit whenever a new sensitive env var
# lands in Dockerfile / svc-casa/run / HA options / Supervisor context.
unset TELEGRAM_BOT_TOKEN WEBHOOK_SECRET \
      SUPERVISOR_TOKEN HASSIO_TOKEN \
      {EXTRA_UNSET}

export HOME="/data/engagements/{ID}/.home"
export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1
# Unified plugin architecture (§3.8): plugins load ONLY via the pinned
# --plugin-dir flags below — no seed/cache env (the marketplace is gone).
{EXTRA_EXPORT}
cd "/data/engagements/{ID}"

exec </data/engagements/{ID}/stdin.fifo

RESUME_FLAG=""
if [ -f "/data/engagements/{ID}/.session_id" ]; then
  RESUME_FLAG="--resume $(cat /data/engagements/{ID}/.session_id)"
fi

# E-12 (v0.37.0): --channels server:casa-engagement-channel binds the
# per-engagement Channels MCP server defined in workspace .mcp.json.
# v0.64.0: the remote-control flag was dropped — with non-TTY stdio the CLI
# degrades to one-shot print mode and never starts an interactive/remote
# session (live-verified; see the 2026-07-10 remote-control-honesty design).
exec claude --channels server:casa-engagement-channel \
            $RESUME_FLAG \
            --permission-mode {PERMISSION_MODE} \
            {ADD_DIR_FLAGS} \
            {PLUGIN_DIR_FLAGS}
