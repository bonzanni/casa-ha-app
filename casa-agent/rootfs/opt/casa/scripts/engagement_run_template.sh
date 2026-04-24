#!/command/with-contenv sh
# Casa claude_code engagement run script.
# Substitutions performed by drivers.workspace.render_run_script():
#   {ID}             — engagement id (hex uuid)
#   {ID_SHORT}       — first 8 chars of engagement id (remote-control slug)
#   {PERMISSION_MODE}— permission-mode flag value
#   {ADD_DIR_FLAGS}  — space-joined --add-dir <path> flags
#   {EXTRA_UNSET}    — additional space-separated env var names to unset

set -e

exec 2>&1

# Sensitive env vars — never forwarded to the subprocess.
# Update this list in the same commit whenever a new sensitive env var
# lands in Dockerfile / svc-casa/run / HA options / Supervisor context.
unset TELEGRAM_BOT_TOKEN HONCHO_API_KEY WEBHOOK_SECRET \
      SUPERVISOR_TOKEN HASSIO_TOKEN \
      {EXTRA_UNSET}

export HOME="/data/engagements/{ID}/.home"
export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1
# Plan 4b §B.9: plugin seed + shared cache dirs for CC plugin resolution.
export CLAUDE_CODE_PLUGIN_SEED_DIR="/opt/claude-seed"
export CLAUDE_CODE_PLUGIN_CACHE_DIR="/addon_configs/casa-agent/cc-home/.claude/plugins"
{EXTRA_EXPORT}
cd "/data/engagements/{ID}"

exec </data/engagements/{ID}/stdin.fifo

RESUME_FLAG=""
if [ -f "/data/engagements/{ID}/.session_id" ]; then
  RESUME_FLAG="--resume $(cat /data/engagements/{ID}/.session_id)"
fi

exec claude --remote-control "engagement-{ID_SHORT}" \
            $RESUME_FLAG \
            --permission-mode {PERMISSION_MODE} \
            {ADD_DIR_FLAGS}
