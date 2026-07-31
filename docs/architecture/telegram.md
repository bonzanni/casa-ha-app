---
last_reviewed: 2026-07-30
---

# The Telegram channel

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How Telegram messages become turns and how answers come back: transport selection,
authentication, the per-topic causal log, interactive keyboards, and message rendering. It
does not cover the Bot API itself, nor what an agent does with a message once dispatched.

## Mental model

**Two transports, chosen by configuration.** Polling is the default. Webhook mode is selected
explicitly and additionally requires a public URL — without one, the system logs and falls
back to polling rather than failing.

**Webhook mode with no secret is not "unauthenticated but working" — it is dead.** The
receiving route refuses every request when no secret is configured. Registration with the Bot
API can still succeed and report itself as started, so the failure looks like Telegram not
delivering rather than like a configuration error. This is deliberate fail-closed behaviour:
the route would otherwise accept forged updates from anyone who found the URL. Polling is
unaffected, because it does not use that route.

**Two kinds of inbound message go to different places.** A direct message becomes an ordinary
turn on the bus. A message in an engagement topic is delivered to that engagement's driver
instead — it is input to running work, not a new conversation.

**Ordering in a topic is a property of the sequencer, not of Telegram sends.** An engagement
topic is a causal log: a single serialized writer keeps narration, discrete posts and edits
in an order that matches what actually happened. That guarantee belongs to the sequencer
seam, and **only claude-code engagement topics have one** — platform notices for other
engagements post directly to the topic, so an in-casa specialist topic sits outside the
ordering guarantee entirely. **A direct send bypasses it, and nothing mechanically prevents
one** — fallback paths exist for when no driver seam is present, and they are outside the
ordering guarantee.

**A tap is authorised against the request it answers.** Callback data is versioned and
carries the namespace and request id; resolution is bound to the operator the request was
posted for. A tap from someone else is refused. Note that the parser still accepts a legacy
permission format — but actionability is bounded by process memory: a callback resolves only
while its request lives in the current process's broker, so buttons from before a restart
are rejected as expired however well their format parses.

**Two rendering paths count length differently, and only one of them counts what Telegram
counts.** The rich path paginates against Telegram's limits, adjusting for UTF-16. The plain
streaming and splitting path counts Python code points. For text outside the basic plane —
emoji, most notably — those two numbers differ, and only the first matches the platform.

## Contracts & invariants

**INV-TG-001**: A webhook update is accepted only when a secret is configured and the request's secret-token header matches it exactly.

Enforced in the update route before the payload is parsed or enqueued, using a constant-time
comparison over the encoded bytes of both header and secret — so a non-ASCII value is
handled without an error, and a matching non-ASCII secret is accepted.

What it does not cover: polling updates do not pass through this route at all, and the header
establishes only that the sender knows the shared secret.

**INV-TG-002**: A callback can resolve a request only from the operator that request is bound to.

Enforced when the callback arrives, before the broker claim. A missing or mismatched user is
refused with a best-effort acknowledgement.

What it does not cover: topic commands are authorised separately and to different rules —
some are originator-only and at least one is open to topic participants. Do not generalise
this invariant to commands.

**INV-TG-003**: A live request is resolved exactly once.

Enforced by a claim-then-commit protocol in the broker: claiming marks the request, commit
validates the claim token, and finishing removes it and resolves its waiter.

What it does not cover: it does not make the keyboard edit succeed. Settlement hooks run
asynchronously, and their failures are logged rather than reversing the resolution.

**INV-TG-004**: Writer operations on a sequenced topic are serialized under one lock.

Enforced by the sequencer for narration, discrete posts and edits, notices, and inbound
high-water advances.

What it does not cover, and it is the thing to check before assuming ordering: direct sends
that bypass the sequencer, including the fallback used when no driver seam is present.

**INV-TG-005**: A rich response is paginated to Telegram's message-length and entity budgets.

Enforced in the rich renderer's pagination, which measures in UTF-16 units as the platform
does.

What it does not cover: the plain streaming and splitting path applies its own limit counted
in code points, so the two paths do not agree for non-basic-plane text.

## Failure behavior

**No secret, or a wrong one, in webhook mode.** The route refuses before parsing. Nothing
reaches the channel.

**Webhook transport selected without a public URL.** Boot does not fail; the system logs and
uses polling.

**A duplicate update arrives.** A webhook redelivery is absorbed by a bounded, process-local
recent-update cache consulted on the webhook path only. Polling updates never pass that
cache, and no equivalent deduplication is established for them here.

**A message arrives from an unconfigured chat.** Logged and dropped. Note that leaving the
chat id empty accepts other chats — the check is only as narrow as the configuration.

**A tap is stale, expired, for the wrong topic, or from the wrong user.** Absorbed with a
single best-effort acknowledgement; failures answering are themselves absorbed.

**Posting a keyboard fails.** The request is unregistered and its waiter resolves with a
delivery-failure outcome rather than hanging.

**Delivering a turn into a topic raises.** Logged, with a best-effort failure notice posted
to the topic. Cancellation is quiet by design.

## Extension points

**Changing transport** touches the manifest schema, the environment-driven selection, the
route's authentication, and the channel rebuild.

**A new callback namespace** needs the namespace list, the parser and formatter, the dispatch
and authorization metadata, the broker registration, and the settlement hook — six places,
none of which will tell you the others were missed.

**A new topic output** should go through the sequencer seam if causal order matters. A direct
send is available and is not prevented.

**A new rich response** belongs on the paginating path. The plain splitter does not measure
what Telegram measures.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel`
- `casa/rootfs/opt/casa/channels/telegram.py::_parse_callback_data`
- `casa/rootfs/opt/casa/channels/telegram.py::_split_message`
- `casa/rootfs/opt/casa/casa_core.py::_make_telegram_update_handler`
- `casa/rootfs/opt/casa/channels/output_sequencer.py::OutputSequencer`
- `casa/rootfs/opt/casa/channels/tg_richtext.py::render_paged`
- `casa/rootfs/opt/casa/verdict_broker.py::VerdictBroker`

**Tests**
- `tests/test_telegram_update_handler.py`
- `tests/test_tg_richtext_remnants.py`
- `tests/test_verdict_broker.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
<!-- END SOURCEMAP -->
