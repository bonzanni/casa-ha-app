# Plugin-emitted domain events — casa.emits / casa.subscribes

Not `casa.triggers` (`ingress.md`) — that is a THIRD PARTY reaching Casa over
a webhook. This is a PLUGIN telling another installed plugin "something
happened in my data", entirely inside Casa. Two different mechanisms,
easy to conflate because both are colloquially "events" — if the source
of the event is outside Casa, read `ingress.md` instead.

## Mental model: a wake-up, not a data channel

An event carries **no payload**. Emitting one is a pure "something happened"
signal; the subscriber's own durable state (a database row, a file, an API
you call again) is the real queue. A lost, suppressed, coalesced, or
duplicate wake costs promptness, never correctness — design your
subscriber-side logic so that is true. Never treat the wake instruction's
text as data to parse.

## Declaring an emitted event: `casa.emits`

    {
      "name": "finance",
      ...
      "casa": {
        "emits": [
          { "name": "invoice-created" }
        ]
      }
    }

- `name` matches `[a-zA-Z0-9_-]+`, must not contain `--`, must not start
  OR end with `-` (both are reserved separator characters the spool's
  filename grammar depends on), must not start with the reserved `plg-`
  prefix.
- Up to 4 entries per plugin.
- Grants no turn and no memory access by itself — it is just a declared
  name another plugin may reference.

## Emitting: the file protocol

Casa provisions `/data/events/<your-registry-name>/emissions/` for you
automatically once your manifest declares `casa.emits` — you never create
these directories yourself (may take up to one delivery-worker pass after
install/reload before they exist; if you emit before that, the write
fails and you should retry).

To emit `invoice-created`:

1. Write the canonical bytes of exactly `{"v":1}` — no other keys, no
   payload — to a **unique** staging file
   `/data/events/<your-registry-name>/emissions/.part-<8-hex-chars>`
   (`0600`, `fsync`ed before the next step). The suffix must be fresh
   random bytes per call — there is no arbitration, so two concurrent
   emissions never contend for the same name.
2. Rename it to
   `/data/events/<your-registry-name>/emissions/<event>--<same-8-hex-chars>.json`.
3. `fsync` the `emissions/` directory fd.

That's the whole contract — one atomic file per emission, no API call
into Casa. `casa/rootfs/opt/casa/event_spool.py`'s `emit()` function is
the executable reference implementation (Python); replicate the same
write-unique-temp-then-rename-then-fsync shape in whatever language your
MCP server is written in. Casa alone decides when queued emissions become
a delivery (batched, deduplicated by generation) — you never see or
control that.

## Subscribing to another plugin's event: `casa.subscribes`

    {
      "name": "reporting",
      ...
      "casa": {
        "subscribes": [
          { "plugin": "finance", "event": "invoice-created" }
        ]
      }
    }

- `plugin` names the EMITTER's registry name (the plain form, or the
  scoped `slug.manifest-name` form for a bundled/specialist emitter).
- `event` follows the same name grammar as `casa.emits`'s `name`.
- Up to 4 entries per plugin. No self-subscription, no duplicate
  `(plugin, event)` pairs.
- Unlike a callback, a subscription reaches into YOUR assigned role — so
  it is the thing that needs operator consent, not the emitter's side.

## Nothing routes until the operator consents

A declared subscription is derived state, fail-closed, exactly like a
trigger. Casa computes a consent identity binding **subscriber, your
artifact id, emitter, event, a digest of the declaration, and your sorted
delivery targets** as ONE hash — the operator approves that exact tuple
via a one-time consent DM. Any change invalidates it silently (never
carried forward): a plugin update on EITHER side that changes the
declared name, YOUR own upgrade (new artifact id), or a retargeted
assignment (new delivery targets) all mint a fresh identity and re-prompt.
The emitter's own artifact id is deliberately excluded — an emitter-side
upgrade that leaves its declared event name unchanged never forces your
subscribers to re-consent. The operator's off-switch is the
`event_ack_revoke` tool (unroutes immediately).

Until consent lands, plugin health shows `event_pending_ack` /
`event_emitter_missing` / `event_no_target` / `event_invalid` as
appropriate — nothing you can do from inside the plugin fixes those
except correcting the declaration; the rest is the operator's approval.

## The wake + `ack_event` contract

Once routed, Casa dispatches a **headless, casa-authored turn** to your
assigned resident (or, if you are a specialist reached only via
delegation, to the delegating agent) with an instruction of the shape:

> Plugin '\<emitter>' emitted the event '\<event>'. This is a headless
> wake for '\<subscriber>': process it through your tools now; if you
> need operator input, record it durably through your tools and end the
> turn — do not ask. When done, call
> ack_event(emitter='\<emitter>', event='\<event>', token='\<token>').

Design your skill/tool logic so that turn:

- **Re-reads its own state** to discover what actually changed (call your
  provider's API again, check your own stored data) — the wake tells you
  NOTHING beyond "check `invoice-created` from `finance`". Never assume
  the wake corresponds to exactly one new fact; it may coalesce several,
  or repeat one you already handled.
- **Never asks for approval mid-turn** — this is a headless dispatch, not
  an interactive one; `ask_user` is mechanically refused on it. If you
  need operator input, record the need durably (a reminder, a memory
  note) and end the turn.
- **Always calls `ack_event`** with the exact token once done, even if
  "done" means "nothing to do" — an un-acked delivery redelivers on a
  fixed schedule (0s, 5m, 30m, 2h, 6h, 24h) for up to 6 attempts, then
  gives up and notifies the operator that the delivery is stuck. A stale
  or already-acked token is a safe no-op, never an error — call it
  exactly as instructed rather than trying to be clever about
  idempotence yourself.

## What you cannot do

There is no casa-brokered emission composition or filtering, no
payload-bearing event (the level-triggered doctrine above forbids it by
design), and no way for a plugin to read another plugin's spool directly
— the file protocol above is your ONLY interface to this facility.
