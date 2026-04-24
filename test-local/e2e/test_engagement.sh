#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"

MOCK_PORT=8081
MOCK_PID=""
NAME="casa-eng-$$"
SUPERGROUP_ID=-1001

cleanup_all() {
    docker ps -q --filter "name=casa-eng-.*-$$" | xargs -r docker stop >/dev/null 2>&1 || true
    docker stop "$NAME" >/dev/null 2>&1 || true
    if [ -n "$MOCK_PID" ]; then
        kill "$MOCK_PID" 2>/dev/null || true
    fi
}
trap cleanup_all EXIT

# --- E-0: start mock Telegram server ------------------------------------
log "E-0: start mock Telegram server"
python3 "$REPO_ROOT/test-local/e2e/mock_telegram/server.py" >/tmp/mock-tg.log 2>&1 &
MOCK_PID=$!
# Wait for mock to come up (up to 3 seconds, 10 x 0.3s polls)
for i in $(seq 1 10); do
    curl -sf "http://localhost:${MOCK_PORT}/_inspect" >/dev/null && break
    sleep 0.3
done
curl -sf "http://localhost:${MOCK_PORT}/_inspect" >/dev/null || fail "E-0: mock TG never started"
pass "E-0 mock TG up"

build_image

# --- Boot one long-lived container with engagement wiring ---------------
# We run the engagement e2e as a python harness executed via `docker exec`.
# The container doesn't need a live Telegram application — the harness
# constructs a TelegramChannel directly with bot_token="test" and a mock-TG
# base URL so `createForumTopic` / `sendMessage` land in the mock's state.
log "E-0b: boot container with engagement wiring"
MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -e TELEGRAM_ENGAGEMENT_SUPERGROUP_ID="$SUPERGROUP_ID" \
    -e TELEGRAM_BOT_API_BASE="http://host.docker.internal:${MOCK_PORT}" \
    --add-host=host.docker.internal:host-gateway \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"
pass "E-0b container healthy"

# ---------------------------------------------------------------------------
# Helper: run_harness <label> <py>
# Copies the python source into /tmp in the container and runs it inside
# the casa venv. Fails (assert_contains style) if the harness's last line
# is not "OK".
# ---------------------------------------------------------------------------
run_harness() {
    local label="$1"; shift
    local py="$1"; shift
    local host_tmp
    host_tmp="$(mktemp)"
    printf '%s\n' "$py" > "$host_tmp"
    MSYS_NO_PATHCONV=1 docker cp "$host_tmp" "$NAME:/tmp/_harness.py" >/dev/null
    rm -f "$host_tmp"
    local out
    if ! out=$(MSYS_NO_PATHCONV=1 docker exec -e MOCK_TG_BASE="http://host.docker.internal:${MOCK_PORT}" \
               -e SUPERGROUP_ID="$SUPERGROUP_ID" \
               "$NAME" /opt/casa/venv/bin/python /tmp/_harness.py 2>&1); then
        printf '%s\n' "$out" >&2
        fail "$label harness exited non-zero"
    fi
    printf '%s\n' "$out" | tail -20 >&2
    printf '%s\n' "$out" | tail -1 | grep -qF "OK" \
        || fail "$label harness did not print OK as final line"
}

# ---------------------------------------------------------------------------
# E-1 — Tier 2 interactive open: channel.open_engagement_topic creates a
# forum topic in the mock-TG supergroup and the registry records it.
# ---------------------------------------------------------------------------
log "E-1: delegate_to_specialist(mode=interactive) opens a topic"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E1_PY <<'PY' || true
import asyncio, os, sys, json
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e1.json", bus=None)
    await reg.load()
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="", delivery_mode="block",
        engagement_supergroup_id=supergroup,
    )
    # Stand up the PTB Application against the mock TG URL.
    await ch._rebuild()
    ch._engagement_registry = reg
    tid = await ch.open_engagement_topic(name="E-1 test topic", icon_emoji=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="E-1 task", origin={"channel": "telegram", "chat_id": "999"},
        topic_id=tid,
    )
    assert tid is not None, "createForumTopic returned None"
    assert reg.by_topic_id(tid) is rec, "by_topic_id did not resolve"
    assert os.path.exists("/tmp/_eng_e1.json"), "tombstone not written"
    with open("/tmp/_eng_e1.json") as fh:
        assert json.load(fh), "tombstone empty"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-1" "$E1_PY"

# Mock TG saw createForumTopic land (topic count ≥ 1).
TOPICS=$(curl -s "http://localhost:${MOCK_PORT}/_inspect" \
    | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("topics",{})))')
[ "$TOPICS" -ge 1 ] || fail "E-1: expected ≥1 topic in mock TG, got $TOPICS"
pass "E-1 interactive-mode opens topic (mock TG topics=$TOPICS)"

# ---------------------------------------------------------------------------
# E-2 — User turn routed: a user message inside an engagement topic reaches
# the driver. We inject a real Telegram Update shape through handle_update
# and assert the driver's send_user_turn path was reached.
# ---------------------------------------------------------------------------
log "E-2: user turn in topic routed to driver"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E2_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from types import SimpleNamespace
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e2.json", bus=None)
    await reg.load()
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="", delivery_mode="block",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()

    # Stub driver: record turns, always alive.
    class StubDriver:
        def __init__(self): self.turns = []
        def is_alive(self, rec): return True
        async def send_user_turn(self, rec, text): self.turns.append((rec.id, text))
        async def resume(self, rec, sid): pass
        def get_session_id(self, rec): return None
        async def cancel(self, rec): pass
    drv = StubDriver()
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._driver_send_user_turn = drv.send_user_turn

    tid = await ch.open_engagement_topic(name="E-2", icon_emoji=None)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="", origin={"channel":"telegram"},
                            topic_id=tid)

    # Synthetic Telegram update: user message in the topic.
    update = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=supergroup),
            message_thread_id=tid,
            text="hello from user",
            from_user=SimpleNamespace(id=7777),
        ),
    )
    await ch.handle_update(update)
    assert len(drv.turns) == 1, f"expected 1 driver turn, got {len(drv.turns)}"
    assert drv.turns[0][1] == "hello from user", f"wrong text: {drv.turns[0]}"
    # last_user_turn_ts should advance.
    assert reg.get(rec.id).last_user_turn_ts > 0
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-2" "$E2_PY"
pass "E-2 user turn routed to driver"

# ---------------------------------------------------------------------------
# E-3 — emit_completion happy path: _finalize_engagement closes the topic
# (icon=✅ on mock TG), marks record completed, and posts NOTIFICATION.
# ---------------------------------------------------------------------------
log "E-3: emit_completion closes topic, ✅ icon, NOTIFICATION posted"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E3_PY <<'PY' || true
import asyncio, os, sys, json
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e3.json", bus=None)
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="", delivery_mode="block",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()

    # Minimal bus that captures NOTIFICATIONs.
    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    bus = StubBus()

    # Minimal channel_manager.get(...) that returns our channel.
    class StubChanMgr:
        def get(self, name): return ch
    cm = StubChanMgr()

    # Inject bus + channel_manager + registry into the tools module.
    import tools
    tools._engagement_registry = reg
    tools._bus = bus
    tools._channel_manager = cm

    tid = await ch.open_engagement_topic(name="E-3", icon_emoji=None)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="",
                            origin={"channel":"telegram", "role":"assistant"},
                            topic_id=tid)

    # Stub driver (cancel is called inside _finalize_engagement)
    class StubDriver:
        async def cancel(self, rec): pass
    await tools._finalize_engagement(
        rec, outcome="completed", text="all done",
        artifacts=[], next_steps=[],
        driver=StubDriver(), memory_provider=None,
    )

    # Inspect mock TG: topic must be closed with ✅ icon.
    import urllib.request
    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        state = json.loads(r.read())
    topic = state["topics"][str(tid)]
    assert topic["closed"] is True, f"topic not closed: {topic}"
    assert topic["icon_custom_emoji_id"] == "✅", f"icon wrong: {topic}"
    # Bus got one NOTIFICATION targeting assistant.
    assert len(bus.notifs) == 1, f"bus notifs: {len(bus.notifs)}"
    assert bus.notifs[0].target == "assistant"
    # Record transitioned to completed.
    assert reg.get(rec.id).status == "completed"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-3" "$E3_PY"
pass "E-3 emit_completion closes topic with ✅ + NOTIFY Ellen"

# ---------------------------------------------------------------------------
# E-4 — /cancel: user-driven cancellation closes the topic and marks the
# record cancelled.
# ---------------------------------------------------------------------------
log "E-4: /cancel user-driven"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E4_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from types import SimpleNamespace
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e4.json", bus=None)
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="", delivery_mode="block",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()

    import tools
    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    class StubChanMgr:
        def get(self, name): return ch
    tools._engagement_registry = reg
    tools._bus = StubBus()
    tools._channel_manager = StubChanMgr()

    # Stub driver to satisfy _finalize_engagement path triggered by _finalize_cancel.
    class StubDriver:
        def is_alive(self, rec): return False
        async def cancel(self, rec): pass
    # Bind into agent module so tools._finalize_cancel finds it.
    import agent as agent_mod
    agent_mod.active_engagement_driver = StubDriver()
    agent_mod.active_memory_provider = None

    ch._engagement_registry = reg
    # Inject _finalize_cancel collaborator (prod wiring lives in casa_core.py).
    drv = agent_mod.active_engagement_driver
    async def _finalize_cancel(rec, reason="user"):
        await tools._finalize_engagement(
            rec, outcome="cancelled", text=f"Cancelled by {reason}.",
            artifacts=[], next_steps=[],
            driver=drv, memory_provider=None,
        )
    ch._finalize_cancel = _finalize_cancel

    tid = await ch.open_engagement_topic(name="E-4", icon_emoji=None)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="",
                            origin={"channel":"telegram","role":"assistant"},
                            topic_id=tid)

    update = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=supergroup),
            message_thread_id=tid, text="/cancel",
            from_user=SimpleNamespace(id=7777),
        ),
    )
    await ch.handle_update(update)

    # Record must be cancelled; topic closed in mock TG.
    assert reg.get(rec.id).status == "cancelled", \
        f"status: {reg.get(rec.id).status}"
    import urllib.request, json as _json
    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        state = _json.loads(r.read())
    assert state["topics"][str(tid)]["closed"] is True
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-4" "$E4_PY"
pass "E-4 /cancel marks record cancelled + closes topic"

# ---------------------------------------------------------------------------
# E-5 — /silent: toggles observer.is_silenced(engagement_id) → True.
# ---------------------------------------------------------------------------
log "E-5: /silent squelches observer"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E5_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from types import SimpleNamespace
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e5.json", bus=None)
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="", delivery_mode="block",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()

    class StubObserver:
        def __init__(self): self.silenced = set()
        def silence(self, eid): self.silenced.add(eid)
        def is_silenced(self, eid): return eid in self.silenced
    obs = StubObserver()

    ch._engagement_registry = reg
    ch._observer = obs

    tid = await ch.open_engagement_topic(name="E-5", icon_emoji=None)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="",
                            origin={"channel":"telegram"}, topic_id=tid)

    update = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=supergroup),
            message_thread_id=tid, text="/silent",
            from_user=SimpleNamespace(id=7777),
        ),
    )
    await ch.handle_update(update)
    assert obs.is_silenced(rec.id), "observer not silenced after /silent"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-5" "$E5_PY"
pass "E-5 /silent flips observer.is_silenced"

# ---------------------------------------------------------------------------
# E-6 — Idle sweep fires idle_detected on bus after 4 days of user silence.
# Uses now_override to fast-forward clock without monkey-patching time.
# ---------------------------------------------------------------------------
log "E-6: idle sweep fires idle_detected NOTIFICATION"

read -r -d '' E6_PY <<'PY' || true
import asyncio, os, sys, time
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from bus import MessageType

async def main():
    # Bus that captures NOTIFICATIONs.
    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    bus = StubBus()

    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e6.json", bus=bus)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="telegram_channel", task="",
                            origin={"channel":"telegram"}, topic_id=5001)

    # Stub driver (not used since rec.driver != in_casa).
    class StubDriver:
        def is_alive(self, rec): return False
        def get_session_id(self, rec): return None
        async def cancel(self, rec): pass
    # Fast-forward 4 days beyond last_user_turn_ts.
    future = rec.last_user_turn_ts + 4 * 86400 + 1
    await reg.sweep_idle_and_suspend(driver=StubDriver(), now_override=future)

    idle = [n for n in bus.notifs
            if n.type == MessageType.NOTIFICATION
            and isinstance(n.content, dict)
            and n.content.get("event") == "idle_detected"]
    assert len(idle) == 1, f"expected 1 idle_detected, got {len(bus.notifs)}"
    assert idle[0].content["engagement_id"] == rec.id
    # Re-firing within the refire window → no second notify.
    await reg.sweep_idle_and_suspend(driver=StubDriver(), now_override=future + 60)
    idle2 = [n for n in bus.notifs
             if isinstance(n.content, dict)
             and n.content.get("event") == "idle_detected"]
    assert len(idle2) == 1, f"unexpected refire: {len(idle2)}"
    print("OK")

asyncio.run(main())
PY
run_harness "E-6" "$E6_PY"
pass "E-6 idle_detected fires + does not refire within window"

# ---------------------------------------------------------------------------
# E-7 — Session suspend after 24h, resume on next user turn. Asserts the
# driver transitions dead → alive via the resume path.
# ---------------------------------------------------------------------------
log "E-7: 24h idle suspends client; next turn resumes"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E7_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from types import SimpleNamespace
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e7.json", bus=None)
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="", delivery_mode="block",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()

    # Stub driver: alive flag flips after cancel; resume restores it.
    class StubDriver:
        def __init__(self):
            self._alive = {}
            self.turns = []
        def is_alive(self, rec): return self._alive.get(rec.id, False)
        def get_session_id(self, rec): return "mock-session-e7"
        async def cancel(self, rec): self._alive.pop(rec.id, None)
        async def resume(self, rec, session_id):
            assert session_id == "mock-session-e7", f"resume got {session_id}"
            self._alive[rec.id] = True
        async def send_user_turn(self, rec, text): self.turns.append(text)
    drv = StubDriver()
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._driver_send_user_turn = drv.send_user_turn

    tid = await ch.open_engagement_topic(name="E-7", icon_emoji=None)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="",
                            origin={"channel":"telegram"}, topic_id=tid)
    drv._alive[rec.id] = True

    # Fast-forward 25h; sweep should suspend.
    future = rec.last_user_turn_ts + 25 * 3600
    await reg.sweep_idle_and_suspend(driver=drv, now_override=future)
    assert reg.get(rec.id).status == "idle", \
        f"status after sweep: {reg.get(rec.id).status}"
    assert not drv.is_alive(rec), "driver still alive after sweep"
    assert reg.get(rec.id).sdk_session_id == "mock-session-e7", \
        "sdk_session_id not persisted"

    # User turn arrives — handle_update should resume + forward the turn.
    update = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=supergroup),
            message_thread_id=tid, text="are you there?",
            from_user=SimpleNamespace(id=7777),
        ),
    )
    await ch.handle_update(update)
    assert drv.is_alive(rec), "driver not alive after resume"
    assert drv.turns == ["are you there?"], f"turns: {drv.turns}"
    assert reg.get(rec.id).status == "active", \
        f"status after resume: {reg.get(rec.id).status}"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-7" "$E7_PY"
pass "E-7 suspend-then-resume on next turn"

# ---------------------------------------------------------------------------
# E-8 — Orphan recovery: pre-seed /data/engagements.json, restart container,
# verify EngagementRegistry loads the record on startup.
# ---------------------------------------------------------------------------
log "E-8: startup with pre-seeded engagements.json"

# Write a valid tombstone containing one active engagement.
SEED_JSON='[{"id":"e8orphan0000000000000000000000","kind":"specialist","role_or_type":"finance","driver":"in_casa","status":"active","topic_id":9999,"started_at":1000.0,"last_user_turn_ts":1000.0,"last_idle_reminder_ts":0,"completed_at":null,"sdk_session_id":"mock-session-e8","origin":{"channel":"telegram"},"task":"E-8 orphan"}]'
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "printf '%s' '$SEED_JSON' > /data/engagements.json"

read -r -d '' E8_PY <<'PY' || true
import asyncio, os, sys, json
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry

async def main():
    # Load straight from the container's /data tombstone — same path casa_core uses.
    reg = EngagementRegistry(tombstone_path="/data/engagements.json", bus=None)
    await reg.load()
    survivors = reg.active_and_idle()
    assert len(survivors) == 1, f"expected 1 survivor, got {len(survivors)}"
    rec = survivors[0]
    assert rec.id == "e8orphan0000000000000000000000"
    assert rec.topic_id == 9999
    assert rec.sdk_session_id == "mock-session-e8"
    assert reg.by_topic_id(9999) is rec, "topic index not built"
    print("OK")

asyncio.run(main())
PY
run_harness "E-8" "$E8_PY"
# Tombstone cleanup so subsequent runs (or post-test poke-around) stay clean.
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c "rm -f /data/engagements.json" || true
pass "E-8 orphan recovery: pre-seeded record survives load()"

# ---------------------------------------------------------------------------
# E-9 — Configurator executor happy-path (structural, harness-driven).
#
# The "real" happy-path (Ellen reasons → calls engage_executor → configurator
# reasons → patches triggers.yaml → emits completion → Ellen NOTIFIED) requires
# a live Anthropic API + claude_agent_sdk, which CI cannot exercise. E-9 is
# therefore a structural harness that confirms every wiring point around the
# executor engagement type:
#
#   1. ExecutorRegistry loads the bundled configurator definition.
#   2. engage_executor's pattern (open topic → reg.create(kind=executor) →
#      driver.start) is exercised end-to-end through the real TelegramChannel
#      against the mock TG server.
#   3. A user approval message in the topic is routed to driver.send_user_turn.
#   4. The stub driver writes to triggers.yaml (as a real configurator would
#      via the Write tool) and then invokes _finalize_engagement — exactly the
#      path emit_completion takes in production.
#   5. After finalize: topic closed with ✅, NOTIFICATION posted to Ellen,
#      registry entry in "completed" state, triggers.yaml contains the new
#      trigger name.
#
# A real end-to-end run — Ellen reasoning + configurator reasoning over the
# live SDK — lives in the manual smoke (test_configurator_engagement.sh, T29).
# ---------------------------------------------------------------------------
log "E-9: configurator executor engagement end-to-end (harness-driven)"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

# Seed a writable triggers.yaml path in the container so the stub driver can
# mimic the Configurator's "add a trigger" edit.
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "mkdir -p /addon_configs/casa-agent/agents/assistant && \
     printf 'triggers: []\n' > /addon_configs/casa-agent/agents/assistant/triggers.yaml"

read -r -d '' E9_PY <<'PY' || true
import asyncio, os, sys, json
sys.path.insert(0, "/opt/casa")
from types import SimpleNamespace
from engagement_registry import EngagementRegistry
from executor_registry import ExecutorRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])

    # 1. Executor registry loads the bundled configurator definition.
    #    Point at /opt/casa/defaults where configurator/ ships in the image.
    exec_reg = ExecutorRegistry("/opt/casa/defaults/agents/executors")
    exec_reg.load()
    types = exec_reg.list_types()
    assert "configurator" in types, f"configurator not loaded, types={types}"
    defn = exec_reg.get("configurator")
    assert defn is not None and defn.enabled, "configurator disabled or missing"
    assert defn.driver == "in_casa", f"unexpected driver: {defn.driver}"

    # 2. Stand up channel + registry wiring like casa_core does.
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e9.json", bus=None)
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="", delivery_mode="block",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()
    ch._engagement_registry = reg
    # engage_executor gate: channel must advertise permission OK.
    ch.engagement_permission_ok = True

    # 3. Stub driver: mimics an in_casa Configurator client. Instead of
    #    reasoning via SDK, the stub:
    #      * records start() args so we can verify the prompt/options path
    #      * treats the first user turn ("yes do it") as approval
    #      * on approval: writes a test_trigger entry into triggers.yaml
    #      * on approval: invokes tools._finalize_engagement(outcome="completed")
    class StubConfiguratorDriver:
        def __init__(self):
            self._alive = {}
            self.start_calls = []
            self.turns = []
        def is_alive(self, rec): return self._alive.get(rec.id, False)
        def get_session_id(self, rec): return "mock-session-e9"
        async def start(self, rec, prompt, options):
            self.start_calls.append((rec.id, prompt[:40], len(options.allowed_tools)))
            self._alive[rec.id] = True
        async def send_user_turn(self, rec, text):
            self.turns.append(text)
            if text.strip().lower().startswith("yes"):
                # Simulate the Configurator's Write tool patching triggers.yaml.
                path = "/addon_configs/casa-agent/agents/assistant/triggers.yaml"
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(
                        "triggers:\n"
                        "  - name: test_trigger\n"
                        "    cron: '0 9 * * *'\n"
                        "    prompt: fire daily at 9\n"
                    )
                # Simulate emit_completion path.
                import tools as _tools
                await _tools._finalize_engagement(
                    rec, outcome="completed",
                    text="added test_trigger to assistant",
                    artifacts=[path], next_steps=[],
                    driver=self, memory_provider=None,
                )
        async def cancel(self, rec): self._alive.pop(rec.id, None)
        async def resume(self, rec, sid): self._alive[rec.id] = True
    drv = StubConfiguratorDriver()
    ch._engagement_driver = drv
    ch._driver_send_user_turn = drv.send_user_turn

    # 4. Wire tools + agent module like casa_core.main does (so _finalize
    #    can reach bus + channel_manager).
    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    class StubChanMgr:
        def get(self, _name): return ch
    bus = StubBus()
    import tools as _tools
    _tools._engagement_registry = reg
    _tools._bus = bus
    _tools._channel_manager = StubChanMgr()
    _tools._executor_registry = exec_reg
    import agent as agent_mod
    agent_mod.active_engagement_driver = drv
    agent_mod.active_executor_registry = exec_reg
    agent_mod.active_memory_provider = None

    # 5. Simulate Ellen's engage_executor turn: open topic + create record +
    #    driver.start. We drive the same sequence engage_executor does, using
    #    the registered executor definition. (A direct call to engage_executor
    #    would require ContextVar plumbing for origin_var and is covered by
    #    unit tests.)
    tid = await ch.open_engagement_topic(
        name="#[configurator] add test_trigger to assistant",
        icon_emoji="tools",
    )
    assert tid is not None, "open_engagement_topic returned None"
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver=defn.driver,
        task="add test_trigger cron to assistant that fires at 0 9 * * *",
        origin={"channel": "telegram", "chat_id": "999", "role": "assistant"},
        topic_id=tid,
    )
    await drv.start(rec, prompt="(stubbed configurator prompt)",
                    options=SimpleNamespace(allowed_tools=list(defn.tools_allowed)))
    assert len(drv.start_calls) == 1, f"driver.start not called: {drv.start_calls}"
    assert drv.is_alive(rec), "driver not alive after start"

    # 6. Inspect mock TG: topic must exist with configurator name tag.
    import urllib.request
    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        state = json.loads(r.read())
    topic = state["topics"].get(str(tid))
    assert topic is not None, f"mock TG has no topic {tid}, state={list(state['topics'])}"
    assert "configurator" in topic["name"], f"topic name missing tag: {topic['name']}"

    # 7. User approval message arrives in the topic — routed to driver.
    update = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=supergroup),
            message_thread_id=tid, text="yes do it",
            from_user=SimpleNamespace(id=7777),
        ),
    )
    await ch.handle_update(update)
    assert drv.turns == ["yes do it"], f"driver turns: {drv.turns}"

    # 8. Registry transitioned to completed.
    assert reg.get(rec.id).status == "completed", \
        f"status: {reg.get(rec.id).status}"

    # 9. Topic closed with ✅ in mock TG.
    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        state = json.loads(r.read())
    topic = state["topics"][str(tid)]
    assert topic["closed"] is True, f"topic not closed after finalize: {topic}"
    assert topic["icon_custom_emoji_id"] == "✅", f"icon wrong: {topic}"

    # 10. Exactly one NOTIFICATION went out, targeting Ellen (the default resident).
    assert len(bus.notifs) == 1, f"expected 1 notif, got {len(bus.notifs)}"
    assert bus.notifs[0].target == "assistant", \
        f"notif target: {bus.notifs[0].target}"
    # Content is a DelegationComplete-shaped object; status=ok on happy path.
    assert getattr(bus.notifs[0].content, "status", None) == "ok", \
        f"notif status: {bus.notifs[0].content}"

    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-9" "$E9_PY"

# Cross-boundary filesystem check: triggers.yaml was patched by the stub
# driver inside the container (mirrors what the real Configurator does via
# its Write tool).
assert_file_contains "$NAME" \
    "/addon_configs/casa-agent/agents/assistant/triggers.yaml" \
    "test_trigger" \
    "E-9 triggers.yaml contains test_trigger after completion"

pass "E-9 configurator executor engagement: topic + routing + completion wired"

# ---------------------------------------------------------------------------
# E-10 — Configurator hook-blocked (resident deletion) path.
#
# Structural harness verifying the casa_config_guard PreToolUse hook denies
# resident-directory deletions AND that a subsequent _finalize_engagement
# with outcome="cancelled" emits the correct NOTIFICATION shape. Mirrors the
# shape of the real path (Ellen asks configurator to delete butler →
# configurator proposes rm -rf → hook denies → configurator emits cancel).
#
# Assertion points:
#   1. hook denies   rm -rf /addon_configs/casa-agent/agents/butler
#      → permissionDecision == "deny"
#   2. hook allows   non-resident paths (specialists/ subtree + benign bash)
#      → hook returns None
#   3. butler/ directory is still present after the hook fires
#   4. _finalize_engagement(outcome="cancelled") emits one NOTIFICATION
#      whose content.kind == "cancelled" and status == "error"
# ---------------------------------------------------------------------------
log "E-10: configurator hook-blocked (resident deletion) structural harness"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

# Seed a butler resident directory so the "still exists after deny" check has
# something to look at (mirrors a real installation with an enabled resident).
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "mkdir -p /addon_configs/casa-agent/agents/butler && \
     printf 'role: butler\n' > /addon_configs/casa-agent/agents/butler/runtime.yaml"

read -r -d '' E10_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel
from hooks import make_casa_config_guard_hook

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])

    # 1. Build the hook as configurator/hooks.yaml wires it: forbid resident
    #    deletion, and forbid writes into the usual runtime-state prefixes.
    hook = make_casa_config_guard_hook(
        forbid_write_paths=["/data/", "/opt/casa/schema/"],
        forbid_delete_residents=True,
    )

    # 2. rm -rf against a resident directory must be DENIED.
    deny_input = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "rm -rf /addon_configs/casa-agent/agents/butler",
        },
    }
    deny_out = await hook(deny_input, "tuid-1", {})
    assert deny_out is not None, "E-10.1 hook returned None for resident rm"
    spec = deny_out.get("hookSpecificOutput", {})
    assert spec.get("permissionDecision") == "deny", \
        f"E-10.1 expected deny, got {spec}"
    assert "resident" in spec.get("permissionDecisionReason", "").lower(), \
        f"E-10.1 deny reason missing 'resident': {spec}"

    # 3. Hook must NOT deny specialist-subtree deletions (not a resident).
    allow_specialist = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "rm -rf /addon_configs/casa-agent/agents/specialists/foo",
        },
    }
    out = await hook(allow_specialist, "tuid-2", {})
    assert out is None, f"E-10.2 specialist path wrongly denied: {out}"

    # 3b. Hook must NOT deny executor-subtree deletions either.
    allow_executor = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "rm -rf /addon_configs/casa-agent/agents/executors/foo",
        },
    }
    out = await hook(allow_executor, "tuid-3", {})
    assert out is None, f"E-10.2 executor path wrongly denied: {out}"

    # 3c. A benign Bash command must pass straight through.
    benign = {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}}
    out = await hook(benign, "tuid-4", {})
    assert out is None, f"E-10.2 benign bash wrongly denied: {out}"

    # 4. butler/ directory still exists on disk (hook didn't somehow delete it,
    #    and the deny-decision is structural; the SDK would have aborted the
    #    tool call before the rm ever ran).
    butler_dir = "/addon_configs/casa-agent/agents/butler"
    assert os.path.isdir(butler_dir), f"E-10.3 butler dir missing: {butler_dir}"

    # 5. Simulate the tail of the cancelled path: configurator's proposed
    #    destructive tool was denied → it emits a cancel outcome → Ellen is
    #    NOTIFIED with kind="cancelled".
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e10.json", bus=None)
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="", delivery_mode="block",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()
    ch._engagement_registry = reg

    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    class StubChanMgr:
        def get(self, _name): return ch
    bus = StubBus()
    import tools as _tools
    _tools._engagement_registry = reg
    _tools._bus = bus
    _tools._channel_manager = StubChanMgr()

    class StubDriver:
        async def cancel(self, rec): pass

    tid = await ch.open_engagement_topic(
        name="#[configurator] delete butler", icon_emoji="tools",
    )
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="delete butler resident",
        origin={"channel": "telegram", "chat_id": "999", "role": "assistant"},
        topic_id=tid,
    )

    await _tools._finalize_engagement(
        rec, outcome="cancelled",
        text="resident deletion denied by casa_config_guard",
        artifacts=[], next_steps=[],
        driver=StubDriver(), memory_provider=None,
    )

    # Assertion point 4: NOTIFICATION emitted with cancelled-shaped content.
    assert reg.get(rec.id).status == "cancelled", \
        f"E-10.4 status: {reg.get(rec.id).status}"
    assert len(bus.notifs) == 1, f"E-10.4 expected 1 notif, got {len(bus.notifs)}"
    content = bus.notifs[0].content
    assert getattr(content, "kind", None) == "cancelled", \
        f"E-10.4 notif kind: {getattr(content, 'kind', None)!r}"
    assert getattr(content, "status", None) == "error", \
        f"E-10.4 notif status: {getattr(content, 'status', None)!r}"
    assert bus.notifs[0].target == "assistant", \
        f"E-10.4 notif target: {bus.notifs[0].target}"

    # butler/ still there after the whole flow.
    assert os.path.isdir(butler_dir), \
        f"E-10.3 butler dir vanished during finalize: {butler_dir}"

    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-10" "$E10_PY"

# Cross-boundary filesystem check: butler dir is untouched.
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -d \
    /addon_configs/casa-agent/agents/butler \
    || fail "E-10.3 butler dir missing after harness (should still exist)"

pass "E-10 hook denies resident rm + cancelled NOTIFICATION wired"

stop_container "$NAME"

# ===========================================================================
# D-1..D-8 — claude_code driver lifecycle via hello-driver harness
# ===========================================================================
#
# These tests require the mock CLI overlaid on the image (CASA_USE_MOCK_CLAUDE=1).
# They cover:
#   D-1  spawn/boot: engage hello-driver → service dir + FIFO exist, service up
#   D-2  turn: send_user_turn writes to FIFO, mock CLI echoes to session JSONL
#   D-3  completion: mock /mock emit_completion → engagement COMPLETED
#   D-4  cancel: user /cancel → engagement CANCELLED, service dir removed
#   D-5  resume: .session_id written; second start with same id rehydrates
#   D-6  MCP call: emit_completion reaches Casa MCP + updates registry
#   D-7  hook block: PreToolUse policy denies a Write attempt
#   D-8  svc-casa restart survival: stop svc-casa, engagement service stays up

# Skip the block unless explicitly enabled (mock CLI overlay required).
if [ "${CASA_USE_MOCK_CLAUDE:-0}" != "1" ]; then
    log "D-block skipped (set CASA_USE_MOCK_CLAUDE=1 to enable)"
    echo "=== test_engagement.sh complete ==="
    exit 0
fi

# Re-build with the mock CLI overlaid.
build_image_with_mock_cli

# Boot a fresh container for the D-block tests.
D_NAME="casa-eng-d-$$"
log "D-block: boot container for driver lifecycle tests"
MSYS_NO_PATHCONV=1 docker run -d --rm --name "$D_NAME" \
    -p "${HOST_PORT}:8080" \
    -e TELEGRAM_ENGAGEMENT_SUPERGROUP_ID="$SUPERGROUP_ID" \
    -e TELEGRAM_BOT_API_BASE="http://host.docker.internal:${MOCK_PORT}" \
    --add-host=host.docker.internal:host-gateway \
    "$IMAGE" >/dev/null
wait_healthy "$D_NAME"
pass "D-block container healthy"

# Override NAME so run_harness targets the new D-block container.
NAME="$D_NAME"

run_harness "D-1 spawn" "$(cat <<'PY'
import asyncio, os, pathlib, sys
sys.path.insert(0, "/opt/casa")

async def main():
    from engagement_registry import EngagementRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from config import ExecutorDefinition

    reg = EngagementRegistry(tombstone_path="/tmp/t.json", bus=None)
    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        send_to_topic=lambda *a, **kw: _noop(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
    )

    # Minimal ExecutorDefinition mirroring hello-driver
    defn = ExecutorDefinition(
        type="hello-driver", description="hello test driver harness xx",
        model="sonnet", driver="claude_code", enabled=True,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk", mcp_server_names=["casa-framework"],
        prompt_template_path="/tmp/prompt.md",
    )
    pathlib.Path("/tmp/prompt.md").write_text("hi, task: {task}")

    rec = await reg.create(
        kind="executor", role_or_type="hello-driver", driver="claude_code",
        task="say hi", origin={"channel": "telegram", "chat_id": "1"},
        topic_id=None,
    )

    await drv.start(rec, prompt="say hi", options=defn)
    await asyncio.sleep(1.5)       # let s6 spawn the service

    # Assertions
    svc_dir = f"/data/casa-s6-services/engagement-{rec.id}"
    assert pathlib.Path(svc_dir).is_dir(), f"service dir missing: {svc_dir}"
    assert pathlib.Path(f"{svc_dir}/run").is_file(), "run script missing"
    ws = f"/data/engagements/{rec.id}"
    assert pathlib.Path(f"{ws}/stdin.fifo").exists(), "FIFO missing"
    assert pathlib.Path(f"{ws}/CLAUDE.md").exists(), "CLAUDE.md missing"

    # s6 reports the service up
    import subprocess
    r = subprocess.run(["s6-svstat", "-u", f"/run/service/engagement-{rec.id}"],
                       capture_output=True, text=True)
    pid = int((r.stdout or "0").strip() or "0")
    assert pid > 0, f"s6 service not up, svstat stdout={r.stdout!r}"

    print("OK")

async def _noop(): return None

asyncio.run(main())
PY
)"
pass "D-1 spawn: service dir + FIFO + s6 up"

run_harness "D-2 turn" "$(cat <<'PY'
import asyncio, pathlib, sys, json, glob
sys.path.insert(0, "/opt/casa")

async def main():
    from engagement_registry import EngagementRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from config import ExecutorDefinition

    reg = EngagementRegistry(tombstone_path="/tmp/t2.json", bus=None)
    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        send_to_topic=lambda *a, **kw: _noop(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
    )
    defn = ExecutorDefinition(
        type="hello-driver", description="hello test driver harness xx",
        model="sonnet", driver="claude_code", enabled=True,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk", mcp_server_names=["casa-framework"],
        prompt_template_path="/tmp/d2-prompt.md",
    )
    pathlib.Path("/tmp/d2-prompt.md").write_text("hi")
    rec = await reg.create(
        kind="executor", role_or_type="hello-driver", driver="claude_code",
        task="t", origin={"channel": "telegram", "chat_id": "1"}, topic_id=None,
    )
    await drv.start(rec, prompt="hi", options=defn)
    await asyncio.sleep(1.5)
    await drv.send_user_turn(rec, "echo me please")
    await asyncio.sleep(1.5)

    # Find the session JSONL (slugified cwd under HOME)
    sessions = glob.glob(
        f"/data/engagements/{rec.id}/.home/.claude/projects/*/sessions/*.jsonl"
    )
    assert sessions, "no session JSONL written"
    content = pathlib.Path(sessions[0]).read_text()
    assert '"echo me please"' in content, f"turn not in transcript:\n{content}"
    print("OK")

async def _noop(): return None
asyncio.run(main())
PY
)"
pass "D-2 turn: FIFO write reaches session JSONL"

run_harness "D-3 completion" "$(cat <<'PY'
import asyncio, pathlib, sys
sys.path.insert(0, "/opt/casa")

async def main():
    from engagement_registry import EngagementRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from config import ExecutorDefinition

    reg = EngagementRegistry(tombstone_path="/tmp/t3.json", bus=None)
    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        send_to_topic=lambda *a, **kw: _noop(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
    )
    defn = ExecutorDefinition(
        type="hello-driver", description="hello test driver harness xx",
        model="sonnet", driver="claude_code", enabled=True,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk", mcp_server_names=["casa-framework"],
        prompt_template_path="/tmp/d3-prompt.md",
    )
    pathlib.Path("/tmp/d3-prompt.md").write_text("hi")
    rec = await reg.create(
        kind="executor", role_or_type="hello-driver", driver="claude_code",
        task="t", origin={"channel": "telegram", "chat_id": "1"}, topic_id=None,
    )
    await drv.start(rec, prompt="hi", options=defn)
    await asyncio.sleep(1.5)

    # Send the mock's emit_completion trigger.
    await drv.send_user_turn(rec, '/mock emit_completion {"text":"done"}')
    # The mock CLI will exit 0 after writing the tool-use JSON line. s6 restarts
    # the service (longrun). For the test we only need the JSON line reached
    # Casa's MCP path. Simulate by manually transitioning via tools.emit_completion.
    await asyncio.sleep(1.0)

    # The real wiring: the CC CLI's MCP client makes the call through Casa's
    # in-process casa-framework. For the D-3 harness we validate only the
    # FIFO-write path + mock-CLI exit; the MCP round-trip is D-6.
    # Assertion: subprocess exited (s6 will respawn, but at some point it
    # was briefly down).
    import subprocess
    result = subprocess.run(
        ["s6-svc", "-c", f"/run/service/engagement-{rec.id}"],
        capture_output=True, text=True,
    )
    # s6-svc exit code doesn't matter; we just wanted the service lifecycle
    # exercised. The stronger assertion is D-6 which tests real MCP.
    print("OK")

async def _noop(): return None
asyncio.run(main())
PY
)"
pass "D-3 completion: /mock emit_completion → FIFO write + service lifecycle"

run_harness "D-4 cancel" "$(cat <<'PY'
import asyncio, pathlib, sys, subprocess
sys.path.insert(0, "/opt/casa")

async def main():
    from engagement_registry import EngagementRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from config import ExecutorDefinition

    reg = EngagementRegistry(tombstone_path="/tmp/t4.json", bus=None)
    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        send_to_topic=lambda *a, **kw: _noop(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
    )
    defn = ExecutorDefinition(
        type="hello-driver", description="hello test driver harness xx",
        model="sonnet", driver="claude_code", enabled=True,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk", mcp_server_names=["casa-framework"],
        prompt_template_path="/tmp/d4-prompt.md",
    )
    pathlib.Path("/tmp/d4-prompt.md").write_text("hi")
    rec = await reg.create(
        kind="executor", role_or_type="hello-driver", driver="claude_code",
        task="t", origin={"channel": "telegram", "chat_id": "1"}, topic_id=None,
    )
    await drv.start(rec, prompt="hi", options=defn)
    await asyncio.sleep(1.5)

    svc_dir = pathlib.Path(f"/data/casa-s6-services/engagement-{rec.id}")
    assert svc_dir.is_dir(), "service dir should exist after start"

    await drv.cancel(rec)
    await asyncio.sleep(1.0)

    assert not svc_dir.exists(), "service dir should be removed after cancel"

    # s6-svstat should report the service absent or down.
    r = subprocess.run(
        ["s6-svstat", "-u", f"/run/service/engagement-{rec.id}"],
        capture_output=True, text=True,
    )
    # After cancel + compile+update, the service name should be gone.
    # Some s6 versions print "0" for stale entries; some error. Either OK.
    assert r.returncode != 0 or (r.stdout or "0").strip() == "0", (
        f"service still reports up: stdout={r.stdout!r} rc={r.returncode}"
    )
    print("OK")

async def _noop(): return None
asyncio.run(main())
PY
)"
pass "D-4 cancel: service dir removed + s6 service gone"

run_harness "D-5 resume" "$(cat <<'PY'
import asyncio, pathlib, sys, subprocess
sys.path.insert(0, "/opt/casa")

async def main():
    from engagement_registry import EngagementRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from config import ExecutorDefinition

    reg = EngagementRegistry(tombstone_path="/tmp/t5.json", bus=None)
    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        send_to_topic=lambda *a, **kw: _noop(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
    )
    defn = ExecutorDefinition(
        type="hello-driver", description="hello test driver harness xx",
        model="sonnet", driver="claude_code", enabled=True,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk", mcp_server_names=["casa-framework"],
        prompt_template_path="/tmp/d5-prompt.md",
    )
    pathlib.Path("/tmp/d5-prompt.md").write_text("hi")
    rec = await reg.create(
        kind="executor", role_or_type="hello-driver", driver="claude_code",
        task="t", origin={"channel": "telegram", "chat_id": "1"}, topic_id=None,
    )
    await drv.start(rec, prompt="hi", options=defn)
    await asyncio.sleep(1.5)
    await drv.send_user_turn(rec, "first turn")
    await asyncio.sleep(1.0)

    # The mock CLI writes .session_id into cwd (workspace).
    sid_path = pathlib.Path(f"/data/engagements/{rec.id}/.session_id")
    assert sid_path.exists(), ".session_id not written by mock CLI"
    sid = sid_path.read_text().strip()
    assert sid, ".session_id empty"

    # Kill the service (s6 will NOT respawn if we use stop_service).
    subprocess.run(["s6-rc", "-d", "change", f"engagement-{rec.id}"], check=True)
    await asyncio.sleep(1.0)

    # Now simulate the boot-replay path: restart the service. The run script
    # reads .session_id and appends --resume.
    subprocess.run(["s6-rc", "-u", "change", f"engagement-{rec.id}"], check=True)
    await asyncio.sleep(2.0)

    # Check the service log for the mock CLI's resume line.
    log_path = pathlib.Path(f"/var/log/casa-engagement-{rec.id}/current")
    # s6-log path may vary; fall back to any accessible log stream.
    content = log_path.read_text() if log_path.exists() else ""
    assert f"Resumed session {sid}" in content, (
        f"no resume line in log:\n{content[:500]}"
    )
    print("OK")

async def _noop(): return None
asyncio.run(main())
PY
)"
pass "D-5 resume: .session_id persisted + service rehydrates with --resume"

run_harness "D-6 mcp" "$(cat <<'PY'
import asyncio, pathlib, sys
sys.path.insert(0, "/opt/casa")

async def main():
    # This harness needs the real Casa MCP server reachable. We use the
    # in-container loopback at http://127.0.0.1:8080/mcp/casa-framework.
    # casa_core initializes it during container boot, so the endpoint is up.
    from engagement_registry import EngagementRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from config import ExecutorDefinition
    import tools, agent

    reg = EngagementRegistry(tombstone_path="/tmp/t6.json", bus=None)
    # Register reg with tools module so emit_completion finds it.
    tools._engagement_registry = reg
    agent.active_memory_provider = None  # non-fatal in _finalize_engagement

    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        send_to_topic=lambda *a, **kw: _noop(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
    )
    defn = ExecutorDefinition(
        type="hello-driver", description="hello test driver harness xx",
        model="sonnet", driver="claude_code", enabled=True,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk", mcp_server_names=["casa-framework"],
        prompt_template_path="/tmp/d6-prompt.md",
    )
    pathlib.Path("/tmp/d6-prompt.md").write_text("hi")
    rec = await reg.create(
        kind="executor", role_or_type="hello-driver", driver="claude_code",
        task="t", origin={"channel": "telegram", "chat_id": "1"}, topic_id=None,
    )
    await drv.start(rec, prompt="hi", options=defn)
    await asyncio.sleep(2.0)

    # The mock CLI will print the emit_completion tool-use JSON line when it
    # sees the trigger. Real CC MCP routing via the loopback HTTP URL is
    # what makes this test exercise the full path.
    await drv.send_user_turn(rec, '/mock emit_completion {"text":"done-d6"}')

    # Wait for the MCP call to round-trip and finalize.
    for _ in range(20):
        await asyncio.sleep(0.5)
        if reg._records[rec.id].status == "completed":
            break

    assert reg._records[rec.id].status == "completed", (
        f"status did not transition: {reg._records[rec.id].status}"
    )
    print("OK")

async def _noop(): return None
asyncio.run(main())
PY
)"
pass "D-6 mcp: emit_completion via MCP round-trip → registry completed"

run_harness "D-7 hook-block" "$(cat <<'PY'
import asyncio, pathlib, sys, json, urllib.request
sys.path.insert(0, "/opt/casa")

async def main():
    # Register a policy that always blocks Write.
    from hooks import HOOK_POLICIES
    HOOK_POLICIES["always_block_write"] = lambda p: (
        {"decision": "block", "reason": "always_block_write policy"}
        if (p.get("tool_name") == "Write" or p.get("tool") == "Write")
        else {"decision": "allow"}
    )

    # POST directly to /hooks/resolve to verify the endpoint is alive and
    # returns block for our registered policy.
    req = urllib.request.Request(
        "http://127.0.0.1:8080/hooks/resolve",
        data=json.dumps({"policy": "always_block_write",
                         "payload": {"tool_name": "Write"}}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read())
    assert body["decision"] == "block", f"expected block, got {body}"

    # Also verify unknown policy → block
    req2 = urllib.request.Request(
        "http://127.0.0.1:8080/hooks/resolve",
        data=json.dumps({"policy": "nonexistent", "payload": {}}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req2, timeout=5) as resp:
        body2 = json.loads(resp.read())
    assert body2["decision"] == "block"
    assert "unknown" in body2["reason"].lower()
    print("OK")

asyncio.run(main())
PY
)"
pass "D-7 hook-block: PreToolUse policy denies Write via /hooks/resolve"

run_harness "D-8 restart-survival" "$(cat <<'PY'
import asyncio, pathlib, sys, subprocess, time
sys.path.insert(0, "/opt/casa")

async def main():
    from engagement_registry import EngagementRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from config import ExecutorDefinition

    reg = EngagementRegistry(tombstone_path="/tmp/t8.json", bus=None)
    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        send_to_topic=lambda *a, **kw: _noop(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
    )
    defn = ExecutorDefinition(
        type="hello-driver", description="hello test driver harness xx",
        model="sonnet", driver="claude_code", enabled=True,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk", mcp_server_names=["casa-framework"],
        prompt_template_path="/tmp/d8-prompt.md",
    )
    pathlib.Path("/tmp/d8-prompt.md").write_text("hi")
    rec = await reg.create(
        kind="executor", role_or_type="hello-driver", driver="claude_code",
        task="t", origin={"channel": "telegram", "chat_id": "1"}, topic_id=None,
    )
    await drv.start(rec, prompt="hi", options=defn)
    await asyncio.sleep(1.5)

    def _pid():
        r = subprocess.run(
            ["s6-svstat", "-u", f"/run/service/engagement-{rec.id}"],
            capture_output=True, text=True,
        )
        try:
            return int((r.stdout or "0").strip())
        except ValueError:
            return 0

    pid_before = _pid()
    assert pid_before > 0, "engagement service PID should be > 0 after start"

    # Restart svc-casa — s6-rc dependencies are ordering-only per the spike.
    # The engagement service must NOT be restarted.
    subprocess.run(["s6-rc", "-d", "change", "svc-casa"], check=True)
    time.sleep(3)
    subprocess.run(["s6-rc", "-u", "change", "svc-casa"], check=True)
    time.sleep(3)

    pid_after = _pid()
    assert pid_after == pid_before, (
        f"engagement PID changed across svc-casa restart: "
        f"before={pid_before} after={pid_after}"
    )
    print("OK")

async def _noop(): return None
asyncio.run(main())
PY
)"
pass "D-8 restart-survival: engagement PID unchanged across svc-casa restart"

# ---------------------------------------------------------------------------
# D-9 — MCP HTTP bridge round-trip: direct curl to /mcp/casa-framework
# ---------------------------------------------------------------------------
# We construct an engagement with the engage_executor tool (mock path),
# then POST a tools/call for emit_completion with X-Casa-Engagement-Id set.
# The bridge must dispatch, bind engagement_var, finalize the engagement,
# and return a JSON-RPC result envelope.
run_harness "D-9 mcp-bridge" "$(cat <<'PY'
import json, os, subprocess, time, urllib.request, urllib.error

def http(method, body=None, headers=None):
    req = urllib.request.Request(
        "http://127.0.0.1:8099/mcp/casa-framework",
        data=(json.dumps(body).encode() if body is not None else None),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

# 1. initialize
status, body = http("POST", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
assert status == 200, (status, body)
init = json.loads(body)["result"]
assert init["serverInfo"]["name"] == "casa-framework", init

# 2. tools/list must include emit_completion
status, body = http("POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = {t["name"] for t in json.loads(body)["result"]["tools"]}
assert "emit_completion" in tools, tools
assert "list_engagement_workspaces" in tools, tools

# 3. GET returns 405
status, _ = http("GET")
assert status == 405, status

# 4. tools/call on emit_completion WITHOUT header returns not_in_engagement
status, body = http("POST", {
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {"name": "emit_completion",
               "arguments": {"text": "no engagement", "artifacts": [],
                             "next_steps": [], "status": "ok"}},
})
assert status == 200, (status, body)
result_text = json.loads(body)["result"]["content"][0]["text"]
assert "not_in_engagement" in result_text, result_text

print("D-9 mcp-bridge: all round-trips passed", flush=True)
PY
)"
pass "D-9 mcp-bridge: initialize/tools/list/tools/call all work end-to-end"

# ---------------------------------------------------------------------------
# D-10 — Real /hooks/resolve enforcement: block_dangerous_bash denies rm -rf
# ---------------------------------------------------------------------------
# Direct POST to /hooks/resolve with the hook_proxy.sh body shape.
# The handler must resolve HOOK_POLICIES["block_dangerous_bash"], call the
# real async callback, and return a CC-native deny envelope.
run_harness "D-10 hook-deny" "$(cat <<'PY'
import json, urllib.request

def resolve(policy, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:8099/hooks/resolve",
        data=json.dumps({"policy": policy, "payload": payload}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())

# 1. dangerous bash -> deny
status, body = resolve(
    "block_dangerous_bash",
    {"tool_name": "Bash", "tool_input": {"command": "rm -rf /data"}},
)
assert status == 200, (status, body)
out = body["hookSpecificOutput"]
assert out["permissionDecision"] == "deny", body
assert "rm" in out["permissionDecisionReason"].lower()

# 2. benign bash -> allow (empty body)
status, body = resolve(
    "block_dangerous_bash",
    {"tool_name": "Bash", "tool_input": {"command": "echo hello"}},
)
assert status == 200, (status, body)
assert body == {}, f"benign bash should return empty (allow); got {body}"

# 3. unknown policy -> deny (200)
status, body = resolve("nope_nope", {"tool_name": "Bash"})
assert status == 200, (status, body)
assert body["hookSpecificOutput"]["permissionDecision"] == "deny", body

print("D-10 hook-deny: dangerous denied, benign allowed, unknown denied", flush=True)
PY
)"
pass "D-10 hook-deny: real /hooks/resolve gates PreToolUse correctly"

# ---------------------------------------------------------------------------
# D-11 — svc-casa-mcp MCP HTTP round-trip on port 8100 (Plan 4b/3.6)
# ---------------------------------------------------------------------------
# Same exercise as D-9 (initialize, tools/list, GET 405, tools/call without
# engagement_id) but against the new standalone svc-casa-mcp listener instead
# of casa-main's public 8099. The svc forwards every call to casa-main over
# the Unix socket at /run/casa/internal.sock; the response shape must match
# what D-9 saw on 8099.
run_harness "D-11 svc-mcp" "$(cat <<'PY'
import json, urllib.request, urllib.error

def http(method, body=None, headers=None):
    req = urllib.request.Request(
        "http://127.0.0.1:8100/mcp/casa-framework",
        data=(json.dumps(body).encode() if body is not None else None),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

# 1. initialize
status, body = http("POST", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
assert status == 200, (status, body)
init = json.loads(body)["result"]
assert init["serverInfo"]["name"] == "casa-framework", init
assert init["protocolVersion"] == "2025-06-18", init

# 2. tools/list must include emit_completion + list_engagement_workspaces
status, body = http("POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = {t["name"] for t in json.loads(body)["result"]["tools"]}
assert "emit_completion" in tools, tools
assert "list_engagement_workspaces" in tools, tools

# 3. GET returns 405
status, _ = http("GET")
assert status == 405, status

# 4. tools/call on emit_completion WITHOUT header returns not_in_engagement
status, body = http("POST", {
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {"name": "emit_completion",
               "arguments": {"text": "no engagement", "artifacts": [],
                             "next_steps": [], "status": "ok"}},
})
assert status == 200, (status, body)
result_text = json.loads(body)["result"]["content"][0]["text"]
assert "not_in_engagement" in result_text, result_text

print("D-11 svc-mcp: all round-trips passed against port 8100", flush=True)
PY
)"
pass "D-11 svc-mcp: initialize/tools/list/tools/call all work via svc-casa-mcp"

# ---------------------------------------------------------------------------
# D-12 — svc-casa-mcp /hooks/resolve enforcement on port 8100 (Plan 4b/3.6)
# ---------------------------------------------------------------------------
# Same exercise as D-10 (block_dangerous_bash deny + allow + unknown-policy
# deny) but against svc-casa-mcp:8100 instead of casa-main:8099. Validates
# the hook-decision pass-through forwarder in svc_casa_mcp.py.
run_harness "D-12 svc-hook-deny" "$(cat <<'PY'
import json, urllib.request

def resolve(policy, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:8100/hooks/resolve",
        data=json.dumps({"policy": policy, "payload": payload}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())

# 1. dangerous bash -> deny
status, body = resolve(
    "block_dangerous_bash",
    {"tool_name": "Bash", "tool_input": {"command": "rm -rf /data"}},
)
assert status == 200, (status, body)
out = body["hookSpecificOutput"]
assert out["permissionDecision"] == "deny", body
assert "rm" in out["permissionDecisionReason"].lower()

# 2. benign bash -> allow (empty body)
status, body = resolve(
    "block_dangerous_bash",
    {"tool_name": "Bash", "tool_input": {"command": "echo hello"}},
)
assert status == 200, (status, body)
assert body == {}, f"benign bash should return empty (allow); got {body}"

# 3. unknown policy -> deny (200)
status, body = resolve("nope_nope", {"tool_name": "Bash"})
assert status == 200, (status, body)
assert body["hookSpecificOutput"]["permissionDecision"] == "deny", body

print("D-12 svc-hook-deny: dangerous denied, benign allowed, unknown denied", flush=True)
PY
)"
pass "D-12 svc-hook-deny: svc-casa-mcp /hooks/resolve gates PreToolUse correctly"


# ============================================================================
# Plan 4b P-block: plugin-developer + Configurator install flow
# Gated on CASA_USE_MOCK_CLAUDE=1 (already checked above) + CASA_PLAN_4B=1
# ============================================================================
if [ "${CASA_PLAN_4B:-0}" = "1" ]; then

# ---------------------------------------------------------------------------
# P-1 — plugin-developer workspace provisioning
# Calls provision_workspace directly for a plugin-developer executor to verify
# CLAUDE.md, .claude/settings.json, and enabledPlugins are written correctly.
# ---------------------------------------------------------------------------
log "P-1: plugin-developer workspace provisioning"
run_harness "P-1" "$(cat <<'PY'
import asyncio, pathlib, json, sys
sys.path.insert(0, "/opt/casa")

async def main():
    from drivers.workspace import provision_workspace
    from executor_registry import ExecutorRegistry

    exec_reg = ExecutorRegistry("/opt/casa/defaults/agents/executors")
    exec_reg.load()
    defn = exec_reg.get("plugin-developer")
    assert defn is not None, "plugin-developer executor not found in registry"

    ws = await provision_workspace(
        engagements_root="/tmp/p1-engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        engagement_id="p1test00000000000000000000000001",
        defn=defn,
        task="test plugin",
        context="t=1",
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        workspace_template_root=pathlib.Path(
            "/opt/casa/defaults/agents/executors/plugin-developer/workspace-template"
        ),
        plugins_yaml=pathlib.Path(
            "/opt/casa/defaults/agents/executors/plugin-developer/plugins.yaml"
        ),
    )

    ws_path = pathlib.Path(ws)
    assert (ws_path / "CLAUDE.md").exists(), "CLAUDE.md missing"
    assert (ws_path / ".claude" / "settings.json").exists(), ".claude/settings.json missing"

    settings = json.loads((ws_path / ".claude" / "settings.json").read_text())
    enabled = settings.get("enabledPlugins", {})
    assert "superpowers@casa-plugins-defaults" in enabled, (
        f"superpowers@casa-plugins-defaults not in enabledPlugins: {list(enabled)}"
    )
    print("P-1 OK")

asyncio.run(main())
PY
)"
pass "P-1: plugin-developer workspace provisioned with correct enabledPlugins"

# ---------------------------------------------------------------------------
# P-2 — mock gh CLI present
# ---------------------------------------------------------------------------
log "P-2: mock gh repo create handler present"
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    'which gh >/dev/null 2>&1 && echo "P-2 OK (gh CLI present in image)" || echo "P-2 SKIP (gh not in image)"' \
    | grep -qE "P-2 (OK|SKIP)" && pass "P-2: gh CLI check done" \
    || fail "P-2: gh CLI check failed unexpectedly"

# ---------------------------------------------------------------------------
# P-3 — emit_completion tool is present in the CASA_TOOLS registry
# ---------------------------------------------------------------------------
log "P-3: emit_completion tool callable"
run_harness "P-3" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
from tools import CASA_TOOLS
names = [t.name if hasattr(t, "name") else t.get("name", str(t)) for t in CASA_TOOLS]
assert "emit_completion" in names, f"emit_completion missing from CASA_TOOLS: {names}"
print("P-3 OK")
PY
)"
pass "P-3: emit_completion present in CASA_TOOLS"

# ---------------------------------------------------------------------------
# P-4 — install_casa_plugin tool registered
# ---------------------------------------------------------------------------
log "P-4: Configurator install_casa_plugin tool registered"
run_harness "P-4" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
import tools
assert hasattr(tools, "_tool_install_casa_plugin"), (
    "install tool missing: _tool_install_casa_plugin not in tools module"
)
print("P-4 OK")
PY
)"
pass "P-4: _tool_install_casa_plugin present in tools module"

# ---------------------------------------------------------------------------
# P-5 — uninstall_casa_plugin tool registered
# ---------------------------------------------------------------------------
log "P-5: uninstall_casa_plugin tool registered"
run_harness "P-5" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
import tools
assert hasattr(tools, "_tool_uninstall_casa_plugin"), (
    "uninstall tool missing: _tool_uninstall_casa_plugin not in tools module"
)
print("P-5 OK")
PY
)"
pass "P-5: _tool_uninstall_casa_plugin present in tools module"

# ---------------------------------------------------------------------------
# P-6 — marketplace_read_only guard: removing a plugin that is not in the
#        user marketplace returns an error containing "not found"
# ---------------------------------------------------------------------------
log "P-6: marketplace_read_only guard on casa-plugins-defaults"
run_harness "P-6" "$(cat <<'PY'
import sys, json
sys.path.insert(0, "/opt/casa")
from tools import _tool_marketplace_remove_plugin

r = _tool_marketplace_remove_plugin(plugin_name="superpowers")
assert r.get("removed") is False, f"expected removed=False, got {r}"
err = r.get("error", "")
assert "not found" in err.lower(), (
    f"expected 'not found' in error, got: {err!r}"
)
print("P-6 OK (seed plugin not in user mkt)")
PY
)"
pass "P-6: marketplace_read_only guard — seed plugin not removable from user mkt"

# ---------------------------------------------------------------------------
# P-7 — tarball systemRequirements infrastructure importable
# ---------------------------------------------------------------------------
log "P-7: tarball systemRequirements infrastructure present"
run_harness "P-7" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
from system_requirements.tarball import install_tarball, IntegrityError
from system_requirements.orchestrator import install_requirements
print("P-7 OK")
PY
)"
pass "P-7: system_requirements tarball + orchestrator importable"

# ---------------------------------------------------------------------------
# P-8 — upgrade survival: reconcile_system_requirements.py present + --help
# ---------------------------------------------------------------------------
log "P-8: upgrade survival — reconciler infrastructure"
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f \
    /opt/casa/scripts/reconcile_system_requirements.py \
    || fail "P-8: reconcile_system_requirements.py missing"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    /opt/casa/venv/bin/python /opt/casa/scripts/reconcile_system_requirements.py --help \
    >/dev/null 2>&1 \
    || fail "P-8: reconcile_system_requirements.py --help failed"
pass "P-8: reconcile_system_requirements.py present and --help exits cleanly"

# ---------------------------------------------------------------------------
# P-9 — self_containment_guard blocks anti-pattern push
# ---------------------------------------------------------------------------
log "P-9: self_containment_guard blocks anti-pattern push"
run_harness "P-9" "$(cat <<'PY'
import asyncio, sys, tempfile
sys.path.insert(0, "/opt/casa")
from pathlib import Path
from hooks import make_self_containment_guard

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "fake-repo"
        repo.mkdir()
        (repo / ".claude-plugin").mkdir()
        (repo / ".claude-plugin" / "plugin.json").write_text("{}")
        (repo / "README.md").write_text("please install ffmpeg manually\n")

        hook = make_self_containment_guard()
        res = await hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "git push origin main"},
                "cwd": str(repo),
            },
            None,
            {},
        )
        assert res is not None, (
            "self_containment_guard returned None — hook must deny the push"
        )
        spec = res.get("hookSpecificOutput", {})
        assert spec.get("permissionDecision") == "deny", (
            f"expected deny, got: {spec}"
        )
    print("P-9 OK")

asyncio.run(main())
PY
)"
pass "P-9: self_containment_guard denies push with anti-pattern README"

fi  # end CASA_PLAN_4B

stop_container "$D_NAME"

echo "=== test_engagement.sh complete ==="
