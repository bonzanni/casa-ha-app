# Memory architecture — current state (2026-04-26)

> Supersedes for "what is true today" purposes:
> - `2026-04-17-honcho-v3-memory-design.md` (Phase 2.2a)
> - `2026-04-17-sqlite-memory-2.2b.md` (Phase 2.2b)
> - `2026-04-20-3.2-domain-scope-runtime.md` (Phase 3.2)
> - `2026-04-21-3.2.1-scope-routing-eval.md` (3.2.1 eval harness)
> - `2026-04-21-3.2.2-scope-routing-hardening.md` (3.2.2 corpus tuning)
>
> Originals stay readable for design rationale ("why"). This document
> is descriptive of *current behaviour as shipped at v0.15.2*; for any
> historical "why" question, follow the link to the predecessor spec.

---

## 1. Doctrine

**Honcho is primary.** Everything important — dialectic retrieval,
peer cards, summarisation, cross-channel `nicola` continuity — runs on
Honcho v3 via the `honcho-ai` SDK. When a `HONCHO_API_KEY` is set, the
addon resolves to `HonchoMemoryProvider` and that provider services
every `ensure_session` / `get_context` / `add_turn` call.

**SQLite is graceful degradation.** When `HONCHO_API_KEY` is unset and
the operator has not opted out via `MEMORY_BACKEND=noop`, Casa falls
back to `SqliteMemoryProvider` writing to `/data/memory.sqlite`. This
mode is explicitly *not* feature-equivalent: no semantic retrieval, no
summary, no peer-card writer, no auto-pruning. Last-N exchange replay
is the entire surface. Anything that's only meaningful when SQLite is
the primary store is out of scope.

**NoOp is opt-out.** `MEMORY_BACKEND=noop` selects `NoOpMemory`, where
every method is a stub. Useful for tests and for installs that
specifically want no persistence; never the auto-resolved default
post-2.2b.

The three-tier doctrine collapses the older 2.2a "no key → no memory"
default. Fresh installs since v0.4.0 persist to SQLite on first turn.

---

## 2. The three-method ABC

Defined at `casa-agent/rootfs/opt/casa/memory.py:12`.

```python
class MemoryProvider(ABC):
    async def ensure_session(self, session_id, agent_role, user_peer="nicola") -> None
    async def get_context(self, session_id, agent_role, tokens,
                          search_query=None, user_peer="nicola") -> str
    async def add_turn(self, session_id, agent_role,
                       user_text, assistant_text, user_peer="nicola") -> None
```

**`ensure_session`** — idempotent. Returns `None`. Caller passes the
session id, the agent's role, and the user-peer identity. Safe to call
every turn. May raise on transport failures (Honcho HTTP error,
SQLite OperationalError); the agent layer logs WARNING and continues
into a degraded turn (no memory context).

**`get_context`** — returns a rendered markdown digest as a string, or
`""` when the session is empty / has no relevant content. `tokens` is
the per-call budget. `search_query` is the current user utterance,
used by Honcho for semantic retrieval and ignored by SQLite. May
raise; raises are caught at the agent layer and substituted with `""`.

**`add_turn`** — persists exactly one user→assistant turn
unconditionally. `user_text` is attributed to `user_peer`;
`assistant_text` to `agent_role`. Storage is never filtered;
write-scoping is structural via the `session_id` (see §5). May raise;
raises are caught in `_add_turn_bg` after the user response has
already been delivered.

The contract is unchanged across all three concrete providers and the
`CachedMemoryProvider` wrapper. Adding methods to this ABC is a
breaking change every backend must implement.

---

## 3. Provider catalogue

Four providers implement (or wrap) the §2 ABC: **HonchoMemoryProvider**
(§3.1), **SqliteMemoryProvider** (§3.2), **CachedMemoryProvider** (§3.3,
a wrapper), and **NoOpMemory** at `memory.py:66` — every method is a
stub; `MEMORY_BACKEND=noop` selects it. Used to disable memory entirely
without an `if memory:` guard at every call site.

### 3.1 HonchoMemoryProvider — `memory.py:161`

Honcho v3 backed. Constructed with `(api_url, api_key, workspace_id="casa")`.

**v3 SDK calls actually used:**

- `client.session(id=...)` — get/create session
- `client.peer(id=...)` — get/create peer
- `session.add_peers([...])` with `SessionPeerConfig(observe_others, observe_me)` — wire trust topology
- `session.context(tokens=..., peer_target=..., peer_perspective=..., search_query=...)` — fetch turn history + summary + peer_representation (see §9 for field semantics)
- `session.add_messages([...])` — append turn messages

**`SessionPeerConfig` flags** (from `honcho.api_types`):

- The user peer is added with `observe_me=True, observe_others=False`
  — Honcho watches the user's own messages but does not form a
  perspective on the agent for the user.
- The agent peer is added with `observe_me=False, observe_others=True`
  — Honcho forms a session-level theory-of-mind of the user from the
  agent's perspective. This is what populates `peer_representation` on
  later `session.context()` calls.

All SDK calls run inside `asyncio.to_thread` via the local
`_to_thread` helper. The `Honcho` and `SessionPeerConfig` symbols are
imported with a `try`/`except ImportError` guard so the module loads
on installs without `honcho-ai` (NoOp/SQLite-only paths).

### 3.2 SqliteMemoryProvider — `memory.py:396`

Durable local-storage backend. Single `sqlite3.Connection` opened with
`check_same_thread=False` and held for process lifetime. Every public
method bounces through `asyncio.to_thread` to a `_*_sync` helper that
runs the actual SQL.

**Schema** (CREATE-IF-NOT-EXISTS, idempotent on every open):

- `messages(id INTEGER PK AUTOINCREMENT, session_id, peer_name,
  content, ts)` plus index `idx_messages_session_id_id ON
  messages(session_id, id DESC)`.
- `sessions(session_id PK, agent_role, user_peer, created_ts,
  last_active)`.
- `peer_cards(peer_name, bullet, PRIMARY KEY (peer_name, bullet),
  created_ts)`. *Read-only in current code; no writer ships
  (M1 cleanup will drop this table).*
- `schema_meta(key PK, value)` seeded with `('schema_version', '1')`.

**PRAGMAs** applied on every connection open:

```
PRAGMA journal_mode = WAL
PRAGMA synchronous  = NORMAL
PRAGMA foreign_keys = ON
PRAGMA busy_timeout = 5000
```

**Sync-via-asyncio.to_thread pattern.** `sqlite3` is synchronous;
`asyncio.to_thread` offloads each call to the default thread pool.
`check_same_thread=False` lets the connection be reused across pool
threads; SQLite's own locking + `busy_timeout=5000` covers transient
collisions. Casa's actual write rate makes contention a non-issue.

**`_render` shim.** `_get_context_sync` builds an internal
`_SqliteCtx` dataclass (`messages: list[_SqliteMsg]`, `peer_card:
list[str]`, `summary: None`, `peer_representation: None`) and feeds
it into the same `_render` function the Honcho path uses.
Section omission rules in `_render` mean SQLite digests render only
`## What I know about you` (when peer_card has bullets — never, today)
and `## Recent exchanges`. `## Summary so far` and `## My perspective`
never appear on SQLite. `tokens // 40` is the rough last-N truncation
(`memory.py:464`).

### 3.3 CachedMemoryProvider — `memory.py:245`

Warm-cache + background-refresh wrapper around any `MemoryProvider`.
Built for the voice (butler) latency budget per 2.2a §7 strategy S1.

**Cache key:** `(session_id, agent_role, tokens)`. `search_query` is
intentionally *not* part of the key — voice trades per-turn semantic
retrieval for speed, and an utterance-keyed cache would never hit.

**Per-key Lock.** A `dict[key, asyncio.Lock]` (`self._locks`)
serialises concurrent first-fetches for the same key; double-checked
locking prevents two parallel turns from both firing the upstream
`get_context`. The lock is held for the duration of the upstream
fetch only.

**Background refresh.** After every successful `add_turn`, the wrapper
fires `asyncio.create_task(self._refresh(...))` to re-fetch every
cached `(session_id, agent_role, *)` entry. Tasks are tracked in
`self._bg_tasks` and discarded on completion. Refresh failures log a
WARNING; they never surface to the caller.

**Auto-skip on SQLite.** `_wrap_memory_for_strategy` in `casa_core.py`
detects an underlying `SqliteMemoryProvider` and skips the wrap entirely
when `read_strategy: cached`, emitting one INFO line at boot ("SQLite
backend — caching not applied"). SQLite reads are sub-millisecond; the
wrapper would add staleness without measurable benefit.

---

## 4. Backend resolution

`resolve_memory_backend_choice` at
`casa-agent/rootfs/opt/casa/casa_core.py:640` is a pure function
mapping an environment dict to a `_MemoryChoice` dataclass. The 4-rule
ladder, in order:

1. **Invalid `MEMORY_BACKEND` → raise.** Anything outside
   `{"honcho", "sqlite", "noop"}` raises `ValueError` at boot. Fail-fast
   on config typos because memory behaviour is too load-bearing to
   leave ambiguous.
2. **Explicit `MEMORY_BACKEND=honcho` without `HONCHO_API_KEY` → raise.**
   The backend is selectable but not usable; raise with a clear message
   rather than silently degrading.
3. **`MEMORY_BACKEND` set and valid → use it.** Explicit choice wins.
   `MEMORY_BACKEND=sqlite` forces SQLite even when `HONCHO_API_KEY`
   is set; `MEMORY_BACKEND=noop` disables persistence wholesale.
4. **No `MEMORY_BACKEND`:** if `HONCHO_API_KEY` is set → Honcho; else
   → SQLite (fresh-install default).

Other env vars consumed:

- `HONCHO_API_URL` — default `https://api.honcho.dev`.
- `MEMORY_DB_PATH` — default `/data/memory.sqlite`. Parent directory
  is `mkdir -p`'d on `SqliteMemoryProvider.__init__`.

The choice object is later turned into a concrete provider in
`casa_core.main`, then optionally wrapped per agent by
`_wrap_memory_for_strategy` (§3.3).

---

## 5. Session-id topology

Live session ids are 4 segments:

```
{channel}:{chat_id}:{scope}:{role}
```

- `channel` — `telegram`, `voice`, `webhook`, `scheduler`, etc.
- `chat_id` — the per-channel conversation key (Telegram chat id, voice
  room, webhook conversation handle); `"default"` when missing.
- `scope` — one of `personal | business | finance | house` (or any
  scope declared in `policies/scopes.yaml`); inserted by Phase 3.2.
- `role` — `assistant`, `butler`, `finance`, etc.

**Two-stage build.** The first two segments are joined by
`build_session_key` in `session_registry.py:12`:

```python
def build_session_key(channel: str, scope_id: str | None) -> str:
    sid = scope_id if scope_id else "default"
    return f"{channel}:{sid}"
```

The agent appends the remaining two segments at call time. The
canonical join site is `agent.py:312` inside the `_one_scope` helper:

```python
sid = f"{channel_key}:{scope}:{self.config.role}"
```

The same shape is reused on the write path
(`agent.py:532`: `write_sid = f"{channel_key}:{write_scope}:{self.config.role}"`).

**Pre-3.2 IDs are orphaned.** The 2.2a topology used 3 segments
(`{channel_key}:{role}`, no scope). When v0.8.0 inserted the scope
segment, prior Honcho sessions and SQLite rows were abandoned in place
— still queryable in the Honcho dashboard or the SQLite file, but never
read by Casa again. Phase 3.2 §10 made this an explicit ship-time
decision: cold start on upgrade, no migration. Peer cards (Honcho-side)
carried Nicola's biographical facts across the cut.

**Out-of-band consumers can drift.** The voice channel pre-warmer in
`channels/voice/channel.py` was found in the M1 audit to still build a
3-segment session id (`voice:{scope_id}:{role}`), so its cache key
never matches the 4-segment id the agent computes. M2 (G1) closes that
drift by looping over `scopes_readable` and warming one entry per
scope.

**Engagements carry the engager's scope.** When an MCP tool spawns an
engagement during a turn (e.g. `engage_executor` → Tina, `delegate_to_agent`
→ a specialist), the engagement record's `origin` dict carries
`scope = argmax_scope(scores, default_scope)` stamped onto `origin_var`
by `agent.py:309-314` immediately after the read-path classifier runs.
Downstream consumers — chiefly `query_engager` at `tools.py:1357`,
which rebuilds `{channel}:{chat_id}:{scope}:{role}` to retrieve from
the engager's actual session — read it via `engagement.origin.get(
"scope", "meta")`. The literal `"meta"` fallback handles edge paths
(cron triggers, boot replay) that engage without going through
`_process`. M2 (G6) shipped this stamp.

---

## 6. Read path

Implemented in `Agent._process` at `agent.py:292-348`. Per-turn flow:

1. **Trust resolution.** `trust_token = channel_trust(msg.channel)` —
   see `channel_trust.py:9` for the live tier ordering.
2. **Trust filter.** `self._scope_registry.filter_readable(
   self.config.memory.scopes_readable, trust_token)` drops every
   scope whose `minimum_trust` is not satisfied by the channel's tier.
   Filter happens *before* scoring — denied scopes never get an
   embedding lookup.
3. **Scoring.** `self._scope_registry.score(user_text, readable)`
   computes cosine similarity between the per-utterance e5-large
   embedding and each readable scope's pre-embedded `description`.
   Returns `{scope: float}`.
4. **Active set.** `active_from_scores(scores, default_scope)` keeps
   scores above `threshold` (default `0.35`); if everything falls
   short and `default_scope` is in `scores`, returns
   `[default_scope]`; otherwise returns `[]` (all paths refuse the
   write rather than leak into a forbidden scope).
5. **Per-scope budget.** `per_scope_tokens =
   max(self.config.memory.token_budget // max(len(active), 1), 1)` —
   total budget divided evenly so the system prompt stays bounded.
6. **Parallel ensure + fetch.** `asyncio.gather` runs an `_one_scope`
   coroutine per active scope. Each builds its 4-segment `sid`, calls
   `ensure_session` then `get_context`, and returns
   `(scope, digest)`. Per-scope exceptions are caught and replaced
   with `digest=""` so one Honcho hiccup doesn't kill the whole turn.
7. **Digest concatenation.** Non-empty digests are joined as

   ```
   <memory_context scope="finance">
   ...digest...
   </memory_context>
   <memory_context scope="house">
   ...digest...
   </memory_context>
   ```

   (one block per active scope). The full `memory_blocks` string is
   appended to the system prompt alongside `<channel_context>`,
   `<delegates>`, `<executors>`, `<current_time>`.
8. **Token budget tracking.** `BudgetTracker.record(...)` measures
   the assembled `memory_blocks` against the agent's
   `token_budget` and emits a per-turn over-budget streak counter
   used by the spec-5.2 budget telemetry.

Disclosure (`render_disclosure_section` in `policies.py`) is
independent of this path — it appends a `### Disclosure` section to
the system prompt at agent-load time, not per turn.

---

## 7. Write path

Implemented at `agent.py:505-538`, on the same `_process` task path
that just received `response_text` from the SDK.

1. **Skip on empty response.** `if response_text:` — no write when the
   SDK turn produced nothing (error / refusal / tool-only response).
2. **`owned ∩ readable`.** `[s for s in self.config.memory.scopes_owned
   if s in readable]`. Writing to a scope outside the channel's trust
   tier would leak the exchange into a forbidden namespace, so the
   intersection is the correct candidate set. Empty intersection →
   skip the write entirely (no fall-through to `default_scope`).
3. **Single-candidate shortcut.** When the intersection has exactly
   one scope, `write_scope = owned_and_readable[0]` directly — no
   ONNX forward pass. This is the common butler case
   (`scopes_owned = [house]`, voice channel, `house` is the only
   survivor) and saves ~90 ms per turn.
4. **Classifier on full exchange.** With ≥2 candidates,
   `self._scope_registry.score(f"{user_text}\n{response_text}",
   owned_and_readable)` re-embeds the joined user+assistant text and
   scores against each candidate.
5. **Argmax → write_scope.** `argmax_scope(write_scores,
   default_scope)` — top score wins if it clears `threshold`,
   otherwise falls back to `default_scope`. Ties are broken by dict
   iteration order (input list order in practice).
6. **Background `add_turn`.** `asyncio.create_task(self._add_turn_bg(
   write_sid, ...))` fires the persistence call off the critical path.
   The user response has already streamed; persistence can take its
   time. Failures log a WARNING in `_add_turn_bg`; they never surface.

Tracking task references in `self._bg_tasks` keeps them anchored against
GC, with `task.add_done_callback(self._bg_tasks.discard)` cleaning up
on completion.

---

## 8. Disclosure (separate layer)

Disclosure lives in `casa-agent/rootfs/opt/casa/policies.py`:

- `load_policies(path)` → `PolicyLibrary` — loaded once at boot from
  `policies/disclosure.yaml`.
- `PolicyLibrary.resolve(name, overrides)` — applies a resident's
  shallow overrides on top of the named base policy.
- `render_disclosure_section(resolved)` — emits a markdown
  `### Disclosure` section composed of:
  - `Confidential on untrusted channels:` — categories list.
  - `Safe on any channel:` — always-shareable list.
  - `Deflection patterns:` — keyed by trust tier.

The composed text is appended verbatim to each agent's
`system_prompt` at agent-load time by `agent_loader`. It is *not*
re-rendered per turn.

**Independent of memory enforcement.** Memory enforcement is
structural, on the read side: the trust filter (§6 step 2) drops
scopes the channel may not see *before* fetch, so the SDK never has
the option of "disclosing" something from a forbidden scope — there's
nothing in its context to disclose. Disclosure prose is prompt-side
*advice* that backs up the structural guarantee for cases where the
agent already holds context within its visible scope but the channel
trust still warrants caution.

The 3.2 spec (§4.7) shortened butler's overrides because the trust
filter renders the categories list redundant on the voice channel —
the deflection patterns remain.

---

## 9. Honcho-as-primary contract

What Casa expects Honcho to deliver on `session.context(...)` and
renders via `_render(context)` (`memory.py:100-133`):

- **`messages: list[Message]`** — recent exchanges, each with
  `peer_name` and `content`. Rendered as a `## Recent exchanges`
  block, one line per message.
- **`summary.content: str | None`** — running summary maintained by
  Honcho. Rendered as `## Summary so far` when populated.
- **`peer_representation: str | None`** — the agent's theory-of-mind
  of the user, accumulated via `observe_others=True` on the agent
  peer. Rendered as `## My perspective` when populated.
- **`peer_card: list[str]`** — bullets on the `peer_target`'s card,
  durable across sessions. Rendered as `## What I know about you` when
  populated.

`_render` silently omits any section whose source field is empty or
`None`, so missing data never produces placeholder lines.

**M3 closure (v0.15.4).** Real-response coverage now lives at
`tests/test_memory_honcho.py::test_get_context_renders_summary_and_peer_repr_when_honcho_returns_them`,
which primes the SDK stub with populated `summary.content` +
`peer_representation` + `peer_card` + recent `messages` and asserts
that all four canonical sections appear in the rendered output, in
the order `_render` emits (peer_card → summary → perspective →
recent). The test exercises the SDK return → `HonchoMemoryProvider.
get_context` → `_render` → markdown wiring end-to-end, which is the
contract this section names. A live `HONCHO_LIVE_TEST=1`-gated
integration test is deferred as M3a.1 follow-up; the cassette-style
hand-rolled fixture is sufficient for day-one confidence per the
plan-2026-04-27 design notes (determinism, no CI secret, SDK shape
fully observable).

---

## 10. SQLite "graceful degradation" contract

What SQLite is, in current code:

- **Last-N exchange replay only.** `_get_context_sync` reads
  `tokens // 40` rows (`memory.py:464`) from `messages` ordered by
  `id DESC`, reverses to chronological order, and feeds them through
  the same `_render` the Honcho path uses. The `_SqliteCtx` shim sets `summary=None` and
  `peer_representation=None`, so the only sections that can appear
  are `## What I know about you` (driven by `peer_cards`) and
  `## Recent exchanges`.
- **No summariser.** The 2.2c summariser seam reserved in the
  predecessor spec was never built; `summary` is hard-coded `None`.
- **No peer-card writer.** The `peer_cards` table exists in the
  schema but no code path writes to it. Reads will always return an
  empty list. Roadmap M1 cleanup drops the table entirely; the
  `## What I know about you` section therefore never renders on
  SQLite in practice.
- **No semantic retrieval.** `search_query` is accepted (ABC
  compatibility) and ignored.
- **Storage is bounded by user disk.** No TTL, no per-session cap, no
  pruning job. Roadmap explicitly defers retention/pruning under "log
  a warning above N MB, never auto-prune".
- **Switching backends = fresh start.** No dual-write, no migration,
  no export/import CLI. Session ids stay derived (`{channel}:{chat_id}
  :{scope}:{role}`), so a swap from Honcho back to SQLite resumes the
  prior SQLite sessions naturally; switching from SQLite → Honcho
  walks away from accumulated SQLite history.

The framing rules out: SQLite summariser, SQLite peer-card writer,
backend migration tooling, embedding-based search. These are
explicitly in the "ruled out" section of `docs/MEMORY-ROADMAP.md`.

---

## 11. Scope-routing classifier (3.2)

Implemented in `casa-agent/rootfs/opt/casa/scope_registry.py`.

- **`ScopeLibrary`** (`scope_registry.py:44`) — loads + validates
  `policies/scopes.yaml` against `defaults/schema/policy-scopes.v1.json`.
  Each scope has a `minimum_trust` and a `description`.
- **`ScopeRegistry`** (`scope_registry.py:114`) — wraps the library
  with the trust-filter helpers and the embedding model.
- **Model.** `intfloat/multilingual-e5-large` via `fastembed`'s ONNX
  runtime. CPU-only, ~500 MB, downloaded on first boot to
  `/data/fastembed/`. Lazy import (`_load_text_embedding_cls`) keeps
  interpreter start fast and gives tests a monkeypatch point.
- **`prepare()`** (`scope_registry.py:205`) — loads the model,
  embeds each scope's `description` once, caches the vector dict.
  All failures log ERROR and flip the registry into degraded mode
  (`self._degraded = True`); boot does not abort.
- **Per-query embedding LRU cache.** `OrderedDict` keyed by
  `text.strip().lower()`, default size 256 entries
  (`embed_cache_size`). Voice retriggers and casing variants collapse
  to one entry. `cache_stats()` returns `(hits, misses)` for the
  `scope_route` log line.
- **`score()`** — single-pass `embed(text)` plus cosine against each
  pre-embedded scope vector. In degraded mode returns `{s: 1.0 for s
  in scopes}` so the agent fans out to every readable scope (same
  behaviour as Revised-A in the design history).
- **Threshold.** Default `0.35`, sourced from the addon option
  `scope_threshold` (Phase 3.2.1) which is read at boot via
  `bashio::config` and exported as `CASA_SCOPE_THRESHOLD`. Exposed
  read-only as `ScopeRegistry.threshold` since 3.2.2 so the
  `scope_route` log emission can include it.
- **`active_from_scores`** — returns scopes ≥ threshold, falling back
  to `[default_scope]` when default survived the trust filter, else
  `[]`.
- **`argmax_scope`** — returns top score's scope when it clears
  threshold, else `default_scope`.

Phase 3.2.1 added `casa_eval/` as a generic eval framework with the
`ScopeRoutingTester` as its first tester. The eval suite at
`tests/fixtures/eval/scope_routing/default.yaml` is the regression
guard against `scopes.yaml` description edits. Phase 3.2.2 hardened
the shipped corpora to keyword-style phrases and committed
`ACCURACY_BASELINE = 0.85`.

---

## 12. Open questions / known gaps

The full backlog lives in `docs/MEMORY-ROADMAP.md`. The phases
remaining for memory work:

- **M2 — Shipped v0.15.3.** G1 voice prewarm session-id repaired
  (4-segment shape, one entry per `scopes_readable`); G4 cancel +
  force-delete paths now resolve `memory_provider` so meta + executor
  archival writes fire on cancellations; G6 `engagement.origin.scope`
  stamped via `argmax_scope` so `query_engager` retrieves from the
  engager's rooted scope, not the `"meta"` fallback. See §5 for the
  origin-scope narrative.
- **M3 — Shipped v0.15.4.** Real-response integration test added at
  `tests/test_memory_honcho.py::test_get_context_renders_summary_and_peer_repr_when_honcho_returns_them`
  closes the § 9 gap; `memory_call` info-level log line emits from each
  concrete provider's `get_context` plus `CachedMemoryProvider`'s cache-
  hit branch (see § 13 for the field contract). Live-flag-gated test
  deferred as M3a.1 follow-up. NoOp provider is intentionally silent —
  see § 13 emission-sites note.
- **M4 — Engagement memory.** Combines G5 (`meta` and
  `executor:<type>` not declared in `scopes.yaml`), G9 (Tier-3
  Executors get no memory injection at all), B4 (engagement summaries
  are write-only). Brainstorm-required before plan-write because the
  "executors stateless" framing has real arguments either way.
- **M5 — `remember_fact` tool.** The deferred-since-v0.4.0 feature.
  Honcho path appends to `peer_card` via the v3 SDK; SQLite path
  log-and-skips per the §1 doctrine.
- **M6 — Cross-role recall (optional, large).** A
  `consult_other_agent_memory(role, query)` tool reading via Honcho's
  `peer_perspective`. Defer until after M3-M5.

Explicitly ruled out by the §1 doctrine and the roadmap "explicitly
NOT doing" section: B3 SQLite summariser, B2 SQLite retention/pruning,
G10 backend migration, B7 disclosure-as-enforcement, G2 SQLite
peer_cards writer.

---

## 13. `memory_call` telemetry (M3b, v0.15.4)

Every concrete memory provider emits one `memory_call` info-level log
line per `get_context` call. The line is the per-memory-read companion
to the per-turn `scope_route` line at `agent.py:554-568` (§ 11). Both
share the `logger.info("event_name", extra={...})` shape so a single
downstream parser handles both.

**Fields** (always present, types as listed):

| Field | Type | Notes |
|---|---|---|
| `event` (log message) | `"memory_call"` | constant |
| `backend` | `str` | `"honcho"`, `"sqlite"`, or — on cache-hit emissions — the wrapped backend's mapped name |
| `session_id` | `str` | the 4-segment session id (§ 5) |
| `agent_role` | `str` | role passed to `get_context` |
| `t_ms` | `int` | latency in milliseconds, measured from method entry to just-before-return |
| `peer_count` | `int \| None` | length of `messages` on the SDK / SQLite return; `None` on cache-hit emissions (cache stores rendered string only) |
| `summary_present` | `bool \| None` | `True` when SDK's `summary.content` is non-empty; `False` on SQLite always (§ 10); `None` on cache-hit emissions |
| `peer_repr_present` | `bool \| None` | `True` when SDK's `peer_representation` is non-empty; `False` on SQLite always; `None` on cache-hit emissions |
| `cache_hit` | `bool` | `True` only on `CachedMemoryProvider`'s wrapper-served path; `False` on every direct backend emission, including the wrapped-backend's own emission on cache miss |

Note on field naming: `peer_count` measures messages-actually-rendered
(token-budget-bounded), not total messages in storage. Honcho's SDK
return list and SQLite's row-fetch list both reflect the per-call
ingestion count — the field name predates the M3b telemetry framing
and is preserved for grep-compatibility with the operator dashboards.

**Emission sites** (live code refs as of v0.15.4 — re-grep for class
names if line numbers drift):

- `HonchoMemoryProvider.get_context` (search `casa-agent/rootfs/opt/casa/memory.py` for `class HonchoMemoryProvider`) — `cache_hit=False`, all fields populated from the SDK return.
- `SqliteMemoryProvider.get_context` (search same file for `class SqliteMemoryProvider`) — `cache_hit=False`, `summary_present=False`, `peer_repr_present=False` per the § 10 graceful-degradation contract.
- `CachedMemoryProvider.get_context` (search same file for `class CachedMemoryProvider`) — emits ONLY on cache hit, with `cache_hit=True` and `backend` resolved by `CachedMemoryProvider._resolve_backend_name` (see below).
- `NoOpMemory.get_context` — does NOT emit. Operators using `MEMORY_BACKEND=noop` have explicitly disabled persistence; per-turn telemetry would be noise. The boot-time `_MemoryChoice` log emitted by `casa_core.main` is the operator's confirmation that noop was selected.

**Backend-name resolution** (`CachedMemoryProvider._resolve_backend_name`):

The wrapper's cache-hit emission needs a string for the `backend`
field even though it doesn't call the inner backend (which would have
self-identified). The static helper resolves the inner provider's
class name through three tiers, in order:

1. **Production-provider lookup table.** Direct mapping for the three
   shipped classes:
   - `HonchoMemoryProvider` → `"honcho"`
   - `SqliteMemoryProvider` → `"sqlite"`
   - `NoOpMemory` → `"noop"`
2. **`MemoryProvider`-suffix strip.** Any class name ending in
   `MemoryProvider` not in the table gets the suffix removed and
   lowercased.
3. **`Provider`-suffix strip.** Any class name ending in `Provider`
   (but not `MemoryProvider`) gets that suffix removed and lowercased.
   This branch catches test stubs (`RecordingProvider` → `"recording"`)
   and any future generic provider that doesn't follow the
   `MemoryProvider` naming convention.
4. **Fallback.** Bare `cls.__name__.lower()`.

Future production providers should land in the table to short-circuit
the fallbacks, both for clarity and to lock in the operator-facing
name independent of class-name churn.

**Why provider-level, not aggregated per-turn:** a turn fans out to N
scopes (§ 6 step 6), each with its own `_one_scope` coroutine that
calls `get_context` once. Per-scope attribution lets operators answer
"why is finance scope slow?" without re-implementing the join in the
log pipeline. The `cid_var` value (already injected via `extra` by the
existing logger filter in `agent_logging.py`) ties multiple
`memory_call` lines back to the same turn's `scope_route` line.

**Why CachedMemoryProvider's emission is asymmetric** (only on hits,
never on misses): one `memory_call` per logical memory read is the
contract. On miss, the wrapped backend's own emission counts as the
"the memory call that happened"; on hit, the backend never runs so the
wrapper takes ownership of the line. Operators see exactly one
`memory_call` per `_one_scope` call, with `cache_hit` distinguishing
the served path. Double-emission on misses would falsify rate
dashboards.

**Drift risk.** The four emission sites (Honcho, SQLite, two Cached-
hit sites) carry near-identical 8-field dicts. Plan §B chose explicit
per-site emission over a helper to keep the contract visible — the
trade-off is that field-set drift between sites is the failure mode.
Tests at `tests/test_memory_honcho.py`, `tests/test_memory_sqlite.py`,
`tests/test_memory_cached.py` assert each site's field shape; any
future addition to the contract MUST update all four sites and all
three test files in the same commit.
