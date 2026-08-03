This is your weekday morning briefing.

Run the following checklist:

1. Check memory for anything scheduled or due today (meetings, deadlines,
   project milestones).
2. Check for follow-ups the operator committed to in the last 24 hours that
   are now actionable.
3. Check if any scheduled tasks or delegations are queued in your
   schedule (use the get_schedule tool if helpful).

Your tokens are buffered until the turn ends — nothing reaches the operator
until you stop. There are exactly two outcomes:

- SEND — only if there is something actionable or noteworthy. Output ONLY the
  final Telegram message text (3-6 bullet points max, no greeting, no
  preamble, no "good morning").
- STAY SILENT — the default whenever there is nothing actionable or
  noteworthy. To stay silent you MUST output literally the sentinel
  `<silent/>` and nothing else, or produce no output at all.

Staying silent means the sentinel or empty output — never a sentence about
being silent. Do NOT write an "all quiet" / "nothing to report" line, and do
NOT write that you are staying quiet: any such prose is delivered to the
operator as a normal message, which is exactly what this trigger must avoid.
If in doubt, emit `<silent/>`.
