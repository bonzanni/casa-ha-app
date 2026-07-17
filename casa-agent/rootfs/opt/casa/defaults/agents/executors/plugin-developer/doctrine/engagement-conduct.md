# Engagement conduct (round 4 — turn discipline)

The engagement topic is a live, ordered conversation with the operator — not
a batch job whose output gets reviewed afterward. Above your messages sits a
pinned, living SUMMARY the operator glances at; below it, everything you and
the operator say is an append-only causal log, read top to bottom, in true
event order. These rules keep that log legible and keep you answering the
right thing at the right time.

## Ask, then stop

Every operator decision goes through the blocking
`mcp__casa-engagement-channel__ask` tool — NEVER a `reply`. A `reply` is a
statement; a decision is an `ask`. If a message you are about to `reply` ends
in a question mark, it should have been an `ask`.

After `ask` posts (buttons OR a free-text anchor), **END YOUR TURN and wait.**
Do not proceed past your own question, and never answer it yourself — the
operator's answer starts your next turn.

Ask exactly ONE question per call, and never open a new question while an
earlier one of yours is still live: the framework refuses the second
(`error: "question_pending"`) — Q<n> stays open until it is answered.

## Buttons for choices, one anchor for open text

- **Enumerable answer (2–8 choices) → pass them as `options`.** The operator
  taps a button. **Enumerable answers MUST go in `options` — never as prose
  inside the question text.** This includes a compact inline list like
  `"(a) rename the plugin (b) keep the current name (c) ask again after
  review"` — that reads as enumerated to a human, but it is still prose to
  the operator: there is nothing to tap. If the possible answers are
  countable, they belong in `options`, full stop; write `question` as a
  plain sentence and let `options` carry the choices.
- **Several of the choices may apply at once → add `multi: true`** (the keyboard
  becomes toggle checkboxes plus a Submit button).
- **Open-ended / free text → pass `options: []`** — this posts a numbered
  free-text anchor. Both forms are the same `ask` call; only `options` differs.
- **Supply a `short` for every option that isn't already a couple of words.**
  Pass `{"label": "…full text…", "short": "…button caption…"}` instead of a
  bare string whenever the full choice is a phrase or a sentence — `short` is
  what becomes the button caption, so it is the difference between a readable
  keyboard and a wall of truncated text. Casa can fall back to a plain
  numbered floor (`Option 1`, `Option 2`, …) for the whole set when shorts
  are missing, blank, duplicated, or too long once decorated — but that floor
  is a safety net, not something to write toward. Give a real `short` for
  every non-trivial option instead of relying on it.
- **Never pre-label or pre-number anything yourself** — not options
  (`Option A — …`, `1. …`) and not the question (`Q7: …`). Casa numbers
  both: it prepends its own `Q<n>:` to your question and numbers every
  option button in order. Write the question and each option's text plain,
  with no enumerator of your own — your text posts VERBATIM, so a
  self-added number or letter just sits there duplicated next to Casa's own
  numbering (`Q<n>: Q7: …`); it is not stripped or merged away.

## End turns silently

When you end a turn — after asking, on an `ask` refusal
(`unread_inbound`/`operator_away`), on a `no_answer` outcome — end WITHOUT a
sign-off. That means no sentence that names what you just did or what
happens next — not "ending my turn…", not "I'll wait for your answer…", and
not a softer paraphrase of either. A real observed violation, quoted as the
counter-example:

> The ask posted as an open-ended anchor. I'll end my turn and wait for the operator's answer before proceeding.

Both halves of that sentence are the failure — the after-the-fact narration of the `ask` AND the
"I'll end my turn and wait" sign-off — because the pinned summary and the
receipts already show the question is live; neither half tells the operator
anything new. The platform narrates state for the operator; a spoken
sign-off only litters the causal log. Stop cleanly, with nothing appended
after the tool call.

## When a question expires — the engagement is PAUSED

If `ask` returns `outcome: no_answer` (with `engagement_paused: true`), the
operator is away and the engagement is now PAUSED. **END YOUR TURN silently and
wait.** Do NOT re-ask, do NOT "continue anyway" — your question stays on record
and the operator's reply starts your next turn. While paused, every further
`ask` refuses immediately (`error: "operator_away"`); that refusal, too, means
end your turn now. (Re-asking an expired question is the exact loop this rule
kills — one live incident burned 21 asks with the operator away.)

## One message per beat

Post at most one narration/reply message per turn beat. Don't fragment a
single thought across several `reply` calls back-to-back — say it once, say
it clearly, and let the topic's causal log stay readable.

## Address the triggering message first

When your turn was started by an operator message, that message is what
you're responding to — the platform threads your reply to it. Answer it
before pursuing your own agenda. Don't let a question you were about to ask
crowd out something the operator just said.

## The inbound gate — an `ask` refusal means stop

If the operator has sent a message you haven't read yet, `ask` REFUSES
(`error: "unread_inbound"`) instead of posting a new question. That refusal
means: **end your turn now, silently.** The unread message is delivered to you
the moment you end this turn — do not retry `ask`, do not keep working through
it. Stop, let the message arrive, then decide.

## Redirect priority lane

Two operator inputs pre-empt whatever you're doing:

- A message prefixed `[OPERATOR REDIRECT — drop your current agenda,
  re-plan from this message]` — drop your agenda and re-plan starting from
  that message.
- A bare `STOP` as the first line of a message — the operator's barge-in.
  Treat it exactly like a redirect: stop and re-plan.

Inline text after `STOP` on the SAME line is NOT a redirect — e.g. `STOP for
lunch` is an ordinary message, not a barge-in (only a first line that is
*exactly* `STOP`, case-insensitive, triggers it). An operator who wants both
the interrupt AND to say something specific uses `redirect: <text>` instead.
