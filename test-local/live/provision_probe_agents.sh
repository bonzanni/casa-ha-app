#!/usr/bin/env bash
# Provision / remove the throwaway probe agent pair for block S (WS-A live harness).
#
# The pair = one ha_voice resident gateway (RAWCALL contract) + one delegation-only
# specialist. Role names are parameters, so the same fixtures can be instantiated
# under any name (and the block-S probes take roles as parameters too).
#
# Usage:
#   provision_probe_agents.sh create        [--gw R] [--spec R] [--ghost R]
#   provision_probe_agents.sh add-requires  [--spec R]   # unsatisfiable requires{} -> A5 probe
#   provision_probe_agents.sh drop-requires [--spec R]
#   provision_probe_agents.sh remove        [--gw R] [--spec R]
#
# Env overrides (REQUIRED — no host/container baked in): HOST is the ssh alias
# for the HA host, CONTAINER is the casa-agent docker container name. Set them
# in the environment before running; the placeholder defaults below fail loudly.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${HOST:-ha-host}"                 # e.g. an ~/.ssh/config alias
CONTAINER="${CONTAINER:-addon_casa-agent}"   # e.g. addon_<slug>_casa-agent
GW="probe-gw"
SPEC="probe-spec"
GHOST="probe-ghost"

CMD="${1:-}"; shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --gw) GW="$2"; shift 2 ;;
        --spec) SPEC="$2"; shift 2 ;;
        --ghost) GHOST="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

dexec() { ssh "$HOST" -- "sudo -n docker exec $CONTAINER $*"; }
dexec_i() { ssh "$HOST" -- "sudo -n docker exec -i $CONTAINER $*"; }

reload_agents() {
    echo "[provision] casactl reload --scope=agents"
    dexec casactl reload --scope=agents
}

case "$CMD" in
create)
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    cp -r "$HERE/agents/probe-gw" "$STAGE/$GW"
    cp -r "$HERE/agents/probe-spec" "$STAGE/$SPEC"
    # Substitute role tokens (role in character.yaml must equal the dir name).
    find "$STAGE" -type f -exec sed -i \
        -e "s/__GW_ROLE__/$GW/g" -e "s/__SPEC_ROLE__/$SPEC/g" -e "s/__GHOST_ROLE__/$GHOST/g" {} +
    echo "[provision] installing resident $GW -> /config/agents/$GW"
    tar cz -C "$STAGE" "$GW" | dexec_i "tar xz -C /config/agents"
    echo "[provision] installing specialist $SPEC -> /config/agents/specialists/$SPEC"
    tar cz -C "$STAGE" "$SPEC" | dexec_i "tar xz -C /config/agents/specialists"
    reload_agents
    ;;
add-requires)
    # A5 probe: declare deps that cannot resolve -> dependency_unavailable.
    dexec_i "sh -c 'cat >> /config/agents/specialists/$SPEC/runtime.yaml'" <<EOF
requires:
  plugins: [wsa-probe-missing-plugin]
  tools: [mcp__plugin_wsa_probe__missing_tool]
EOF
    reload_agents
    ;;
drop-requires)
    dexec "python3 -c \"
import io
p='/config/agents/specialists/$SPEC/runtime.yaml'
lines=open(p).read().splitlines(True)
out=[]; skip=False
for ln in lines:
    if ln.startswith('requires:'): skip=True; continue
    if skip and (ln.startswith(' ') or ln.strip()==''): continue
    skip=False; out.append(ln)
open(p,'w').write(''.join(out))
print('requires block removed')
\""
    reload_agents
    ;;
remove)
    echo "[provision] removing /config/agents/$GW + /config/agents/specialists/$SPEC"
    dexec "rm -rf /config/agents/$GW /config/agents/specialists/$SPEC"
    reload_agents
    echo "[provision] residual check"
    dexec "sh -c 'ls /config/agents /config/agents/specialists'"
    ;;
*)
    echo "usage: $0 create|add-requires|drop-requires|remove [--gw R] [--spec R] [--ghost R]" >&2
    exit 2
    ;;
esac
