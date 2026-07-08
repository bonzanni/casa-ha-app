#!/usr/bin/env python3
"""Phase 0.1 — sharpened pollution measurement (resolves the Phase 0 confound).

Phase 0 used a tiny corpus + a generous budget, so recall returned ~everything
and the 25%-technical figure partly reflected bank COMPOSITION, not a RANKING
failure. This script closes that gap with:

  * a LARGER corpus (24 household NL facts + 16 executor JSON blobs) so a tight
    recall budget cannot return the whole bank — ranking is forced into play;
  * a RECALL-BUDGET SWEEP (max_tokens 200 -> 2048), since at low budget only the
    top-ranked facts survive;
  * metrics that separate the two regimes:
      - SPECIFIC queries (one expected household fact): its RANK, how many
        technical facts rank ABOVE it, and whether it survives a tight budget.
        -> answers "do technical facts outrank/crowd out the relevant fact?"
      - BROAD queries (no single answer): % technical in the top-K.
        -> answers "where does pollution actually bite?" (broad/non-specific asks)

Throwaway bank only (eval-phase01-*); created + DELETED here. Run in the casa
container. Stock-default bank (no mission/entity_labels) = today's prod behaviour.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("HINDSIGHT_URL", "http://5884eb17-hindsight:8888").rstrip("/")
BANK = os.environ.get("EVAL_BANK", f"eval-phase01-{os.getpid()}-{int(time.time())}")
ALL_TIERS = ["public", "friends", "family", "private"]
VOICE_TIERS = ["public", "friends"]
BUDGETS = [200, 400, 800, 2048]
TOPK = 10  # for broad-query crowding

# --- household corpus (24) : (id, tier, content) -------------------------------
HOUSEHOLD = [
    ("wifi",     "family",  "The main wifi password is Hunter2Galaxy."),
    ("guestwifi","friends", "The guest wifi password is WelcomeFriend."),
    ("thermo",   "friends", "Nicola keeps the thermostat at 20 degrees in winter."),
    ("pickup",   "friends", "The kids, Sofia and Luca, are picked up from school at 3:30pm on weekdays."),
    ("dentist",  "friends", "Our dentist is Dr. Chen and the next cleaning is on June 18th at 2pm."),
    ("doctor",   "friends", "The family GP is Dr. Olsson at the Maple Street clinic."),
    ("allergy",  "friends", "Nicola is allergic to penicillin."),
    ("nutfree",  "friends", "Luca has a peanut allergy, so the house is nut-free."),
    ("energy",   "private", "We pay the energy bill by direct debit around the 5th of each month, about 140 euros."),
    ("mortgage", "private", "The mortgage payment of 1,250 euros goes out on the 1st of each month."),
    ("sparekey", "family",  "The spare house key is with our neighbour Mrs. Albright at number 12."),
    ("alarm",    "family",  "The home alarm disarm code is 4471."),
    ("grandma",  "friends", "Grandma visits every second Sunday for lunch."),
    ("bins",     "public",  "The bins are collected on Thursday mornings; recycling every other week."),
    ("paint",    "public",  "The living room is painted sage green."),
    ("vacuum",   "public",  "The robot vacuum is a Roborock S8."),
    ("car",      "public",  "The family car is a blue Volvo XC60."),
    ("plumber",  "friends", "Our plumber is Dave from Riverside Plumbing, number on the fridge."),
    ("piano",    "friends", "Sofia has piano lessons on Tuesdays at 5pm with Mr. Bauer."),
    ("anniv",    "friends", "Nicola and Anna's anniversary is on September 9th."),
    ("cat",      "friends", "The cat, Pepper, is fed twice a day and is due her vaccination in August."),
    ("holiday",  "friends", "We are going to the lake house for the first week of August."),
    ("milk",     "public",  "We usually buy oat milk, not dairy."),
    ("boiler",   "public",  "The boiler is a Vaillant ecoTEC serviced every September."),
]

# --- technical corpus (16) : executor JSON-blob summaries (tools.py:1578/1634) --
def _eng(eid, who, task, text, arts):
    return json.dumps({"kind": "engagement_summary", "engagement_id": eid,
                       "specialist_or_type": who, "task": task, "status": "completed",
                       "text": text, "artifacts": arts, "next_steps": []})
def _exe(eid, etype, task, last, arts):
    return json.dumps({"kind": "executor_engagement_summary", "engagement_id": eid,
                       "executor_type": etype, "task": task, "terminal_state": "completed",
                       "engager": "assistant", "last_text": last, "artifacts": arts})

TECHNICAL = [
    ("weathercard", "public", _eng("e1","frontend","build a weather Lovelace card",
        "Created weather-card.js using Lit 3.x, registered ha-weather-card, added a visual editor via getConfigElement.",
        ["weather-card.js","weather-card-editor.js"])),
    ("mqtt", "public", _exe("e2","configurator","add the MQTT integration",
        "Appended mqtt broker 192.168.1.10 port 1883 to configuration.yaml and reloaded.", ["configuration.yaml"])),
    ("s6crash", "public", _eng("e3","backend","debug the s6 service crash",
        "Fixed the svc-casa/run shebang; a CRLF line ending broke the interpreter. Converted to LF.", ["svc-casa/run"])),
    ("skill", "public", _exe("e4","plugin-builder","create a notification skill",
        "Wrote SKILL.md and notify.py implementing a Telegram push via the bot API; added to the marketplace manifest.",
        ["SKILL.md","notify.py"])),
    ("addonver", "public", _exe("e5","configurator","bump the add-on version",
        "Set version 0.46.1 in config.yaml and prepended a CHANGELOG entry for the hindsight toggle fix.", ["config.yaml","CHANGELOG.md"])),
    ("dockerfile", "public", _eng("e6","backend","optimize the Dockerfile",
        "Reordered the Dockerfile layers to cache pip installs; build dropped from 9 to 3 minutes.", ["Dockerfile"])),
    ("apparmor", "public", _exe("e7","configurator","tighten the AppArmor profile",
        "Added deny rules for /proc and network raw in apparmor.txt; smoke test passed.", ["apparmor.txt"])),
    ("ingress", "public", _eng("e8","frontend","fix ingress 502",
        "NPM was pointing at a stale container IP; switched the proxy host to c071ea9c-casa-agent:8099.", [])),
    ("pytest", "public", _exe("e9","backend","add unit tests",
        "Added test_run_script_env.py asserting MEMORY_BACKEND derivation; 7 tests pass under pytest -m unit.", ["tests/test_run_script_env.py"])),
    ("lovelace2", "public", _eng("e10","frontend","build a chore-chart card",
        "Implemented chore-chart-card.ts with Lit, a reactive store, and a YAML config schema.", ["chore-chart-card.ts"])),
    ("zigbee", "public", _exe("e11","configurator","pair a Zigbee sensor",
        "Paired a Sonoff SNZB-02 via ZHA; entity sensor.kitchen_temp created.", ["zha.db"])),
    ("backup", "public", _eng("e12","backend","script a nightly backup",
        "Wrote backup.sh using tar + restic to an S3 bucket; scheduled via an s6 cron oneshot.", ["backup.sh"])),
    ("hacs", "public", _exe("e13","plugin-builder","publish to HACS",
        "Added hacs.json and a GitHub release workflow; the repo now installs via custom repositories.", ["hacs.json",".github/workflows/release.yml"])),
    ("template", "public", _eng("e14","backend","write a Jinja template sensor",
        "Created a template sensor computing daily energy cost from sensor.power and a fixed tariff.", ["templates.yaml"])),
    ("oauth", "public", _exe("e15","configurator","wire the OAuth token",
        "Stored CLAUDE_CODE_OAUTH_TOKEN via the add-on options schema; svc-casa/run exports it to env.", ["config.yaml"])),
    ("websocket", "public", _eng("e16","backend","add a voice WS transport",
        "Implemented the /api/converse/ws handler with HMAC verify and prosodic block streaming.", ["channels/voice/channel.py"])),
]

SPECIFIC = [
    ("What's the wifi password?", "wifi"),
    ("When is the next dentist appointment?", "dentist"),
    ("What temperature do we keep the house at?", "thermo"),
    ("When are the kids picked up from school?", "pickup"),
    ("Is anyone in the house allergic to anything?", "allergy"),
    ("How much is the energy bill?", "energy"),
    ("Where is the spare house key?", "sparekey"),
    ("When does grandma come over?", "grandma"),
    ("Which robot vacuum do we own?", "vacuum"),
    ("What car do we drive?", "car"),
    ("When are Sofia's piano lessons?", "piano"),
    ("When is our holiday?", "holiday"),
]
BROAD = [
    "What do I need to remember about the house?",
    "Give me a summary of everything important at home.",
    "What's coming up this week?",
    "Tell me about the family.",
]


def _req(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw.strip() else {})


def recall(query, tiers, max_tokens):
    _, resp = _req("POST", f"/v1/default/banks/{BANK}/memories/recall",
                   {"query": query, "tags": tiers, "tags_match": "any",
                    "max_tokens": max_tokens,
                    "types": ["world", "experience", "observation"],
                    "budget": "mid"})
    return resp.get("results", []) or []


def origin_of(d):
    head = (d or "").split(":", 1)[0]
    return head.split("--", 1)[0] if "--" in head else "?"
def cid_of(d):
    head = (d or "").split(":", 1)[0]
    return head.split("--", 1)[1] if "--" in head else "?"


def build_items():
    items = []
    for cid, tier, content in HOUSEHOLD:
        items.append({"content": content, "tags": [tier],
                      "metadata": {"speaker": "nicola"}, "document_id": f"hh--{cid}:0"})
    for cid, tier, content in TECHNICAL:
        items.append({"content": content, "tags": [tier],
                      "metadata": {"speaker": "assistant"}, "document_id": f"tech--{cid}:0"})
    return items


def main():
    print(f"=== Phase 0.1 pollution/rank — bank={BANK} ===")
    print(f"corpus: {len(HOUSEHOLD)} household + {len(TECHNICAL)} technical items\n")
    st, _ = _req("POST", f"/v1/default/banks/{BANK}/memories",
                 {"async": False, "items": build_items()}, timeout=300)
    print(f"retain -> HTTP {st}; waiting for consolidation...")
    time.sleep(10)

    # ----- SPECIFIC queries: rank of target + technical facts ranked above it -----
    print("\n=== SPECIFIC queries (target = the one right household fact) ===")
    print(f"{'budget':>7} | {'found':>5} | {'avg rank':>8} | {'avg #tech-above-target':>22} | {'tech in result %':>16}")
    spec_summary = {}
    for b in BUDGETS:
        found = 0; ranks = []; tech_above = []; tech_frac = []
        for q, tgt in SPECIFIC:
            res = recall(q, ALL_TIERS, b)
            ids = [cid_of(m.get("document_id")) for m in res]
            origins = [origin_of(m.get("document_id")) for m in res]
            n = len(res)
            tech_frac.append((sum(1 for o in origins if o == "tech") / n) if n else 0)
            if tgt in ids:
                found += 1
                r = ids.index(tgt)
                ranks.append(r)
                tech_above.append(sum(1 for o in origins[:r] if o == "tech"))
        avg = lambda L: (sum(L) / len(L)) if L else 0.0
        spec_summary[b] = (found, avg(ranks), avg(tech_above), avg(tech_frac))
        print(f"{b:>7} | {found:>2}/{len(SPECIFIC):>2} | {avg(ranks):>8.1f} | "
              f"{avg(tech_above):>22.2f} | {avg(tech_frac)*100:>15.0f}%")

    # ----- BROAD queries: % technical in the top-K -----
    print(f"\n=== BROAD queries (no single answer) — technical share of top-{TOPK} ===")
    print(f"{'budget':>7} | {'avg %tech in top-'+str(TOPK):>20} | {'avg #facts returned':>20}")
    for b in BUDGETS:
        fracs = []; sizes = []
        for q in BROAD:
            res = recall(q, ALL_TIERS, b)[:TOPK]
            sizes.append(len(res))
            origins = [origin_of(m.get("document_id")) for m in res]
            fracs.append((sum(1 for o in origins if o == "tech") / len(res)) if res else 0)
        avg = lambda L: (sum(L) / len(L)) if L else 0.0
        print(f"{b:>7} | {avg(fracs)*100:>19.0f}% | {avg(sizes):>20.1f}")

    # ----- tier-leak guard -----
    leak = recall("how much is the energy bill?", VOICE_TIERS, 800)
    leaked = any(cid_of(m.get("document_id")) == "energy" for m in leak)
    print(f"\n(guard) tier-leak (private energy fact at voice/friends clearance): "
          f"{'LEAK!' if leaked else 'no leak (ok)'}")

    print("\n=== READING THE RESULT ===")
    print("- 'avg #tech-above-target' ~0  => technical facts do NOT outrank the relevant household")
    print("  fact (pollution is harmless long-tail; semantic ranking already deprioritises it).")
    print("- 'avg #tech-above-target' high => real crowding => Phase 2 (anti-pollution) justified.")
    print("- BROAD '%tech in top-K' high  => pollution bites on non-specific asks even if specific")
    print("  asks are fine. Compare 'found' at tight budget (200) to see recall survival.")
    return 0


def cleanup():
    try:
        _req("DELETE", f"/v1/default/banks/{BANK}", timeout=30)
        print(f"cleanup: deleted throwaway bank {BANK}")
    except Exception as e:  # noqa: BLE001
        print(f"cleanup: failed to delete {BANK}: {e}")


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        cleanup()
    sys.exit(rc)
