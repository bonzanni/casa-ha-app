#!/command/with-contenv bash
# Casa claude_code engagement run script (v0.75.0 — interactive-engagements
# design §W1). bash REQUIRED: process substitution below.
set -e

unset TELEGRAM_BOT_TOKEN WEBHOOK_SECRET SUPERVISOR_TOKEN HASSIO_TOKEN \
      {EXTRA_UNSET}

export HOME="/data/engagements/{ID}/.home"
export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1
export MCP_TOOL_TIMEOUT=660000     # [D:§W5] must exceed the 585s ask client bound
{EXTRA_EXPORT}
cd "/data/engagements/{ID}"

# Per-spawn bounded stderr + stream-correlated epoch. UNIQUE file per epoch —
# NOT a mod-4 slot (Sol r5-B2): bash does not wait for the process-substitution
# consumer when the exec'd child exits, so a lingering ringlog for epoch E could
# still append/rotate a REUSED slot while epoch E+4 truncates it → mixed stderr.
# A unique `.stderr.<EPOCH>.log` filename means a lingering consumer only ever
# writes to ITS OWN epoch's file; disk stays bounded by pruning old epochs.
EPOCH=$(( $(cat .spawn_epoch 2>/dev/null || echo 0) + 1 ))
printf '%s\n' "$EPOCH" > .spawn_epoch.tmp && mv -f .spawn_epoch.tmp .spawn_epoch  # atomic publish (r8-B4)
STDERR_LOG=".stderr.$EPOCH.log"
# SWEEP-prune every epoch file <= EPOCH-4 (r6-B2): a single exact `EPOCH-4` rm
# would leak on a crash-skipped spawn or a briefly-resurrected path; sweeping
# revisits all stale files each spawn, keeping the total bounded (~4 epochs).
# Safe against a lingering writer because ringlog holds its fd (no resurrection).
for _f in .stderr.*.log; do
  _e=${_f#.stderr.}; _e=${_e%.log}
  if [ "$_e" -le "$(( EPOCH - 4 ))" ] 2>/dev/null; then rm -f "$_f" "$_f.1"; fi
done
exec 2> >(/opt/casa/scripts/ringlog.sh "$STDERR_LOG" 65536 "$EPOCH")   # pass epoch for the stale fence (r7-B4)
printf '{"casa_control": "spawn", "epoch": %s}\n' "$EPOCH"   # NDJSON, pre-exec

exec </data/engagements/{ID}/stdin.fifo

RESUME_FLAG=""
if [ -f "/data/engagements/{ID}/.session_id" ]; then
  RESUME_FLAG="--resume $(cat /data/engagements/{ID}/.session_id)"
fi

exec claude --channels server:casa-engagement-channel \
            --print --verbose --output-format stream-json \
            $RESUME_FLAG \
            --permission-mode {PERMISSION_MODE} \
            {ADD_DIR_FLAGS} \
            {PLUGIN_DIR_FLAGS}
