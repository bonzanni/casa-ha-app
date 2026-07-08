#!/usr/bin/env python3
"""Phase 0 memory-accuracy baseline measurement (design:
docs/superpowers/specs/2026-06-04-casa-led-hindsight-memory-profiles-design.md §4 Phase 0).

Measures casa's CURRENT (stock-default) Hindsight memory behaviour against a
faithful, labelled corpus, on a THROWAWAY bank — never the `casa` bank. It is a
measurement *script*, not a framework: it answers two questions the design is
gated on, plus a tier-leak guard:

  (a) POLLUTION  — do ephemeral technical task-agent facts (executor JSON
                   summaries) surface in resident "Ellen-style" household recall?
  (b) EXTRACTION — does stock extraction + recall actually surface the household
                   facts a resident would expect (and not bury them in junk)?
  (guard) TIER LEAK — a private fact must NOT surface at voice/friends clearance.

Faithfulness to production (verified 2026-06-04):
  * retain item shape  : {content, tags:[tier], metadata:{speaker}, document_id}
                         (session_saver.transcript_to_items / delegated_memory.retain_delegated)
  * technical writes    : JSON blobs {kind, task, text/last_text, artifacts, ...}
                         (tools.py:1578 engagement_summary / :1634 executor_engagement_summary)
  * recall              : tags=readable_tiers(clearance), tags_match="any",
                          types=[world,experience,observation], budget="mid"
                         (hindsight_memory.recall + delegated_memory.delegated_recall)

The bank is created fresh (stock defaults) and DELETED at the end. Run it inside
the casa addon container (reaches Hindsight on the hassio network):

    python3 phase0_memory_baseline.py            # default URL + a unique throwaway bank
    HINDSIGHT_URL=http://5884eb17-hindsight:8888 python3 phase0_memory_baseline.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("HINDSIGHT_URL", "http://5884eb17-hindsight:8888").rstrip("/")
BANK = os.environ.get("EVAL_BANK", f"eval-phase0-{os.getpid()}-{int(time.time())}")

# Tier ladder (sensitivity.py): public < friends < family < private.
ALL_TIERS = ["public", "friends", "family", "private"]
VOICE_TIERS = ["public", "friends"]  # readable_tiers("friends") — voice clearance

# ---------------------------------------------------------------------------
# Corpus. document_id prefix encodes ground truth: "<origin>--<id>:<idx>".
#   origin ∈ {hh (household, resident), tech (technical, executor JSON blob)}.
# Household items are natural-language resident turns. Technical items are the
# JSON-summary blobs executors retain today (tools.py:1578/1634).
# ---------------------------------------------------------------------------
HOUSEHOLD = [
    ("wifi",      "family",  "The main wifi password is Hunter2Galaxy."),
    ("thermo",    "friends", "Nicola keeps the thermostat at 20 degrees in winter."),
    ("pickup",    "friends", "The kids, Sofia and Luca, are picked up from school at 3:30pm on weekdays."),
    ("dentist",   "friends", "Our dentist is Dr. Chen and the next cleaning is on June 18th at 2pm."),
    ("allergy",   "friends", "Nicola is allergic to penicillin."),
    ("energy",    "private", "We pay the energy bill by direct debit around the 5th of each month, about 140 euros."),
    ("sparekey",  "family",  "The spare house key is with our neighbour Mrs. Albright at number 12."),
    ("grandma",   "friends", "Grandma visits every second Sunday for lunch."),
    ("paint",     "public",  "The living room is painted sage green."),
    ("vacuum",    "public",  "The robot vacuum is a Roborock S8."),
]

TECHNICAL = [
    ("weathercard", "public", json.dumps({
        "kind": "engagement_summary", "engagement_id": "e1",
        "specialist_or_type": "frontend", "task": "build a weather Lovelace card",
        "status": "completed",
        "text": "Created weather-card.js using Lit 3.x, registered the custom element "
                "ha-weather-card, and added a visual editor via getConfigElement.",
        "artifacts": ["weather-card.js", "weather-card-editor.js"],
        "next_steps": ["test in the dashboard"]})),
    ("mqtt", "public", json.dumps({
        "kind": "executor_engagement_summary", "engagement_id": "e2",
        "executor_type": "configurator", "task": "add the MQTT integration",
        "terminal_state": "completed", "engager": "assistant",
        "last_text": "Appended mqtt broker 192.168.1.10 port 1883 to configuration.yaml "
                     "and reloaded the YAML config.",
        "artifacts": ["configuration.yaml"]})),
    ("s6crash", "public", json.dumps({
        "kind": "engagement_summary", "engagement_id": "e3",
        "specialist_or_type": "backend", "task": "debug the s6 service crash",
        "status": "completed",
        "text": "Fixed the svc-casa/run shebang; a CRLF line ending broke the interpreter. "
                "Converted the file to LF and the service started.",
        "artifacts": ["svc-casa/run"], "next_steps": []})),
    ("skill", "public", json.dumps({
        "kind": "executor_engagement_summary", "engagement_id": "e4",
        "executor_type": "plugin-builder", "task": "create a notification skill",
        "terminal_state": "completed", "engager": "assistant",
        "last_text": "Wrote SKILL.md and notify.py implementing a Telegram push via the bot "
                     "API, and added the skill to the marketplace manifest.",
        "artifacts": ["SKILL.md", "notify.py"]})),
]

# Ellen-style household recall queries. expect = household ids that SHOULD surface.
# The broad final queries are deliberate pollution magnets.
QUERIES = [
    ("What's the wifi password?",                         ["wifi"]),
    ("When is the next dentist appointment?",             ["dentist"]),
    ("What temperature do we keep the house at?",         ["thermo"]),
    ("When are the kids picked up from school?",          ["pickup"]),
    ("Is anyone in the house allergic to anything?",      ["allergy"]),
    ("How much is the energy bill and when is it paid?",  ["energy"]),
    ("Where is the spare house key?",                     ["sparekey"]),
    ("When does grandma come over?",                      ["grandma"]),
    ("What colour is the living room?",                   ["paint"]),
    ("Which robot vacuum do we own?",                     ["vacuum"]),
    ("What do I need to remember about the house?",       []),  # broad: pollution magnet
    ("Give me a summary of everything important at home.", []),  # broad: pollution magnet
]


def _req(method, path, body=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw.strip() else {})


def retain(items):
    # async=False → block until extraction completes, so facts are queryable now.
    return _req("POST", f"/v1/default/banks/{BANK}/memories",
                {"async": False, "items": items}, timeout=300)


def recall(query, tiers):
    _, resp = _req("POST", f"/v1/default/banks/{BANK}/memories/recall",
                   {"query": query, "tags": tiers, "tags_match": "any",
                    "max_tokens": 2048,
                    "types": ["world", "experience", "observation"],
                    "budget": "mid"}, timeout=60)
    return resp.get("results", []) or []


def origin_of(doc_id):
    # "<origin>--<id>:<idx>" → origin
    head = (doc_id or "").split(":", 1)[0]
    return head.split("--", 1)[0] if "--" in head else "?"


def corpus_id_of(doc_id):
    head = (doc_id or "").split(":", 1)[0]
    return head.split("--", 1)[1] if "--" in head else "?"


def build_items():
    items = []
    for cid, tier, content in HOUSEHOLD:
        items.append({"content": content, "tags": [tier],
                      "metadata": {"speaker": "nicola"},
                      "document_id": f"hh--{cid}:0"})
    for cid, tier, content in TECHNICAL:
        items.append({"content": content, "tags": [tier],
                      "metadata": {"speaker": "assistant"},
                      "document_id": f"tech--{cid}:0"})
    return items


def main():
    print(f"=== Phase 0 baseline — bank={BANK} url={BASE} ===")
    try:
        st, _ = retain(build_items())
        print(f"retain {len(HOUSEHOLD)} household + {len(TECHNICAL)} technical items -> HTTP {st}")
    except urllib.error.HTTPError as e:
        print("retain failed:", e.code, e.read().decode()[:300]); return 2

    # Give consolidation a moment (observations are async even when retain is sync).
    time.sleep(8)

    n_recall_expected = 0
    n_recall_hit = 0
    polluted_queries = 0
    total_tech_surfaced = 0
    total_facts_surfaced = 0
    rows = []
    for q, expect in QUERIES:
        res = recall(q, ALL_TIERS)
        ids = {corpus_id_of(m.get("document_id")) for m in res}
        origins = [origin_of(m.get("document_id")) for m in res]
        tech = sum(1 for o in origins if o == "tech")
        total_tech_surfaced += tech
        total_facts_surfaced += len(res)
        if tech:
            polluted_queries += 1
        hit = [e for e in expect if e in ids]
        n_recall_expected += len(expect)
        n_recall_hit += len(hit)
        rows.append((q, len(res), tech, expect, hit))
        flag = "  <-- POLLUTED" if tech else ""
        miss = "" if len(hit) == len(expect) else f"  MISS={set(expect)-set(hit)}"
        print(f"  q={q[:46]:46} facts={len(res):2} tech={tech}{miss}{flag}")

    # Tier-leak guard: the private energy fact must NOT surface at voice/friends clearance.
    leak = recall("how much is the energy bill and when is it paid?", VOICE_TIERS)
    leaked = any(corpus_id_of(m.get("document_id")) == "energy" for m in leak)

    print()
    print("=== SCORES (stock-default baseline) ===")
    recall_rate = (n_recall_hit / n_recall_expected) if n_recall_expected else 0.0
    poll_q_rate = polluted_queries / len(QUERIES)
    poll_fact_rate = (total_tech_surfaced / total_facts_surfaced) if total_facts_surfaced else 0.0
    print(f"(b) extraction/recall: {n_recall_hit}/{n_recall_expected} expected household facts recalled "
          f"({recall_rate:.0%})")
    print(f"(a) pollution: {polluted_queries}/{len(QUERIES)} queries surfaced technical facts "
          f"({poll_q_rate:.0%}); {total_tech_surfaced}/{total_facts_surfaced} of all surfaced facts "
          f"were technical ({poll_fact_rate:.0%})")
    print(f"(guard) tier-leak (private energy fact at voice/friends clearance): "
          f"{'LEAK!' if leaked else 'no leak (ok)'}")
    print()
    print("Decision gate (design §4 Phase 0): if pollution ~0 AND recall high -> ship nothing. "
          "Else the failing axis justifies Phase 1 (extraction) / Phase 2 (pollution).")
    return 0


def cleanup():
    try:
        _req("DELETE", f"/v1/default/banks/{BANK}", timeout=30)
        print(f"cleanup: deleted throwaway bank {BANK}")
    except Exception as e:  # noqa: BLE001
        print(f"cleanup: failed to delete {BANK}: {e} (delete manually)")


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        cleanup()
    sys.exit(rc)
