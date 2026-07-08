# Changelog

## [0.57.1] - 2026-07-09 — publishing readiness: repo shell + green CI

Repository/presentation release — no runtime behavior changes.

### Fixed

- **e2e mock Telegram `getChatMember` (CI red since v0.52.0).** python-telegram-bot
  22.7 parses the response strictly (`User` requires `first_name`/`is_bot`, and
  `ChatMemberAdministrator` requires the full admin-rights field set), so the mock's
  thin payload made the boot-time bot-permissions check fail and disabled
  engagements, breaking the tier2 Engagement E-block. The mock now returns a
  complete `ChatMemberAdministrator` payload.

### Added

- **Store presentation:** root `README.md` (add-repository button, badges, app
  list), `casa-agent/README.md` store intro, MIT `LICENSE`.
- **Translations** for the last untranslated options: `hindsight_api_url`,
  `casa_tz`, `log_level`.
- **Dev tooling:** `setup-dev.sh` falls back to uv-managed CPython when the system
  python can't build a venv, and auto-symlinks `docs/` in linked git worktrees;
  `.worktreeinclude`; memory-accuracy eval scripts tracked under `test-local/eval/`.

### Changed

- `repository.yaml`: repository name now "Casa Apps" (HA renamed add-ons → apps);
  maintainer contact switched to the public noreply address.
- AI-attribution policy: commits now use kernel/Fedora-style `Assisted-by: Claude Code`
  trailers instead of `Co-Authored-By`; the root README discloses AI-assisted
  development. Historical trailers are left untouched.

## [0.57.0] - 2026-07-08 — Theme 10: final correctness lows (11 fixes)

### Fixed

- **`svc-casa/run` no longer execs a PATH-resolved `python3` (L1).** The user-writable
  `/config/tools/bin` is prepended ahead of `/opt/casa/venv/bin` in the s6 container PATH (intentional,
  for engagement tool overrides); `svc-casa/run` now execs `/opt/casa/venv/bin/python3` by absolute
  path, matching `svc-casa-mcp/run` and the Dockerfile's stated venv invariant, so a planted/plugin-installed
  `python3` shim can no longer hijack the main process.
- **A non-object `settings.json` no longer permanently disables agent-home provisioning (L2).**
  `provision_agent_home` only self-healed on `JSONDecodeError`; valid-but-non-object JSON (`null`,
  `[]`, a bare string/number) raised `AttributeError` and was swallowed by the per-role try/except,
  silently skipping default-plugin seeding on every boot thereafter. Non-dict JSON is now treated the
  same as invalid JSON — logged and recreated.
- **Voice client context can no longer clobber channel-computed routing identity (L8/L59).** The SSE
  and WS handlers spread the client-supplied `context` dict *after* the channel-computed
  `chat_id`/`utterance_id`/`cid`, letting a client override them and fork SDK session/rate-limiter
  keying from the transport scope and forge log-correlation `cid`s. The client context now spreads
  first so channel-computed keys always win; benign passthrough keys (e.g. `device_id`) still survive.
- **WS per-connection utterance tasks are now pruned and their exceptions retrieved (L9/L60).** The
  `tasks` dict in `_ws_handler` grew one entry per utterance for the life of the connection and never
  retrieved exceptions from failed tasks (logged as "Task exception was never retrieved"). Each task
  now carries a done-callback that prunes its dict entry and logs any exception; the frame-local
  strong reference to the task is also dropped so a finished task is collectable while the connection
  stays open.
- **`engage_executor`'s `context=` argument now reaches the workspace `CLAUDE.md` (L10/L61).** It was
  stored only in the FIFO first-turn prompt — `engagement.origin` never carried a `context` key, so
  `ClaudeCodeDriver.start`'s read of `engagement.origin.get("context", "")` was always empty and the
  persistent `## Context` section rendered blank. `context` (and the world-state summary) are now
  threaded onto the record's `origin` at creation time and read back out by the driver.
- **The "remote control URL not yet available" fallback notice now actually fires (L11/L62).** It lived
  inside the tail loop's per-line branch, so it never ran when the engagement log file never appeared
  (the production reality) or stayed quiet past the window. A detached one-shot timer now posts the
  fallback at the deadline regardless of whether the log ever yields a line, and is cancelled the
  moment a URL is found.
- **Marketplace mutations on legally hand-edited entries no longer crash (L16/L66).** A string-form
  `source` (legal in Claude Code) raised `TypeError` on `update_plugin_entry`, and an entry missing
  `name` raised `KeyError` from add/remove/update — both escaped the module's `MarketplaceError`
  contract. `load_user_marketplace` now validates entry shape up front, and `update_plugin_entry`
  guards the `source` shape before mutating, both raising a clean `MarketplaceError`.
- **The observer's 3-interjection budget is now consumed only on an actual post (L17/L68).** Declined
  or skipped evaluations (no registry record, LLM says no, notify failure) previously still counted
  against the per-engagement cap, so three declined evaluations could silence a later genuine alert.
  `_interject` now reports whether it actually posted, and only those posts increment the counter;
  per-engagement bookkeeping is also pruned on terminal transition to bound memory growth.
- **The MCP `tools/call` forwarding timeout no longer trips on legitimate slow tool calls (L21/L72).**
  The 10s default was shorter than `emit_completion`/`query_engager` routinely take (Telegram
  round-trips, `classify_tier` SDK one-shots, Hindsight recall + synthesis), producing spurious
  `casa_temporarily_unavailable` errors while the call was actually succeeding server-side. Raised to
  180s; the `/hooks/resolve` route is unaffected (keeps its own explicit `timeout_s=None`).
- **`emit_completion` can no longer double-finalize against a racing `/cancel` (L24/L75).** The
  check-then-act between the terminal-status read and the registry write could interleave with a
  concurrent `/cancel` across a real suspension point (e.g. the G-2 forced-reload await), letting both
  paths run finalize side effects (duplicate topic close, duplicate `DelegationComplete`
  notification). `EngagementRegistry.try_transition_terminal` is now the single atomic gate — only the
  first caller to flip the record terminal runs finalize; `cancel_engagement` also now replies
  `already_terminal` instead of silently no-opping against an already-finalized engagement.
- **A cross-agent webhook trigger name collision is now rejected instead of silently rerouting traffic
  (L28/L79).** `register_agent` only rejected duplicate webhook *paths* and duplicate *names within
  the same agent*; two different agents declaring a webhook trigger with the same name (different
  paths) silently overwrote the wildcard `/webhook/{name}` dispatch target, misrouting the first
  agent's webhook traffic to the second. Cross-role name collisions now raise `TriggerError` at
  registration time.

## [0.56.0] - 2026-07-08 — Prompt-cache + hot-path optimizations, Dockerfile slimming, config_sync boot backstop

### Changed

- **Prompt caching no longer defeated by the per-turn `<current_time>` block (M27).** The
  second-resolution timestamp was regenerated into the *system prompt* every turn, so the cached
  prefix changed every second and Anthropic prompt caching was invalidated for the whole
  conversation (system + replayed messages) on every resumed turn. The `<current_time>` block now
  rides on the per-turn *query text* instead, leaving the large stable system-prompt prefix
  byte-identical across turns (cache-eligible) while the agent still sees the current wall-clock time
  to second precision.

- **Hindsight memory reuses one pooled HTTP connection (L32).** `HindsightSemanticMemory` opened and
  tore down a fresh `aiohttp.ClientSession` (new TCP handshake) for every memory call on the
  per-message path. It now lazily creates and reuses a single long-lived session, closed cleanly on
  shutdown (`SemanticMemory.close()`, wired into `casa_core` teardown — no "Unclosed client session"
  warning).

- **`_finalize_engagement` no longer blocks on tier classification (L33).** The two
  `retain_delegated` calls (engagement + executor summaries) each run an LLM tier-classification
  subprocess; they now run as background tasks (strong-ref'd with exception-logging done-callbacks)
  instead of inline `await`s, so `emit_completion` / `/cancel` return promptly. The rare
  deferred-hard-reload path drains them before the Supervisor restart so the H-1 ordering invariant
  ("all retain writes have landed") still holds.

- **`/new` transcript classification parallelized + acks first (M29).** `transcript_to_items`
  classified every transcript item with a sequential full SDK query, so `/new` on a long
  conversation blocked for minutes; classification now runs with bounded concurrency
  (`asyncio.gather` + a semaphore of 4), preserving item order, tags and idempotent `document_id`s.
  The Telegram `/new` handler also sends its "Starting fresh" ack *before* the save so the user gets
  instant feedback (`reset_channel` stays awaited for crash-durability).

### Removed

- **Dead superpowers v5.0.7 baseline clone dropped from the image (L30).** The Dockerfile cloned
  `obra/superpowers@v5.0.7` into `/opt/casa/claude-plugins/base` on every build, but
  `provision_workspace` has ignored `base_plugins_root` since v0.14.x (plugin symlinks removed).
  Deleted the clone layer and both stale `ARG SUPERPOWERS_REF` pins (the live pin lives solely in
  `marketplace-defaults/.claude-plugin/marketplace.json`, superpowers v5.1.0), and dropped the dead
  `base_plugins_root` parameter from `provision_workspace`, `ClaudeCodeDriver.__init__`, and every
  call site. No add-on option removed.

### Fixed

- **Dockerfile layer order: seed install now precedes `COPY rootfs /` (L31).** The network-bound
  plugin-seed install (marketplace add + 5 GitHub plugin installs) sat *after* the broad
  `COPY rootfs /`, so any code change busted its layer cache and re-ran all installs on every
  rebuild. The seed block (with its narrow gitconfig / credential-helper / marketplace-defaults
  inputs) now runs before the broad COPY; a code edit no longer re-runs the seed install. Same
  reorder applied to `test-local/Dockerfile.test`.

- **`config_sync` post-sync boot-parity backstop (Finding 2).** Deleting an image-owned
  `agents/<role>/delegates.yaml` passes the pre-commit gate (the committed tree is internally
  valid), but `config_sync` re-injects the image-owned file post-commit, producing a
  delegates-without-delegate-tool mismatch that FATALs the next boot. After reconciling, config_sync
  now validates the POST-SYNC `/config` tree with `agent_loader.validate_config_repo` (the hardened
  v0.55.0 boot-parity loader); it self-heals the specific re-injected-delegates case (removing the
  image-owned copy, only when byte-equal to the default) and surfaces any residual error loudly in
  the sync report + logs. Best-effort and boot-safe — the backstop never itself crashes boot.

## [0.55.0] - 2026-07-08 — Boot/driver robustness + concurrency mediums

### Fixed

- **Boot-replay no longer plants a service for a vanished workspace (M7).** When a UNDERGOING
  `claude_code` engagement's s6 service dir was gone but its `/data/engagements/<id>/` workspace was
  also wiped (partial `/data` restore, operator `rm -rf`), `replay_undergoing_engagements` re-planted
  and started the service anyway; the generated run script does `set -e; cd <workspace>`, so it
  exited immediately and s6 respawned it forever. The heal loop now checks the workspace dir exists
  and warn-and-skips when it doesn't (4a.1 §7.3), matching the documented contract. Added an
  `engagements_root` kwarg (defaulted to `/data/engagements`) so the check is testable.

- **`config_git_commit` pre-commit gate now enforces boot-fatal cross-file invariants (M5).**
  `validate_config_repo` only ran per-file JSON-schema validation, so a commit could pass the gate
  yet crash-loop the add-on on the next boot (e.g. a copied resident dir still declaring
  `role: assistant`, a stray unknown file in a resident dir, a schema-valid `executors.yaml` on a
  non-assistant role, a non-empty `delegates.yaml` without the delegate MCP tool whitelisted, or a
  stray non-directory file directly under `agents/` — which `load_all_agents` fatals on).
  The gate now runs a boot-parity pass that exercises the real resident loader (`load_agent_from_dir`)
  and refuses those commits. The parity pass also refuses a committed tree with **no primary
  assistant** — only a non-assistant resident (e.g. `butler`), an empty `agents/` dir, or a sole
  disabled specialist carrying `role: assistant` — which passes every per-file check yet crash-loops
  boot on `casa_core.main`'s "No agent with role 'assistant'" `RuntimeError`.
  Known limitation (by design): the gate validates only the committed tree under `config_dir`; it does
  not simulate `config_sync`'s post-commit re-injection of image-owned defaults (e.g. a committed
  deletion of the image-owned `agents/assistant/delegates.yaml`, which is internally valid here but is
  restored by `config_sync` at boot). That reconciler mismatch is a `config_sync` backstop, not a
  gate-replay defect.

- **`_write_to_fifo` can no longer hang a pooled executor thread forever (M13).** Opening the
  engagement stdin FIFO for writing with a blocking `open()` inside `asyncio.to_thread` parked an
  (uncancellable) pool thread indefinitely when the s6 service had no reader (downed/crash-looping
  service); a handful of stuck writes starved all subprocess orchestration app-wide. It now opens and
  writes non-blocking (`O_NONBLOCK` + `select`-free poll) under a bounded deadline, drops the turn and
  notifies the topic if no reader appears in time.

- **`InCasaDriver.start` rolls back the opened SDK client when the first turn fails (M14).** A
  first-turn `_deliver_turn` failure propagated to `engage_executor` (which marks the record error),
  but error-status records are excluded from `active_and_idle()`, so no sweeper ever tore the client
  down — the opened `claude` subprocess leaked until Casa restarted. `start` now closes + deregisters
  the client via `cancel()` on first-turn failure, then re-raises (the Bug-13 rollback the
  `claude_code` driver already had).

- **Boot reconciler no longer masks a broken install as ready (M23).** `_resolves` (and the
  `verify_plugin_state` MCP tool) treated a **dangling** symlink in `/config/tools/bin` as a resolving
  `verify_bin` via `is_symlink()`, so a rolled-back/wiped plugin was reported `ready` and the boot
  exited 0. Both now use `is_file()` (which follows symlinks and is False for a dangling link), so a
  broken install is correctly reported `degraded`/`missing`.

- **`finish_save`/`clear_save_claim` no longer delete a newly-registered session (M24).** During a
  slow multi-minute freshness-reaper save, a concurrent user turn re-registers the channel with a new
  `sdk_session_id`; `finish_save` then unconditionally popped the entry, wiping the fresh
  registration (mid-conversation amnesia + an orphaned, never-retained transcript). Both methods now
  take an optional `sdk_session_id` and only mutate the entry when it still matches the saved session.

- **npm install strategy namespaces its prefix per plugin (M25).** All npm-type plugins installed into
  one shared `tools_root/npm` prefix, reported as `install_dir`; the two-stage-commit rollback
  (`shutil.rmtree(install_dir)`) therefore wiped `node_modules` for **every** npm plugin and dangled
  their symlinks. The prefix is now `tools_root/npm/<plugin>` (mirroring `venv-<plugin>`), isolating
  rollback. Existing deployments re-namespace on the next install of each plugin.

- **`peek_engagement_workspace` reads at most `max_bytes` off disk (M26).** It called `read_text()` on
  the whole file before slicing, so peeking a multi-GB workspace log loaded the entire file into RAM
  (likely OOM-killing the container) and blocked the event loop. It now reads only the capped byte
  prefix in a thread and decodes it, honouring the documented byte cap.

## [0.54.0] - 2026-07-08 — Hygiene sweep: dead config keys, resource leaks, and small correctness lows

### Removed

- **`subagent_model` add-on option removed (M1).** It was declared in `config.yaml`'s options +
  schema, exported as `SUBAGENT_MODEL` by `svc-casa/run`, and documented in `DOCS.md` /
  `translations/en.yaml` — but no code anywhere ever consumed it (executors and specialists
  hardcode `model: sonnet` in their `definition.yaml`/`runtime.yaml`). Removed the option, its
  export, and its docs; appended `subagent_model` to `DEPRECATED_OPTION_KEYS` in
  `setup-configs.sh` so any stored value is pruned on boot.

### Fixed

- **`telegram_bot_api_base` add-on option is now actually wired to the casa process (M2).** The
  option was consumed by `channels/telegram.py` via `os.environ.get("TELEGRAM_BOT_API_BASE")`, but
  `svc-casa/run` never exported it — only the local e2e test harness did — so a self-hosted Bot API
  server configured via the add-on UI was silently ignored since v0.12.0. `svc-casa/run` now reads
  and exports it (null-normalized, matching the existing optional-string pattern).
- **`webhook_auth_enabled: false` now actually disables webhook auth (L50).** `svc-casa/run`
  exported `WEBHOOK_SECRET` unconditionally from the `webhook_secret` option, so setting the toggle
  off had no effect once a secret value was configured — `casa_core`'s auth-enabled check is purely
  "is the secret non-empty". The export is now gated on `webhook_auth_enabled`.
- **`casactl reload` now accepts `--scope=config_sync` (L80/L29).** The v0.47.0 `config_sync` reload
  scope was registered server-side and advertised by the `casa_reload` MCP tool, but the operator
  CLI's argparse `choices` predated it, so `casactl reload --scope=config_sync` failed with "invalid
  choice" even though the equivalent `POST /admin/reload` succeeded. Added to `casactl` and to the
  configurator's `reload.md` doctrine table (now "eight reload scopes").
- **`_synthesize_answer` now honors its `max_tokens` argument (L76/L25).** `query_engager`'s bounded
  synthesis pass built `ClaudeAgentOptions` without ever applying the caller-supplied token budget,
  so answers were effectively unbounded. Caps output via the `CLAUDE_CODE_MAX_OUTPUT_TOKENS` CLI env
  knob, adds a budget instruction to the synthesis prompt, and hard-truncates any overshoot as a
  belt-and-braces stop; the tool-level arg is also clamped to `[1, 4000]`.
- **`casa_reload_triggers` now enforces the same privileged-role guard as `casa_reload(scope='triggers')` (L77/L26).**
  The Bug 7 (v0.14.6) defense-in-depth check — refuse callers whose effective role isn't
  `configurator` — covered `config_git_commit`, `casa_reload`, and `casa_restart_supervised`, but its
  back-compat alias `casa_reload_triggers` had no such check, so a misconfigured agent's
  `allowed_tools` could re-register another role's triggers with no refusal.
- **A failed engagement start no longer leaks an open Telegram forum topic (L74/L23).**
  `engage_executor` and `delegate_to_agent`'s interactive path create the forum topic before
  starting the driver; when the prompt template was missing or `driver.start` raised, the topic was
  never closed — only `_finalize_engagement` (never reached on these failure paths) does that. Added
  a best-effort `_abort_engagement_topic` helper that flips the topic to `failed` and closes it on
  every `no_driver` / `driver_start_failed` / `prompt_template_missing` path, without routing through
  `_finalize_engagement` (which would double-notify Ellen and run memory-retention side effects).
- **`POST /invoke/{agent}` now returns 400 instead of 500 for non-object JSON bodies and
  `"context": null` (L3).** A body that parsed to a non-dict, or an explicit `"context": null` /
  non-dict context, crashed with an unhandled `AttributeError`/`TypeError` instead of the handler's
  own 400 contract. `invoke_handler` is now extracted into a testable `_make_invoke_handler`
  factory (mirroring `_make_webhook_handler`) with both cases validated.
- **`/internal/hooks/resolve` no longer crashes with HTTP 500 on valid-JSON non-object bodies
  (L65/L14).** A body that parsed to a list/string/number, or a truthy non-dict `payload`, raised an
  unguarded `AttributeError`/`TypeError`; `svc_casa_mcp` then surfaced a misleading "forwarder error"
  deny instead of the intended structured deny. The handler now validates body/policy/payload shape
  and returns the same structured fail-closed deny used for malformed JSON.

### Leak fixes

- **`PERMISSION_QUEUES` entries are now evicted at engagement finalization (L5).** The per-engagement
  `asyncio.Queue` (and any undrained verdict inside it) previously persisted in memory for the
  process lifetime. `_finalize_engagement` now pops the entry, and the verdict-POST handler refuses
  to re-materialize a queue for an engagement that is no longer `active`/`idle`.
- **Compiled s6-rc databases in `/tmp` are now reaped (L63/L12).** Every engagement lifecycle
  compile (`s6-rc-compile` into `/tmp/s6-casa-db-<uuid>`) left the previously-live db orphaned.
  `_compile_and_update_locked` now removes the prior live db after a successful swap (or the
  just-compiled db after a failed one); a new `sweep_orphan_compiled_dbs()` also reaps stale dirs
  from a prior container run during boot replay.
- **`plugin-env.conf` is now created 0600 atomically (L69/L18).** The secrets file was written with
  default umask permissions (typically 0644) and only chmod'd to 0600 afterward — a crash or denied
  chmod between the two steps left the secrets file world-readable. It is now opened with
  `os.O_CREAT` and mode `0o600` from the first byte; the trailing chmod remains as a belt-and-braces
  repair for any legacy 0644 file.
- **`RateLimiter` buckets are now evicted when idle (L70/L19).** Every distinct rate-limit key (e.g.
  an arbitrary Telegram `chat_id` from any sender) permanently allocated a `TokenBucket`, growing the
  per-key dict without bound for the process lifetime. A periodic sweep (every 1024 checks) now
  drops buckets idle for a full `window_s`, which is behaviorally invisible — an idle bucket has
  already refilled to full capacity, identical to a fresh one.

## [0.53.0] - 2026-07-08 — Silent hangs & cross-module contract drift (bus REQUEST resolution, executor hook params, observer drain, permission-relay correlation)

### Fixed

Five defects where a request/response path never resolved, a bus queue was never drained, executor hook params never reached the enforcer, or a permission verdict reached the wrong waiter:

- **A bus REQUEST now ALWAYS resolves its caller's future (M4 + M6).** A REQUEST whose handler
  produced empty/suppressed output — `Agent.handle_message` returning `None` on a `<silent/>` or
  no-text turn — left the pending future unresolved, so voice SSE/WebSocket and `POST /invoke`
  (all `bus.request(timeout=300)`) hung the full ~300s and then surfaced a spurious timeout for a
  turn that actually completed. Fixed on both sides of the contract: `bus.run_agent_loop._dispatch`
  now resolves a REQUEST with an explicit empty `RESPONSE` when the handler returns without
  responding (guarded by `msg.id in self.pending`, so NOTIFY / fire-and-forget stay a no-op), and
  `Agent.handle_message` now returns a typed empty `RESPONSE` for REQUEST turns instead of `None`
  (channel delivery of the empty text is still suppressed). `test_bus.py::test_request_timeout`
  reworked to register a handler-less target for the genuine timeout path.

- **Executor `hooks.yaml` parameters now reach the claude_code HTTP hook path (H3).**
  `_build_cc_hook_policies` invoked every factory with no kwargs, so the `/hooks/resolve` path (the
  only enforcement path for claude_code engagements) ran default-configured policies — an empty
  `path_scope` that denied ALL Read/Write/Edit for a plugin-developer engagement, and
  `commit_size_guard` at the wrong `max_files`. New `hooks.build_policy_callbacks_from_hooks_yaml`
  + `casa_core._build_executor_cc_hook_policies` build per-executor parameterised callbacks from the
  executor's `hooks.yaml`; the resolve handler resolves the engagement from the payload `cwd` and
  prefers that executor's callback, falling back to the defaults for unknown engagements. Boot-time
  snapshot (an operator edit needs a restart to affect the HTTP path).

- **The observer bus queue is now drained (H4).** `observer.subscribe()` registered an `observer`
  target queue + handler, but the boot loop spawned `run_agent_loop` consumers only for resident
  roles + `telegram`, so every engagement event sent to `target='observer'` (subprocess_respawn,
  idle_detected, error tool_results) enqueued forever with no consumer — operator interjections
  never fired and the queue leaked for the process lifetime. New `_bus_loop_targets(agents)` adds
  `observer` (deduped) to the spawn list; the tracked task is cancelled on shutdown with the others.

- **Concurrent permission requests each receive THEIR OWN verdict (M18).** All pending permission
  requests for an engagement shared one `asyncio.Queue`, and `_await_matching_verdict` discarded any
  item whose `request_id` was not its own — so with two parallel tool calls in flight (Claude Code
  issues parallel tools), whichever waiter won `q.get()` for the operator's verdict destroyed it on
  an rid mismatch, denying the approved call by timeout. Verdicts are now correlated by request_id: a
  single per-engagement drain lock lets exactly one waiter read the queue at a time and routes a
  non-matching verdict into a per-`request_id` mailbox for its owning waiter, so cross-delivery is
  impossible and the stale-click defence still holds.

### Fixed

Seven Telegram-channel defects, all in `channels/telegram.py` (plus one agent hook):

- **Webhook ACK no longer blocks on the SDK turn (H5).** `process_webhook_update` awaited
  `Application.process_update`, which (default `block=True` handlers) ran the ENTIRE engagement
  SDK turn — minutes — before the aiohttp route could return 200. Telegram timed out and
  redelivered the update, duplicating turns. The update is now enqueued onto PTB's
  `update_queue` (the fetcher started by `app.start()` drains it) so the route returns in
  milliseconds, both message and callback handlers are registered `block=False` so one long
  turn can't stall PTB's sequential fetcher (which in polling mode also froze Ellen DMs), and a
  bounded `update_id` LRU drops any redelivery already in flight before the first ACK landed.

- **`_teardown_app` runs each step independently (M8).** A failing `delete_webhook` (common
  during the very outage that triggered the rebuild) used to skip `app.stop()`/`shutdown()`,
  leaking the started Application's fetcher task, JobQueue, and HTTPX pools on every reload.
  Each teardown step now has its own try/except, and `_rebuild` rolls back a half-started
  Application if any bring-up step raises before re-raising to the supervisor.

- **`/cancel` can interrupt an in-flight turn (M9).** The per-topic lock was held across the
  whole multi-minute turn, so `/cancel` queued behind the turn it was meant to kill. The user
  turn is now delivered in a tracked background task (strong ref + done-callback), so the lock
  is released as soon as routing/validation completes and `/cancel` acquires it immediately.
  The Bug-10 status re-check still runs under the lock before any task is spawned.

- **`/cancel@botname` is recognized (M10).** Group command menus send `/cancel@<botusername>`;
  the matcher now strips the bot's own `@mention` suffix (commands addressed to a different bot
  fall through to the agent, matching PTB's `CommandHandler`). The bot username is cached at
  engagement setup.

- **Permission-relay keyboard escapes MarkdownV2 (M11).** `post_perm_keyboard` sent tool names
  and previews as MarkdownV2 without escaping, so an MCP tool name (`mcp__x__y`) or a Bash
  preview with a backtick/backslash triggered a Telegram 400 that the relay hook turned into a
  silent auto-DENY. Reserved characters are now escaped (general escaping for the bold tool
  name, pre/code escaping for the fenced preview), with a plain-text fallback on any residual
  parse failure.

- **Typing circuit breaker no longer trips on transient outages (L6).** A transient
  `NetworkError`/`TimedOut` used to count toward the 401 breaker, which then never reset —
  killing typing indicators for the process lifetime. Transport errors now back off without
  counting toward the breaker (the reconnect supervisor owns transport recovery), and a
  successful `_rebuild` heals a previously-tripped breaker.

- **Typing indicator stops after an empty/silent turn (L7).** A turn that strips to empty or
  `<silent/>` never called `send()`/`finalize_stream()`, so the typing loop ran forever
  (permanent "typing…" plus a Bot API call every 4 s, notably in block mode). `agent.py` now
  calls a new `turn_finished()` channel hook on the suppressed-turn path, which stops the
  per-chat typing indicator.

## [0.51.0] - 2026-07-08 — crash-safe on-disk state writes (atomic writes + tolerant load)

### Fixed

On-disk state files were written with a plain truncate-in-place `open("w")` + `json.dump`
(or `write_text`) directly over the live file, so a crash or power-loss mid-write could
leave a truncated/corrupt file. In the worst case a corrupt `sessions.json` was then loaded
intolerantly and crash-looped the add-on on every boot. All such writes now route through a
new shared atomic-write helper (`atomic_io.py`): write to a same-directory temp file, fsync,
then `os.replace` — so a crash can never expose a half-written file. The helper preserves the
prior `open("w")` permission semantics — an existing file keeps its current mode and a fresh
file lands at `0o644` — so it never leaks the `tempfile.mkstemp()` `0o600` onto the replaced
inode.

- **`sessions.json` crash-loop eliminated (H12).** `SessionRegistry._write` is now atomic,
  and `__init__` loads tolerantly: a corrupt/unreadable (or wrong-shape) registry is logged,
  quarantined to `sessions.json.corrupt`, and the fleet starts from an empty registry instead
  of raising and dying on boot. Losing session pointers is recoverable; a boot crash-stop was
  not.
- **Engagement tombstone atomic (M15).** `engagement_registry._write_tombstone` no longer
  risks losing all in-flight engagement state to a truncated `engagements.json`.
- **Delegation tombstone atomic (L20).** `specialist_registry._write_tombstone` — the exact
  file that exists for delegation crash recovery — is now crash-safe.
- **Marketplace + system-requirements manifests atomic (L15, L).** `marketplace_ops._write`
  and `system_requirements/manifest._write` no longer risk bricking marketplace ops / the
  crash-recovery manifest with a truncated file.
- **Config-sync no longer silently destroys user edits when git is failing (M12).** The
  image-wins conflict/backstop paths only wrote a `.casabak` backup when git was entirely
  unavailable. `RealGit.snapshot()` now fails closed (returns `None` on any git error —
  dubious-ownership, a stale `index.lock`, a corrupt repo — instead of returning a stale
  pre-edit HEAD), and both overwrite sites now write a `.casabak` whenever no commit actually
  captured the edit, so an operator's config edit is always recoverable. The boot-time
  snapshot in `setup-configs.sh` also stops logging false success when its commit failed.

### Tests

New crash-simulation unit tests (`test_atomic_io.py` plus additions to the registry,
marketplace, manifest, and config-sync suites) assert the original file stays intact when a
crash is injected between temp-write and `os.replace`, that a corrupt `sessions.json` loads
empty and is quarantined, and that a broken-git conflict falls back to `.casabak`. The
`test_session_registry.py`, `test_engagement_registry.py`, and `test_specialist_registry.py`
suites gained the `unit` marker so the tier2 gate actually runs them.

## [0.50.0] - 2026-07-08 — security hardening: ingress source filter, auth/parsing controls

### Security

Seven security fixes closing authentication, path-traversal, command-parsing, and
secret-handling gaps. Several were controls that existed on paper but were bypassable in
practice; each fix ships with an attack-encoding regression test (the affected test files
also gained the `unit` marker so the tier2 gate actually runs them).

- **nginx ingress now restricts to the Supervisor proxy (H1).** The generated ingress
  `server` block was missing the HA-mandated source filter, so any peer container on the
  hassio bridge could reach the operator dashboard, all proxied API routes, and the web
  terminal (an unauthenticated root shell when `enable_terminal` is on) with HA's ingress
  auth fully bypassed. Added `allow 172.30.32.2; deny all;` at server scope (per
  developers.home-assistant.io), so it filters every route including `/terminal/`.
  Defense-in-depth: the aiohttp backend now binds `127.0.0.1:8099` instead of `0.0.0.0:8099`
  (its only legitimate consumer is nginx in the same container).
- **`telegram_chat_id` is now enforced as an allowlist (H6).** The option is documented as
  "restrict messages to this chat" but was never applied — any Telegram user who found the
  bot got full agent access (home control + shared memory). When `telegram_chat_id` is set,
  updates from any other chat are now dropped (logged, not answered). Empty/unset still
  accepts all chats (documented default); the engagement supergroup and its forum topics,
  and the configured DM, are unaffected. No option removed → no `DEPRECATED_OPTION_KEYS`
  change; DOCS.md already described this behavior.
- **`peek_engagement_workspace` path-traversal closed (H15).** Only the `path` argument was
  guarded; the `engagement_id` was joined into the workspace root unchecked, so `..` or an
  absolute id re-rooted the "workspace" anywhere on disk (leaking `/data/options.json`,
  `plugin-env.conf`, etc.) via the unauthenticated 8099 MCP fallback. The id is now validated
  (`[A-Za-z0-9_-]+`) and the resolved workspace must sit directly under the engagements root.
- **`block_dangerous_bash` no longer bypassed by newlines or quotes (H8 + L13).** Newlines
  are now first-class command separators (so `echo hi\nrm -rf /` is caught on line 2), with
  backslash-newline continuations collapsed first. The pipeline splitter is now quote-aware
  (shlex `punctuation_chars`), so operators inside quoted strings are data, not boundaries —
  fixing both the newline bypass and the false-positive denials of benign commands like
  `git commit -m "cleanup && rm -rf handling"`. Security review of this fix found the
  substitution/exec-wrapper class still open; the detector now also recurses into command
  substitution (`echo $(rm -rf /)`, including double-quoted), backticks, `eval "rm -rf /"`,
  and `… | xargs rm -rf` — while `awk '{print $(NF-1)}'`, `echo $((1+2))`, and
  `eval "$(ssh-agent -s)"` stay allowed (denies only when the *inner* content is dangerous).
- **`casa_config_guard` resident-deletion guard hardened (M16).** The brittle regex was
  evaded by quoted paths, long flags (`--recursive`), and `--`. Replaced with an argv-aware
  detector (shared splitter + path normalization + wrapper-shell recursion) that catches
  every spelling while still exempting `specialists/` and `executors/` subtrees. Security
  review found one residual hole: a leading `//` (which the Linux kernel resolves as `/`,
  but PurePosixPath preserves as a distinct root) slipped past every prefix check — both
  `rm -r //config/agents/<name>` and `Write //data/…`. Path normalization now collapses
  redundant slashes first, and the guard also recurses into `eval` (same wrapper class as
  `bash -c`).
- **Command-parsing guards round 2: `|&`/`;&`/`;;&` now split pipelines; exec-wrapper
  prefixes unwrapped (H8/M16 follow-up).** shlex emits `|&` (pipe stdout+stderr) and the
  case-branch terminators `;&`/`;;&` as single tokens that were missing from the
  pipeline-separator set, so `echo x |& rm -rf /` merged into one argv and the right-hand
  command was never scanned as argv[0]. True redirections (`>`, `>>`, `<`, `>&`, `&>`,
  `2>&1`) still do not split. Exec-wrapper prefixes (`nohup`, `timeout`, `env`, `stdbuf`,
  `setsid`, `time`, `nice`, `ionice`, `chrt`, `taskset`, `unbuffer`, `sudo`, `doas`) are now
  unwrapped in both `block_dangerous_bash` and the resident-deletion guard, so
  `timeout 5 rm -rf /` and `nohup rm -r /config/agents/<name>` resolve to the same decision
  as the bare command (arg-consuming forms like `timeout 5`, `env A=B`, `nice -n 5`,
  `sudo -u root` handled). These guards remain defense-in-depth behind the SDK permission
  system and workspace isolation; known residuals: command/process substitution and non-rm
  destructive verbs (e.g. `find -delete`, `truncate`, `shred`) are not decomposed.
- **Anthropic API keys are now redacted from logs (M19).** The `sk-` redaction pattern could
  never match `sk-ant-api03-…` / `sk-ant-oat01-…` (the hyphen after `ant` broke it), so
  Casa's own primary credential could leak into logs. Added an explicit `sk-ant-` pattern
  ahead of the generic one.
- **Constant-time Telegram webhook token check (L4).** The `X-Telegram-Bot-Api-Secret-Token`
  header was compared with `!=` (timing side-channel); it now uses `hmac.compare_digest` with
  both sides byte-encoded (non-ASCII header → 403, not 500). The handler was extracted into a
  unit-testable factory.

## [0.49.0] - 2026-07-08 — reload subsystem: memory wiring, resident lifecycle, lock + env-drop fixes

### Fixed

Five interconnected defects in the reload subsystem (`reload.py`). The configurator invokes
`casa_reload` routinely after config edits (scope=`agent`|`policies`|`executors`|`full`), so all
of these fired in normal operation, not edge cases. They were invisible to the unit gate because
the existing reload tests stubbed exactly the seams that were broken (`_construct_agent`,
MagicMock bus); the new regression tests drive the real factory and the real `MessageBus`.

- **Reloaded residents no longer lose Hindsight memory (H9).** `reload._construct_agent` — used
  by every reload scope — omitted `semantic_memory` when rebuilding an Agent, so from the first
  reload until the next add-on restart every resident silently fell back to
  `NoOpSemanticMemory`: per-turn overlay/auto-recall returned nothing and cold-session retains
  were permanently lost (a v0.45.0 memory-retirement regression). `CasaRuntime` now carries the
  boot-built `semantic_memory` (new defaulted field, kept last) and the factory passes it through.
- **Residents added via reload now actually consume their queue (H10).** Bus consumer tasks
  (`run_agent_loop`) were only spawned at boot, so a resident created at runtime +
  `casa_reload(scope='agents'|'full')` was registered on the bus but nothing ever read its
  queue — cron triggers, webhooks, and NOTIFICATIONs targeting it sat enqueued (and `/invoke`
  504'd) until a container restart. The bus now owns the consumer lifecycle:
  `MessageBus.start_agent_loop(name)` spawns an idempotent tracked consumer; boot and every
  reload registration path go through it.
- **Evicted residents no longer keep running as ghost agents (H11).** Eviction called
  `bus.unregister(...)` — a method `MessageBus` never had; the `AttributeError` was swallowed, so
  a deleted resident kept its queue, handler, live consumer, APScheduler jobs, and webhook
  allowlist entries, and went on executing scheduled prompts until restart. `MessageBus.unregister`
  now exists (cancels the tracked consumer task — awaited by the eviction path — and drops the
  queue + handler, so later sends silently drop), and eviction also unwinds the role's triggers
  via `trigger_registry.reregister_for(role, [], [])`. Add and remove are now symmetric:
  register + start loop ⇄ cancel loop + unregister + trigger unwind.
- **`scope='full'` is now actually exclusive (M21).** The reload `_RWLock` writer path recorded
  no lock state, so a `full` reload was not mutually exclusive with concurrent per-scope
  reloads — both could interleave their multi-step mutations of `runtime.agents` /
  `role_configs` / `agent_registry` across `to_thread` awaits. The lock now tracks an active
  writer: readers wait for it, and the writer waits for both readers and any prior writer.
- **First `plugin_env` reload can now drop boot-applied keys (M22).** The deletion diff in
  `reload_plugin_env` compared against a snapshot that started empty and was never seeded by the
  boot path, so a secret removed from `plugin-env.conf` after boot survived in `os.environ` (and
  kept reaching plugin MCP subprocesses) for the container's lifetime. Boot now seeds the
  snapshot via `reload.note_boot_plugin_env(...)` right after sourcing `plugin-env.conf`.

## [0.48.0] - 2026-07-08 — move blocking I/O off the single event loop

### Fixed

- **No more whole-add-on freezes from blocking calls on the shared event loop.** Casa runs one
  asyncio loop serving every agent and channel (Telegram, voice SSE/WebSocket, scheduler, bus),
  so any synchronous subprocess / download / heavy filesystem walk on it froze *all* conversations
  for its full duration. Seven such call sites are now dispatched off the loop (and the network
  fetch is bounded):
  - **Resident plugin resolution (H2/M20).** `Agent._process` shelled out to
    `claude plugin list --json` (a blocking Node spawn, 30s timeout) on *every* turn. It now runs
    via `asyncio.to_thread` and is cached per Agent instance — the install doctrine already makes
    `casa_reload(scope='agent')` mandatory after a plugin change, and that rebuilds the Agent, so
    the cache can never surface a stale plugin set (a degraded/empty CLI result is not cached, so
    it retries). The three delegation/executor call sites in `tools.py` are offloaded too.
  - **Plugin tarball download (H13).** `install_tarball` used `urlretrieve` with no timeout (global
    default `None`), so a stalled marketplace server hung the loop forever. Now `urlopen(timeout=…)`
    bounds every socket op, and `install_casa_plugin` / `uninstall_casa_plugin` run off the loop.
  - **Plugin / marketplace / 1Password tool handlers (H16).** `install`/`uninstall`/`marketplace_*`
    (`claude plugin …`, up to 300s per role) and the `op` CLI vault handlers now offload via
    `asyncio.to_thread`; a new `_PLUGIN_TOOLS_LOCK` preserves the mutual exclusion the single loop
    used to give the mutating handlers for free.
  - **`commit_size_guard` (M17).** The per-Write/Edit `git status --porcelain` (up to 5s) is
    offloaded.
  - **`self_containment_guard` (M28).** The per-`git push` tree scan now filters by filename
    *before* reading, caps each read at 256 KiB, and runs off the loop.
  - **`list_engagement_workspaces` (L27).** The du-style `os.walk` + `os.stat` over every retained
    workspace is offloaded.

  Deferred to a separate PR: `session_saver.transcript_to_items` sequential SDK classify queries
  (M29) — its fix is architectural (SDK-query concurrency).

## [0.47.1] - 2026-06-08 — prune deprecated add-on option keys on boot

### Added

- **Deprecated-options prune.** On boot, `setup-configs.sh` deletes add-on option keys that
  Casa has removed from its schema (via `bashio::addon.option '<key>'`), so HA Supervisor
  stops logging `Option '<key>' does not exist in the schema` after a field-removing release.
  Warning-level hygiene only — under current HA an unknown stored option is a warning, not a
  crash, and casa already ignores unknown keys; this just silences the recurring warning and
  follows HA's documented recommendation. Seeded from a git-history audit of every option ever
  removed (`github_token`, `heartbeat_enabled`, `heartbeat_interval_minutes`, `honcho_api_key`,
  `honcho_api_url`, `repos`, `scope_threshold`, `telegram_webhook_url`). Additive
  `DEPRECATED_OPTION_KEYS` list; idempotent (no-op on clean installs). Completes the add-on-
  options half of the schema-tightening drift (the `/config` half shipped in v0.47.0).

## [0.47.0] - 2026-06-08 — `/config` default-sync reconciler (no more manual `cp` after a deploy)

### Added

- **Automatic `/config` default sync.** Image-default-owned config under `/config/{agents,policies}`
  now tracks the shipped `/opt/casa/defaults` on every boot (and via
  `casa_reload(scope=config_sync)`) — **including file removals** — so a config change baked into a
  new image takes effect without the manual `cp` that the v0.46.3→v0.46.7 toolbox arc required after
  every deploy. New module `config_sync.py` does a three-way merge (baseline `/data/config-baseline`
  / new defaults / live `/config`): untouched files track the image; genuine runtime edits are
  preserved; on a true conflict the **image wins** after a commit-first snapshot to the `/config` git
  repo (the prior edit stays recoverable), and **Ellen** proactively tells the operator with a
  carry-over offer. A **schema-validation backstop** force-applies the default to any kept-live file
  that is invalid against a newly tightened schema, so casa **always boots** (closes the
  schema-tightening crash-loop class structurally). New configurator doctrine recipe
  `recipes/config/reconcile-defaults.md` drives the operator-initiated carry-over via `git diff`.

### Changed

- **`setup-configs.sh`**: the dir-level `seed_agent_dir` seeder and the warn-only `drift-check` block
  are replaced by the reconciler (which seeds, tracks, and removes per file, and *acts* instead of
  only warning). The `c1-relay-migration` content migration is retained. New persistent state:
  `/data/config-baseline/` (last-synced defaults) and `/data/config-sync-report.json` (per-boot
  result, consumed by the Ellen notification).

## [0.46.7] - 2026-06-07 — configurator secrets doctrine: gitignored plugin-env.conf → empty commit SHA is expected

### Changed

- **`recipes/plugin/secrets.md` now sets the no-SHA expectation explicitly.** A live dogfood —
  driving Ellen → the configurator to wire context7's optional `CONTEXT7_API_KEY` from
  `op://Casa/Context7/credential` — passed cleanly (read recipe → `set_plugin_env_reference` →
  `config_git_commit` → `casa_reload(scope='plugin_env')` → `emit_completion`). It surfaced one
  latent ambiguity: `plugin-env.conf` is a mode-0600 **gitignored** secrets file, so
  `config_git_commit` after a secret-only change stages nothing and returns an empty SHA. Sonnet
  handled it ("gitignored so no commit SHA"), but the canonical `emit_completion` template still said
  `committed SHA <sha>`. The doctrine now states the empty SHA is expected (not a failure) and the
  completion text should say "no SHA (secrets file gitignored)". Doctrine-only change.

## [0.46.6] - 2026-06-07 — context7 re-modeled as a proper plugin (+ configurator doctrine for its optional key)

### Changed

- **context7 is now a real CC plugin, not a driver special-case.** It *is* an official plugin
  (`claude-plugins-official` `external_plugins/context7`), so v0.46.5's driver-level HTTP wiring in
  `drivers/workspace.py` (injecting a context7 MCP server into each engagement `.mcp.json`) was the
  wrong model and is **reverted**. context7 is added to the dev marketplace (pinned sha `bd7cf41`) +
  the plugin-developer's `plugins.yaml` + the image seed install; `mcp__context7` stays allow-listed.
  The plugin brings its own MCP server (`npx @upstash/context7-mcp`).

### Added

- **Configurator doctrine for context7's optional key** (`recipes/plugin/secrets.md`): context7's
  `CONTEXT7_API_KEY` is **optional + not declared** in the plugin's `.mcp.json` (so `install`/`verify`
  don't surface it), and is a **global** env var. The new section tells the configurator to wire it
  via `set_plugin_env_reference(var_name="CONTEXT7_API_KEY", op_ref_or_value="op://…")` →
  `casa_reload(scope='plugin_env')`. Keyless works (rate-limited); the key raises limits.

### Notes

- The official context7 plugin runs `npx -y @upstash/context7-mcp` — the plugin *source* is pinned,
  but the npm package is fetched latest at runtime (needs node in the engagement). Acceptable for now;
  can pin the npm version later if the freeze matters.

## [0.46.5] - 2026-06-05 — plugin-developer: context7 (current library/SDK docs) — toolbox complete

### Added

- **The `plugin-developer` executor now has the `context7` MCP server** — current, version-accurate
  library/SDK/CLI docs (boto3, the MCP SDK, …), so it codes against today's APIs instead of stale
  training memory. The `claude_code` driver wires it into the engagement `.mcp.json` when an executor
  declares `context7` in `mcp_server_names` (per-executor, not hardcoded for all). The hosted endpoint
  `https://mcp.context7.com/mcp` works **keyless** (verified 2026-06-05); an optional
  `CONTEXT7_API_KEY` env raises the rate limits. Server-level allow (`mcp__context7`) auto-approves its
  tools (`resolve-library-id`, `query-docs`). Regression tests added (`.mcp.json` includes context7
  iff declared).

This **completes the plugin-developer toolbox** (v0.46.3 freeze + drop document-skills; v0.46.4 broad
`Bash` + web; v0.46.5 context7). A live end-to-end check happens when the executor is enabled
(currently `enabled: false`).

## [0.46.4] - 2026-06-05 — plugin-developer: broad Bash + web research (it can finally run/test + read docs)

### Changed

- **The `plugin-developer` executor now has broad `Bash` + `WebFetch`/`WebSearch`.** Previously its
  Bash was limited to `Bash(git*)`/`Bash(gh*)` and it had no web/doc access — so it authored code it
  could neither run, test, nor research (coding from stale memory). It now runs open-ended toolchains
  (python/pytest/uv/npm/ruff/tsc/…) and can read docs/examples. Safety is unchanged and lives in the
  hook stack — `block_dangerous_bash` + `path_scope` (writes confined to `/data/engagements/`) + the
  `engagement_permission_relay` (operator approval) — not in a Bash allowlist; `git push` still fires
  `self_containment_guard`. A dev sandbox (isolate *where* dev executors run) is on the roadmap.
- Widened the engagement permission filter (`drivers/workspace.py` `_VALID_CC_PERMISSION_RE`) to
  accept bare `Bash` and `WebFetch`/`WebSearch` (it previously required `Bash(...)` and dropped the
  web tools). Regression test added.

### Notes

- `context7` (structured library/SDK docs) follows in v0.46.5 — it's an MCP server and the engagement
  `.mcp.json` is hardcoded to casa-framework, so it needs a small driver change + an HTTP-vs-`npx`
  decision + a live check.

## [0.46.3] - 2026-06-05 — plugin-developer dev tooling: freeze at official pins + drop mis-bundled document-skills

### Changed

- **Re-sourced + froze the dev-tooling marketplace** (`casa-plugins-defaults`) at official, pinned
  versions — nothing floats now: `superpowers` `v5.0.7`→**`v5.1.0`** (obra/superpowers); the
  `claude-plugins-official` subdirs (`plugin-dev`/`skill-creator`/`mcp-server-dev`) re-pinned
  `020446a`→**`bd7cf41`**.

### Fixed / Removed

- **Removed `document-skills` from the plugin-developer's toolbox.** It is `xlsx/docx/pptx/pdf`
  document *processing* — not plugin-dev tooling — and was mis-bundled: its catalog description
  claimed "mcp-builder, doc-coauthoring, theme-factory", but those live in a *different*
  anthropics/skills pack (`example-skills`). The builder's workspace template even referenced a
  non-existent **`document-skills:mcp-builder`** skill — fixed to rely on `mcp-server-dev` for MCP
  building. Updated the marketplace catalog, `plugins.yaml`, the Dockerfile/test seed installs, the
  setup scripts, the configurator doctrine, `DOCS.md`, and the catalog test (which now guards both
  the removal and that every entry is pinned).

### Notes

- A fast-follow (v0.46.4) will add the plugin-developer's **broad `Bash`** + `WebFetch`/`WebSearch`
  + `context7` — those need executor driver-layer changes (the permission regex drops bare `Bash`/web
  tools, and the engagement `.mcp.json` is hardcoded), so they're handled separately with verification.

## [0.46.2] - 2026-06-04 — Fix: disabled specialists no longer advertised in a resident's delegate list

### Fixed

- **A `delegates.yaml` entry pointing at a disabled specialist is no longer shown to the resident.**
  The `<delegates>` system-prompt block was rendered straight from the static `delegates.yaml`, with
  no cross-check against the enabled-specialist registry — so a specialist set `enabled: false` (but
  still listed as a delegate) was advertised to e.g. Ellen, who would then try to delegate and get
  an `unknown_agent` rejection from the tool. `_render_delegates_block` now filters delegates through
  the live `AgentRegistry` (residents + **enabled** specialists only, via new `AgentRegistry.is_known`);
  a disabled/removed specialist is neither advertised nor callable. Back-compat preserved (no registry
  → render all). Regression test added.

## [0.46.1] - 2026-06-04 — Fix: `hindsight_api_url` actually enables long-term memory

### Fixed

- **Setting `hindsight_api_url` now turns long-term memory ON.** `casa_core` requires
  `MEMORY_BACKEND=hindsight` (anything else → `noop`), but **nothing in the add-on ever set
  `MEMORY_BACKEND`** — no option, no `environment:` block, no export in `svc-casa/run`. So
  long-term memory was effectively **unreachable**: even with `hindsight_api_url` configured, casa
  stayed on the NoOp backend (short-term only). `svc-casa/run` now derives
  `export MEMORY_BACKEND="${MEMORY_BACKEND:-hindsight}"` inside the `hindsight_api_url` conditional,
  making the URL the single toggle (set it → on; empty → off). A regression guard test asserts the
  derivation. `DOCS.md` updated accordingly.

## [0.46.0] - 2026-06-04 — Add-on config conformance: config in Supervisor-managed `/config`

Moves casa's persistent configuration to the **Supervisor-managed `addon_config` mount** at
**`/config`**, conforming to HA add-on conventions.

### Changed

- **`config.yaml` `map: all_addon_configs:rw` → `addon_config:rw`.** Casa now reads its config from
  `/config` (host `/addon_configs/{REPO}_casa-agent/`), the dir Supervisor recognizes as the add-on's
  config. Every hardcoded `/addon_configs/casa-agent` (~60 refs across code, s6 scripts, AppArmor,
  configurator/plugin-developer doctrine, and `DOCS.md`) now uses `/config`. AppArmor updated to
  `/config/** rwk`.

### Why

The old layout mapped the **entire** `/addon_configs/` tree (every add-on's config) and hardcoded
the *base* slug path `/addon_configs/casa-agent` — which Supervisor does **not** recognize as the
add-on's config dir. Consequences (observed live): an uninstall with "remove configuration" did
**not** clean casa's config, and **HA add-on backups silently missed it** (they capture the
slug-prefixed dir, which was empty). Casa never read any other add-on's config, so the broad mount
was unnecessary. Now config is backed up by HA, removed on remove-config uninstall, and conforming.

### Migration

**None — this is a path change with no auto-migration.** A fresh install seeds `/config` cleanly.
An in-place upgrade does **not** move an existing `/addon_configs/casa-agent/` config to `/config`;
re-create/seed config after upgrading (or restore from a backup of the old dir). The `/data` volume
(sessions, `webhook_secret`) is unaffected.

## [0.45.1] - 2026-06-04 — Fix: tier classifier runs as root

### Fixed

- **`tier_classifier` no longer uses `permission_mode="bypassPermissions"`** (→ `acceptEdits`).
  `bypassPermissions` makes the SDK pass `--dangerously-skip-permissions` to the bundled
  `claude` CLI, which **refuses to run as root/sudo** — and HA add-ons run as root, so every
  classification call failed and silently defaulted to `private` (leak-safe but over-restrictive:
  *all* new long-term memories ended up `private`, and the logs flooded). With `allowed_tools=[]`
  there is nothing to approve, so `acceptEdits` (the mode the rest of Casa runs as root) is
  equivalent and works. Found live on the N150 right after the v0.45.0 deploy. A regression guard
  test now asserts the classifier never uses `bypassPermissions`.

## [0.45.0] - 2026-06-04 — Tiered memory access (4/4): full legacy retirement

Completes the tiered-memory re-architecture by **deleting the entire legacy memory stack**.
`active_semantic_memory` (Hindsight) is now the only memory; short-term continuity stays on the
Claude Agent SDK session. `MEMORY_BACKEND ∈ {hindsight, noop}` (any other value resolves to
`noop`, never crashes).

### Removed

- **`memory.py`** — `MemoryProvider` / `HonchoMemoryProvider` / `SqliteMemoryProvider` /
  `CachedMemoryProvider` / legacy `NoOpMemory`, plus the per-(role,user_peer) render helpers.
- **The ONNX domain classifier** — `scope_registry.py`, the **`fastembed`** dependency, the
  per-scope read fan-out in `agent.py`, the `scopes_owned`/`scopes_readable`/`default_scope`
  `MemoryConfig` fields + their boot validation, the `policies/scopes.yaml` corpus +
  `policy-scopes.v2.json` schema, and the `scope_threshold` option/env plumbing. The whole
  scope-routing block was dead since v0.43 (its outputs fed only a telemetry log + an unread
  `origin_var["scope"]` stamp); the resident read path already uses the shared `casa` bank with
  sensitivity-tier tags. `channel_trust` (trust tokens for the system prompt + peer mapping) is
  **preserved** — it is independent of the scope registry.
- **The legacy backend-selection machinery** — `_MemoryChoice`, `resolve_memory_backend_choice`,
  `_wrap_memory_for_strategy`, the `active_memory_provider` field/seam, and `Agent`'s vestigial
  `memory: MemoryProvider` param + `self._memory`. The dashboard memory row now reads the
  surviving semantic-backend choice (defensively — `GET /` never raises on a memory misconfig).
- **Honcho** — the `honcho-ai` dependency, `honcho_ids.py`, the `honcho_api_url`/`honcho_api_key`
  add-on options + schema + translations, the `HONCHO_API_KEY`/`HONCHO_API_URL` s6 exports, and
  `HONCHO_API_KEY` from the engagement-template unset list / `_PASSWORD_ENV_VARS`. `MEMORY_BACKEND`
  no longer accepts `honcho` or `sqlite`.

### Changed

- `session_registry.build_session_key` now validates the session-key charset inline (was
  delegated to `honcho_ids.honcho_session_id`); the produced key format is **byte-identical**.
- Configurator doctrine + `DOCS.md` updated to the shared-`casa`-bank model (no SQLite default,
  no Honcho options, no per-role Honcho sessions, no scope corpus).

### Migration note (pre-1.0)

A stale user `runtime.yaml` still carrying `scopes_owned`/`scopes_readable`/`default_scope` (or a
saved `honcho_api_*` / `scope_threshold` add-on option) is now rejected/ignored. The store is cold
and these are pre-1.0 removals — set `MEMORY_BACKEND=hindsight` + `hindsight_api_url` for long-term
memory, or leave unset for short-term-only.

## [0.44.0] - 2026-06-04 — Tiered memory access (3/4): collapse specialists/executors/engagements

Folds the **specialist / executor / engagement** memory subsystem off the legacy
`MemoryProvider` (Honcho/SQLite, per-role banks) and onto the shared tier-tagged Hindsight bank
`casa` shipped in v0.43.0. Every delegated context now inherits the **originating context's BOTH
axes** — read-clearance *and* write-trust (design `2026-06-03-tiered-memory-access-design` §3):

- **Reads** become a single `recall("casa", text, tags=readable_tiers(clearance_for_channel(
  origin_channel)))` at the parent/engagement origin's clearance. A finance specialist spawned
  from a private Telegram turn recalls at `private`; one spawned from voice recalls at `friends`.
- **Writes** are explicit, tier-classified `retain`s gated by `writes_to_bank(origin_channel)` —
  because specialists/executors are **ephemeral** (no session registry → the freshness reaper
  never sees them), so the reaper can't catch their turns. **Voice-originated delegation writes
  nothing** (recall-only): no speaker auth → it cannot poison the trusted store.

### Added

- `delegated_memory.py` — the delegated-context bridge: `delegated_recall(...)` (read at the
  inherited clearance, best-effort) and `retain_delegated(...)` (explicit, write-trust-gated,
  per-item tier-classified retain with idempotent `document_id`). One place that holds the
  inheritance rule.

### Changed

- **Specialist delegation** (`_run_delegated_agent`) reads via `delegated_recall` and writes one
  tier-tagged `retain` of the exchange via `retain_delegated` — the bespoke per-turn `add_turn`
  and Ellen meta-write are gone (under shared tier memory Ellen recalls everything at her
  clearance, so the meta-session was redundant).
- **Executor archive** (`_fetch_executor_archive`) becomes a semantic recall keyed on the current
  task at the engagement's inherited clearance (was a query-less per-executor recency read).
- **Engagement finalize** (`_finalize_engagement`) retains the structured engagement summary
  (and the executor-type summary, distinct `document_id`) as tier-tagged `retain`s; the
  completion **post-back NOTIFICATION is unchanged** — the resident reaper still retains it at the
  engager's trust.
- **`query_engager`** reads via `delegated_recall` at the engager's clearance.

### Removed

- **`consult_other_agent_memory`** is retired — under shared tier memory it was an access-control
  **bypass** (it read another role's bank *unfiltered*, ignoring clearance). Removed from the
  tool registry, the assistant `runtime.yaml` allowlist, the assistant `system.md` (replaced with
  shared-bank "when to delegate vs. recall" guidance), and the configurator doctrine recipes.
- **`cross_recall`** removed from the `SemanticMemory` seam (abstract + NoOp + Hindsight) — the
  retired tool was its only consumer.
- All residual `active_memory_provider` **reads** in the delegated/engagement paths
  (`emit_completion` / `cancel_engagement` / workspace-delete / `query_engager` plumbing), plus
  the now-dead Honcho meta-summary retry loop. `active_memory_provider` itself remains an inert
  bootstrap seam (removed in 4/4).

## [0.43.0] - 2026-06-04 — Tiered memory access (2/4): tier model

Long-term memory moves onto a **sensitivity-tier access model over one shared Hindsight bank**
(`casa`), replacing the per-role banks + domain-scope tags of v0.39–v0.41. Two independent
axes (design `2026-06-03-tiered-memory-access-design`, revised 2026-06-04):

- **Read-clearance** — *who may see a fact*. Per channel: voice = `friends`, a private
  Telegram DM = `private`. Recall is a single `recall("casa", text, tags=readable_tiers(
  clearance))`; the un-tier-filterable mental-model **overlay** (`profile`) is pushed **only at
  `private` clearance**. A `private` fact is therefore invisible to voice — including on a later
  friends-present voice night.
- **Write-trust** — *may we believe & store a fact*. Per channel, distinct from clearance.
  Authenticated channels (Telegram) classify **each retained message-item at its true
  sensitivity tier** (`tier_classifier`, eval-validated `SENSITIVITY_PROMPT`, default-`private`
  on uncertainty) in the **background save path** — off the turn's hot path. **Voice is
  recall-only**: it has no speaker recognition yet, so it writes nothing (it cannot poison the
  trusted store with a guest's words / a friend's joke).

### Added

- `tier_classifier.py` — per-item tier classifier (one-shot SDK query over the converged
  `SENSITIVITY_PROMPT`; leak-safe `private` default on blank/unparseable/error). Runs in the
  reaper / backgrounded gap-retain, never on the turn's critical path.
- `channel_policy.py` — `writes_to_bank(channel)` write-trust predicate (voice → recall-only;
  unknown channels fail safe to no-write).
- `session_saver.retain_cold_session(...)` — a **claim-free, registry-decoupled** background
  retain for the next-turn-after-gap path, so the prior session's classify+retain runs off the
  new turn's hot path and cannot race the registry pointer rewrite.

### Changed

- **One shared bank `casa`** for all roles (was per-role `casa-{role}`); item tags are now
  **sensitivity tiers**, not domain scopes. Read path, the `recall_memory` pull tool, the save
  reaper, and the gap-retain all use the shared bank + clearance helpers.
- Overlay (`profile`) gated to `private` clearance; voice no longer receives it (the obsolete
  per-role voice **prewarm** was removed).
- The freshness reaper saves authenticated channels only and **drops cold voice entries**
  (registry hygiene) instead of persisting them.

### Removed

- The per-turn ONNX **`write_scope`** classification and its registry recording
  (`SessionRegistry.record_write_scope`) — tiering now happens per-item in the background save
  path. (The read-side scope routing / `origin_var["scope"]`, the ONNX classifier, `fastembed`,
  and the legacy `MemoryProvider` stack are deliberately retained for the later retirement
  plans.)

## [0.42.1] - 2026-06-03 — Sensitivity prompt tune (clear the accuracy gate with margin)

The v0.42.0 `SENSITIVITY_PROMPT` shipped before its live-LLM accuracy was ever measured (no
credentials in the build env). Measured against the 35-row eval set, it straddled the 0.90
gate (0.886–0.91 across runs — flaky at the threshold), with the **`family` tier** as the
weak point. This patch refines the prompt (eval labels unchanged) so the gate clears with
margin before the tier model is built on it.

### Changed

- `sensitivity.py` `SENSITIVITY_PROMPT` — sharpened the three boundaries the eval flagged:
  - **family** — a SHARED-SPACE secret/credential (home alarm/disarm code, the MAIN wifi
    password) is `family`: explicitly NOT `private` (not a personal-account login) and NOT
    `friends` (not guest-facing).
  - **rule 2** — finances are `private` including **invoicing/billing patterns or habits**
    (not only amounts/accounts).
  - **public** — the make/model/brand of a household device (thermostat, tap, appliance) is
    impersonal → `public`.

  Live accuracy now **0.94–0.97** across three runs (was 0.886–0.91). The lone stable miss
  is the alarm-disarm-code row over-escalating to `private` — the *safe* direction (forget,
  never leak), which the design's failure-asymmetry favors. Still inert: nothing imports
  `sensitivity.py` at runtime yet.

## [0.42.0] - 2026-06-03 — Tiered memory access (1/4): sensitivity-tier classifier foundation

First step of the tiered-memory-access work (design `2026-06-03-tiered-memory-access-design`):
the accuracy-critical classifier foundation, shipped **inert** (not yet wired into the turn
flow — that lands in the tier-model step). Long-term memory access will be gated by a
per-fact **sensitivity tier** rather than domain, since retrieval is already semantic.

### Added

- `sensitivity.py` — the access-tier vocabulary: a `private ⊃ family ⊃ friends ⊃ public`
  ladder (`TIERS`), `readable_tiers(clearance)`, `apply_ceiling(tier, ceiling)`,
  `clearance_for_channel` (voice = `friends`), `parse_tier`, and `SENSITIVITY_PROMPT` — a
  classification prompt **converged with the maintainer** via an interactive eval session
  (friends is the broad default; finances/diagnoses-meds-mental-health/personal-account
  secrets/intimate/identity-PII → private; family is narrow — shared-space secrets +
  family-internal sensitive; public = impersonal general knowledge).
- `tests/fixtures/sensitivity_eval.jsonl` — a 35-fact, all-tier **eval set** (the
  maintainer-graded ground truth) + a schema unit test and a `slow`, credential-gated
  live-LLM accuracy regression harness (threshold 0.90), kept out of the fast unit gate.

Inert: nothing imports `sensitivity.py` at runtime yet. No behavioural change.

Final step of the **resident** memory re-architecture (design spec §4.3). The resident
agents' READ path now runs on the SemanticMemory seam: a cheap mental-model **overlay**
(`profile`) at fresh-session start + a single relevance-ranked **recall** over the
readable scopes (replacing the per-scope `get_context` fan-out), plus a `recall_memory`
pull tool and the cross-agent consult re-implemented on `cross_recall`. Combined with
v0.40.0's save path, a `MEMORY_BACKEND=hindsight` instance now both **writes and reads**
its long-term memory on the self-hosted Hindsight add-on.

**Scope note:** this completes the RESIDENT memory model. The specialist / executor /
engagement memory subsystem (delegation, executors, engagements) still runs on the legacy
`MemoryProvider` (Honcho/SQLite) — migrating it onto the seam, and the full retirement of
`MemoryProvider` + Honcho + SQLite, is a **deferred follow-up** (the spec designed only the
resident model). Honcho/SQLite options therefore remain. Without `MEMORY_BACKEND=hindsight`,
residents have no long-term recall (short-term conversation continuity is unaffected — it
is owned by the SDK session).

### Added

- `recall_memory` pull tool — on-demand semantic recall against the agent's own role bank,
  trust-filtered by readable scope; voice uses `budget=low` so the cross-encoder rerank
  never stalls the first utterance.
- `agent.active_semantic_memory` + `agent.active_scope_registry` handles, wired in `main()`
  (the latter also fixes the status dashboard's scope display, previously always "(none)").

### Changed

- Resident read path (`agent.py`): `peer_overlay_context` → `profile` overlay (pushed only
  at fresh-session start; rides along on resume), and the per-scope `get_context` fan-out →
  one channel-aware `recall(tags=<readable scopes>)`. Text channels auto-recall the opening
  utterance; voice pushes the prewarmed overlay only and recalls on demand via the tool.
- `consult_other_agent_memory` now reads via `SemanticMemory.cross_recall` against the
  target role's Hindsight bank (was `MemoryProvider.cross_peer_context`).
- Voice prewarmer warms the cheap `profile` overlay instead of the per-scope session
  fan-out; `VoiceChannel`'s memory handle is now the SemanticMemory seam.

## [0.40.0] - 2026-06-03 — Memory re-arch (2/3): long-term save on Hindsight

Second step of the memory re-architecture (design spec §4.2). Wires the
session-granularity **long-term save** path onto the SemanticMemory seam: ended
conversations are retained to the self-hosted Hindsight add-on. **Saves are active** when
`MEMORY_BACKEND=hindsight` + `hindsight_api_url` are set; long-term **recall** (reads)
lands in the next step (3/3), so a hindsight-selected instance writes facts but does not
yet read them back.

**Behaviour change for non-Hindsight backends:** residents no longer write memory
per-turn on ANY backend (the per-turn `add_turn` is gone). Short-term continuity is
unaffected — it is owned by the per-channel SDK session, which resumes as before. But the
legacy Honcho/SQLite stores are no longer written by residents (they still serve reads
until retired in step 3/3), so a `sqlite`/`honcho`/`noop` instance has **no resident
long-term memory** until you switch to `MEMORY_BACKEND=hindsight`. Specialist/engagement
memory writes are unchanged. (This is the spec §7 "cold cut" — `Hindsight` is the only
backend with active long-term writes from v0.40.0 on.)

### Added

- **Freshness reaper** (`freshness_reaper.py`) — the primary save trigger: a background
  task that sweeps at boot then ~hourly and retains any conversation idle past its
  per-channel freshness window (voice ~30 min, telegram ~12 h; env-overridable via
  `FRESHNESS_VOICE_MINUTES` / `FRESHNESS_TELEGRAM_HOURS`), with crash-safe stale-claim
  recovery.
- Session-granularity save (`session_saver.py`): `save_session` (idempotent retain under
  an atomic registry claim), `transcript_to_items` (SDK transcript → Hindsight items with
  a deterministic `document_id`), `freshness_window`, and the `/new` `reset_channel`.
- Explicit `/new` reset on Telegram — retains the current conversation, then starts fresh
  ("Starting fresh — I still remember what matters.").
- Registry save-support fields + atomic helpers (`session_registry.py`): dominant
  `write_scope` and a `consolidated_at` save-claim guarding the reaper/next-turn race.
- `MEMORY_BACKEND=hindsight` is now a valid backend (the legacy read path runs cold/NoOp;
  long-term save is served by Hindsight via the SemanticMemory seam).

### Changed

- `agent.py` no longer persists memory per turn (the `add_turn` write is removed). It
  records the turn's dominant write-scope on the session registry, and the freshness
  reaper retains the whole conversation once it goes cold. The resume-vs-new decision now
  honours the per-channel freshness window and saves a cold prior session before opening a
  new one (next-turn-after-gap).
- The session sweeper now hard-deletes an evicted session's on-disk transcript via the
  SDK's `delete_session(sid, directory)` (replacing the dead `_prune_sdk_session`, which
  would have armed on the 0.2.87 SDK), guarded so a conversation inside its freshness
  window is never evicted.

## [0.39.0] - 2026-06-03 — Memory re-arch (1/3): SemanticMemory seam (inert)

First step of the memory re-architecture (design spec §5). Introduces the long-term
**SemanticMemory** seam and a self-hosted Hindsight HTTP client as building blocks.
**No runtime behaviour change** — the seam is constructed and fully unit-tested but is
*not yet wired* into the agent read/write path (that lands in the save/load steps), and
long-term Hindsight memory is **not yet user-selectable** (`hindsight_api_url` is reserved).

### Added

- `SemanticMemory` ABC (`retain` / `recall` / `profile` / `cross_recall`) with a
  `NoOpSemanticMemory` degraded implementation and pure `render_recall` /
  `render_mental_models` renderers (`semantic_memory.py`).
- `HindsightSemanticMemory` — an `aiohttp` client for the self-hosted Hindsight bank API
  (`/v1/default/banks/{bank}/...`), with fail-fast `bank_id` validation
  (`hindsight_memory.py`, `hindsight_ids.py`).
- `resolve_semantic_memory_choice()` + `build_semantic_memory()` in `casa_core.py`,
  added alongside the existing memory-backend resolution (`main()` and the legacy
  `MemoryProvider` path are unchanged).
- New add-on option `hindsight_api_url` → `HINDSIGHT_API_URL` env (configurable Hindsight
  base URL, reached via the add-on's hassio network alias/IP — never the bare host
  `hindsight`). **Reserved: not yet active.**

## [0.38.0] - 2026-06-02 — Hygiene: pin claude CLI + bump claude-agent-sdk 0.1.72 → 0.2.87

Decoupled version-hygiene PR (memory re-architecture spec §6). No memory-layer or
behavioural changes to any happy path.

### Changed

- Bumped `claude-agent-sdk` `0.1.72` → `0.2.87` (`requirements.txt`), gaining the
  v0.2.82 stderr-callback exception-isolation fix that `sdk_logging.with_stderr_callback`
  already assumes. The pip pin also pins the SDK-bundled CLI used by the residents +
  `in_casa` driver.
- Pinned the global `@anthropic-ai/claude-code` npm CLI to `2.1.150` (`Dockerfile`) —
  the version `claude-agent-sdk==0.2.87` bundles — so the two CLI consumers (SDK-bundled
  vs the global `claude` used by plugin management + the `claude_code` driver) no longer
  drift.

### Added

- `tests/test_cli_sdk_pin_assert.py` — static guard that the CLI install stays pinned and
  the SDK pin stays exact, plus a docker assertion that the pinned `claude --version`
  lands in the built image.

## [0.37.13] - 2026-05-29 — Hotfix: idle-reminder reset (C) + turn_done log de-collision (G)

Two small fixes surfaced by the current-state-spec accuracy pass (Open questions
C and G; discrepancy log D7 and D15). No behavioural change to any happy path.

### Fixed

- **Idle-reminder debounce now resets on a user turn (D7 / Open-Q C).**
  `EngagementRegistry.update_user_turn()` now sets `last_idle_reminder_ts = 0.0`
  alongside `last_user_turn_ts`. Previously the debounce was only cleared
  post-fire, so a re-engaged **specialist** (3-day reminder threshold < 7-day
  refire window) got its second idle reminder on the "7 days since last
  reminder" clock instead of the "3 days since last activity" threshold —
  delaying it a few days. The reminder now tracks activity as intended. The
  common case (a user reply dropping `idle_s` below threshold) was already
  fine; this only affects the specialist re-engagement edge.

- **`turn_done` log-line name collision resolved (D15 / Open-Q G).** Two log
  lines fired per assistant turn sharing the `turn_done` prefix but carrying
  disjoint fields: the SDK logger's `turn_done turns=/cost_usd=/ms=` and the
  per-turn token summary's `turn_done role=/cache_read=/cache_write=`. The
  token-summary line (`tokens.format_turn_summary`, emitted on the `agent`
  logger) is renamed **`turn_tokens`**, so log aggregation no longer conflates
  the two. No fields changed; only the prefix. The SDK `turn_done` line is
  unchanged.


Closes the carried-over `policies/* schema validation gap` (filed v0.31.1,
2026-05-01) and clears three stale backlog entries that already shipped in
v0.37.5 but were never struck from `docs/ROADMAP-backlog.md`.

### Fixed

- **LOW policies/* schema validation gap.** `validate_config_repo` is
  now path-aware: in addition to walking `agents/`, it walks `policies/`
  and validates `disclosure.yaml` against `policy-disclosure.v1.json`
  (NOT the agent `disclosure.v1.json` — same basename, different schema)
  and `scopes.yaml` against `policy-scopes.v2.json`. The configurator
  can edit both files per its doctrine; without this gate, schema-
  invalid YAML committed there FATALs the addon on next boot in
  `policies.py::load_policies` or `scope_registry.py`. Same blast
  radius as the original E-G repro (v0.30.0 P4.2 `TRAIT:` incident)
  but for a different file class. New `_SCHEMA_BY_POLICY_FILE` map
  stores `(schema_name, version)` tuples; `_load_schema(name, version)`
  takes an optional version suffix (default `v1`) so `policy-scopes`'s
  `.v2.json` loads correctly. The original v0.31.0 walk had a flat
  basename map that mis-applied the agent schema to `policies/
  disclosure.yaml` and falsely refused every commit; v0.31.1 scoped to
  agents/ only as a stopgap, which left this gap open until v0.37.12.

### Housekeeping

- **Backlog cleanup.** Three entries already shipped in v0.37.5 (PR #57,
  master `36d772c4`) but stayed in `ROADMAP-backlog.md`: F-1 MEDIUM
  (plugin-developer prompt for honest `is_error=true` failure
  narration), A-3-bis LOW (assistant prompt anti-pattern forbidding
  `#[role]` constructions), and the D-1-followup attribution (already
  documented in the v0.37.1 and v0.37.5 archive entries). Struck.
- **F-3 triage carry-forward.** N150 confirmed on HA core `2026.5.1`
  (latest, no GetDateTime failure path in source). No active
  reproduction in recent logs — the tool fires only when an agent
  selects it. Backlog entry updated with current state; re-test via
  a dedicated probe session in a future exploration.

### Tests

- `tests/test_agent_loader.py::TestValidateConfigRepo` gains three new
  cases (`test_invalid_policies_disclosure_caught`,
  `test_valid_policies_scopes_passes`,
  `test_invalid_policies_scopes_caught`) and renames the existing
  `test_skips_policies_dir` → `test_valid_policies_disclosure_passes`
  to reflect the new walk semantics. `test_skips_non_schema_files`
  adjusted: its previous `policies/scopes.yaml` fixture targeted the
  old "policies are skipped entirely" contract and would now fail
  validation, replaced with `policies/README.md` to keep the
  "non-schema files don't trip the gate" assertion intact. Local
  pytest non-slow non-docker 1533 PASS, 68 SKIP, 22 deselected.

## [0.37.11] - 2026-05-14 — Hotfix: DE-1 e2e harness shape mismatch

Surgical hotfix for the master-CI tier2-functional / Delegation E-block
failure that has been red since v0.37.9 (PR #61, run 25871932172) and
remained red after v0.37.10 (PR #62, run 25880387018). No product change.

### Fixed

- **DE-1 e2e harness: tuple-destructure `load_all_specialists` return
  value.** v0.37.9's O-2b fix promoted `load_all_specialists`'s return
  shape from `dict[str, AgentConfig]` to `tuple[dict, list]` (per-
  specialist isolation, mirroring v0.37.1 B-1b's `load_all_executors`
  pattern). Product callers (`casa_core.py`, `agent_registry.py`) were
  updated; the DE-1 e2e harness was missed. Result: every master CI run
  since v0.37.9 failed with `ValueError: dictionary update sequence
  element #0 has length 1; 2 is required` at `merged.update(
  specialist_configs)`. Filed 2026-05-14 in
  `docs/bug-review-2026-05-14-exploration7.md`. One-line fix at
  `test-local/e2e/test_delegation_E.sh:93`:
  `specialist_configs, _failed = load_all_specialists(...)`.

### Notes

- This is a test-harness fix only. Product code on master has been
  correct since v0.37.9; production (N150) ran healthy on 0.37.10
  through exploration7's operator-attended verifies (P31 + P32 + O-1
  all GREEN end-to-end). Not reverted per `feedback_ship_gate_doctrine`
  "revert if red" because reverting v0.37.9 + v0.37.10 would lose 7
  real bug fixes for a 1-line test-script issue.

## [0.37.10] - 2026-05-14 — Hotfix bundle: P31 + P32

Closes the 2 regressions filed in `docs/bug-review-2026-05-14-exploration6.md`
(one MEDIUM, one LOW) when 3 of 5 v0.37.9 fixes verified clean but
O-5 + O-6 were re-opened as P31 + P32.

### Fixed

- **MEDIUM P31: claude_code session_id is now captured reliably.** The
  v0.37.9 O-5 fix tailed `/var/log/casa-engagement-<id>/current` but
  that log file is never created in production — the s6-rc service
  dir's `log/` subdir lacks the `producer-for` / `consumer-for`
  wiring required to compile the producer-consumer pipe, so claude
  CLI's stdout goes to a pipe with no reader. Latent infrastructure
  gap since v0.13.0 Plan 4a. Live evidence: 2026-05-14 exploration6
  engagements `28fdeb04` + `3e44c2cf` — `.session_id` never written,
  `/var/log/casa-engagement-<id>/` never exists, post-restart claude
  CLI runs as a fresh SDK session. Fix:
  `ClaudeCodeDriver._capture_session_id` now watches the claude CLI's
  own session storage at
  `<ws>/.home/.claude/projects/-data-engagements-<id>/<uuid>.jsonl`
  — that file IS reliably written, and the filename (minus `.jsonl`)
  IS the session UUID. Persists atomically to `<ws>/.session_id`. The
  deeper s6-rc producer-consumer wiring fix that would also unlock
  Phase 4b G5 log relay + remote-control URL notice is backlogged
  for a v0.38.x design pass.
- **LOW P32: engage_executor now refuses duplicate-task spawns at the
  tool layer.** The v0.37.9 O-6 fix added a prompt section forbidding
  context bleed but the SDK conversation context's natural inertia to
  re-emit the prior turn's task overpowered it. Live evidence:
  2026-05-14 exploration6 O-6.2 turn — Ellen fired TWO `engage_executor`
  calls in one assistant message, the first a wrong configurator with
  `context="Probe O-6.1"` running the prior turn's rename task.
  Fix: a new `_jaccard_task_similarity` helper at the `engage_executor`
  MCP call site refuses spawns whose `task=` overlaps with the
  most-recent engagement for this `(channel, chat_id)` within 60s at
  word-level Jaccard ≥ 0.5. The refused envelope carries
  `kind: duplicate_task` with the offending engagement's id so the
  caller knows what to do. The v0.37.9 prompt section is retained as
  documentation but its strong-claim anchors ("ONLY", "Do not carry",
  "fire two separate engage_executor calls") are softened — the
  tool-level guard is the real enforcement, prompt is advisory.
  New `EngagementRegistry.recent_for_origin` query method.

### Tests

- 9 new tests (+2 driver session-id, +5 registry `recent_for_origin`,
  +3 engage_executor duplicate-task guard). v0.37.9 session-id capture
  tests rewritten for the new projects-dir watch approach.

### Notes

- The 3 v0.37.9 fixes that verified clean in exploration6 (O-1, O-2a,
  O-2b) are unchanged. O-3 (cross-channel memory) remains deferred
  to v0.38.0.
- Latent infrastructure gap: Phase 4b G5 "claude_code log relay"
  (`_relay_log_lines`) and "Remote control URL" topic notice
  (`_capture_url`) are also non-functional in production due to the
  same s6-rc producer-consumer wiring gap. Both backlogged with P31
  Option A for a v0.38.x design pass.

## [0.37.9] - 2026-05-14 — Hotfix bundle: O-1 + O-2a + O-2b + O-4 + O-5 + O-6

Closes 5 findings from `docs/bug-review-2026-05-14-p21-p30.md` (one
MEDIUM, four LOW). O-3 (cross-channel memory) is deferred to a v0.38.0
brainstorm — it's an architectural design choice, not a hotfix.

### Fixed

- **MEDIUM O-5: claude_code engagements now survive Casa restarts.**
  Boot-replay used to restore the s6 service but lose conversation
  context — the run script's `--resume $(cat .session_id)` plumbing
  shipped without a writer, so every restart spawned the CLI fresh.
  Live evidence: 2026-05-14 P25 cid `7a9cba59` — engagement `44389d8a`
  zombied for 7 minutes after a mid-engagement Casa restart.
  Fix: `ClaudeCodeDriver._capture_session_id` tails the per-engagement
  s6-log for `system_init session_id=<uuid>`, persists it atomically
  to `<workspace>/.session_id` (temp+os.replace), and invokes the
  `persist_session_id` callback so `EngagementRecord.sdk_session_id`
  stays in lockstep with the on-disk file. `casa_core` now wires
  `engagement_registry.persist_session_id` into the driver constructor.
- **LOW O-1: install/uninstall plugin failures now surface as MCP errors.**
  `_result()` auto-detected `is_error` only when `payload["status"] ==
  "error"`, so the `{"ok": False, "error": ...}` envelope used by
  `install_casa_plugin` and `uninstall_casa_plugin` landed as `ok=True`
  in `sdk_logging.log_tool_result` telemetry — contradicting F-7
  v0.32.0 intent. Extended the auto-detect to also recognise
  `payload.get("ok") is False`. Live evidence: 2026-05-14 P29.1 cid
  `52240634` saw `tool_result name=install_casa_plugin ok=True ms=12594`
  for a plugin-not-in-marketplace failure.
- **LOW O-2a: `--scope=executors` now refreshes residents' cached
  prompts.** `reload_executors` previously only called
  `executor_registry.load()`, leaving residents with a stale
  `<executors>` system-prompt block (rendered from
  `self.config.executors` at construct_agent time). Fan-out to
  `reload_agent` for each resident regenerates that state. Live
  evidence: 2026-05-14 P22 row5b — Ellen said "No" to "is pd enabled?"
  between an executor-scope reload and the next agent-scope reload.
- **LOW O-2b: specialist load failures now surface in casactl output.**
  `agent_loader.load_all_specialists` returns `(found, failed)` with
  per-specialist isolation (mirroring `load_all_executors` v0.37.1
  B-1b), `SpecialistRegistry.load_failures()` exposes them, and
  `reload_agents` appends `failed:<role>:<msg>` entries to the action
  trail. Pre-fix, a malformed new specialist returned `ok=True` with no
  trace — operator had to grep addon logs. Live evidence: 2026-05-14
  P22 row 4 first attempt — probe22 missing `response_shape.yaml` +
  `voice.yaml`, reload returned `ok=True`.
- **LOW O-4: playbook P21 step 2 reworded.** Engagement subprocesses
  have workspace-scoped HOME by design (per `drivers/workspace.py`
  `render_run_script` template). The H-1 verify path is `casa-main` +
  `svc-casa-mcp` `/proc/<pid>/environ`, NOT the engagement subprocess.
  Updated `docs/exploration-playbook/blocks/G-lifecycle.md`.
- **LOW O-6: Ellen now scopes engage_executor `task=` to the new task.**
  Prompt section added to `defaults/agents/assistant/prompts/system.md`
  forbidding context bleed from prior conversation turns into the
  `task=` arg. Live evidence: 2026-05-14 P27.2 cid `093a02c7` — Ellen
  spawned BOTH configurator AND pd in one turn, the configurator
  engagement received P27.1's rename task description instead of
  P27.2's repo creation task.

### Tests

+9 vs v0.37.8 baseline (1514 → 1523 PASS):
- `test_install_casa_plugin.py::test_install_plugin_failure_envelope_is_error`
- `test_reload.py::TestExecutorsScope::test_executors_scope_fans_out_to_residents`
- `test_reload.py::TestReloadAgents::test_surfaces_specialist_load_failures`
- `test_agent_loader.py::TestLoadAllSpecialists::test_per_specialist_isolation`
- `test_claude_code_driver.py::TestSessionIdCapture` (3 tests)
- `test_workspace.py::test_render_run_script_consumes_persisted_session_id`
- `test_assistant_prompts.py::test_system_prompt_forbids_engage_executor_context_bleed`

Existing `test_specialist_registry.py::test_rejects_non_empty_channels`
and `test_agent_loader.py::TestLoadAllSpecialists::test_finds_specialist`
updated for the new per-specialist isolation contract.

## [0.37.8] - 2026-05-14 — Hotfix: H-1 (HOME propagation) + N-1 (playbook 7-scope fix)

Closes the two findings from `docs/bug-review-2026-05-13-exploration4.md`:
**MEDIUM H-1** (configurator narrating a per-engagement `claude plugin
marketplace add` workaround during plugin install) and **LOW N-1**
(playbook P19 listed 6 reload scopes when `reload.py:709` registers 7).

### Fixed

- **MEDIUM H-1: `HOME=cc-home` now propagated to s6-supervised services.**
  setup-configs.sh writes `/addon_configs/casa-agent/cc-home` to
  `/run/s6/container_environment/HOME`, mirroring the existing
  GITHUB_TOKEN / CLAUDE_CODE_OAUTH_TOKEN / PATH propagation pattern.
  K-1 (v0.34.1) is the standing lesson: shell-level `export HOME=...`
  (setup-configs.sh:322) only governs the script's own claude calls;
  s6 services need `/run/s6/container_environment/`. Without this,
  casa-main + svc-casa-mcp booted with HOME=/root, so the 6
  `subprocess.run(["claude", "plugin", ...])` call sites in `tools.py`
  (install_casa_plugin, uninstall_casa_plugin, marketplace_add_plugin,
  marketplace_remove_plugin, marketplace_update_plugin — two of these
  call `claude plugin install/uninstall`, four call `claude plugin
  marketplace update`) read `/root/.claude/plugins/known_marketplaces.json`
  (empty) instead of `cc-home/.claude/plugins/known_marketplaces.json`.
  The configurator improvised a `Bash(claude plugin marketplace add ...)`
  workaround mid-engagement to make installs succeed — that workaround
  is no longer needed. 1 new test (`tests/test_setup_configs_claude_home.py`)
  mirrors the K-1 precedent.

- **LOW N-1: playbook documents `casactl --scope=executors` (7th scope).**
  `docs/exploration-testing-playbook.md::P19` and the "Granular reload
  via `casactl`" scope table both listed 6 scopes when `reload.py:709`
  has registered `executors` as a 7th since v0.37.1. `casactl --help`
  and `configurator/doctrine/reload.md` already listed all 7 — only
  the playbook was stale. Doc-only.

### Changed (cosmetic)

- `setup-configs.sh:267-273` and `agent.py:642` comments refreshed
  to reflect post-H-1 reality (HOME=cc-home instead of HOME=/root).
  Defensive `/root/.claude/projects` symlink + SDK-resume recovery
  logic unchanged.

---

## [0.37.7] - 2026-05-13 — Hotfix bundle: G-1 + G-2 + playbook doc-fixes

Closes two HIGH findings from `docs/bug-review-2026-05-13-exploration3.md`
plus the coupled seed flip for plugin-developer's default permission_mode,
and three doc-fixes to the exploration playbook.

### Fixed

- **HIGH G-1: `permission_mode: auto` now suppresses the C-1 relay hook.**
  `engagement_permission_relay` in `hooks.py` short-circuits with `{}`
  when the engagement's executor was created with
  `permission_mode in {auto, bypassPermissions}`. Plumbed via a new
  `EngagementRecord.permission_mode` field, snapshotted at engagement
  creation from `ExecutorDefinition.permission_mode` (mirrors the
  existing `tools_allowed` snapshot pattern). `acceptEdits` and
  `default` modes still fall through to the allow-list + Telegram relay
  pipeline. Autonomous claude_code engagements (P5/P12 in the
  exploration playbook) no longer block on the first ToolSearch
  permission prompt.
- **HIGH G-2: `casa_reload(scope=agent role=<new>)` now provisions
  agent-home.** Previously only `scope=agents` (plural — the diff-based
  adds/evicts path) called `agent_home.provision_agent_home`; the
  granular per-role scope used by the configurator's
  `recipes/specialist/create.md` flow skipped it, so the first
  `delegate_to_agent target=<new>` failed with
  `Working directory does not exist: /addon_configs/casa-agent/agent-home/<role>`.
  Moved provisioning into `reload._construct_agent` so it fires
  regardless of which reload scope triggered the construction
  (idempotent — no-op on existing dirs).
- **Coupled seed: plugin-developer ships `permission_mode: auto`.**
  `casa-agent/rootfs/opt/casa/defaults/agents/executors/plugin-developer/definition.yaml`
  flipped from `acceptEdits` to `auto` per operator directive
  (2026-05-13). Now operationally effective via G-1 — plugin-developer
  engagements run autonomously by default.

### Doc

- **Playbook P14 — two-line `turn_done` contract.** `sdk_logging` and
  `agent.py` emit two independent lines (sdk: cost/latency; agent:
  role/channel/tokens), not the single-line shape the spec implied.
- **Playbook P19 — `>=90s` timeout on post-reload turn probes.** The
  bare 60s urllib timeout is too tight for post-`scope=full` cold
  starts (17-19s on top of `policies:rebuild_scope_registry` +
  `agent:*:construct_agent`). Use the smoke skill OR `>=90s`. The
  `bus.register` idempotency regression is only confirmed when a `>=90s`
  retry also fails.
- **Playbook P20.2 — U3 title format.** Role-emoji is in the topic-icon
  bubble (`icon_custom_emoji_id`), not inline in the title text. State
  emoji prefixes the title.

## [0.37.6] - 2026-05-13 — Hotfix: CI tier1-smoke + tier2-functional boot timeout

Closes the pre-existing CI red that started intermittently after v0.36.1
and became 100% reliable from v0.37.1 onward. Every fresh container in
`test-local/Dockerfile.test` was downloading `intfloat/multilingual-e5-large`
(~2.24GB) on boot because `scope_registry.py` calls
`TextEmbedding(model_name=...)` without a `cache_dir`, and the image had
neither `FASTEMBED_CACHE_PATH` set nor the model pre-cached. The download
routinely exceeded the 30s `/healthz` wait_healthy ceiling between
`agent-home provisioned: role=butler` and `ScopeRegistry ready`.

### Fixed

- **CI: fastembed model pre-cached in test image.** `test-local/Dockerfile.test`
  now sets `ENV FASTEMBED_CACHE_PATH=/opt/casa/fastembed-cache` and adds a
  build-time `RUN python3 -c "TextEmbedding(model_name='intfloat/multilingual-e5-large')"`
  warm-up. The env var is honored by both build-time RUN and runtime
  container (fastembed's `define_cache_dir` reads it) so the model is
  baked into a Docker layer and reused, not re-downloaded per container.
  Image gains ~2.5GB but boot now reaches `/healthz` in seconds. No
  production image change (N150 has persistent caches across restarts).

## [0.37.5] - 2026-05-13 — Bug-bundle: E-1 + F-1 + A-3-bis

Bundles the four findings from `docs/bug-review-2026-05-13-exploration2.md`.
The load-bearing one is E-1: the v0.37.2/v0.37.3 C-1 PreToolUse permission
relay was contract-incomplete because the svc-layer forwarder cut off
operator response after 10s regardless of the policy's declared
`timeout: 600`. Yesterday's GREEN live-verify for C-1 (5 sequential Allow
taps on engagement `986f254e`) was operator-hand-speed-bound; synthetic
LAN probes reliably exceeded the 10s window and reproduced a fail-closed
deny with empty error reason.

### Fixed

- **HIGH E-1: `/hooks/resolve` forwarder timeout truncated permission relay.**
  `svc_casa_mcp._forward_to_internal` defaulted `timeout_s=10.0` and
  `_build_hooks_handler` did not override it. Result: any operator
  response taking >10s caused `engagement_permission_relay` to fail-closed
  deny with an empty `asyncio.TimeoutError()` reason; the actual verdict
  arrived later with no waiter and was silently dropped, leaving the
  engagement in a Schrödinger state (agent narrates success, topic shows
  🟢, tool actually failed). Fix: `_build_hooks_handler` now passes
  `timeout_s=None` so casa-main's policy-driven timeout (declared per
  hook in `hooks.yaml`, e.g. 600s for `engagement_permission_relay`) is
  the only effective gate. tools/call path keeps the 10s default (no
  human-in-the-loop). Five new tests in `test_svc_casa_mcp.py` cover
  the contract.

- **MEDIUM F-1: Agent misnarrated hook errors as success.**
  Two changes:
  (1) `svc_casa_mcp._build_hooks_handler` deny reasons rewritten as
  actionable text. Old `"hook forward error: "` (empty `str(exc)` on
  TimeoutError) replaced with `"Permission relay failed: forwarder error
  talking to casa-main (<ExcType>: <detail>). The tool was not run."` and
  the socket-unreachable variant gets a parallel message ending in
  `"The tool was not run. Retry shortly or check addon logs."`.
  (2) New "Tool results: honest failure narration" section in
  `defaults/agents/executors/plugin-developer/prompt.md` instructing the
  executor to treat `is_error=true` as failure verbatim, even when the
  error text mentions "hook" or "permission relay" — those words are not
  a signal the call succeeded.

- **LOW A-3-bis: Ellen still produced legacy `#[role]` topic references.**
  v0.37.1's prompt update added a description of the new U3 topic shape
  (bubble icon + state-prefixed title) but didn't explicitly forbid the
  old format. Reproduced twice in 2026-05-13 exploration2 ("Head to the
  Engagements supergroup, topic `#[plugin-developer] curl probe`").
  `defaults/agents/assistant/prompts/system.md` now carries an explicit
  anti-pattern paragraph forbidding `#[role]`, `#[role:topic]`, and
  `[role] topic-name` constructions.

### Documented

- **D-1 attribution (housekeeping).** Wire shape for engagement topic
  icons is spec-compliant as of v0.37.1 (commit `800a3516`): numeric
  `icon_custom_emoji_id` from `getForumTopicIconStickers`, state-prefixed
  title only. 2026-05-13 exploration2 confirmed the wire shape on the
  N150. Visual rendering pending operator-attended supergroup
  inspection. Memory `project_v037_1_bug_bundle_shipped` updated to
  move D-1 out of the "deferred to operator verify" list.

## [0.37.1] - 2026-05-13 — Bug-bundle: D-1 + B-1 + B-1b + A-1 + A-2 + A-3

Catches up the addon version from 0.36.1 → 0.37.1 (the v0.37.0
Phase 2 E-12 source landed on master 2026-05-12 without a release
artefact; this release bundles those Phase 2 changes plus the
six findings from `docs/bug-review-2026-05-13-exploration.md`).

### Fixed

- **HIGH D-1: Engagement topic icons silently broken since v0.37.0.**
  Telegram's Bot API requires a numeric `custom_emoji_id` from
  `getForumTopicIconStickers` for `icon_custom_emoji_id`; Casa was
  passing literal chars (`'tools'`, `'✅'`). Result: bubble fell
  back to default blue chrome AND the leading state emoji was
  silently stripped from the topic name. New `channels/topic_icons.py`
  module owns the locked role → custom_emoji_id map (📁 configurator,
  💻 plugin-developer, 💰 finance, 🤖 default), verified live
  against N150's curated set on 2026-05-13. `compose_topic_title`
  now emits `<state> <task>` (role lives in the bubble).
  `close_topic_with_check` renamed to `close_topic` and no longer
  flips the icon. Specialist engagement open path harmonised to
  U3 format (was legacy `#[<role>] <task> · id8`).

- **MEDIUM B-1: Executor schema rejects `permission_mode: auto`.**
  Casa's `executor.v1.json` enum was stuck on a 4-mode list
  (`acceptEdits`, `bypassPermissions`, `default`, `plan`) but CC
  CLI 2.1.119 supports 6 modes (adds `auto` and `dontAsk`).
  Enum expanded to match.

- **MEDIUM B-1b: One broken executor YAML wiped the entire registry.**
  `load_all_executors` now returns `(loaded, failed)`; per-executor
  parse errors are isolated (catches `LoadError`, `OSError`,
  `ValueError`, `TypeError`, `yaml.YAMLError`). `ExecutorRegistry.load`
  logs each failure at ERROR and continues. New log shape
  `Executors: loaded=[...] failed=[...] disabled=[...]`
  distinguishes "no executors configured" from "all executors
  broken".

- **MEDIUM A-1: No granular reload scope for ExecutorRegistry.**
  New 7th scope `executors` (`casa_reload(scope='executors')` /
  `casactl reload --scope=executors`) re-scans `executors/` and
  rebuilds the registry. Included in `reload_full` before the
  per-role agent loop. New `doctrine/recipes/executor/{enable,
  disable,edit-definition}.md` and a 7th row in the doctrine
  scopes table.

- **MEDIUM A-2: Residents echo stale memory of system state.**
  Ellen + butler `system.md` now include a "Stale system-state in
  memory" section: any time memory says a capability is missing
  and the user re-asks, always retry the tool call rather than
  relaying the memory'd "no" verbatim.

- **LOW A-3: Ellen's prompt referenced legacy `#[role] <task>`
  topic format.** Updated to U3 wording (icon in bubble, state in
  title prefix).

### Deferred

- **HIGH C-1** — CC CLI 2.1.119 does not emit
  `notifications/claude/channel/permission_request` for actual
  permission gates during real engagements; the U1 inline-keyboard
  relay is non-operational under real workloads. Spike + fix in
  a follow-up session.

## [0.36.1] - 2026-05-11 — Hotfix: H-2 (hook callbacks return {} not None)

### Fixed
- **LOW H-2: Casa hook callbacks return `None` from no-op paths, violating
  the SDK's `HookJSONOutput` typed contract.** The SDK's
  `_convert_hook_output_for_cli` (`claude_agent_sdk/_internal/query.py`)
  calls `hook_output.items()` unconditionally — returning `None` emits
  `'NoneType' object has no attribute 'items'` to stderr ~73× per
  ~30-min engagement window across `block_dangerous_commands`,
  `make_path_scope_hook_v2`, `make_casa_config_guard_hook`,
  `make_commit_size_guard_hook`, and `make_self_containment_guard`.
  Operationally harmless (the SDK error-responds back to the CLI which
  proceeds normally; deny payloads still route correctly per
  exploration5 P15) so this is purely log hygiene. Originally filed as
  upstream-blocked; 2026-05-10 triage during v0.36.0 confirmed the SDK
  is unchanged 0.1.72 → 0.1.80, fix is Casa-side. Changed every
  HookCallback no-op `return None` → `return {}`; tightened the
  `HookCallback` type alias and `_hook` return annotations from
  `dict[str, Any] | None` → `dict[str, Any]`. 10 new regression tests
  in `TestHookNoopReturnsEmptyDict` lock the contract per factory; the
  HTTP-proxy layer at `internal_handlers.py:_make_internal_hooks_resolve_handler`
  keeps its defensive `None → {}` translation for third-party callbacks.

## [0.35.2] - 2026-05-02 — Hotfix bundle: Q-1 + R-1 + S-1

### Fixed
- **MEDIUM Q-1: `casa_reload_triggers` returned a stale `registered`
  list.** Triggers DID register in apscheduler and DID fire on
  schedule, but `reload.py::reload_triggers` never wrote the fresh
  cfg back into `runtime.role_configs[role]`, so the back-compat
  consumer (`tools.casa_reload_triggers`) read the boot-time list.
  Configurator hallucinated failure narratives on every trigger-add
  (live evidence: P8.1 in exploration5, engagement `2cf6fb6f`
  finalized `outcome=error` despite probe-p8-sched firing twice).
  Fix mirrors the resident vs specialist branching of `reload_agent`
  at `reload.py:339-348`. Adds `TestReloadTriggers` regression
  coverage. Latent in v0.35.0.
- **LOW R-1: configurator specialist-create recipe wrote a
  non-existent default `cwd`.** Recipe template defaulted to
  `cwd: /addon_configs/casa-agent/workspace` (a directory that does
  not exist on disk); finance seed ships `cwd: ""`. Delegation to a
  newly-created specialist failed with `sdk_error (Working directory
  does not exist)`. Doctrine-only fix in
  `defaults/agents/executors/configurator/doctrine/recipes/specialist/create.md`.
  Live evidence: P11.2 in exploration5, cid `f032f185`. Latent since
  configurator shipped (v0.12.0).
- **LOW S-1: `agent_loader` rejected ANY unknown file in an agent
  directory.** Editor-backup artifacts (`.bak`/`.swp`/`.tmp`/`.orig`/`*~`)
  broke `casactl reload --scope=agent` with `LoadError: unknown
  file(s)`. Footgun for ad-hoc N150 SSH edits using `sed -i.bak`.
  Adds `_is_editor_backup()` helper that skips those suffixes,
  parallel to the existing dotfile skip. Diagnostic for genuine
  unknown files now mentions the whitelist + suggests `git restore`.
  Live evidence: P19.7v1 in exploration5. Latent since agent_loader's
  strict-mode shipped.

## [0.35.1] - 2026-05-02 — Hotfix: bus.register idempotent on queue

### Fixed
- **HIGH: post-`scope=agent` reload broke turn dispatch.** v0.35.0
  live verify on N150 hung every `/invoke/assistant` turn until 504
  after `casactl reload --scope=agent --role=...`.  Root cause:
  `MessageBus.register()` always replaced `self.queues[name]` with a
  fresh `asyncio.PriorityQueue`. The reload handler called
  `bus.register` to rebind the per-role handler — which orphaned the
  running `run_agent_loop` task on the old queue while every new
  `bus.send()` landed on the new queue. The existing dispatch loop
  already supports handler rebinding via `bus.handlers[name]`
  (intentional, per the in-source comment); the queue replacement was
  always wrong for that case.  Fix: `register()` is now idempotent on
  queue creation. Adds `TestRegisterIdempotent` regression coverage in
  `tests/test_bus.py`. Latent in v0.35.0 (2 hours).

## [0.35.0] - 2026-05-02 — Granular in-process reload

### Added
- **Granular in-process reload.** `casa_reload(scope=...)` replaces the
  no-arg Supervisor-restart shape. Six scopes: `agent`, `triggers`,
  `policies`, `plugin_env`, `agents`, `full`. Configurator engagements
  reload state in <1s instead of ~10–15s. See
  `defaults/agents/executors/configurator/doctrine/reload.md` and spec
  `docs/superpowers/specs/2026-05-02-granular-reload-design.md`.
- **`casa_restart_supervised` MCP tool** for the rare cases that need a
  full process restart (s6 service-tree edits, addon options mutations).
- **`casactl` operator CLI** at `/usr/local/bin/casactl` — same dispatch
  path as the MCP tool. `casactl reload --scope=... [--role=...]` /
  `casactl restart-supervised`.
- **`POST /admin/reload` route** on the internal aiohttp app
  (`/run/casa/internal.sock`).

### Changed
- **`casa_reload()` no-arg shape removed** (pre-1.0 license — no
  back-compat shim). Doctrine carries the rename.
- **`casa_reload_triggers(role=...)`** kept as a back-compat alias for
  `casa_reload(scope='triggers', role=...)`.
- **`CasaRuntime` dataclass** introduced as the canonical container for
  process-global Casa state; `init_tools(runtime=...)` is now the
  primary wiring point.

### Doctrine + playbook
- `executors/configurator/doctrine/reload.md` rewritten.
- All `recipes/**/*.md` updated for new tool shape.
- `docs/exploration-testing-playbook.md` adds `casactl reload` recipes.

## [0.34.3] - 2026-05-02 — Hotfix: O-1 + O-3 (P12 plugin lifecycle unblock)

Hotfix for two HIGH bugs surfaced by the 2026-05-02 P12 full plugin
lifecycle exploration session against v0.34.2 (`docs/bug-review-2026-05-02-p12-fulllifecycle.md`).
Both latent since v0.14.1 (Plan 4b ship, 2026-04-25 — ~7 days). Together
they unblock the P12 chain end-to-end: plugin-developer can now call
casa-framework MCP tools, and Casa-installed plugins now surface in
resident SDK subprocesses.

### Fixed

- **O-1 (HIGH, mcp_envelope serializes no-arg tools with empty
  `inputSchema: {}`).** `mcp_envelope.py::_tool_schema` had a
  `raw and …` short-circuit on the dict-of-types branch that fails
  for empty dicts (Python falsy semantics). No-arg tools — declared
  via `@tool(name, desc, {})` — fell through to the passthrough
  branch and emitted `inputSchema: {}` instead of
  `{"type":"object","properties":{}}`. CC v2.1.119 strict-validates
  and rejects the entire `tools/list` payload with `Invalid input:
  expected "object"`, so plugin-developer subprocesses could connect
  to svc-casa-mcp + see capabilities but could not call ANY
  casa-framework MCP tool — `mcp__casa-framework__emit_completion`
  etc. returned `No such tool available`. Affected tools today:
  `casa_reload` + `marketplace_list_plugins` (both no-arg). Latent
  since v0.14.1 because K-1 + L-1 verifications never reached
  `emit_completion` (Bash invocations only); P12 was the first
  end-to-end exercise. Fix: drop the `raw and ` short-circuit so
  `{}` falls through the dict-of-types branch and emits the correct
  shape.

- **O-3 (HIGH, plugins_binding filters out project-scope plugins).**
  `plugins_binding.build_sdk_plugins` shells out to `claude plugin
  list --json` from `HOME=cc-home` and filtered by
  `e.get("enabled")`. But Casa installs at `--scope project` to
  per-role `agent-home/<role>/.claude/settings.json`. The CLI
  evaluates the `enabled` field against the calling HOME's
  settings.json — for a cc-home call, that's cc-home's settings,
  which doesn't list any project-scope plugin from agent-home.
  Result: every Casa-installed plugin reported as `enabled: false`
  from cc-home → binding filtered it out → resident SDK subprocesses
  never saw Casa-installed plugins. Symptom: Ellen called
  `Skill(target=<plugin>:<skill>)` → `tool_result ok=False`. Latent
  since v0.14.1; P12.5 was the first end-to-end skill-use exercise.
  Fix: `build_sdk_plugins` accepts an optional `role` kwarg; when
  provided (residents at `agent.py:524`), project-scope entries
  whose `projectPath == /addon_configs/casa-agent/agent-home/<role>`
  are included regardless of the CLI's `enabled` field; user-scope
  entries still honour the `enabled` check. When `role` is None
  (specialists + executors at `tools.py:270` and `:321` — neither
  carries plugins per `install.md` doctrine), project-scope entries
  are filtered out entirely, preserving v0.34.2 behavior.

### Test plan

- 1 new unit test in `tests/test_mcp_envelope.py` for the empty
  `input_schema={}` case; pre-existing dict-of-types and passthrough
  cases unchanged and still passing (8 → 9 tests in this file).
- 3 new unit tests in `tests/test_binding_layer.py` covering the
  role-based project-scope filter: matching role includes the
  project plugin, mismatched role excludes it, no-role drops all
  project-scope entries (preserves v0.34.2 specialist + executor
  behavior). Pre-existing 4 cases continue to pass under the
  no-role branch (4 → 7 tests in this file).

### Live verification (planned)

- O-1: drive a fresh plugin-developer engagement end-to-end on N150
  post-deploy. Expect `mcp__casa-framework__emit_completion` to
  succeed (NOT `No such tool available`); engagement should finalize
  via emit_completion path, NOT via topic `/complete` slash command.
- O-3: configurator-install a probe plugin into Ellen, then DM Ellen
  to use the plugin's skill. Expect `Skill(target=<plugin>:<skill>)
  ok=True` and Ellen's reply to be the actual skill output (not a
  graceful-degradation "skill not found" narration).

### Carry-forward

- N-1 + N-2 (webhook trigger reload + name-agnostic handler) —
  reproduced this session, both already in `ROADMAP-backlog.md`.
  Address in v0.35.0.
- E-12 (claude_code driver doesn't stream incremental progress to
  engagement topic) — observed live this session as ~6min approval-
  gate silence. Already-deferred backlog.
- H-2 (claude-agent-sdk hook callback errors) — third-party.
  v0.1.72 still latest at session start; recheck `gh release list
  --repo anthropics/claude-agent-sdk-python --limit 3` at next ship
  gate.

## [0.34.2] - 2026-05-01 — Bug bundle: L-1 + L-1b + L-2 + L-3 + remove hello-driver

Closes 4 findings from
`docs/bug-review-2026-05-01-deferred-probes.md` plus 1 latent bug
surfaced during code review (L-1b).

### Fixed

- **L-1 (HIGH, claude_code engagement settings.json missing
  `permissions.allow`).** `drivers/workspace.py` now materializes
  `defn.tools_allowed` (filtered to valid CC permission patterns:
  `Bash(...)`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Skill`,
  `mcp__*`) + `defn.permission_mode` into engagement-scoped
  `.claude/settings.json::permissions`, for both legacy and template
  provisioning paths. `drivers/claude_code_driver.py` now passes
  `workspace_template_root` + `plugins_yaml` so plugin-developer
  flows through the template path (its
  `workspace-template/CLAUDE.md.tmpl` becomes the engagement system
  prompt). HOME dir creation lifted out of the if/else for parity.
  Latently broken since v0.13.0 — every Bash invocation in
  plugin-developer engagements got "This command requires approval"
  with no TTY to escalate.

- **L-1b (HIGH, hook_bridge translator silently dropped all hooks).**
  `drivers/hook_bridge.py::translate_hooks_to_settings` was reading
  PascalCase keys (`PreToolUse`, `PostToolUse`) but on-disk hooks.yaml
  + the canonical schema (`defaults/schema/hooks.v1.json`) use
  snake_case (`pre_tool_use`, `post_tool_use`). Result: every
  claude_code engagement got `{"hooks": {}}` since v0.13.0. Sibling
  of L-1: defense-in-depth (`block_dangerous_bash`, `path_scope`,
  `casa_config_guard`) was completely absent for any claude_code
  subprocess. Existing `test_hook_bridge.py` fixture used PascalCase
  input — masked the bug. Fixed translator to read snake_case (still
  emits PascalCase per CC settings.json shape); regression test loads
  bundled `plugin-developer/hooks.yaml` and asserts non-empty
  PreToolUse block.

- **L-2 (LOW, cosmetic).** `hooks.py::_normalize_path` was producing
  `"//addon_configs/..."` instead of `"/addon_configs/..."` in
  path_scope deny payloads. Self-consistent on both sides of the
  prefix comparison so the deny logic was unaffected; only display
  cleaner now.

- **L-3 (DOCTRINE).** Configurator's `completion.md` now has a
  `## Status semantics` section explaining that `emit_completion`
  `status="ok"` reflects engagement-task outcome (a hook-deny
  correctly fired during a security probe is `ok`), and clarifying
  that the valid enum is `"ok" | "partial" | "failed" |
  "cancelled"` — not `"error"`.

### Removed

- **`hello-driver` test-harness executor.** Bundled defaults
  (`defaults/agents/executors/hello-driver/`) deleted. Smoke harness
  `test-local/smoke/test_claude_code_driver.sh` deleted. Comments in
  `hook_bridge.py` and `setup-configs.sh` updated to drop
  hello-driver references. Doctrine `scaffold.md` example listings
  cleaned. With the L-1 call-site change, plugin-developer becomes
  the only `claude_code` driver caller — hello-driver was never
  user-facing and its only role was driver validation, now covered
  by the plugin-developer P12 chain. Closes M-1 (FIFO subprocess
  hang) by deletion.

### Live verification

- L-1: plugin-developer engagement built `casa-probe-2026-05-01-greet`
  end-to-end (gh + git push round-trip) — every Bash call returned
  ok=True. Engagement workspace `.claude/settings.json` carried
  `permissions.allow` populated from defn + `permissions.defaultMode
  = "acceptEdits"` + `hooks` block + `enabledPlugins`. Engagement
  workspace CLAUDE.md was the structured `.tmpl` content.
- L-1b: same engagement subprocess transcript shows PreToolUse hooks
  fired (block_dangerous_bash + path_scope) — defense-in-depth live
  on the only remaining claude_code executor.
- L-2: P15.2 path_scope deny payload now shows
  `'/addon_configs/...'` (single slash).

### Test plan

- 9 unit tests for L-1 helper + legacy/template path permissions.
- 1 regression test in `test_hook_bridge.py` loads bundled
  `plugin-developer/hooks.yaml` end-to-end.
- 1 regression test in `test_workspace_template_renders.py` loads
  bundled `plugin-developer/definition.yaml` + `hooks.yaml` +
  `plugins.yaml` + `workspace-template/` end-to-end.
- 4 unit/integration tests for L-2 single-slash output.

## [0.34.1] - 2026-05-01 — Hotfix: K-1 (claude_code_driver auth)

Hotfix for K-1 (HIGH) discovered in
`docs/bug-review-2026-05-01-exploration4.md`. Plugin-developer (and
hello-driver, and any future Tier-3 executor using `claude_code_driver`)
has been latently broken since v0.13.0 (Plan 4a, 2026-04-23 — ~8 days)
because the Claude Code OAuth token never propagated to the engagement
subprocess. Surfaced when exploration session 4 retracted a disguised
time-budget deferral on P12 (the canonical case for the playbook's new
"Time is never a deferral reason" doctrine).

### Fixed

- **K-1 (HIGH, claude_code_driver auth) — engagement subprocesses had
  no Claude API auth.** `setup-configs.sh` propagates `GITHUB_TOKEN`
  to `/run/s6/container_environment/` so every s6-supervised child
  (including engagement subprocesses launched via `with-contenv`)
  inherits it. There was no equivalent block for `CLAUDE_CODE_OAUTH_TOKEN`
  — the token was only exported into svc-casa's process env at
  `svc-casa/run:13`, which feeds casa_core (in-process Claude API
  calls work) but NOT child s6-rc services. Result: every
  claude_code_driver subprocess started in workspace
  `/data/engagements/<id>/.home/` with a fresh `.home/.claude.json`
  and no `.credentials.json`, and the CC CLI's first turn produced
  the literal text `"Not logged in · Please run /login"`. Engagement
  hung in `status=active` until manually cancelled.

  **Fix:** new `claude-oauth-token` block in `setup-configs.sh`
  (between the `github-token` block and the `seed-copy` block).
  Mirrors the GITHUB_TOKEN propagation: read `claude_oauth_token`
  via bashio, op:// resolution via the same `op` CLI path, write
  the resolved value to `/run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN`
  with mode 0600. If the option is unset/null, removes the target
  file (defensive — fresh installs go directly to "anonymous"
  state) and emits a WARNING that explicitly references K-1 so the
  next time this fails, the operator finds the bug review.

  **Tests:** new `tests/test_setup_configs_claude_oauth.py` (7
  cases): raw-token write, empty-value file-removal, "null"-string
  file-removal, op://-reference resolution via stubbed `op` CLI,
  op:// without OP_SERVICE_ACCOUNT_TOKEN fails-safe, run template
  doesn't UNSET the OAuth token, and a structural sanity-check
  asserting block ordering vs github-token / seed-copy markers.

### Process note

The exploration4 session originally marked P12 (full plugin
lifecycle e2e) DEFERRED with reasoning that boiled down to "ran out
of time." Operator instructed retraction; new playbook doctrine
"Time is never a deferral reason" was committed (inner docs commit
`f975967`) before re-running. Re-running P12 immediately surfaced
K-1 — the canonical example of why the rule matters. **A probe
skipped on time grounds is, sometimes, a HIGH bug not yet found.**

### J-1 (LOW, docs-only — also bundled)

`memory/feedback_phase_z_default_recipe.md` updated with an
empty-overlay verification step. The v0.34.0 ship's claimed Phase Z
at 13:30Z did not actually wipe the overlay (every config file had
pre-13:30Z mtimes), producing 4 doctrine drift WARNINGs at next
boot (filed as I-1, now CLOSED). Adding `ls -A` empty-verify
between rm and start prevents recurrence. No code change.

### Carry-forward

- **H-2** (LOW, third-party regression) — claude-agent-sdk
  v0.1.72 still latest; recheck `gh release list --repo
  anthropics/claude-agent-sdk-python` at next ship gate.

## [0.34.0] - 2026-05-01 — Bug bundle: H-1 + H-3

Bug bundle from `docs/bug-review-2026-05-01-exploration3.md`. Two NEW
HIGH findings filed during the third exploration session against
v0.33.1: H-1 (configurator engagement lifecycle race — every
hard-reload workflow leaves a stuck `status=active` engagement and no
user-DM completion message) + H-3 (soft reload of triggers on residents
permanently broken since 2026-04-22 commit `e81f264`, surfaced 9 days
later by v0.33.0's G-4 structured outcome=error logging). G-1/H-2
third-party regression carries forward — claude-agent-sdk-python
latest is still v0.1.72; no upstream CC CLI fix shipped.

### Fixed

- **H-1 (HIGH, configurator engagement lifecycle) — `casa_reload`
  Supervisor restart races SDK subprocess.** The doctrine ordering
  (`commit -> casa_reload -> emit_completion`) is correct for user
  experience, but the platform-side race was: `casa_reload` POSTed
  Supervisor's `addons/self/restart` synchronously, the POST returned
  in <1s, and Supervisor scheduled an async container kill that
  arrived ~13s later — cancelling the SDK subprocess BEFORE the model
  could call `emit_completion`. Engagement stuck `status=active
  completed_at=null sdk_session_id=null`, no `_finalize_engagement`,
  no user-DM completion message. Reproduced 100% across 3 hard-reload
  configurator engagements in exploration3 (P4.2 + P4.3 + P11.1).
  **Fix:** when `casa_reload` is called inside an active engagement
  (`engagement_var.get(None) is not None`), it now adds the
  engagement id to a new module-level
  `_ENGAGEMENTS_DEFERRED_HARD_RELOAD: set[str]`, drains the v0.33.1
  G-2 PENDING_RELOAD obligation, and returns immediately with
  `{supervisor_status: 200, deferred: true}` — NO Supervisor POST.
  At the end of `_finalize_engagement` (after the bus message + Honcho
  meta-summary have landed), if the deferred marker is present AND
  `outcome=completed`, the platform performs the actual Supervisor
  POST. The marker is drained on every terminal path
  (completed/cancelled/error) to prevent stale state. Out-of-engagement
  calls (operator-driven `/invoke`) still POST inline as before.
  Doctrine `agents/executors/configurator/doctrine/{reload.md,completion.md}`
  + recipes `specialist/create.md` + `plugin/install.md` updated to
  describe the deferred-restart mechanism. `casa_reload` tool docstring
  updated from "Only call AFTER emit_completion has been sent" to
  "Call BEFORE emit_completion. The actual addon restart is deferred"
  — pre-fix the docstring directly contradicted the doctrine.
  Tests:
  - `tests/test_h1_deferred_hard_reload.py` (8 cases) covering
    in-engagement defer, out-of-engagement inline POST,
    PENDING_RELOAD drain on call, and the four
    `_finalize_engagement` outcome combinations.
  - `tests/test_casa_reload_tool.py::test_configurator_engagement_var_path_allowed`
    updated for new `deferred: True` shape.

- **H-3 (HIGH, soft-reload broken for residents) —
  `casa_reload_triggers` always failed for residents because
  `agent_loader.load_agent_from_dir` was called with `policies=None`.**
  Latent since commit `e81f264cae103722c75970f2186076eb351b1d98`
  (2026-04-22, "feat(3.5-p3): casa_reload_triggers MCP tool"). 9 days.
  Residents have `disclosure.yaml`; `agent_loader._compose_prompt`
  raises `LoadError` when `disclosure is not None AND policies is
  None`. The pre-fix unit tests only covered specialists (no
  disclosure.yaml), so the bug slipped through. Surfaced by v0.33.0's
  G-4 structured outcome=error logging in exploration3 P8.1 — prior
  sessions hid the same failure mode as a silent error. **Fix:**
  load `PolicyLibrary` fresh from disk on each call via
  `policies.load_policies(/addon_configs/casa-agent/policies/disclosure.yaml)`
  and thread it into `load_agent_from_dir`. Stateless — no
  `init_tools` plumbing required. Returns a structured `load_error`
  with a useful message if the policy file is missing.
  Tests: `tests/test_casa_reload_triggers_resident.py` (3 cases —
  resident-with-disclosure happy path, missing-policies-file load
  error, specialist regression check).

### Carry-forward (no Casa-side fix)

- **H-2 (LOW, third-party regression).** Continuation of G-1 from
  v0.33.x: CC CLI 2.1.126 still throws `'NoneType' object has no
  attribute 'items'` on hook callbacks during Edit/tool_use; ~24
  callbacks per Edit-using configurator engagement (UP from ~13 in
  exploration2). Bytecode line unchanged at 9212. Latest
  claude-agent-sdk-python release is still v0.1.72 (2026-05-01) —
  no post-G-1 release shipped. Functionally harmless; operator log
  noise + minor log-storage cost. Recheck `gh release list` at next
  ship gate.

### Verification recipe corrections (carry forward)

The v0.33.1 verify recipe missed H-1 because it didn't inspect
`engagements.json` post-restart. The new verify shapes:

- **H-1 verify:** drive a configurator engagement that touches
  `agents/assistant/character.yaml`. Assert: artifact lands +
  activates AND `engagements.json` shows `status=completed
  completed_at=<float>` AND user DM receives a "Done"-shape relay
  via Ellen. The pre-fix recipe (just check that the addon restarted
  + the artifact is on disk) is INSUFFICIENT.
- **H-3 verify:** drive a configurator engagement that adds a trigger
  to `agents/assistant/triggers.yaml`. Assert
  `casa_reload_triggers(assistant)` returns `status=ok` with
  `registered: [trigger_name]`. Check apscheduler has the new job
  within ~5s. SOFT-RELOAD-ONLY path — no hard reload.

## [0.33.1] - 2026-05-01 — Hotfix: G-2 defensive reload guard

v0.33.0's doctrine-only fix for G-2 failed to converge live. Active
verify on cid `a9313680` (2026-05-01 11:39:57Z): the configurator
read the inverted-order `completion.md` + `reload.md`, then still
skipped the `casa_reload` tool_use (idx=15 `config_git_commit` →
idx=16 `emit_completion`, no reload between or after) and emitted
the same false-positive narration ("Reload triggered to apply.")
without the actual call. Same `committed but inert` failure mode as
v0.32.x.

### Fixed

- **G-2 (MEDIUM, ringleader) — defensive reload guard.** Per kickoff
  option (b) once the doctrine fix didn't converge, add a platform-
  side post-condition check. New module-level
  `_ENGAGEMENTS_PENDING_RELOAD: set[str]` in `tools.py` is populated
  by `config_git_commit` when its return SHA is non-empty (real
  commit landed) and drained by `casa_reload` /
  `casa_reload_triggers` on success. `emit_completion` inspects the
  set on `outcome=completed` entry — if the engagement is still
  pending a reload, it logs a WARNING citing the engagement id and
  force-calls `casa_reload.handler({})` BEFORE
  `_finalize_engagement` so the bus message lands after the
  Supervisor restart is scheduled (matching the existing
  bus-persists-across-restart contract). The set is drained
  unconditionally on every `emit_completion` exit to prevent stale
  state on idempotent re-emit / outcome=error paths.
  Tests:
  `tests/test_emit_completion_defensive_reload.py` (4 cases):
  - committed-without-reload force-calls + WARNING.
  - reload-already-called skips force-call.
  - no-commit skips force-call.
  - outcome=error skips force-call (engagement bailed; reload
    decision is the operator's, not the platform's).

## [0.33.0] - 2026-05-01 — Bug bundle: G-1 + G-2 + G-3 + G-4

Bug bundle from `docs/bug-review-2026-05-01-exploration2.md`. Four
collateral findings filed during the second exploration session against
v0.32.1: two MEDIUM (G-2 configurator doctrine compliance — ringleader,
G-4 engagement-error reason logging) + two LOW (G-1 carrying-over CC
CLI hook noise that v0.32.0 tried-but-failed to fix, G-3 sentinel-leak
on user-driven turns).

### Fixed

- **G-2 (MEDIUM, doctrine compliance) — configurator narrated
  `casa_reload(_triggers)(scope)` in completion-summary text but never
  tool-called it before `emit_completion`.** Reproduced 100% across
  P8 (trigger create) and P11 (specialist create) in exploration2.
  Trigger + specialist commits landed schema-valid in YAML but never
  activated in scheduler / agent registry — operator saw a "Done"
  message that was empirically false ("committed but inert"). Fix is
  doctrine-only: invert the canonical order in `completion.md`,
  `reload.md`, and every `recipes/*` recipe so the reload step lands
  BETWEEN `config_git_commit` and `emit_completion`. Pre-fix order was
  `commit → emit_completion → reload`; the model treated
  `emit_completion` as terminal and dropped step 3. Post-fix order is
  `commit → reload → emit_completion`, making `emit_completion` the
  natural terminal step AFTER the reload has run. New regression test
  `tests/test_configurator_doctrine_reload_order.py` parametrizes over
  every recipe and asserts the textual position of the first reload
  tool_use precedes the first `emit_completion` call.

- **G-4 (MEDIUM, configurator robustness) — engagement finalized
  outcome=error 24s after subprocess `system_init` with zero log
  evidence of why** (live: P8.2-followup cid `be9471b7` engagement
  `fa3c1486` 2026-05-01 10:30:55Z). Fix splits the finalize log line:
  outcome=error now emits at WARNING with structured `kind=` (from
  registry origin's `error_kind`, populated by `mark_error`) and
  `reason=` (text from emit_completion or registry message, with
  `no_reason_provided` sentinel as fallback). Companion fix in
  `drivers/in_casa_driver.py::_deliver_turn`: when the SDK loop
  completes without producing any `AssistantMessage` frames, emit
  `subprocess_terminated reason=no_assistant_message` at WARNING so
  hook-deny / model-refusal / subprocess-crash paths surface
  immediately instead of leaving operators chasing silent finalizes.
  Tests: `tests/test_finalize_engagement_error_reason.py` (3 cases)
  + `tests/test_in_casa_driver.py::test_start_empty_turn_logs_subprocess_terminated`.

- **G-1 (LOW, third-party regression) — F-5 was a false-positive.**
  v0.32.0's claude-agent-sdk 0.1.61 → 0.1.72 bump (CC CLI 2.1.112 →
  2.1.126) was supposed to close F-5's `'NoneType' object has no
  attribute 'items'` from hook_0/1/2/3 callbacks. Live verify in
  exploration2 (P4.2 cid `9946b835`) showed 13 hook callback errors
  during a single configurator engagement — same error class, bytecode
  line moved from 8382 to 9212 only. v0.32.0's verify was a passive
  10-min log scan with no Edit-using engagement active; the bug only
  fires while the hook bridge is processing tool_use payloads. Fix:
  no upstream patch available as of 2026-05-01 (claude-agent-sdk
  v0.1.72 is the latest tag); added a TODO comment to
  `casa-agent/requirements.txt` next to the pin. Active-verify recipe
  pinned for the next ship: drive a configurator engagement that uses
  Edit and assert `docker logs ... | grep -c "Error in hook callback
  hook_" == 0`.

- **G-3 (LOW, doctrine leak) — Ellen's outer turn echoed `<silent/>`
  literally to operator DM** after a configurator engagement
  (live: cid `dcc3c30b` 2026-05-01 10:27:02Z). The sentinel
  suppression in `agent.py::_deliver_response` was scoped to
  `MessageType.SCHEDULED` per
  `reference_scheduled_silence_contract` — Ellen had absorbed the
  heartbeat trigger's `<silent/>` doctrine via mid-engagement Read of
  triggers.yaml and emitted it on her user-DM follow-up turn where the
  gate did not fire. Fix lifts the SCHEDULED-only condition: any turn
  whose accumulated text strips to `<silent/>` (or to whitespace) is
  now suppressed regardless of trigger source. Tests:
  `test_silent_sentinel_suppresses_send_on_request_turn` +
  `test_whitespace_suppresses_send_on_request_turn` — verify both
  shapes also gate user-driven REQUEST turns.

### Not in this release

- F-1 (ha-prod-console plugin smoke skill HMAC noise) — not Casa.
- F-3 (HA MCP `GetDateTime ok=False`) — defer to next bug-review pass;
  check HA MCP server's current release first.
- policies/* schema validation gap (carried over from v0.31.1).
- Configurator UX gap on multi-file pre-existing schema offenders
  (carried over from v0.31.0).

## [0.32.1] - 2026-05-02 — Hotfix: F-7 envelope key snake_case

v0.32.0's F-7 fix used `isError` (camelCase) on the MCP envelope dict.
The Anthropic Agent SDK's MCP-server adapter reads
`result.get("is_error", False)` (snake_case) at
`claude_agent_sdk/__init__.py:512` and converts to the wire field
`isError` itself — so our `isError` key was silently dropped and
`engage_executor` for a disabled executor still showed `ok=True` in
the cid trace (live evidence: cid `6f56682c` post-v0.32.0 deploy,
`tool_result idx=3 name=mcp__casa-framework__engage_executor ok=True
ms=9125` for `target=plugin-developer`).

### Fixed

- **F-7 (LOW, contract) — envelope key swap.** `_result()` now sets
  `is_error: True` (snake_case) instead of `isError`. Test was
  updated to assert on the correct key. Behavior is now wire-correct:
  the SDK reads the dict, sets `ToolResultBlock.is_error=True`, and
  `sdk_logging.log_tool_result` emits `ok=False`.

## [0.32.0] - 2026-05-02 — Bug bundle: F-2 + F-4 + F-5 + F-6 + F-7

Bug bundle from `docs/bug-review-2026-05-02-exploration.md`. Five
collateral findings filed during the first exploration session against
v0.31.1 with NPM upstream finally bound. No HIGH-severity bugs in the
bundle — one MEDIUM doctrine drift + four LOW (telemetry, intermittent,
third-party, contract). F-1 (ha-prod-console plugin) and F-3 (HA MCP
GetDateTime) deferred — out of Casa scope.

### Fixed

- **F-6 (MEDIUM, doctrine drift) — `defaults/agents/assistant/
  executors.yaml` listed fictional `engagement` as a third
  executor_type.** Ellen counted three executors and named "engagement"
  as the third — but the executor registry has only two real types
  (configurator + plugin-developer; hello-driver is `enabled: false` by
  design). The third entry's `when:` text actually described
  `delegate_to_agent(mode='interactive')` (a Tier 2 specialist primitive),
  conceptually misclassified as a Tier 3 Executor. Fix: deleted the
  fictional entry from the seed YAML; folded its sync-vs-interactive
  delegation guidance into `defaults/agents/assistant/prompts/system.md`
  under a new "Sync vs interactive delegation" subsection. Added a
  regression test
  (`test_executors_yaml_lists_only_real_registered_executor_types`)
  that enumerates real executor directories and asserts the doctrine
  list is a subset.

- **F-2 (LOW, telemetry) — `CachedMemoryProvider._refresh` dropped
  `agent_role`.** The v0.30.0 / M3-self ship threaded `agent_role`
  through `agent.py::_one_scope` and v0.31.0 added a caller-side
  regression-locker, but the locker only asserts the kwarg-set is a
  *subset* of allowed — empty-kwargs callers passed trivially. The
  third caller in `memory.py:642` (post-turn cache refresh, fired from
  `add_turn` for every turn that hit the cache) emitted
  `memory_call ... agent_role="?"` lines on every voice prewarm and
  cached text-channel turn. Fix: plumbed `agent_role` from `add_turn`
  into `_refresh`, then into the inner backend's `get_context` call.
  Live evidence: voice-sse cid `d7378b64` from the 2026-05-02
  exploration.

- **F-7 (LOW, contract) — `engage_executor` returned `ok=True` for
  registry-rejected calls.** The MCP envelope returned by the tool
  carried no `isError` flag, so `sdk_logging.log_tool_result` emitted
  `ok=True ms=...` even when the executor type was disabled or unknown.
  Operator telemetry showed false-positive engagement spawns; user-
  facing narration was already correct. Fix: `_result()` helper now
  auto-detects `payload["status"] == "error"` and sets `isError: True`
  on the envelope. Behavior is consistent across every status:error
  return in tools.py — engage_executor was the surfaced symptom but
  the contract gap was system-wide. Live evidence: P5 cid `20a903c3`
  from the 2026-05-02 exploration (plugin-developer disabled).

- **F-4 (LOW, intermittent) — engagement finalize meta-summary write
  lost on Honcho TLS/SSL connection close.** The Honcho client reuses
  HTTPS connections; on long idles the upstream may close the TLS
  session, surfacing as `Connection error: TLS/SSL connection has been
  closed (EOF)` on the next request. Engagement still finalized
  `outcome=completed` (no user-visible impact) but the M4 meta-scope
  summary was lost. Fix: added a one-shot retry on transient
  connection-class errors at the meta-summary write site; non-
  transient errors (schema rejects, programming bugs) skip the retry.
  Live evidence: P4.2 cid `0fb4428d` engagement `9230dfd6` from the
  2026-05-02 exploration.

- **F-5 (LOW, third-party) — bundled CC CLI 2.1.112 hook callbacks
  threw `'NoneType' object has no attribute 'items'`.** Three hooks
  fired per Edit tool_use, each spewing ~6KB of minified JS source per
  error. Turn completed successfully but logs were noisy. Fix: bumped
  `claude-agent-sdk` from 0.1.61 → 0.1.72, which bundles CC CLI
  2.1.126 (past the buggy 2.1.112 version). No SDK API drift; full
  pytest passes (mod 2 known Windows installer flakes per memory
  `reference_npm_winerror_test`).

### Out of scope (filed, not fixed)

- **F-1 (not Casa) — ha-prod-console plugin smoke skill logs HMAC
  ERROR even when `webhook_auth_enabled: false`.** Fix belongs in the
  ha-prod-console plugin's smoke skill, not Casa.
- **F-3 — HA MCP `GetDateTime` returns `ok=False`.** Tool is shadowed
  by SDK `<current_time>` injection; user-visible impact is zero.
  Investigate against current HA MCP server release in a separate
  session; possibly upstream.

## [0.31.1] - 2026-05-01 — Hotfix: validate_config_repo scoping + hello-driver/hooks.yaml seed

Live N150 verify against v0.31.0's E-G gate exposed two follow-on
issues that had to be fixed before the gate works for actual
configurator engagements.

### Fixed

- **E-G follow-on (HIGH) — `validate_config_repo` walked the whole
  repo and applied `_SCHEMA_BY_FILENAME` by basename only, so
  `policies/disclosure.yaml` was validated against the per-agent
  `disclosure.v1.json` schema instead of its actual schema
  (`policy-disclosure.v1.json`). The two schemas have completely
  different shapes — agent disclosure has a single top-level `policy:`
  string, policy disclosure has a top-level `policies:` map of named
  bundles. Validation rejected
  `Additional properties are not allowed ('policies' was unexpected)`
  on the (untouched, valid) live policies file, falsely refusing
  every commit. Fix: scope the walk to `<config_dir>/agents/` only.
  Boot-time `policies.py::load_policies` and `scope_registry.py`
  catch policy-side schema violations on their own. The gate now
  fires on its intended target (configurator hallucinating fields
  under `agents/<role>/character.yaml`) without false positives on
  policy files. Verified live during the v0.31.0 ship's E-G probe
  (cid `4ee4013a`, configurator engagement `ef728344`): the gate
  correctly refused the `TRAIT:` top-level key edit; the configurator
  self-corrected to a valid edit inside the `card:` field; my v0.31.0
  bug then blocked the valid commit. v0.31.1 lets that flow through.

- **Latent hello-driver seed bug (LOW) — `defaults/agents/executors/
  hello-driver/hooks.yaml`** shipped with `PreToolUse: []` /
  `PostToolUse: []` (PascalCase, claude-code-driver style) but
  `hooks.v1.json` requires `schema_version: 1` + `pre_tool_use: []`
  (snake_case). The file would FATAL boot validation if hello-driver
  were enabled. Latent because hello-driver is `enabled: false` by
  default (per `project_3_5_plan4a_shipped`). Surfaced by v0.31.0's
  `validate_config_repo` walk; the bug had been silently shipped since
  the executor was first introduced. Fix: corrected to schema-conformant
  shape.

### Tests

- `tests/test_agent_loader.py::TestValidateConfigRepo` — 2 new tests:
  `test_no_agents_dir_returns_empty` (defensive: tool path must not
  crash on a fresh repo without the agents/ subtree); `test_skips_policies_dir`
  (regression-guard: realistic `policies/disclosure.yaml` with
  `policies:` block must NOT trip the agent gate). Existing
  `test_skips_dotgit_dir` updated to land its dotgit at
  `agents/.git/` since the gate no longer walks the repo root.

### Notes

- v0.31.0 / E-H + caller-side regression-locker shipped clean and
  was verified live: butler delegation cid `fec3a3a4` →
  `Delegation 1529c6e6 → butler ok (11.03s)`; zero
  `specialist memory read failed` WARNINGs in the 5min post-deploy
  probe window. M4b specialist memory restored.
- v0.31.0 / E-G ALSO shipped clean in its primary contract:
  configurator's first commit attempt with the `TRAIT:` invented
  key was correctly refused; configurator's full-context reasoning
  trace shows the schema error message reached the model and the
  model self-corrected to add the trait inside `card:` (the only
  free-text field in the character schema). Net result on N150:
  zero schema-invalid YAML landed in the inner addon_configs git.
  v0.31.1 just unblocks the valid commit.
- A pre-existing valid `card:` edit on `agents/assistant/
  character.yaml` plus the configurator's accidental rewrite of
  `agents/executors/hello-driver/hooks.yaml` (which corrected the
  PascalCase shape — itself the latent bug fixed in this ship) are
  both dirty in the live N150 working tree post-v0.31.1 deploy.
  Recovery: either let the next configurator engagement commit the
  card change (the now-valid hello-driver/hooks.yaml will land in
  the same commit), or manually `git -C /addon_configs/casa-agent
  checkout -- agents/assistant/character.yaml agents/executors/
  hello-driver/hooks.yaml` to discard.

## [0.31.0] - 2026-05-01 — Bug bundle: E-G + E-H + Phase Z playbook corrections

Closes the two HIGH bugs filed in
`docs/bug-review-2026-05-01-exploration.md` (the first exploration
session against v0.30.0) plus the playbook gaps that surfaced during
the same session's Phase Z. Single shipping sprint under pre-1.0.0
license; covers a stale `user_peer=` kwarg dark-spotting M4b
specialist memory since v0.26.0, and a configurator-driven write of
schema-invalid YAML that bricks the next boot until manual sed
recovery.

### Fixed

- **E-H (HIGH) — `delegate_to_agent` passes stale `user_peer` kwarg
  to `get_context`.** Pre-fix, `tools.py:454-460` (inside
  `delegate_to_agent`'s specialist-memory-read block) called
  `MemoryProvider.get_context(..., user_peer=user_peer)`, but
  v0.26.0 / E-14 dropped `user_peer` from the abstract signature.
  Every Ellen → specialist delegation since v0.26.0 (~3 days)
  raised `TypeError: HonchoMemoryProvider.get_context() got an
  unexpected keyword argument 'user_peer'`, was caught by
  `except Exception` at line 461, logged a WARNING, and silently
  degraded to empty memory context — every specialist (butler now;
  finance, eventually) ran with M4b dark. Same kwarg-drift shape as
  v0.29.0 / E-D and v0.30.0 / M3-self companion. Surfaced 2026-04-30
  23:27:13Z exploration session (cid `3407a7fb`, P2.1 butler
  delegation). Fix: drop `user_peer=user_peer` at `tools.py:459`.
  **Audit found a second offender** at
  `casa-agent/rootfs/opt/casa/channels/voice/channel.py:469` (voice
  prewarm); the original 2026-05-01 audit was scoped to tools.py +
  agent.py only and missed the voice channel. Fixed both in the same
  ship; voice prewarm has been silently failing since v0.26.0 too,
  caught by the same `except Exception` block at voice/channel.py:471.

- **E-G (HIGH) — Configurator writes schema-invalid YAML keys
  (CONFIRMED LIVE 2× on v0.29.0 + v0.30.0).** The configurator's
  `mcp__casa-framework__config_git_commit` accepted any
  structurally-valid YAML the agent produced and committed it to the
  inner addon_configs git, with NO schema validation. The model
  consistently invented YAML shapes — `TRAIT:` as a top-level key,
  `traits: [...]` collection, etc. — that are not in the schema's
  `additionalProperties: False` allowlist. Boot validation then
  FATALed on the next addon restart with `agent_loader.LoadError:
  schema violation at (root): Additional properties are not allowed
  ('TRAIT' was unexpected)`. Two prior incidents (v0.29.0 P4.2-V3 cid
  `1cef7687`; v0.30.0 P4.2 cid `cf9eb4cc`, engagement `15693b55`,
  commit `5cd731ac`) bricked the addon until manual `sed -i '/^TRAIT: /d' ...`
  recovery. Fix shape: pre-commit schema-validation gate. New
  `agent_loader.validate_config_repo(config_dir)` walks the repo for
  every schema-bearing YAML file (`character.yaml`, `voice.yaml`,
  `runtime.yaml`, `disclosure.yaml`, `delegates.yaml`,
  `executors.yaml`, `triggers.yaml`, `hooks.yaml`,
  `response_shape.yaml`, executor `definition.yaml`) and runs the
  same `_validate(...)` codepath boot uses, returning per-file error
  messages on failure. `tools.py::config_git_commit` calls this
  before `config_git.commit_config`; on any error, returns
  `{"status": "error", "kind": "schema_invalid", "errors": [...]}`
  WITHOUT committing — so the agent sees the schema error in its
  tool_result and can fix the YAML on the next iteration instead of
  bricking the addon on next boot. Defense-in-depth: same
  `_validate` codepath as boot ⇒ a passing pre-commit gate
  guarantees a green boot validation.

### Tests

- `tests/test_engage_executor_memory.py::test_get_context_callers_kwargs_match_signature`
  — caller-side regression-locker. AST-walks every `.py` under
  `casa-agent/rootfs/opt/casa/` and asserts every
  `.get_context(...)` call's kwargs ⊆
  `{session_id, tokens, search_query, agent_role}`. Verified to
  FAIL with E-H present and PASS after the fix. Complements the
  v0.29.0 signature-side `test_get_context_signature_locks_kwargs`,
  which only catches ABC-side drift.
- `tests/test_agent_loader.py::TestValidateConfigRepo` — 5 tests
  covering the new `validate_config_repo` API: clean repo returns
  empty list; the exact `TRAIT:` repro from the v0.30.0 P4.2 incident
  is caught with the expected error shape; non-schema files
  (markdown, plain text) are skipped; `.git/` is skipped; multiple
  offenders aggregate.
- `tests/test_config_git_commit_tool.py::TestConfigGitCommitSchemaGate` —
  3 tests covering the tool wiring: tool refuses with
  `kind: schema_invalid` when validation reports errors AND
  `config_git.commit_config` is NOT called; tool proceeds to commit
  when validation is clean; multiple errors aggregate in the
  response payload with `len(errors)` reflected in the message.

### Documentation

- `docs/exploration-testing-playbook.md::Phase Z` — three
  load-bearing corrections after the v0.30.0 ship's Phase Z burned
  four operator secrets (rotation deferred per pre-1.0.0 latitude)
  and FATALed on a leftover schema-invalid YAML: (a) options-backup
  step with `{"options": {...}}` envelope wrap at backup time;
  (b) **mandatory** `rm -rf /addon_configs/<slug>/*` step after
  install, before first start (E-11 persistent overlay does NOT
  survive uninstall — wait, it DOES survive, that's the whole
  problem; was never wiped); (c) options-restore via Supervisor REST
  API with envelope-shaped POST and `result`-only response extractor.
  Defense-in-depth note added: prefer HA UI Configuration panel for
  at-keyboard restores; reserve API path for unattended Phase Z.

### Notes

- v0.30.0's headline fixes (E-F engagement boot-race + M3-self
  peer_target) were live-verified clean on a fresh organic boot
  during the 2026-05-01 exploration session (engagement supergroup
  permissions registered from `_rebuild`'s tail; every Ellen DM
  produced `memory_call agent_role=assistant` across all 5 scopes).
  Coverage 14/18 PASS or PASS-with-note; 1 PARTIAL; 3 DEFERRED
  (configurator-write-risk pre-E-G fix, pre-existing NPM 502 from
  container-IP change post-reinstall).
- The v0.30.0 deploy hiccup mitigation
  (`character.yaml.bak-pre-v030` orphan) is now obsolete: the
  schema-validation gate prevents the class of bug that produced it.
  The orphan was wiped in the post-probe Phase Z; a fresh install
  carries no trace.

## [0.30.0] - 2026-04-30 — Bug bundle: E-F + M3-self peer_target

Closes the two HIGH bugs filed in
`docs/bug-review-2026-04-30-exploration3.md`. Single shipping sprint
under pre-1.0.0 license; covers a first-boot race that left every
engagement spawn refused as `engagement_not_configured` until manual
restart, and a Honcho 2.1.1 contract change that has been silently
dropping per-scope session digests on every Ellen DM since the M3
landing.

### Fixed

- **E-F (HIGH) — `setup_engagement_features` boot-race.** Pre-fix,
  `casa_core.py:1483` invoked `setup_engagement_features()` once at
  boot, immediately after `channel_manager.start_all()`. If the first
  `_rebuild` raised on `set_webhook` (transient first-boot DNS or
  network blip), `self._app` was never set, the boot call hit
  `None.get_me()`, and `engagement_permission_ok` stayed permanently
  False until manual restart. The supervisor's eventual successful
  rebuild populated `self._app`, but no path re-invoked
  `setup_engagement_features()`. Net effect: every `engage_executor`
  call returned `engagement_not_configured` — masking every
  configurator/plugin-developer/UC1/UC3 engagement spawn (P4/P5/P8/P11/
  P12/P15) on a fresh boot that hit any network blip. Fix in two parts:
  (1) `casa-agent/rootfs/opt/casa/channels/telegram.py:_rebuild` —
  `setup_engagement_features()` now runs as a tail step AFTER
  `self._app = app`, so every successful rebuild (initial OR
  supervisor-driven recovery) flips the permission flag automatically;
  (2) `casa-agent/rootfs/opt/casa/casa_core.py` — removed the redundant
  boot-time call. Belt-and-braces (3): `tools.py::engage_executor`
  failure path now attempts one in-line `setup_engagement_features()`
  retry when supergroup IS configured but the flag is still False —
  self-healing on the user's first engagement attempt without waiting
  for a probe-driven rebuild. Surfaced 2026-04-30 ~19:47Z exploration3
  (cid `45cd9e00`); workaround verified live as `ha apps restart` at
  21:36Z (cid `1cef7687`).

- **M3-self (HIGH) — `Session.context()` peer_target requirement.**
  Honcho 2.1.1's `Session.context()` validator rejects `search_query`
  without a paired `peer_target` —
  `ValueError: You must provide a peer_target when search_query is
  provided`. `memory.py::HonchoMemoryProvider.get_context` was issuing
  the SDK call without `peer_target`, so every per-scope session read
  on every Ellen DM raised; v0.29.0's E-B `exc_info=True` exposed the
  underlying exception (previously swallowed since the Honcho 2.1.1
  upgrade ~10 days). Functionally, Ellen fell through to `digest=""`
  on the M3-self path; peer_overlay carried continuity, but per-scope
  session digest was silently absent. Fix at
  `casa-agent/rootfs/opt/casa/memory.py:316-352` — thread `agent_role`
  through the abstract / Honcho / Cached / Sqlite / NoOp providers,
  and pass `peer_target=agent_role` to `session.context()` whenever
  `search_query` is set (spec § 2.3 — session memory is agent-targeted).
  Call sites updated: `agent.py::_one_scope` (the primary failing
  caller) and `tools.py::_fetch_executor_archive` (telemetry-only on
  the no-query path; consistency with the threaded contract).

### Tests

- `tests/test_memory_honcho.py` —
  `test_get_context_passes_peer_target_when_agent_role_and_search_query`
  asserts `peer_target=agent_role` is forwarded when both are
  supplied;
  `test_get_context_omits_peer_target_when_search_query_is_none`
  asserts the no-query path stays minimal (Honcho only requires the
  pairing on the search-query path).
- `tests/test_telegram_reconnect.py::TestSetupEngagementFeaturesInRebuild` —
  `test_first_set_webhook_fails_then_recovers_engagement_permission_flips_true`
  reproduces the E-F race by failing `set_webhook` once, then asserts
  `engagement_permission_ok=True` flips automatically after the
  supervisor's recovery rebuild — no external retry step.
  `test_setup_engagement_features_runs_after_app_is_published` is a
  spy-based ordering invariant: `setup_engagement_features()` MUST
  observe `self._app` already set when invoked.
- `tests/test_engage_executor_tool.py` — two new cases cover the
  defensive in-line retry: it fires once when supergroup is set but
  the flag is False, and it does NOT fire when supergroup is unset
  (the operator hasn't opted into engagements).
- Provider mocks across `tests/test_memory.py`, `tests/test_memory_cached.py`,
  `tests/test_agent_process.py`, `tests/test_engage_executor_memory.py`,
  `tests/test_notification_handling.py` updated to accept the new
  `agent_role` kwarg without changing call-shape assertions.

### Notes

- v0.29.0's E-E + E-D structural fixes were live-verified during
  exploration3 light-cleanup workaround (cid `1cef7687`, blessed-MCP
  path SHA `2b4ccab5`, no Bash fallback). E-F closure makes the
  workaround unnecessary — every fresh boot should land
  engagement-ready on the first successful `_rebuild`.
- Cosmetic-only `CLIConnectionError('ProcessTransport is not ready
  for writing')` from `claude_agent_sdk._internal.query.Query.
  _handle_control_request` after `emit_completion` finalizes is
  filed in `docs/bug-review-2026-04-30-exploration3.md` and deferred
  to a future ship — engagement outcomes are unaffected.

## [0.29.0] - 2026-04-30 — Bug bundle: E-E + E-D + E-B + E-C

Closes the four bugs filed in
`docs/bug-review-2026-04-30-exploration2.md`. Single shipping sprint
under pre-1.0.0 license; covers a CRITICAL ContextVar regression that
broke every in_casa configurator engagement since v0.20.0, a HIGH
silent kwarg-drift dropping the M4 L3 executor archive since v0.26.0,
a MEDIUM observability gap blocking M3-self root-cause investigation,
and a CRITICAL deployment-visibility gap masking every
`/opt/casa/defaults/` change shipped after first boot.

### Fixed

- **E-E (CRITICAL) — `engagement_var` ContextVar not propagating into
  SDK tool dispatch.** Pre-fix, `InCasaDriver.start()` bound
  `engagement_var` inside `_deliver_turn`, AFTER
  `ClaudeSDKClient.__aenter__()` had already called
  `claude_agent_sdk._internal.query.Query.start` — which spawns
  `_read_task` via `loop.create_task(self._read_messages())`. Per
  Python's asyncio semantics, `loop.create_task` captures the
  CURRENT context at task-creation time; the SDK's inner task
  therefore captured `engagement_var = None` and every tool callback
  it dispatched (including the privileged
  `config_git_commit` / `casa_reload` / `emit_completion`) saw
  `_effective_caller_role()` return the engager's `origin_var.role`
  ("assistant"), refusing all three. Net effect: every in_casa
  configurator engagement orphaned silently from v0.20.0 to v0.28.1
  (~5 days, missed because v0.20.0 / Phase 1's "manual configurator
  engagement test pending operator" deferred line was never
  discharged). Fix at
  `casa-agent/rootfs/opt/casa/drivers/in_casa_driver.py:74-115` —
  bind `engagement_var` BEFORE `client.__aenter__()` in `start()`,
  reset in `finally`. Same pattern applied to `resume()` at
  `:128-165`.

- **E-D (HIGH) — `_fetch_executor_archive` passes stale `agent_role`
  kwarg to `get_context()`.** v0.26.0 / E-14 dropped `agent_role` and
  `user_peer` from `MemoryProvider.get_context`'s signature; v0.27.0 /
  Bug 6 swept three call sites for the parallel `executor:<type>` →
  `executor-<type>` regex but missed the `agent_role=agent_role`
  kwarg drift here. Every executor engagement spawn (configurator,
  plugin-developer, hello-driver) raised `TypeError` against the real
  Honcho provider — silently swallowed by the function's `except
  Exception` and logged as a one-line WARNING without `exc_info=True`.
  M4 L3 cross-run executor memory was dark for every executor on
  every spawn since v0.26.0. Fix at
  `casa-agent/rootfs/opt/casa/tools.py:1179-1186` — drop
  `agent_role=agent_role`; also added `exc_info=True` to the warning
  for parity with E-B.

- **E-B (MEDIUM) — `Memory call failed` swallowed without
  `exc_info=True`.** Both warning sites in agent.py's per-turn memory
  read (`agent.py:374-379` for per-scope `_one_scope`; `agent.py:391-397`
  for `_overlay`) lost the underlying exception class + message,
  blocking root-cause investigation of M3-self failures that fired
  5× per Ellen Telegram turn from v0.x to v0.28.1. Fix is a single
  `exc_info=True` keyword on each `logger.warning(...)` call.

- **E-C (CRITICAL — visibility) — Persistent `/addon_configs/` never
  re-seeds.** `seed_agent_dir()` in `setup-configs.sh:28-34` is
  no-op when the destination dir already exists. After E-11's
  persistent ext4 bind mount (v0.19.0), every default-side change
  shipped via `/opt/casa/defaults/` after first boot was silently
  dark. Three confirmed dark-state examples spanning v0.26.1 →
  v0.27.0 → v0.28.0 (E-15 prompt-nudge missing, E-5 financial-
  arithmetic anchor missing, E-16 configurator plugin tools +
  recipes missing). Master CI runs against fresh volumes so the
  upgrade-over-existing-overlay path has zero coverage. Fix at
  `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh:78-145`
  — adds an `# === drift-check ===` block that walks
  `/opt/casa/defaults/{agents,policies}/` vs the live overlay,
  byte-compares each file via `diff -rq`, and logs WARNING per
  drifted/missing file plus a one-line summary. Visibility-only;
  operator decides when to run Phase Z (uninstall+reinstall). The
  block is POSIX-clean (parallel to the existing seed-copy block)
  so it can be unit-tested via `sh -c`.

### Tests

- **`tests/test_in_casa_driver.py::TestInCasaEngagementContext::test_engagement_var_propagates_into_sdk_inner_task`**
  — models the SDK's `Query._read_task` spawn pattern (a fake client
  whose `__aenter__` calls `loop.create_task` then snapshots
  `engagement_var.get(None)` inside that task). Pre-fix the snapshot
  is `[None]`; post-fix it is `[rec]`. Catches any future regression
  of E-E.

- **`tests/test_engage_executor_memory.py::test_returns_empty_when_archive_empty`**
  updated to assert `"agent_role" not in kwargs` and
  `"user_peer" not in kwargs` on the `get_context` call.
- **`tests/test_engage_executor_memory.py::test_get_context_signature_locks_kwargs`**
  — introspection-based regression test that asserts
  `MemoryProvider.get_context`'s parameter set is exactly
  `{self, session_id, tokens, search_query}`. Locks against future
  caller-vs-ABC drift at unit-test time rather than waiting for an
  exploration session to surface it.
- **`tests/test_executor_archive_is_read_on_second_engagement`**
  in-memory `_Mp` mock updated: dropped `agent_role` and
  `user_peer` from its `get_context` signature.

- **`tests/test_agent_process_scope.py::TestMemoryFailureLogsExcInfo`**
  — two tests covering both warning sites:
  `test_one_scope_failure_includes_exc_info` raises a TypeError from
  `ensure_session` and asserts `caplog`-captured record has
  `exc_info` populated with the original exception class + message;
  `test_overlay_failure_includes_exc_info` does the same for
  `peer_overlay_context`.

- **`tests/test_setup_configs_drift_check.py`** — five tests against
  the extracted drift-check block, mirroring the seed-copy test
  shape. Covers: clean trees → INFO summary; drifted file → WARN +
  `drifted=1`; missing file in live → WARN + `missing=1`;
  operator-added file in live → ignored (no false-positive drift);
  missing default dir → graceful early-return.

### Cross-refs

- `docs/bug-review-2026-04-30-exploration2.md::{E-B, E-C, E-D, E-E}`
  — full forensic + suggested-fix-shape that drove this ship.
- v0.20.0 / Phase 1 (commit `077714d`) — E-7's
  `engagement_var.set` in `_deliver_turn` covered Ellen's path but
  missed the SDK inner-task capture-at-`__aenter__` semantics.
  Memory `project_phase1_engagement_context_shipped`'s "manual
  configurator engagement test pending operator" note would have
  caught E-E — discharged here.
- v0.26.0 / E-14 (commit `b3dac55`) — `MemoryProvider` ABC reshape
  that dropped `agent_role` from `get_context`. Memory
  `project_phase5_e14_shipped`.
- v0.19.0 / E-11 (commit `54ae912`) — addon_config map flip to
  `all_addon_configs:rw` made `/addon_configs/casa-agent/`
  persistent. E-C is the unintended consequence of that
  persistence — every fix shipped after v0.19.0 to
  `/opt/casa/defaults/` is silently dark on the live N150 until
  operator wipe.

### Success signal

Next exploration session reruns
`docs/exploration-testing-playbook.md` from P4.2 onwards. Expected:
- **P4.2:** configurator engagement closes cleanly
  (`status=completed`, `config_git_commit` + `emit_completion`
  succeed); no Bash fallback.
- **P5/P8/P11/P12/P15:** all unblocked from E-E.
- **E-B:** turn-trace shows the actual exception class for every
  `Memory call failed` warning (driving the next corrective ship).
- **E-C:** boot logs show `drift_check missing-in-live` /
  `drift_check drifted` WARN lines for any defaults the operator
  hasn't wiped.

## [0.28.1] - 2026-04-30 — E-A: Telegram channel fully broken since v0.22.0

Surfaced live during the 2026-04-30 afternoon exploration session
(`docs/bug-review-2026-04-30-exploration.md`, E-A) on the very first
DM probe. Every inbound Telegram update — DM to Ellen, supergroup-
topic message, slash-command, originator check — has been dropped
since v0.22.0 (Phase 3a, commit `7f58143`, 2026-04-30 morning) with a
silent `WARNING channels.telegram: Telegram handler error (not
retryable): TelegramChannel.handle_update() takes 2 positional
arguments but 3 were given`. PTB returned 200 to the webhook caller,
so smoke probes (`/invoke`, `/api/converse`) and master CI never
noticed; the bug was load-bearing for ~half the exploration playbook
(P4/P5/P6/P11/P12/P15 are all engagement-driven).

### Fixed

- **`casa-agent/rootfs/opt/casa/channels/telegram.py:479`** — added
  the `_context: ContextTypes.DEFAULT_TYPE | None = None` parameter
  to `handle_update`. PTB v20+ `MessageHandler` invokes its callback
  with `(update, context)`; the missing parameter raised TypeError on
  every Telegram update for ~10 hours. Context is unused — the
  channel reads everything it needs from `update` and Casa's
  bus/engagement registry — but the parameter must exist for PTB's
  dispatch contract.

### Tests

- **`tests/test_telegram_engagement_routing.py::TestPTBDispatchContract`** —
  two regression tests:
  - `test_handle_update_accepts_ptb_two_arg_callback` — calls
    `ch.handle_update(update, ptb_context)` directly, asserts no
    TypeError.
  - `test_handle_update_dispatched_through_ptb_message_handler` —
    builds a real `MessageHandler(filters.TEXT, ch.handle_update)`
    and walks a synthetic update through `handler.callback(update,
    context)` exactly the way `Application.process_update` does.
    The single-line difference between this test and the
    `handle_update(u)` unit calls already in the file is what would
    have caught E-A pre-ship.

### Cross-refs

- `docs/bug-review-2026-04-30-exploration.md::E-A` — full
  forensic + suggested-fix-shape that drove this ship.
- `docs/bug-review-2026-04-30-exploration.md::E-B` — companion
  observability gap (`agent.py:374-378` swallows `Memory call failed`
  exception without `exc_info=True`); not fixed in this ship —
  filed for a follow-up session.

## [0.28.0] - 2026-04-30 — E-16: Configurator plugin-tools gap

Closes the Plan 4b consumer-side gap surfaced by the 2026-04-30 audit
and exposed in `docs/exploration-testing-playbook.md::P12 step 3`.
Until this ship, the Configurator could neither call the plugin
install/remove tools (not in `tools.allowed`) nor walk the operator
through the flow (no `recipes/plugin/` doctrine).

### Changed

- **`agents/executors/configurator/definition.yaml::tools.allowed`** —
  added 10 plugin-lifecycle tools:
  `marketplace_{add,remove,update,list}_plugin`, `install_casa_plugin`,
  `uninstall_casa_plugin`, `verify_plugin_state`,
  `set_plugin_env_reference`, `list_vault_items`, `get_item_fields`.
  Excluded `verify_plugin_secrets` — its description marks it a
  back-compat shim for `verify_plugin_state`.

### Added

- **`agents/executors/configurator/doctrine/recipes/plugin/`** — new
  recipe directory. Four files, mirroring `recipes/trigger/` style:
  - `install.md` — five-stage install flow (marketplace →
    system-requirements → per-agent install → secrets → verify),
    with reload + common-mistakes sections.
  - `remove.md` — uninstall flow + optional full-removal sequence
    (marketplace tear-down + secret unwiring).
  - `marketplace.md` — marketplace-only operations (list, register,
    update pin, unregister) with explicit reload-not-needed contract.
  - `secrets.md` — `set_plugin_env_reference` + 1Password discovery
    helpers (`list_vault_items`, `get_item_fields`).

### Cross-refs

- **`recipes/plugin/`** linked from `architecture.md` § "Configurator
  MCP tools (v0.14.1)" and `reload.md` (added two rows: install/remove
  and `set_plugin_env_reference` — both `hard` reload).

### Tests

No new test code. This ship is doctrine + allowed_tools surface only;
no Python or YAML behavior changed beyond the configurator's
self-described tool list. Plan 4b coverage for the underlying tools
remains in place (`tests/test_marketplace_ops.py`,
`tests/test_marketplace_tools.py`, `tests/test_install_casa_plugin.py`,
`tests/test_verify_plugin_state.py`, `tests/test_plugin_env_conf.py`).
`uninstall_casa_plugin`, `set_plugin_env_reference`, `list_vault_items`,
and `get_item_fields` have no direct unit coverage but have been live
since v0.14.1; the canonical end-to-end exercise is P12 in the
exploration playbook.

### Success signal

Next exploration session reruns P12 step 3 (Configurator installs the
`casa-probe-*` plugin into Ellen's `enabledPlugins`) end-to-end.
Steps 4-7 (verify load, in-agent skill use, configurator removes,
graceful degradation) become exercisable for the first time.

## [0.27.0] - 2026-04-30 — Phase 6: Polish (E-5 + E-9 + Bug 6)

Closes the bug-review-2026-04-29 backlog. Three independent
fixes; one ship.

### Fixed

- **E-5 — Ellen LLM arithmetic on finance failure.** New
  `## Financial arithmetic` subsection in
  `assistant/prompts/system.md` forbids Ellen from computing
  financial figures herself. On `delegate_to_agent('finance')`
  failure, Ellen now declines (*"I can't compute that without
  Alex — let's try again once finance is reachable"*) rather
  than producing an LLM-computed table or total. Architectural
  invariant: no answer the user sees was computed by an LLM.
- **E-9 — Telegram engagement topic title truncation.** New
  `text_util.truncate_for_topic` helper with UTF-8 byte-strict
  word-boundary truncation and Unicode ellipsis. Replaces four
  `[:80]` hard slices in `tools.py` (delegate_to_agent +
  engage_executor, open + rename). Topic names now fit the
  128-byte Telegram Bot API limit and break on word boundaries.
- **Bug 6 — Honcho `executor:<type>` regex rejection.** Swapped
  `executor:<type>` → `executor-<type>` at three call sites in
  `tools.py` (read in `_fetch_executor_archive`, write in
  `_finalize_engagement`'s archive branch). Honcho v3 regex
  `^[A-Za-z0-9_-]+$` rejects colon; M4 L3 cross-run memory
  injection was effectively dark for executor engagements
  (configurator, hello-driver, plugin-developer). No migration
  needed — existing colon-keyed peers were unreachable.

### Added

- `casa-agent/rootfs/opt/casa/text_util.py` (new module).

### Tests

- New `tests/test_text_util.py` (6 unit tests for the helper).
- Extended `tests/test_assistant_prompts.py` with the E-5 anchor
  regression guard.
- Extended `tests/test_delegate_to_agent_interactive.py` with a
  long-task topic-name budget assertion.
- Extended `tests/test_finalize_engagement.py` with the
  executor-archive hyphen assertion.
- Updated `tests/test_engage_executor_memory.py` colon → hyphen
  at four assertion sites (Bug 6).

## [0.26.1] - 2026-04-30 — Phase 5: Memory hygiene (E-15)

### Changed

- **`consult_other_agent_memory` falls through to `cross_peer_context`** for disabled-but-known specialists. Memory is data, enablement is operational. Closes E-15 — live N150 verification of M6 cross_peer recall was BLOCKED on Finance disabled (per `project_memory_m6_shipped` Live N150 status). Now Ellen can recall a disabled specialist's accumulated memory.
- **`assistant/prompts/system.md`** — hedge inverted. Ellen now prefers `consult_other_agent_memory` (cheap memory recall) and reserves `delegate_to_agent` for fresh-data cases. Tina added to the cross-role example list.

### Added

- **`SpecialistRegistry.is_disabled(role)` / `disabled_roles()`** public accessors. Surface for E-15's fall-through and any future code that needs to distinguish disabled-but-bundled from genuinely-unknown roles.
- **Configurator doctrine** (resident/{create,update}.md) teaches operators that disabled specialists' peer-level memory remains consultable. Out-of-scope follow-up: optional `cfg.allow_memory_when_disabled: bool` for hard-gate semantics (deferred per spec § 3.3).
- **Unknown-role error message** in `consult_other_agent_memory` now lists disabled roles in the available-roles list (so Ellen self-corrects instead of bouncing off `unknown_role` for legitimate disabled targets).

### Spec / Plan

- Spec: `docs/superpowers/specs/2026-04-30-phase5-memory-hygiene-design.md` (§3)
- Plan: `docs/superpowers/plans/2026-04-30-phase5-memory-hygiene.md` (§B)

### Verification

- Local pytest (`-m "not docker and not slow"`, `tests/` scope): pass count + 7 new tests, 0 failed.
- Smoke 3/3 PASS.
- **Operator-driven Telegram probes** (manual, after deploy):
  - Probe 1 — Tina recall: "what did Tina mention about lights last week?" → expect `memory_call call_type=cross_peer observer=butler`.
  - Probe 2 — disabled-Finance recall: "what did Finance say about my Q1 invoices?" → expect `consult_other_agent_memory_call result_len > 0` (or = 0 if Honcho has no Finance peer-level data; success criterion is `unknown_role` NOT returned).

## [0.26.0] - 2026-04-30 — Phase 5: Memory hygiene (E-14)

### Changed (BREAKING — pre-1.0.0 license)

- **`MemoryProvider` ABC reshape**: `get_context(session_id, tokens, search_query)` drops `agent_role` and `user_peer` parameters; new abstract method `peer_overlay_context(observer_role, user_peer, search_query, tokens)` separates peer-level overlay reads from scope-level session reads.
- **Ellen `memory.token_budget`**: 4000 → 5000. Honest envelope for the new 40/60 overlay/scope split (5 scopes × 600 + 2000 overlay = 5000).
- **`BudgetTracker` warning text**: "Memory digest over budget … Investigate the memory backend." → "Memory digest exceeded expected envelope … Memory shape may have regressed." Reframe from cost cap to regression sentinel.
- **`CachedMemoryProvider` cache key**: `(session_id, agent_role, tokens)` → `(session_id, tokens)`. `agent_role` is no longer threaded through this layer.

### Added

- **Honcho-native two-primitive split**: per-turn memory assembly runs ONE `peer.context(target=…, search_query=…)` call (deduped peer-level overlay) + N `session.context(tokens=scope_budget, search_query=…)` calls (per-scope messages + summary). Closes the 5× peer-overlay duplication that produced `used=6210 budget=4000` warnings every Ellen turn (E-14, `bug-review-2026-04-29-exploration.md:507-512`).
- **`peer_overlay_context` method** on `HonchoMemoryProvider` (real impl) + `NoOpMemory` / `SqliteMemoryProvider` (graceful "" return) / `CachedMemoryProvider` (passthrough). Same fail-soft contract as M6's `cross_peer_context`.
- **`memory_call` `call_type: "self_overlay"` telemetry** at the new emission site, parallel to existing `self` (per-scope) and `cross_peer` (M6). Synthetic `session_id: "overlay-{role}-{user}"` shape.
- **`peer_overlay_empty` INFO log line** on empty overlay digest (cold start / Honcho deriver behind), per spec § 7 Q4.
- **Render helper split**: `_render` → `_render_session` (messages + summary only) + new `_render_peer_overlay` (peer_card + representation, self-perspective headings).

### Fixed

- **E-14 — Memory token budget overflow** (MEDIUM, `bug-review-2026-04-29-exploration.md:500-527`). Live evidence: `WARNING tokens: Memory digest over budget for session telegram-1197017861-assistant: used=6210 budget=4000 (>1.1x for 3 turns).` Steady-state every Ellen turn. Now silent for first 3 turns post-deploy; envelope warning is reframed to fire only on memory-shape regressions.
- **Latent M6 `cross_peer_context` bug** discovered during Task A.0's Honcho contract probe: `honcho-ai==2.1.1`'s `Peer.context()` rejects `tokens=N` kwarg (raises `TypeError` at signature bind via `@validate_call`). M6's `cross_peer_context` has been silently broken since shipped — `try/except` swallowed the TypeError, never fired organically (Finance disabled in production, no enabled non-Ellen peer with own memory). Fixed in this release: drop `tokens=N` from both `peer.context()` invocations, add render-side cap (chars/4) on rendered overlay. Unblocks E-15's Probe 2.

### Spec / Plan

- Spec: `docs/superpowers/specs/2026-04-30-phase5-memory-hygiene-design.md`
- Plan: `docs/superpowers/plans/2026-04-30-phase5-memory-hygiene.md` (this plan, §A)

### Verification

- Local pytest (`-m "not docker and not slow"`, `tests/` scope): pass count + 12 new tests, 0 failed.
- Smoke 3/3 PASS (healthz / turn-assistant / voice-sse).
- N150 telemetry distribution: 1× `call_type=self_overlay` per turn + N× `call_type=self` per turn (vs prior shape of N× `call_type=self` only).
- N150 `BudgetTracker` warning silent for first 3 Ellen turns post-deploy.

## [0.25.0] - 2026-04-30 — Phase 4b: SDK observability

### Added
- **Bug 3** (HIGH): every SDK turn (in_casa engagement and Ellen DM)
  emits per-message structured records. `assistant_message` and
  `turn_done` at INFO; `tool_use`, `tool_result`, `system_init` at
  DEBUG. Operators reading `docker logs` can reconstruct what the
  assistant did without raising the global level. Logger: `sdk`.
- **Bug 4** (HIGH): when the SDK CLI subprocess writes to stderr,
  output appears in Casa's `docker logs` stream tagged with `cid`
  and (where in scope) `engagement_id`. Six wiring sites covered:
  `agent.py` Ellen turn, `in_casa_driver.start` + `.resume`,
  `observer._decide_interjection`, `tools.delegate_to_agent`,
  `tools._synthesize_answer`. Logger: `subprocess_cli`.
- **Bug 5** (MEDIUM): when `agent._process`'s retry-fresh path fires
  (resume sid stale → ProcessError → clear + retry), one INFO line
  records the event with exit_code, prior_sid, stderr_tail. Logger:
  `agent`. Closes Bug 5 by side-effect of Bug 4 (root cause now
  visible) plus auditable retry telemetry.
- **G5** — `claude_code` driver per-engagement s6-log file relayed
  line-by-line into the `subprocess_cli` logger at DEBUG so when
  E-12 (claude_code topic silence) is later tackled the diagnostic
  data already exists.

### Internal
- New module `casa-agent/rootfs/opt/casa/sdk_logging.py` (~150 lines):
  `log_system_init`, `log_assistant_message`, `log_tool_use`,
  `log_tool_result`, `log_turn_done`, `make_stderr_logger`,
  `with_stderr_callback`, `extract_tool_target`. All consumers call
  through this module so log shape is identical and tested in one
  place.
- New `tests/test_sdk_logging.py` covers each function (17 tests).
- `dataclasses.replace` pattern from agent.py (clearing `resume`)
  reused for `with_stderr_callback`.
- Spec doc-rot caught at plan-write: spec § 6.6 listed two
  `ClaudeAgentOptions` construction sites; reality at master had six
  `ClaudeSDKClient` sites. All six wired in this PR (memory
  `feedback_pre_1_0_0_license` — additive change, no compat shims).

### Notes
- **Out of scope**: tool-marker rendering in topic (UX feature; future
  phase); E-12 (claude_code driver topic silence — needs its own design
  epic on whether to drop `--remote-control` vs design a tee); OTEL
  collector / exporter wiring; structured event-bus emission for SDK
  signals.
- **No engagement-data migration**; no schema change.
- **Performance**: the dispatch adds one logger.info + a few
  logger.debug calls per turn (microsecond cost). G5 relay is DEBUG-only,
  invisible in steady prod state.

## [0.24.0] - 2026-04-30 — Phase 4a: OTEL DEBUG-noise cleanup (Bug 7)

### Fixed
- **Bug 7** (LOW, cosmetic): the `claude_agent_sdk._internal.transport.subprocess_cli`
  logger no longer emits an `OTEL trace context injection failed`
  ModuleNotFoundError traceback on every CLI subprocess connect. Two
  changes: (1) `opentelemetry-api>=1.20.0` joins `requirements.txt`
  so the SDK's lazy `opentelemetry.propagate` import succeeds (no
  exception swallowed → no DEBUG traceback emitted). (2)
  `log_cid.install_logging` quiets the `opentelemetry` logger to
  WARNING as belt-and-braces against future SDK paths that emit
  through the same logger. Live evidence: 2026-04-30 06:40:22Z and
  06:40:29Z N150 v0.23.0 cids `c8fcfca1` + `c3fae47c`.

### Internal
- Single new test `test_log_cid.py::TestInstallLogging::test_otel_logger_quieted_to_warning`
  asserting the post-`install_logging()` effective level on the
  `opentelemetry` logger.

### Notes
- **Out of scope:** Phase 4b (Bugs 3 + 4 + 5 + claude_code log relay
  G5) ships separately as v0.25.0 with its own design surface.
  E-12 (claude_code driver topic silence) remains deferred to its
  own design epic.
- Cosmetic-only release. No API or schema changes. No
  config/options changes.

## [0.23.0] - 2026-04-30 — Phase 3b: engagement-topic streaming (Bug 1)

### Fixed
- **Bug 1** (HIGH): `InCasaDriver._deliver_turn` no longer buffers the
  entire SDK turn before posting to the engagement topic. Each
  `AssistantMessage` triggers a cumulative-text emit via the new
  `TopicStreamHandle` (1-second per-topic throttle, edit-in-place via
  Telegram `editMessageText`). Multi-step executor turns
  (Read → Edit → validate → reply) now show progressive visibility
  starting within seconds of the first model output, instead of 60-120s
  of silence followed by a single batch dump. Mirrors Ellen's existing
  `create_on_token` + `finalize_stream` pattern (`channels/telegram.py:739-859`)
  but parameterised by `topic_id` instead of `chat_id`.

### Internal
- New `channels.telegram.TopicStreamHandle` class + `TelegramChannel.create_topic_stream(topic_id)` factory method (~120 lines).
- `InCasaDriver.__init__` constructor signature change: `send_to_topic` kwarg removed; `topic_stream_factory` kwarg added. Pre-1.0.0 license; no shim.
- `casa_core.main` rewires `engagement_driver` to pass the factory; `claude_code_driver` keeps its `send_to_topic` kwarg unchanged (E-12 deferred to Phase 4).
- 8 new unit tests in `tests/test_telegram_topic_stream.py` covering first-emit / throttle / overflow / not-modified swallow / error logging.
- 2 new tests in `tests/test_in_casa_driver.py::TestInCasaStart` covering streaming semantics + skip-on-empty-AssistantMessage.

### Notes
- **Out of scope:** tool-marker rendering during streaming (M1 confirmed in spec §3 Q6); per-message logger lines in `_deliver_turn` (Phase 4 / morning Bug 3); CLI subprocess stderr capture (Phase 4 / morning Bug 4); claude_code driver streaming (E-12, Phase 4).
- **No engagement-data migration:** `engagements.json` schema unchanged; existing engagements (active or cancelled) read identically.

## [0.22.0] - 2026-04-30 — Phase 3a: cosmetic + cancel (E-2 + E-8 + E-13)

### Fixed
- **E-2** (LOW): Ellen's cumulative `attempt_text` in `agent.py:_attempt_sdk_turn`
  now inserts `\n\n` between successive `AssistantMessage` boundaries.
  TextBlocks within the same AssistantMessage remain joined without separator
  (one model thought). User-visible: delegation flows like "ack + Tina's
  answer" now read as discrete paragraphs instead of glued strings.
- **E-8** (MEDIUM): `InCasaDriver._deliver_turn` applies the same separator
  pattern in the configurator's buffered topic-post path. Multi-step
  executor turns ("Reading X. Now editing Y. Validating Z.") get clean
  paragraph breaks. Streaming visibility is still Phase 3b.
- **E-13** (HIGH): PTB `MessageHandler` registration now dispatches webhook
  updates to `handle_update` (engagement-aware router) instead of `_handle`
  (engagement-unaware bus-dispatch leaf). `/cancel`, `/complete`, and
  `/silent` posted in engagement topics are now intercepted as documented;
  previously they fell through to Ellen as plain turns. The engagement
  routing logic itself was already complete and tested (514 lines of
  pre-existing tests in `tests/test_telegram_engagement_routing.py`);
  only the wiring at line 247 was wrong.

### Notes
- Phase 3b (Bug 1 — engagement-topic streaming silence) ships separately as
  v0.23.0 with its own design spec at
  `docs/superpowers/specs/2026-04-30-phase3b-engagement-streaming-design.md`.
- Three orphan engagements (`52fa6ca8`, `9a78971d`, `c798e373`) from the
  v0.18.x exploration session can now be cleaned up via `/cancel` in their
  respective topics.

## [0.21.0] - 2026-04-29 — Phase 2: specialist provisioning + memory peer_id

### Fixed
- **E-4** (HIGH): `casa_core.main`'s agent-home provisioning loop now iterates
  every loaded in_casa **resident or specialist** agent, not residents only.
  Specialists (e.g. `finance`) get their `/addon_configs/casa-agent/agent-home/<role>/`
  directory created at boot, so delegations no longer fail with
  `sdk_error (Working directory does not exist: ...)`. Loop refactored into
  `agent_home.provision_all_homes()` for unit-testability. Executors remain
  excluded (they run with `cwd=/addon_configs/casa-agent`).
- **E-1** (MEDIUM): `memory._render()` now reads `peer_id` (Honcho v3 SDK
  shape per OpenAPI `components.schemas.Message`) before falling back to
  `peer_name` (legacy `_SqliteMsg`). Eliminates the
  `'Message' object has no attribute 'peer_name'` AttributeError that
  silently no-op'd M4b memory for every butler delegation on v0.20.0.

### Internal
- New helper `agent_home.provision_all_homes()` (3 unit tests in
  `tests/test_agent_home_provisioning.py`).
- Test stubs renamed to use `peer_id` (matches real Honcho SDK shape):
  `StubMessage` in `tests/test_memory_honcho.py`, `FakeMessage` in
  `tests/test_memory_render.py`. The wrong-shape stubs were why M3a's
  "real-shape coverage" failed to catch E-1.
- New regression tests `test_render_handles_honcho_v3_message_shape` and
  `test_render_handles_sqlite_message_shape` in `tests/test_memory_render.py`.

## [0.20.0] - 2026-04-29

### Fixed
- **E-6 / E-10**: `_effective_caller_role()` priority flip — `engagement_var`
  now checked before `origin_var`. Configurator engagements can again call
  `config_git_commit` and `casa_reload` without falling back to raw `git
  commit` or "manual addon restart" workarounds.
- **E-7**: `_deliver_turn` binds `engagement_var` for the duration of the
  SDK loop. `emit_completion` now resolves the active engagement instead
  of returning `not_in_engagement`.
- **Bug 2 (sdk_session_id checkpoint timing)**: `InCasaDriver` now eagerly
  persists `sdk_session_id` to the registry the first time
  `client.session_id` becomes non-null, instead of waiting for the 24h
  idle sweeper. An unclean shutdown mid-turn can no longer orphan an
  engagement with `sdk_session_id: null`.

### Changed
- `InCasaDriver.__init__` gains a new keyword arg
  `persist_session_id: Callable[[str, str], Awaitable[None]] | None`
  (default `None`). The single caller (`casa_core.main`) is updated.
  Pre-1.0 minor bump to signal the API change.

### Internal
- New `tests/test_role_gate_priority.py` (3 tests).
- `tests/test_in_casa_driver.py` extended (4 tests).
- `tests/test_engage_executor_tool.py` extended (1 leak-detection test).

## [0.19.0] - 2026-04-29 — Phase 0 / E-11: persistent addon-config mount

**BREAKING — first boot of v0.19.0 wipes and reseeds the entire
`/addon_configs/casa-agent/` tree.**

The previous map declaration paired `addon_config:rw` with `config:ro`,
both of which target `/config` inside the container. HA Supervisor
silently dropped `addon_config:rw` (the conflict loser), so
`/addon_configs/casa-agent/` was never a real bind mount — it was a
rootfs-overlay path that got wiped on every container rebuild. Every
configurator commit, every manual edit under `/addon_configs/casa-agent/`,
every plugin-marketplace install state, and every git history entry
in the addon-config tree vanished on the next `ha apps restart`. See
`docs/bug-review-2026-04-29-exploration.md` § E-11 for the full
forensic write-up + live evidence (mount-table dump, boot-log seed
trail, git-history collapse).

### Changed (BREAKING)

- **`casa-agent/config.yaml::map`** — replaced
  `addon_config:rw` + `config:ro` with a single
  `all_addon_configs:rw` directive. The container now sees
  `/addon_configs/` as a real bind mount of the supervisor's
  addon-configs root (`/mnt/data/supervisor/addon_configs/`), which
  means `/addon_configs/casa-agent/` is finally a persistent
  per-addon subdir surviving container rebuilds.
- **First-boot reseed:** because the underlying mount source changes
  from rootfs-overlay to bind mount, the existing `/addon_configs/casa-agent/`
  contents are NOT migrated. `setup-configs.sh` re-seeds defaults on
  first boot of v0.19.0 (per its existing `[ ! -d "$dst" ]`
  idempotency gate). User-edited configs from prior versions (such as
  `runtime.yaml::enabled: true` flags, custom `character.yaml` traits,
  custom plugin installs, custom marketplace overlays, and the entire
  in-tree git history under `/addon_configs/casa-agent/.git/`) WILL
  be lost. Any post-v0.19.0 customizations made through the
  configurator engagement path or by manual SSH edits will persist.

### Removed

- **`casa-agent/apparmor.txt`** — removed the dead `/config/** r,`
  rule. Casa code has zero references to `/config/` (verified by
  grep across `casa-agent/rootfs/`). The rule existed only to
  service the dropped `config:ro` mount.

### Added

- **`casa-agent/apparmor.txt`** — added `/addon_configs/ r,` rule.
  Defensive: under the new bind mount, the parent dir
  `/addon_configs/` is a real mount point and `setup-configs.sh`'s
  `mkdir -p` calls need read access to stat it. The existing
  `/addon_configs/casa-agent/** rwk,` rule does not cover the parent.

### Verification

Live-N150 smoke (post-deploy):

1. `mount | grep addon_config` → expect a line of the shape
   `/dev/<X> on /addon_configs type <fstype>` (or a `bind` flag if
   docker-info verbose). Pre-fix this returned nothing.
2. Boot logs immediately after first `ha apps update` to v0.19.0 →
   expect six `Seeded agent dir: <name>` lines (assistant, butler,
   finance, configurator, hello-driver, plugin-developer) plus
   `Initialized config git repo at /addon_configs/casa-agent` — proof
   the seed path fired against an empty mount.
3. Restart twice (`ha apps restart`); on the second boot, the seed
   lines must NOT reappear (`[ ! -d "$dst" ]` is now true → no-op).
   Pre-fix every restart re-seeded; post-fix only the first does.
4. The user-edited `runtime.yaml::enabled: true` flag for finance
   that was set on 2026-04-29 morning is GONE — must be re-set via
   the configurator engagement path (or manual edit) post-deploy.
   This is expected per the BREAKING note above.

### Out of scope

This is Phase 0 of the bugfix roadmap. Phases 1-6 are tracked in
`docs/bug-review-2026-04-29-exploration.md` § "Suggested bugfix-roadmap shape".

### Memory hooks

After verification, add a memory entry summarizing the fix-shape
choice (Option B `all_addon_configs:rw` over Option A `/config`
repoint — see plan-doc rationale) and the live-deploy result. The
memory entry `reference_v0_18_1_addon_config_fixes` is now stale
(it referenced SHAs not present in master tip `04037d0`); revisit
it post-ship to either correct or remove.

## [0.18.2] - 2026-04-29 — Engagement setup_engagement_features() ordering fix

**Latent bug since v0.11.0 surfaced by v0.18.1.** Once `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID` started actually reaching `TelegramChannel.__init__` (v0.18.1 fix), `setup_engagement_features()` ran the bot-permission check at startup — but `self._app` was still `None` because `channel_manager.start_all()` hadn't fired yet. The probe failed with `'NoneType' object has no attribute 'get_me'`, leaving `engagement_permission_ok = False` permanently. Every `engage_executor` / `delegate_to_agent(mode="interactive")` then returned the misleading "set telegram_engagement_supergroup_id in addon" error.

### Fixed

- **`casa_core.py`** — `telegram_channel.setup_engagement_features()` is now called AFTER `channel_manager.start_all()`, not immediately after `register()`. The bot isn't built until `_rebuild()` runs inside `start_all()`. The deferred call is wrapped in try/except + ERROR-log to avoid blocking startup if the supergroup probe fails for an unrelated reason (e.g., Telegram API outage).

This was latent for ~7 months because v0.18.0 and earlier never actually exported `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID` to the env (v0.11.0 schema-write regression that v0.18.1 fixed). Operators who set the option still hit the no-op early-return at `setup_engagement_features` line 634; the bug only manifests once the env var actually reaches `TelegramChannel.__init__`.

## [0.18.1] - 2026-04-29 — Engagement supergroup env-export fix + log_level option

**Two operator-facing fixes discovered during M6 (v0.18.0) deploy verification.**

### Fixed

- **`telegram_engagement_supergroup_id` no longer ignored at runtime.**
  The `s6-overlay/s6-rc.d/svc-casa/run` script exported 4 of the 5
  `telegram_*` config options to env vars but missed
  `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID`. This caused
  `casa_core.py:1028` to read the empty env (default `"0"`),
  parse to `0`, and pass `engagement_supergroup_id=None` to
  `TelegramChannel`. Every engagement-tool call (`engage_executor`,
  `delegate_to_agent` with `mode="interactive"`) returned the
  "set telegram_engagement_supergroup_id in addon" error even when
  the option was correctly set in the addon configuration.
  Regression dates from v0.11.0 (engagement primitive ship); silent
  for ~7 months because the manual Telegram smoke probe in v0.11.0
  was performed by an operator who had also not yet set the option.
  Regression test in `tests/test_run_script_env.py` parameterizes
  every TELEGRAM_* env var the run script must export.

### Added

- **Operator-facing `log_level` addon option.** `list(debug|info|warning|error)?`
  with INFO default. Wired through `svc-casa/run` (null-normalized
  like `casa_tz` / `scope_threshold`) → `LOG_LEVEL` env var →
  `casa_core.py::install_logging(level=...)`. Operators can now flip
  to DEBUG via the HA UI without rebuilding the image.

## [0.18.0] - 2026-04-29 — Memory M6: cross-role recall

**Adds `consult_other_agent_memory(role, query)` — a read-only MCP
tool that lets a resident query another agent's accumulated
theory-of-mind of the user without delegating a full agent turn.**

### Added

- **`MemoryProvider.cross_peer_context`** — 4th method on the ABC
  at `memory.py`. `HonchoMemoryProvider` wraps Honcho v3's
  `peer.context(target=user_peer, search_query=query)` primitive;
  `NoOpMemory` and `SqliteMemoryProvider` return `""` per the
  graceful-degradation contract; `CachedMemoryProvider` is
  passthrough.
- **`_render_peer_context` helper** — renders `peer.context()`'s
  `peer_card` + `representation` shape under
  `## What {Observer} knows about you (cross-role)`.
- **`consult_other_agent_memory` MCP tool** — registered in
  `tools.py` and exposed via `CASA_TOOLS`. Validates role against
  the resident/specialist registry; structured-error strings on
  bad input.
- **`memory.cross_peer_token_budget`** — new field on resident
  `runtime.yaml::memory` (default 2000 when unset). JSON-schema
  additive update on `runtime.v1.json`.
- **System prompt — Ellen's `prompts/system.md`** — new
  "Cross-role memory recall" section teaches Case 1 (recall →
  this tool) vs Case 2 (factual lookup → `delegate_to_agent`).
- **Configurator doctrine** — `recipes/resident/create.md` and
  `recipes/resident/update.md` updated to teach the new tool +
  `cross_peer_token_budget` field.
- **`memory_call` telemetry** — new `call_type: "self" | "cross_peer"`
  field across all emission sites. New tool-side
  `consult_other_agent_memory_call` info line with role / query_len /
  result_len / t_ms.

### Changed

- **`MemoryProvider` ABC** — bumped from 3 methods to 4. Pre-1.0.0
  license invoked, no backward-compat shim. Out-of-tree providers
  (none today) would break loudly at import.
- **Ellen (`assistant/runtime.yaml::tools.allowed`)** — gains
  `mcp__casa-framework__consult_other_agent_memory`.
- **`memory_call` log line** — adds `call_type` field across 5
  emission sites in the same commit per arch-spec § 13 drift-risk
  warning.

### Trust posture

- Tina (`butler`) and Finance (specialist) ship WITHOUT the tool —
  guards Tina's guest-accessible voice channel and keeps
  specialist-to-specialist consultation as an operator-opt-in
  decision via Configurator. Regression tests at
  `tests/test_agent_loader.py` guard the omissions structurally.

### Spec / plan

- `docs/superpowers/specs/2026-04-29-memory-m6-cross-role-recall-design.md`
- `docs/superpowers/plans/2026-04-29-memory-m6-cross-role-recall.md`
- Live arch spec § 16: `docs/superpowers/specs/2026-04-26-memory-architecture.md`

## [0.17.2] - 2026-04-28 — Scheduled trigger silence (F1 follow-up)

**Fixes the v0.17.1 regression where every scheduled trigger fire
raised `ValueError` at session-id construction, plus the
longer-standing leak where Ellen's heartbeat emitted
acknowledgement-style first tokens into Telegram before her
silence-check completed.**

### Fixed

- **`trigger_registry.py:117`** — scheduled-trigger `chat_id` now
  hyphenates `{trig.type}-{trig.name}` instead of colon-joining, so
  `build_session_key` + `honcho_session_id` accept it. Eliminates the
  hourly `ERROR Agent 'Ellen' error [unknown]: part 1='interval:heartbeat'
  contains characters outside [A-Za-z0-9_-]` log line.

### Changed

- **`agent.py` `handle_message`** — `MessageType.SCHEDULED` turns no
  longer receive a `create_on_token` streaming callback. The agent
  thinks privately; only the final text is delivered. Other message
  types (`REQUEST`, `NOTIFICATION`, `RESPONSE`, `CHANNEL_IN`) are
  untouched.
- **`agent.py` `handle_message`** — sentinel-based silence gate for
  `SCHEDULED`: when the model returns `<silent/>` (exact match after
  `strip()`) or whitespace-only output, the send path is skipped and
  no `RESPONSE` BusMessage is emitted.
- **`defaults/agents/assistant/triggers.yaml`** — heartbeat prompt
  replaces the obsolete streaming warning with the
  `<silent/>` sentinel contract. Override rules and closing
  instructions unchanged.

### Tests

- `tests/test_trigger_registry.py::TestInterval::test_interval_chat_id_is_honcho_compliant`
  — roundtrip assertion that producer (trigger_registry) and validator
  (`honcho_session_id`) agree on shape.
- `tests/test_agent_process.py::TestScheduledSilence` (5 tests) —
  `create_on_token` count for SCHEDULED vs REQUEST, sentinel
  suppression, whitespace suppression, real-text passthrough.

### Not changed

- No deprecation shim for the colon-shaped `chat_id` (pre-1.0 license,
  per `feedback_pre_1_0_0_license`).
- No silent server-side sanitization in `honcho_session_id` — the
  v0.17.1 fail-fast doctrine stands.
- `morning-briefing.md` — sentinel is opt-in; prompts that always
  send simply never emit `<silent/>`.
- Voice channel user-supplied `scope_id` validation is followup, not
  blocker (see spec §7).

## [0.17.1] - 2026-04-28 — Honcho session-id format fix (F1)

**Fixes the 11-day silent Honcho-write bug discovered post-M4b deploy.**
Every Casa Honcho session-create has 422'd since v0.2.2 (2026-04-17)
because session ids contained `:`, which Honcho's server-side
`^[A-Za-z0-9_-]+$` regex rejects. Reads returned empty digests; writes
were dropped. Failures landed in `try/except → WARNING` so the bug
remained invisible until M4b's `peer_count: 0` telemetry pattern was
finally read as "writes never landed" rather than "fresh sessions".

### Added

- **`casa-agent/rootfs/opt/casa/honcho_ids.py`** — single canonical
  builder `honcho_session_id(*parts)`. Joins parts with `-` (hyphen),
  fail-fasts (`ValueError`) on inputs containing characters outside
  `[A-Za-z0-9_-]`. Strict-reject by design — silent sanitization is
  what blinded us for 11 days.
- **Regression integration test** in `tests/test_honcho_ids.py`
  asserting that the pre-fix colon shape WOULD have tripped Honcho's
  `string_pattern_mismatch` validator.

### Changed

- **All 11 Honcho session-id construction sites** flipped to call
  `honcho_session_id` instead of f-string concatenation:
  - `agent.py:332,552` (resident read/write)
  - `channels/voice/channel.py:454` (voice prewarm)
  - `tools.py:377` (coordinator meta write)
  - `tools.py:439` (M4b specialist)
  - `tools.py:1009` (executor archive read)
  - `tools.py:1326,1330` (engagement-finalize meta)
  - `tools.py:1379` (executor archive write)
  - `tools.py:1553` (query_engager)
- **`session_registry.build_session_key`** rewired through
  `honcho_session_id`. Output shape flipped from `{channel}:{scope_id}`
  to `{channel}-{scope_id}`. Now also accepts `int` `scope_id`
  (Telegram `chat_id`).
- **`session_sweeper`** partitions registry keys on `-` (was `:`).
  Pre-existing colon-shaped JSON entries fall through to the 30-day
  session TTL and age out — no migration shim per pre-1.0.0 license
  (zero data was ever persisted under the old shape; every server
  create 422'd since v0.2.2).

### Breaking

- **Channel-key on-disk format** (`{DATA_DIR}/sessions.json`) flipped
  from `{channel}:{scope_id}` to `{channel}-{scope_id}`. Pre-v0.17.1
  entries become orphans and age out via TTL — no operator action.
- **`build_session_key`** now rejects `scope_id` containing `:`,
  whitespace, or any character outside `[A-Za-z0-9_-]`. Previously
  preserved colons verbatim.

### Spec / doctrine

- `docs/superpowers/specs/2026-04-28-honcho-session-id-format-design.md`
  (new — design rationale, decision log, migration table)
- `docs/superpowers/plans/2026-04-28-honcho-session-id-format-fix.md`
  (new — task-by-task implementation plan)
- `docs/superpowers/specs/2026-04-26-memory-architecture.md` § 5/§ 14/§ 15
  swept to hyphen shape
- Configurator doctrine (`architecture.md`,
  `recipes/specialist/create.md`) swept

## [0.17.0] - 2026-04-28 — Memory M4b: Specialists become memory-bearing

Specialists (Tier 2 — Finance today; future Health/Personal/Business)
gain per-`(role, user_peer)` Honcho memory. One channel-agnostic,
scope-agnostic session per specialist accumulates messages, summary,
and `peer_representation` across all delegate-call channels.

### Added

- **Specialist memory read+write in `_run_delegated_agent`.**
  When `cfg.memory.token_budget > 0` and a memory provider is bound,
  `tools.py:_run_delegated_agent` opens a Honcho session keyed
  `f"{role}:{user_peer}"` (e.g. `finance:nicola`), fetches a digest
  via `get_context(search_query=task_text, tokens=…)`, and prepends
  a `<memory_context agent="{role}">…</memory_context>` block between
  `<delegation_context>` and `Task:`. After the SDK returns text, a
  background task writes the turn back via `add_turn(user_text=task_text,
  assistant_text=…)`. Failures fail-soft (WARNING log, no propagation).
- **`_specialist_meta_write_bg` — coordinator visibility.** Each
  `delegate_to_agent` call also writes a one-line summary to the
  parent's meta session (`{channel}:{chat_id}:meta:{parent_role}`),
  giving Ellen a unified view of specialist activity independent of
  which scope her own per-turn argmax write went to. Task and reply
  truncated to 200 chars per side.
- **Finance opted in by default.** `defaults/agents/specialists/finance/runtime.yaml`
  bumps `memory.token_budget` from 0 to 4000.

### Breaking

Pre-1.0.0 license per `feedback_pre_1_0_0_license.md`:

- `specialist_registry._validate_tier2_shape` no longer rejects
  specialists with `token_budget > 0`. Operators with stateless
  specialists (`token_budget: 0`) are unaffected; operators who set
  `token_budget > 0` will now see Honcho memory engaged.

### Internal additive (non-breaking)

- New module-level helpers in `tools.py`:
  - `_specialist_bg_tasks: set[asyncio.Task]` — GC anchor.
  - `_specialist_add_turn_bg(...)` — fail-soft background writer.
  - `_specialist_meta_write_bg(...)` — fail-soft meta-summary writer.
- `_build_specialist_options` docstring updated; SDK `resume=None`
  unchanged (memory enters via prompt injection, not SDK continuity).

### Architecture

- New 2-segment session id shape `f"{role}:{user_peer}"` joins the
  existing 4-segment `{channel}:{chat_id}:{scope}:{role}` topology.
  Specialists are channel-agnostic and scope-agnostic; both shapes
  are first-class to Honcho (sessions are id-opaque).
- Trust gating stays one level up at the resident's `delegates`
  decision — no per-call channel filter at the memory layer.

### Doctrine + spec

- **Configurator doctrine sync** (per
  `feedback_configurator_doctrine_sync.md`):
  - `recipes/specialist/create.md` — memory-bearing specialist example.
  - `recipes/specialist/update.md` — enable-memory recipe for an
    existing stateless specialist.
  - `architecture.md` — specialist memory subsection + correction to
    the v0.16.0 "stateless specialists" claim.
- **Live arch spec.** `docs/superpowers/specs/2026-04-26-memory-architecture.md`
  § 5 gains a 2-segment specialist-sessions paragraph, plus new § 15
  documenting the read path, write path, meta-scope coordinator
  visibility, and what's deferred to M5/M6.

### Deferred

- Specialist `peer_card` writes / `remember_fact` MCP tool → **M5**.
- Cross-specialist recall via `peer_perspective` → **M6**.
- `read_strategy: cached` for specialists.
- Multi-user (`user_peer != "nicola"`).

## [0.16.0] - 2026-04-27 — Memory M4: Engagement memory

Three layers, one user-visible behavior: engagement summaries flow back
into Ellen's per-turn memory and Configurator engages with prior
context.

### Added

- **L1 — `meta` declared as a system scope.** `policies/scopes.yaml`
  bumps to schema v2 with a new `kind: topical | system` field. System
  scopes are always-on after the trust filter — no embedding, no
  classifier routing. `meta` is the first system scope; assistant adds
  it to `scopes_readable`. Voice (Tina) is excluded by the
  `authenticated` trust gate.
- **L3 — Per-executor archive read at engage-start.** New
  `ExecutorMemoryConfig(enabled, token_budget)` on
  `ExecutorDefinition`; Configurator opts in. `engage_executor`
  interpolates a new `{executor_memory}` prompt slot from the
  per-(channel, chat, executor_type) Honcho session. `claude_code`
  driver-side `workspace.py` slot supported for forward-compat with
  future memory-enabled claude_code executors.
- **L4 — Free benefit.** `_finalize_engagement` already writes
  engagement summaries to the meta session for both specialist and
  executor engagements (since M2.G4, v0.15.3). L1 makes them readable
  on Ellen's normal turn. No new write code.

### Breaking

Pre-1.0.0 license per `feedback_pre_1_0_0_license.md`:

- `policies/scopes.yaml` schema bumped v1 → v2. No migration shim.
  Tenant overlays at `/addon_configs/casa-agent/policies/scopes.yaml`
  must be updated by the operator on upgrade.
- `defaults/schema/policy-scopes.v1.json` removed; replaced by
  `policy-scopes.v2.json`.

### Internal additive (non-breaking)

- `executor.v1.json` gains optional `memory` property; existing
  executor `definition.yaml` files without a `memory:` block remain
  valid (default disabled).

### Doctrine + spec

- **Configurator doctrine sync** (per
  `feedback_configurator_doctrine_sync.md`): `architecture.md`,
  `recipes/scopes/edit.md` (also fixes pre-existing list-style format
  drift), `recipes/executor/scaffold.md`,
  `recipes/resident/{create,update}.md` updated in the same commit
  set.
- **Architecture spec**
  (`docs/superpowers/specs/2026-04-26-memory-architecture.md`): § 5,
  § 6, § 11 updated; new § 14 "Engagement memory"; § 12 M4 entry
  flipped to "Shipped v0.16.0".

### Deferred to future ships

- L2 — Specialists become memory-bearing → M4b (separate brainstorm).
- Synthesized "lessons learned" archive content → future tweak after
  real archive usage patterns emerge.
- `remember_fact` via directional `peer_card` → M5.
- Cross-role recall (`consult_other_agent_memory`) → M6.
- `HONCHO_LIVE_TEST=1`-gated integration test → M3a.1 follow-up
  bundle.

## [0.15.4] - 2026-04-27 — Memory M3: Honcho contract coverage + `memory_call` telemetry

Observability + confidence-coverage release. No runtime-behaviour
changes; closes the M2-era spec § 9 "real-Honcho-response coverage
not in tests today" gap and adds per-memory-call telemetry.

### Added
- **M3a — Honcho populated-response integration test.**
  `tests/test_memory_honcho.py::test_get_context_renders_summary_and_peer_repr_when_honcho_returns_them`
  primes the SDK stub with populated `summary.content` +
  `peer_representation` + `peer_card` + recent `messages` and asserts
  all four `_render` sections appear in canonical order. Closes the
  spec § 9 wiring-coverage gap. Live `HONCHO_LIVE_TEST=1`-gated test
  deferred as M3a.1 follow-up.
- **M3b — `memory_call` info-level log line.** Emitted from each
  concrete provider's `get_context` (Honcho + SQLite) and from
  `CachedMemoryProvider`'s cache-hit branch. Fields: `backend`,
  `session_id`, `agent_role`, `t_ms`, `peer_count`,
  `summary_present`, `peer_repr_present`, `cache_hit`. NoOp provider
  intentionally silent — see new spec § 13 for the full contract.
- **Spec § 13** (`docs/superpowers/specs/2026-04-26-memory-architecture.md`)
  documents the `memory_call` field set and emission rules.

### Migration
- None. M3 adds log lines and tests; no schema, config, or runtime
  contract change. Operators relying on a regex-style log scrape that
  asserted "no `memory_call` lines exist" would need to update —
  vanishingly unlikely.

## [0.15.3] - 2026-04-26 — Memory M1+M2: spec consolidation + Honcho-side fixes

First user-visible memory ship since v0.8.4. Folds the internal-only M1
cleanup (no version bump at the time) into the same release as M2's
three Honcho-touching bug fixes.

### Added (M1)
- `docs/superpowers/specs/2026-04-26-memory-architecture.md` —
  consolidated current-state spec for the memory subsystem. Supersedes
  2.2a/2.2b/3.2/3.2.1/3.2.2 design specs for "what is true today"
  purposes.

### Removed (M1)
- `card_only` read strategy. Reserved in 2.2a, never implemented; the
  branch in `_wrap_memory_for_strategy` warned and fell back to
  `per_turn`. No default YAML used it.
- SQLite-side `peer_cards` table + reader. No code ever wrote to it;
  the deferred `remember_fact` tool stays a Honcho-only feature per the
  graceful-degradation doctrine.
- `archive_session_full` executor field. Parsed and stored but no
  reader. Plan 4a transcript archival fires unconditionally on
  `kind=executor`.

### Fixed (M2)
- **Voice prewarm cache key restored.**
  `channels/voice/channel.py::_prewarm` was building the pre-3.2
  3-segment session id `voice:{scope_id}:{role}`. The agent's read
  path uses 4 segments `{channel}:{chat_id}:{scope}:{role}`, so the
  prewarm cache key never matched the real-turn key — every wake-word
  paid the full cold-read latency. Now loops over the agent's
  `scopes_readable` and warms one entry per scope using the 4-segment
  shape with budget // len(scopes) tokens each.
- **Cancel + force-delete now write engagement summaries.**
  `cancel_engagement` and `delete_engagement_workspace(force=True)`
  passed `memory_provider=None` to `_finalize_engagement`, silently
  skipping the meta-scope summary write and the per-executor-type
  Honcho archival. Both sites now resolve `active_memory_provider`
  from the `agent` module the same way `emit_completion` does.
  Cancellations and force-deletes leave the same Honcho trace as
  normal completions.
- **`query_engager` reads from the engager's actual scope.**
  `tools.py:1357` reads `engagement.origin.get("scope", "meta")`;
  `agent.py` never set `"scope"`, so Tina's `query_engager("what did
  the user say…")` always retrieved from Ellen's meta scope — which
  only contains engagement summaries, never user conversation. The
  agent now stamps `argmax_scope(scores, default_scope)` onto
  `origin_var` after the read-path classifier runs, so engagements
  spawned during a turn carry the scope the turn was rooted in.

### Migration
- M1 migration notes still apply: existing SQLite databases keep their
  now-orphan `peer_cards` table (harmless, no longer read); existing
  `definition.yaml` files with `archive_session_full: ...` will fail
  schema validation — delete the line.
- M2: no migration. Voice prewarm change is transparent (cache hits
  start working again). Cancel-path memory writes are additive (Honcho
  gets entries it was missing). G6 stamps a new `scope` key onto
  `engagement.origin` — code reading `origin` with `.get(..., default)`
  is unaffected; any code doing exact-equality dict comparison would
  need updating but no such site exists.

## [0.15.2] - 2026-04-26 — Heartbeat noise + sweeper crash

Two production bugs visible in `addon_c071ea9c_casa-agent` logs.

`engagement_idle_sweep` (cron 08:00 daily) and `workspace_sweep`
(interval 6h) were registered as `lambda: asyncio.create_task(...)`
in `casa_core.py`. APScheduler's `AsyncIOExecutor` runs sync callables
in a worker thread, so `asyncio.create_task` raised
`RuntimeError: no running event loop` on every fire — silently no-op
since v0.13.0. Fix: pass the coroutine functions directly with
`kwargs={...}`; AsyncIOExecutor schedules them on the loop natively
(same pattern `trigger_registry._register_scheduled` already uses).

Ellen's `heartbeat` trigger fires every 60min and was producing
chatty "checking in" messages despite the prompt's "stay quiet"
instruction. The Telegram channel runs in `stream` mode — the *first
token* posts a new chat message, so any preamble Ellen drafts before
deciding to stay silent has already gone out. Rewrite the prompt:
silence is now framed as the default action, the bar for sending is
explicit and narrow, and a "no preamble, no reflection text" rule
forbids the first-token leak.

### Fixed

- `casa_core.py:1506,1519` — `engagement_idle_sweep` and
  `workspace_sweep` jobs now register the coroutine function
  directly. Adds `tests/test_scheduled_sweeper_jobs.py` to lock
  the wiring (would have caught this since v0.13.0).
- `defaults/agents/assistant/triggers.yaml` heartbeat prompt
  rewritten — silence-first framing, explicit "what NOT to send"
  list, no-preamble rule.

## [0.15.1] - 2026-04-26 — Tina HA control

Tina (butler) becomes the universal Home Assistant operator. Server-level
grant to the homeassistant MCP gives her every Assist tool the user has
exposed; new prompt sections teach her how to use them; Ellen's
delegates.yaml gains a butler entry so the Telegram-via-Ellen path
("ask Tina to turn off the lights") works end-to-end. Closes the
v0.15.0 deferred manual smoke.

### New

- `mcp__homeassistant` server-level grant in `defaults/agents/butler/runtime.yaml` —
  every HA Assist tool callable from Tina, present and future, no
  enumeration required.
- Three new prompt sections in `defaults/agents/butler/prompts/system.md`:
  `## Home Assistant tools`, `## Intent patterns`, `## Error recovery`.
- `butler` entry in `defaults/agents/assistant/delegates.yaml` so Ellen's
  `<delegates>` block advertises Tina and `delegate_to_agent("butler", ...)`
  passes the role-map gate.
- `CASA_HA_MCP_URL` env override on `casa_core.py` — defaults to
  `http://supervisor/core/api/mcp`. Used by e2e to point HA traffic at
  the mock.
- Mock HA MCP server at `test-local/e2e/mock_ha_mcp/server.py` — minimal
  JSON-RPC 2.0 implementation with `HassTurnOn`/`HassTurnOff`/
  `GetLiveContext` and `/_calls`/`/_reset` test side-channels; rejects
  unknown tool names with `-32602`.
- Mock SDK file-driven HTTP MCP tool-invoke hook
  (`MOCK_SDK_TOOL_INVOKE_FILE`) — lets tier-2 e2e exercise the
  resident-options → SDK → HTTP MCP transport chain without a live model.
- Tier-2 e2e `test-local/e2e/test_ha_delegation.sh` — H-0..H-3 covering
  the CASA_HA_MCP_URL flow, the voice-direct path, and the
  agent_loader → SDK options chain.
- Configurator doctrine recipe `recipes/resident/grant_ha_tools.md`.

### Notes

- HA integration must be enabled and entities exposed to default Assist
  pipeline by the user — Casa cannot configure these.
- "Trust the model fully" decision recorded in spec §6 — no per-tool /
  per-domain restrictions. Safety guardrails (irreversible actions
  behind confirmation read-back) tracked as future roadmap item.
- Tier-2 e2e exercises butler→HA directly via the mock-SDK hook;
  the Ellen→delegate_to_agent→butler two-hop chain stays covered by the
  J.5 manual smoke (live SDK on N150).

## [0.15.0] - 2026-04-25 — Resident-to-resident delegation

Residents can now delegate to other residents and specialists by role
via the new `delegate_to_agent` MCP tool. Lifts the previous "Ellen is
the only delegator" architectural constraint.

### New

- `delegate_to_agent(agent=<role>, task=, context=, mode={sync,async,interactive})` —
  unified delegation tool. Resolves `agent` against a merged role map of
  residents + specialists. `mode=interactive` is rejected for residents.
- `<delegates>` and `<executors>` system-prompt blocks rendered at turn
  time from each resident's `delegates.yaml` / `executors.yaml`. Closes
  the long-standing dead-data bug where `cfg.delegates` was loaded but
  never reached the model.
- `<delegation_context>` block prepended to delegated calls so target
  agents can adapt voice/text register.
- New `executors.yaml` (assistant-only) — `configurator`,
  `plugin-developer`, and `engagement` entries moved out of
  `delegates.yaml`.
- `agent_registry` module: name↔role bidirectional map for prompt
  rendering and future code paths.

### Breaking (no back-compat alias; pre-1.0.0)

- `delegate_to_specialist` removed; replace with `delegate_to_agent`.
- `mcp__casa-framework__delegate_to_specialist` removed from
  `runtime.yaml::tools.allowed` allowlists; replace with
  `…delegate_to_agent`.
- Configurator doctrine updated; recipes wire/unwire generalized.

### Behavioral

- Single-hop depth cap: a delegated agent cannot itself call
  `delegate_to_agent` (returns `delegation_depth_exceeded`). Trivially
  relaxable via the `_MAX_DELEGATION_DEPTH` constant in `tools.py`.

### Out of scope (separate specs)

- HA-control plugin / Tina's tool inventory.
- Cross-channel sending.
- Multi-hop chaining.

## [0.14.12] - 2026-04-25

Log-noise sweep — four fixes surfaced by a live N150 log audit
(2026-04-25). All changes target log signal/noise; no behavior shifts
beyond the heartbeat-delivery one called out below.

### Fixed

- **Telegram channel**: `chat_id` validation. The `context["chat_id"]`
  slot is overloaded — user-initiated messages carry a numeric Telegram
  chat id, but scheduled triggers carry session-keying labels like
  `"interval:heartbeat"`. The Telegram API rejects non-numeric values
  with `BadRequest: Chat not found`, which used to bubble through
  `finalize_stream → send` and surface as a full traceback at the bus
  dispatcher. New `_resolve_chat_id` helper falls back to the channel's
  registered default when the value isn't numeric. **Behavioral note:**
  hourly heartbeats now actually deliver to the registered chat instead
  of silently failing — if the agent prompt's "stay quiet" instruction
  isn't honored, the user will see hourly pings. Tune the prompt if so.
- **CC CLI transcript persistence**: `setup-configs.sh` now symlinks
  `/root/.claude/projects` to `/addon_configs/casa-agent/cc-home/.claude/projects`
  on boot. The bundled CC CLI uses `$HOME=/root → ~/.claude/projects/`,
  but `/root/` is wiped on every container rebuild, so the SDK's
  `--resume <sid>` path failed on every first turn after a deploy
  (visible as `claude_agent_sdk._internal.query: Fatal error in message
  reader` for `voice:probe-scope` and `telegram:interval:heartbeat`).
  One-time migration on first boot copies any pre-existing transcripts
  into the persistent location before replacing the dir with a symlink.
- **Empty `s6-rc-compile` at boot**: `replay_undergoing_engagements`
  used to call `_compile_and_update_locked()` unconditionally, which
  printed `source /data/casa-s6-services is empty` to stderr at every
  boot when no claude_code engagements were active. Now early-returns
  when both `undergoing` and `removed_orphans` are empty — the
  engagement sources dir is unchanged, so a compile would be wasted.
- **`svc-nginx/finish` and `svc-ttyd/finish`**: gate the `bashio::log.warning`
  on exit codes 0 and 256, mirroring the existing pattern in
  `svc-casa-mcp/finish`. Code 0 = clean stop (s6 told it to); code 256
  = s6 "do-not-restart" sentinel. Anything else still surfaces.

### Files

- `casa-agent/rootfs/opt/casa/channels/telegram.py` — `_resolve_chat_id`
  helper + 3 call-site updates (`send`, `create_on_token`, `finalize_stream`).
- `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh` — projects
  dir symlink with first-boot migration.
- `casa-agent/rootfs/opt/casa/casa_core.py` — `replay_undergoing_engagements`
  fast path.
- `casa-agent/rootfs/etc/s6-overlay/s6-rc.d/svc-nginx/finish` and
  `.../svc-ttyd/finish` — exit-code gate.

## [0.14.11] - 2026-04-25

Test tiering — Half 1. Re-groups existing CI tests into a three-tier
structure so trivial PRs get sub-2-minute "is the system on fire"
feedback while hardening (timing/chaos) tests run nightly + on-demand.
No runtime / addon code changes; CI plumbing + test-file rearrangement
only.

### CI

- **`.github/workflows/qa.yml`** rewritten as `tier1-smoke` (every push
  + PR + nightly + manual, ~7-8 min cold-cache) / `tier2-functional`
  (push + PR + manual, ~12 min, parallel with tier 1) /
  `baseline-runtime` (unchanged, parallel with tier 2) /
  `tier3-hardening` (nightly + manual only). Tier 1 has no `needs:`
  gating against tier 2 — contributors get fail-fast smoke signal in
  parallel with the full functional sweep.
- **Nightly cron** at 04:00 UTC. Nightly skips tier 2 (already verified
  on the master push that landed the changes); runs tier 1 + tier 3.
- **Manual `workflow_dispatch`** runs all three tiers from any branch.
- **D-block + P-block CI steps** stay commented out, deferred to the
  pre-existing v0.14.10 D/P-block sweep follow-up. Their split scripts
  exist (so the sweep can re-enable them by uncommenting one block) but
  do not run in CI today.

### Tests

- **`test-local/e2e/test_engagement.sh` split into 3 files**
  (1859 lines → 3 self-contained scripts):
  - `test_engagement_E.sh` (~944 lines): E-0..E-10 Tier-2 specialist +
    Configurator. Tier 2.
  - `test_engagement_D.sh`: D-1..D-12 claude_code driver lifecycle.
    Tier 3. Requires `CASA_USE_MOCK_CLAUDE=1`; skips cleanly otherwise.
  - `test_engagement_P.sh`: P-1..P-9 plugin-developer harness. Tier 2.
    Requires `CASA_USE_MOCK_CLAUDE=1` + `CASA_PLAN_4B=1`; skips
    cleanly otherwise.
- **`start_mock_telegram_server` helper** added to
  `test-local/e2e/common.sh`; replaces the inline E-0 spawn block.
- **Checkpoint count** preserved across the split (sum of `^pass "`
  lines in the 3 new files = original + 1; the +1 is the new
  `pass "P-block container healthy"` boot line because P now boots its
  own container, where it previously reused D's).

### Removed

- **`test-local/e2e/test_migration.sh`** (57 lines) — asserted seeded
  YAML markers for behavior the v0.7.0+ pre-1.0 wipe-on-update doctrine
  explicitly does NOT do. Same fate v0.9.1 gave `test_heartbeat.sh`.
  `git log -- test-local/e2e/test_migration.sh` recovers it post-1.0
  if migrations are reintroduced.
- **`test-local/Makefile::test-migration`** target dropped.

### Build

- **`test-local/Makefile`** gains `test-tier1`, `test-tier2`,
  `test-tier3`, `test-all`; legacy `test`/`test-smoke`/`test-runtime`/
  `test-voice` targets retained.

## [0.14.10] - 2026-04-25

v0.14.9 follow-up: enable seeded plugins after seed-copy. The v0.14.9
seed-copy populates cc-home with 5 default plugins but they all carry
`enabled: false` (CC CLI's `--cache-dir`-mode install at build doesn't
auto-enable). The binding layer (`plugins_binding.py::build_sdk_plugins`)
filters out `enabled: false` entries — so engagements were getting
`plugins=[]` even though all 5 plugins were structurally present.
Live verification on N150 v0.14.9 caught this: `claude plugin list
--json` showed 5/5 plugins, all `enabled=False`.

### Fixed

- **`setup-configs.sh` seed-copy block** now runs `claude plugin enable
  <ref>` for each of the 5 default plugins after the cc-home seed-copy
  completes. Runtime `--scope user` enable persists the flag in
  cc-home's `installed_plugins.json` and is idempotent (returns clean
  on a no-op re-run because of the `|| true` and `>/dev/null 2>&1`).

- **`test-local/init-overrides/01-setup-configs.sh`** mirrors the same
  enable loop so the local e2e build matches production.

### Tests

- **`test_invoke_sessions.sh::C-4`** strengthened to assert `5/5`
  (total/enabled), not just `5` (total). Plain count-check would have
  passed on v0.14.9 even with all-disabled plugins; this catches the
  binding-layer's enabled-filter contract explicitly.

## [0.14.9] - 2026-04-25

Unified github access. Replaces the five distinct mechanisms across
build / boot / Configurator runtime / plugin-developer engagement with
one path: a system-level `/etc/gitconfig` (SSH→HTTPS rewrite + a
credential helper) plus `$GITHUB_TOKEN` propagated at addon-wide scope
via `/run/s6/container_environment/GITHUB_TOKEN`.

### Added

- **`/etc/gitconfig`** ships in the image. Contains an SSH→HTTPS
  insteadOf rewrite for github.com (no token) and a `credential.helper`
  pointing at `/opt/casa/scripts/git-credential-casa.sh`. Applies
  system-wide regardless of which user or HOME the process runs under.

- **`/opt/casa/scripts/git-credential-casa.sh`** — stateless POSIX shell
  helper. Reads `$GITHUB_TOKEN` from process env at request time, emits
  `username=x-access-token\npassword=...` on stdout. Token never
  written to any config file. Includes CR/LF strip on token to prevent
  malformed credential responses if `op read` emits trailing newlines.

### Changed

- **`setup-configs.sh`** resolves `op://${ONEPASSWORD_DEFAULT_VAULT}/GitHub/credential`
  at boot via `bashio::config` + `op read`, then writes the token to
  `/run/s6/container_environment/GITHUB_TOKEN` (mode 0600). s6-overlay
  merges this into every supervised service's environment, so casa-main,
  svc-casa-mcp, every engagement subprocess, and every `git`/`gh`/`claude
  plugin install` invocation inherits the same token automatically.

- **`setup-configs.sh`** seed-copies cc-home plugin state from the
  image-baked `/opt/claude-seed/` on first boot (idempotent — sentinel
  is `installed_plugins.json` in cc-home). Replaces the v0.14.8 boot
  install loop. Symlink-based — CC CLI tolerates `installPath` via
  symlink (verified by spike D.1 on N150). No network access required
  at boot for the 5 default plugins.

- **`Dockerfile`** pairs each `claude plugin install` with a
  `claude plugin enable` so the image-baked seed has `enabled: true`
  for all 5 default plugins. Without this, the seed-copy would
  preserve `enabled: false` from the build (CC CLI's `install` does
  not auto-enable).

### Removed

- **v0.14.8 boot install loop** in `setup-configs.sh` (the
  `claude plugin install <ref>` loop with `flock`-serialised stderr
  capture). Default-plugin install state now comes from the seed-copy.

- **`_resolve_plugin_developer_github_token`** in
  `drivers/claude_code_driver.py` and the per-engagement
  `extra_env["GITHUB_TOKEN"]` injection. The token is in the
  addon-wide environment and inherited automatically.

- **`Dockerfile`'s per-USER `git config --global url.X.insteadOf`**
  for the `casa` build user. The same rewrite ships in `/etc/gitconfig`,
  which applies to every USER.

### Tokenless mode

If `onepassword_service_account_token` or `onepassword_default_vault`
is unset/null, OR if `op read` fails for any reason, `GITHUB_TOKEN`
stays unset. Casa runs in **public-only mode**: all anonymous github
clones still work via `/etc/gitconfig`'s SSH→HTTPS rewrite; private-repo
clones return 404/403; plugin-developer's `gh repo create` fails (logged
at engagement scope). No secret material leaks anywhere.

### Notes

- Pre-1.0.0 wipe-on-update: no migration. On addon update,
  `/opt/claude-seed/` is rebuilt fresh in the new image; cc-home is
  refilled by the seed-copy on next boot.

- The unified path is verified by the v0.14.9 spike findings on N150
  (2026-04-25): all four spikes passed against the production
  `op://Casa/GitHub/credential` with the proposed credential-helper
  pattern.

## [0.14.8] - 2026-04-25

Boot-time fix — register the seed marketplace alongside the user
marketplace so default plugins actually install. Caught from the
N150 boot log: every default plugin in
`defaults/agents/**/plugins.yaml` was logging
`WARNING: plugin install skipped: <name>@casa-plugins-defaults`
and `claude plugin list --json` returned `[]`, so the binding
layer (`/opt/casa/plugins_binding.py::build_sdk_plugins`) handed
no plugins to engagements at all.

### Fixed

- **Seed marketplace was never registered with the CC CLI.**
  `setup-configs.sh` only ran
  `claude plugin marketplace add /addon_configs/casa-agent/marketplace/`
  (the user-writable overlay). The read-only seed at
  `/opt/casa/defaults/marketplace-defaults/` — which is where every
  `<name>@casa-plugins-defaults` install ref resolves — was missing
  from the install loop's environment, so all five default plugins
  (`document-skills`, `mcp-server-dev`, `plugin-dev`, `skill-creator`,
  `superpowers`) failed to install with
  `Plugin "<name>" not found in marketplace "casa-plugins-defaults"`.
  Added a second `claude plugin marketplace add` for the seed dir
  immediately before the install loop. Idempotent (`|| true` on
  re-register).

### Diagnostic

- **Surface the CC CLI's stderr in the install warning.** Replaced
  `>/dev/null 2>&1 || bashio::log.warning "plugin install skipped: $ref"`
  with `install_err=$(... 2>&1 >/dev/null) || bashio::log.warning
  "plugin install skipped: $ref — $install_err"`. Future install
  failures stay diagnosable instead of cryptic.

### CI clean-up surfaced by this ship

v0.14.1 was the first Plan 4b commit to enable CI jobs that hadn't
run before (`CASA_USE_MOCK_CLAUDE=1` D-block, `CASA_PLAN_4B=1` P-block).
Every Plan 4b master push since has been red in ways that were not
being tracked, because the unit job failed first and hid everything
downstream. Unblocking CI here surfaced four pre-existing bugs that
also ship fixed in this bump:

- **`drivers/s6_rc.py::service_pid` used the wrong `s6-svstat` flag.**
  `-u` prints the literal `true`/`false` up status; `-p` prints the
  supervised PID. The code asked for `-u` and parsed as `int()`, so
  `service_pid()` always returned `None` and
  `ClaudeCodeDriver.is_alive_async()` always reported every engagement
  as dead. Flipped to `-p`. Shipped since v0.13.0 (2026-04-23).

- **Mock SDK `ClaudeAgentOptions` missing `plugins=` field.**
  v0.14.1's binding-layer wiring in `agent.py` and `tools.py` passes
  `plugins=build_sdk_plugins(...)` into every SDK construction. The
  test-only mock dataclass had no such field, so every resident /
  specialist / executor turn raised `TypeError` on the mock, the SDK
  session id was never captured, and `/data/sessions.json` stayed
  empty — breaking the Invoke-sessions E2E. Added `plugins` to the
  mock dataclass. (Matches `reference_mock_sdk_drift` memory: v0.5.9
  precedent — new kwargs MUST be mirrored into the mock same commit.)

- **Py 3.11+ tarfile raises `AbsoluteLinkError` not "symlink".**
  `tests/test_system_requirements_installer_tarball.py::test_symlink_
  member_rejected` used `pytest.raises(UnsafeArchiveError,
  match="symlink")` but the message is wrapped from
  `tarfile.data_filter`'s "link to an absolute path". Broadened the
  regex to accept either phrasing. This is what turned master CI red
  on every Plan 4b commit; this is the fix.

- **D-block `s6-svstat -u` parse bugs in `test_engagement.sh`.** D-1,
  D-4 cancel, and D-13 restart survival all invoked `s6-svstat -u`
  and parsed stdout as `int`. D-1 / D-13 fixed (D-13 switched to
  `-p`; D-1 parses `"true"` as boolean). D-4 parses `"false"` for
  down.

### Known limitation — CI D/P block disabled for v0.14.8

Plan 4a D-block (`CASA_USE_MOCK_CLAUDE=1`) and Plan 4b P-block
(`CASA_PLAN_4B=1`) are **intentionally disabled** in `.github/workflows/
qa.yml` for this ship. They were authored without ever running on
Linux CI — D-2 alone surfaces a further JSONL-glob mismatch, and
D-3..D-8 / P-1..P-9 are unverified. Sweeping them properly exceeds
this ship's scope. Tracked for **v0.14.9 follow-up**: run D/P block
locally against real Linux s6/mock CLI behaviour, fix each harness,
re-enable the CI env vars in one go.

Plan 2 E-block (E-0..E-10) still runs in every qa.yml e2e-fast run,
which continues to verify engagement primitives end-to-end.

## [0.14.7] - 2026-04-25

Bug-review v0.14.6 follow-up — closes Bug 10, the only finding from
`docs/bug-review-2026-04-24.md` deferred from v0.14.6 because it
needed a locking design rather than a surgical patch.

### Reliability

- **Telegram `handle_update` topic-status race (Bug 10).** aiohttp
  dispatched each Telegram update as its own task, so a `/cancel`
  arriving alongside a regular turn could race: the regular turn
  passed `rec.status` while the cancel was mid-finalize, then routed
  to a driver that `_finalize_engagement` had just torn down (driver
  raised `DriverNotAliveError` or the turn landed on a closed topic).
  Fixed with a per-topic `asyncio.Lock` keyed by `message_thread_id`
  on `TelegramChannel._engagement_handler_locks`, mirroring the
  `in_casa_driver._locks: dict[id, Lock]` idiom. Updates landing on
  the same topic now serialise; different topics still run in
  parallel. Three new tests in
  `tests/test_telegram_engagement_routing.py::TestHandleUpdateConcurrencyRace`
  exercise the cancel-vs-turn race, the two-regular-turns drop-
  resistance case, and cross-topic parallelism (deadlock-detection).

### CHANGELOG cleanup

- Collapsed the inadvertent duplicate `## [0.14.6]` heading from
  commit `615eac1` into a single section; moved the `Removed` /
  `Migration` blocks below `Tests` so they sit in the same v0.14.6
  body as the other notes.

## [0.14.6] - 2026-04-25

Bug-review v0.14.6 — security and correctness sweep against findings
from `docs/bug-review-2026-04-24.md`. No new features; all changes
are surgical fixes with regression tests.

### Security

- **block_dangerous_bash regex bypass.** Replaced flat regex matcher
  with an argv-aware checker that splits on shell separators
  (`;`, `&&`, `||`, `|`, `&`), shlex-parses each piece, and recurses
  into `bash -c "..."` / `sh -c "..."`. Variants that previously
  bypassed the safety hook (`rm -r -f`, `rm --recursive --force`,
  `rm -rfv`, `rm -fR`, `/usr/bin/rm -rf`, `bash -c "rm -rf /"`,
  `; rm -rf /`) are now all blocked. Verified live on N150 v0.14.5
  before the fix.
- **Tarball zip-slip / symlink-escape (`system_requirements/tarball.py`).**
  `tarfile.extractall` and `zipfile.extractall` now validate every
  member up front: symlinks/hardlinks/devices/fifos refused, and any
  member whose resolved path leaves the extract dir is rejected. Uses
  the `data` filter on Python 3.11.4+, falls back to manual member
  validation on the production 3.11.2 runtime. Also: the `extract:`
  field is resolved-path-checked, and unsafe URL schemes (`file://`,
  `ftp://`, `jar:`) are refused before download.
- **Tarball `install_cmd` shell injection.** `install_cmd` is now an
  argv list (`list[str]`) only — `subprocess.run(..., shell=True)` is
  gone. Backwards-incompatible with any marketplace entry that supplied
  a shell string (the first-party manifest does not).
- **Workspace `extra_dirs` shell injection.** Each entry must be an
  absolute path with no shell-special characters
  (`; | & ` ` $ < > ' "` newline / null). Values are still
  `shlex.quote`'d at render time.
- **Workspace `extra_env` key injection.** Keys must match
  `^[A-Z_][A-Z0-9_]*$` (the same convention used by
  `plugin_env_conf.py`); a newline or `$(...)` in the key would
  otherwise escape the rendered `export` line.
- **`casa_reload` / `config_git_commit` defense in depth.** Both tools
  now verify the calling agent's role (`origin_var` for SDK path,
  `engagement_var.role_or_type` for engagement-bridge path) and refuse
  unless it's `configurator`. Pre-fix they relied solely on each
  agent's `runtime.yaml::tools.allowed`, which is a single point of
  failure if a permissive default sneaks into a new role.
- **Telegram `/cancel` and `/complete` are originator-only.** Pre-fix
  any user in the engagement supergroup could fire either command and
  terminate someone else's engagement. Bus context now carries
  `user_id`, propagates through `origin_var` → `engagement.origin`,
  and the slash-command handler refuses unless `from_user.id` matches.
  `/silent` stays open (local to the topic). Legacy engagements with
  no `user_id` in origin still work.

### Reliability

- **`emit_completion` idempotency.** Re-emitting completion (SDK retry
  / hook misfire) is a recognised no-op now: the second call returns
  `{"status": "acknowledged", "kind": "already_terminal"}` without
  re-NOTIFYing Ellen, re-closing the topic, or re-writing the
  meta-scope summary into Honcho.
- **`delete_engagement_workspace` covers `idle`.** The live-state
  guard previously checked only `"active"`; an idle engagement
  (SDK-suspended after 24h) had its s6 service still running, but a
  non-`force` delete still tore down the workspace under it. Now both
  `active` and `idle` require `force=true`.
- **`_tail_file` follows log rotation.** The s6-log 1 MB rotation of
  `/var/log/casa-engagement-<id>/current` no longer drops the new
  file's content. Tracks `st_ino`; on inode change resets `pos = 0`.
  Also resets if the file shrinks below `pos` (truncate-in-place).
- **`ClaudeCodeDriver.start` rolls back on failure.** If
  `provision_workspace`, `write_service_dir`, `_compile_and_update`,
  or `start_service` raises, the partial workspace + s6 service dir +
  s6-rc compile are best-effort cleaned up before the original
  exception is re-raised. No more orphan UNDERGOING ghosts that the
  sweeper skips forever and that boot replay tries to resurrect.
- **Invalid `casa_tz` no longer crashes every turn.** `resolve_tz()`
  catches `ZoneInfoNotFoundError`, logs a warning naming the bad
  value, falls back to `Europe/Amsterdam`. `lru_cache` does not cache
  exceptions, which pre-fix meant every single turn re-raised.

### Tests

- 37 new regression tests covering all of the above. The live N150
  bypass for `block_dangerous_bash` is captured directly in
  `test_rm_recursive_force_all_blocked`; the rest mirror their bug
  preconditions one-for-one.

### Removed

- **`github_token` addon option.** Plugin-developer now resolves
  `op://${onepassword_default_vault}/GitHub/credential` directly at
  engagement-spawn time. Vault is configurable via
  `onepassword_default_vault`; item title (`GitHub`) and field label
  (`credential`) are conventional. One fewer addon option to configure;
  1P is the single source of truth.

### Migration

- Users with `github_token` set in addon options: remove the entry,
  then ensure your 1P vault contains a `GitHub` item with a
  `credential` field holding a GitHub PAT (`repo` scope).

## [0.14.5] - 2026-04-24

### Fixed
- **N150 turn-assistant failure after plan 4b:** `assistant/runtime.yaml`
  had `cwd: /addon_configs/casa-agent/workspace` (legacy), but F.2 dropped
  the `mkdir -p workspace/...` block from setup-configs.sh. SDK spawn
  failed with `CLIConnectionError: Working directory does not exist`.
  Change cwd to empty so B.4 agent-home fallback takes effect.

## [0.14.4] - 2026-04-24

### Removed
- **Partial `ellen/` and `tina/` default agent dirs** (created by Plan 4b B.7
  with only `plugins.yaml`). These are plan-hypothetical agents not yet
  implemented; their partial dirs failed `agent_loader` required-file check
  in CI (`missing required file runtime.yaml`). Delete cleanly — can be
  re-added when the agents are fully specified.

## [0.14.3] - 2026-04-24

### Fixed
- **N150 boot crash on v0.14.2:** `assistant/delegates.yaml` plugin-developer
  entry used `{executor_type, description, typical_task, engagement_mode}`
  shape but `delegates.v1.json` schema only accepts `{agent, purpose, when}`.
  Rewrote the entry in the correct shape. All 4 delegate entries now match
  the schema.

## [0.14.2] - 2026-04-24

### Fixed
- **N150 boot crash on v0.14.1:** `agent_loader._check_file_set` rejected
  `plugins.yaml` (added by Plan 4b B.7) as "unknown file", crashing
  casa-main with exit 1 before SDK clients spawned. Added `plugins.yaml`
  to the `optional` file set for resident, specialist, and executor tiers.

## [0.14.1] - 2026-04-24

### Added
- **plugin-developer executor** (Tier 3 claude_code driver) that authors
  Claude Code plugins in dedicated per-plugin GitHub repos. Default plugin
  pack: superpowers + plugin-dev + skill-creator + mcp-server-dev +
  document-skills. Produces 100% CC-native plugins — installable into Casa
  agents via Configurator OR into any regular CC session.
- **Two-marketplace model** — `casa-plugins-defaults` (seed-managed, read-only,
  ships with the image) + `casa-plugins` (user-writable via Configurator).
- **Binding layer** at `/opt/casa/plugins_binding.py` — resolves
  `enabledPlugins` → `plugins=[{type:"local",path:...}]` for in_casa agents
  via `claude plugin list --json::installPath`. SDK does not auto-consume
  plugins; this closes the gap.
- **Workspace-template** pattern for claude_code executors
  (`defaults/agents/executors/<type>/workspace-template/` rendered into
  every engagement workspace).
- **Seven Configurator MCP tools** — `marketplace_add_plugin` /
  `marketplace_remove_plugin` / `marketplace_update_plugin` /
  `marketplace_list_plugins` / `install_casa_plugin` (two-stage commit) /
  `uninstall_casa_plugin` / `verify_plugin_state`.
- **`casa.systemRequirements`** — tarball/venv/npm install strategies
  into `/addon_configs/casa-agent/tools/`. apt/dpkg declarations rejected
  at add-time (§4.3.2).
- **Boot-time reconciler** — idempotent, non-blocking; records status to
  `system-requirements.status.yaml`.
- **self_containment_guard** pre-push hook policy — greps for hardcoded
  non-baseline paths, "please install X manually" README strings,
  `apt install` in shell scripts.
- **Universal 1P resolver** — all password-typed addon options accept
  `op://vault/item/field`. `op` CLI installed at image build.
- **`github_token` addon option** (required for plugin-developer).
- **Self-containment axiom** (§2.0) codified — plugins fully operational
  on fresh Casa install solely by marketplace-add + install_casa_plugin.

### Removed

- `repos:` addon option + `sync-repos.sh` script. This was a half-built
  scratch-sync mechanism with no runtime consumer (§9 of Plan 4b spec).
  **Migration:** users with non-empty `repos:` entries must remove them
  from the addon config before upgrading. No data migration needed.
- `/opt/casa/claude-plugins/` symlink tree (Tier 1/2 bundled plugins).
  Replaced by seed-managed `casa-plugins-defaults` marketplace.

### Changed
- Resident SDK construction now sets `cwd=/addon_configs/casa-agent/agent-home/<role>/`
  and injects `plugins=[...]` from the binding layer. `"Skill"` added to
  `allowed_tools` automatically.
- Claude_code engagement subprocesses inherit `CLAUDE_CODE_PLUGIN_SEED_DIR=
  /opt/claude-seed` + `CLAUDE_CODE_PLUGIN_CACHE_DIR=
  /addon_configs/casa-agent/cc-home/.claude/plugins`.
- Casa-main HOME moved from `/root` to `/addon_configs/casa-agent/cc-home/`.

### Notes
- Pre-1.0 wipe-on-update doctrine — no migration code shipped.
- Plan 4b spec: `docs/superpowers/specs/2026-04-24-3.5-plan4b-plugin-developer.md`.
- Plan: `docs/superpowers/plans/2026-04-24-3.5-plan4b-plugin-developer.md`.

## [0.14.0] — Phase 3.6 — `casa-framework` MCP extraction

### Added
- `svc-casa-mcp` — new s6-rc-supervised standalone service (s6 service
  files at `etc/s6-overlay/s6-rc.d/svc-casa-mcp/`, Python entry at
  `rootfs/opt/casa/svc_casa_mcp.py`). Listens on `127.0.0.1:8100`,
  serves `POST /mcp/casa-framework` (JSON-RPC 2.0) and `POST /hooks/resolve`,
  forwards every request to casa-main over a Unix domain socket at
  `/run/casa/internal.sock`.
- Casa-main second `aiohttp.AppRunner` on the Unix socket exposing
  `POST /internal/tools/call` and `POST /internal/hooks/resolve`. New
  helper `start_internal_unix_runner()` in `casa_core.py`.
- New module `mcp_envelope.py` — JSON-RPC envelope helpers + tool schema
  translation, shared between svc-casa-mcp and the public-port fallback.
- New module `internal_handlers.py` — pure aiohttp handler factories
  bound to the Unix socket and consumed in-process by the public-8099
  fallback.
- `CASA_FRAMEWORK_MCP_URL` and `CASA_HOOK_RESOLVE_URL` env-var overrides
  for ops-time port redirection.
- E2E coverage: `test-local/e2e/test_mcp_restart_survival.sh` (D-13)
  proves bouncing casa-main does not drop engagement-subprocess MCP
  connections; new D-11 + D-12 blocks in `test_engagement.sh` exercise
  svc-casa-mcp on port 8100.

### Changed
- `drivers/workspace.py` `.mcp.json` writer now points at
  `127.0.0.1:8100/mcp/casa-framework` for newly-provisioned workspaces
  (was 8099). Existing pre-v0.14.0 workspaces unaffected.
- `scripts/hook_proxy.sh` default URL bumped from 8099 → 8100 with
  `CASA_HOOK_RESOLVE_URL` env override.
- Casa-main public port 8099 continues to serve `/mcp/casa-framework`
  and `/hooks/resolve` as a back-compat fallback for pre-v0.14.0
  workspaces. Removed in v0.14.2 or later (one-release migration).

### Removed
- `casa-agent/rootfs/opt/casa/mcp_bridge.py` — logic split between
  `mcp_envelope.py`, `internal_handlers.py`, `svc_casa_mcp.py`, and
  `casa_core.py`'s public-fallback wrappers. Net coverage unchanged.
- `tests/test_mcp_bridge.py` — coverage migrated to
  `tests/test_mcp_envelope.py`, `test_internal_handlers.py`,
  `test_svc_casa_mcp.py`, and `test_public_fallback_routes.py`.

### Notes
- Restart-survival semantics are Level 1 only: mid-restart tool calls
  return `casa_temporarily_unavailable`; the model handles retry. No
  buffering, no replay, no idempotency guarantees beyond what individual
  tool handlers already provide.
- The pre-existing v0.13.1 known limitation (per-executor hook params
  on the HTTP path use factory defaults) is unchanged in v0.14.0 — that
  wiring is a later item.

## [0.13.1] — 2026-04-23

### Added
- **MCP JSON-RPC 2.0 HTTP bridge at `POST /mcp/casa-framework`.** Real
  `claude` CLI subprocesses can now reach Casa MCP tools via the in-process
  bridge. `X-Casa-Engagement-Id` request header binds `engagement_var` for
  the tool call's duration; missing/unknown id binds `None` and tools that
  guard on engagement context return `not_in_engagement`. GET returns 405.
  Stateless (no session, no SSE). New module: `mcp_bridge.py` (244 LoC).
- **`X-Casa-Engagement-Id` header** written into per-engagement `.mcp.json`
  by `provision_workspace` so the CC CLI forwards it on every `tools/call`.
- **Workspace sweeper.** APScheduler job every 6 hours removes
  `/data/engagements/<id>/` for COMPLETED/CANCELLED engagements past
  `retention_until` (default 7 days from terminal transition).
  `_finalize_engagement` writes `.casa-meta.json` with terminal status +
  retention at terminal-transition time for claude_code driver engagements.
- **Three new MCP tools** exposed on both the SDK path and the HTTP bridge:
  - `list_engagement_workspaces(status?)` — enumerate workspaces with
    status + size, truncated at 100 entries.
  - `delete_engagement_workspace(engagement_id, force=false)` — delete
    a workspace; refuses UNDERGOING without `force=true`.
  - `peek_engagement_workspace(engagement_id, path?, max_bytes?)` —
    read-only tree listing or file read with path-traversal guard.
- **Boot-replay heal path.** When an UNDERGOING engagement's s6 service
  dir is missing and the executor type is in the registry,
  `replay_undergoing_engagements` re-renders the run + log/run scripts
  and re-plants the dir (workspace must still exist — missing workspace
  stays warn-and-skip per §7.3 of the 4a.1 spec). Missing executor →
  warn-and-skip. Takes new optional `executor_registry` kwarg.
- **MCP-blip spike harness** at `test-local/spike/mcp_blip/` — throwaway
  aiohttp server + driver script that simulates mid-`tools/call` connection
  loss to empirically classify CC's MCP client as pessimistic (retries) or
  optimistic (no retry). Runs on N150, not CI. Result feeds the
  Plan 4b / 3.6 decision.

### Changed
- **`HOOK_POLICIES` refactored** from `{name: factory_returning_HookMatcher}`
  to two-tier `{name: {"matcher": regex, "factory":
  factory_returning_HookCallback}}`. The SDK path builds the `HookMatcher`
  once, at `resolve_hooks` time; the new HTTP path reuses the same raw
  `HookCallback`. Four `_policy_*` thin-wrapper helpers dropped; four
  slimmer `_*_factory` helpers replace them.
- **`/hooks/resolve`** replaces the v0.13.0 pass-through stub with real
  policy enforcement: `block_dangerous_bash`, `path_scope`,
  `casa_config_guard`, `commit_size_guard` all produce real deny/allow
  decisions for `claude_code` engagements. Returns CC-native
  `{"hookSpecificOutput": {...}}` shape. Defensive matcher regex re-check
  before dispatch. Callback exceptions return deny, not fail-open.
- **`CASA_TOOLS`** extracted to a module-level tuple in `tools.py` for
  iteration by both the SDK server and the HTTP bridge. Adding a tool to
  the tuple exposes it on both transports automatically.

### Fixed
- **`scripts/hook_proxy.sh` port 8080 → 8099.** Casa binds on 8099; the
  stale shim would have always failed open to "casa unreachable". The
  v0.13.0 stub handler hid this bug; flipping to real enforcement without
  the port fix would have wedged all engagements behind the fail-open
  path.

### Spike findings
- (Fill in from `test-local/spike/mcp_blip/README.md` Result section
  after running the spike on the N150. Include the 1-line verdict:
  `retry_observed=yes|no`, and the ROADMAP implication for 3.6 /
  Plan 4b.)

### Known limitations
- Per-executor hook parameters (e.g. `casa_config_guard.forbid_write_paths`)
  on the HTTP path use factory defaults — the Configurator's defaults
  happen to match what that executor wants. Wiring per-executor YAML
  params into the HTTP path is a later item.

## [0.13.0] — 2026-04-23

### Added
- **Plan 4a — `claude_code` driver.** Replaces the v0.11.0 stub.
  Per-engagement s6-rc-supervised `claude` CLI process (instead of
  Casa-main child) — engagement subprocesses outlive Casa-main restarts.
  New modules: `drivers/s6_rc.py`, `drivers/workspace.py`,
  `drivers/hook_bridge.py`, `scripts/hook_proxy.sh`,
  `scripts/engagement_run_template.sh`.
- **Remote control infrastructure.** Each engagement posts its
  `--remote-control` URL to the Telegram topic when it becomes available;
  users can attach via Claude iOS app or claude.ai/code and drive the
  engagement from anywhere.
- **Tier 1 baseline plugin pack.** `superpowers@v5.0.7` bundled at
  `/opt/casa/claude-plugins/base/superpowers/`. Symlinked into every
  `claude_code`-driver engagement's isolated `$HOME` at provisioning.
- **`hello-driver` test harness executor type.** `enabled: false`;
  validates the driver lifecycle in CI via mock CLI.
- **Boot replay for UNDERGOING engagements.** `replay_undergoing_engagements`
  in `casa_core.py` sweeps orphan service dirs, recompiles the s6 db,
  starts each UNDERGOING engagement's service, and spawns URL-capture +
  respawn-poller tasks.
- **Transcript archival to Honcho keyed by executor type.** Retrofits
  the already-shipped v0.12.0 Configurator — every engagement's completion
  summary lands under peer `executor:<type>` for future "Ellen primes a
  new engagement with past lessons" (Plan 4b+).
- **`/hooks/resolve` loopback endpoint.** Routes CC hook decisions through
  Casa's `HOOK_POLICIES` registry via `hook_proxy.sh` — same policy code
  governs `in_casa` and `claude_code` executors.
- **Sensitive-env blocklist.** The per-engagement `run` script unsets
  `TELEGRAM_BOT_TOKEN` / `HONCHO_API_KEY` / `WEBHOOK_SECRET` /
  `SUPERVISOR_TOKEN` / `HASSIO_TOKEN` before spawning the CLI.
  `CLAUDE_CODE_OAUTH_TOKEN` is preserved (CLI needs it). Future sensitive
  vars must be added to this list in the same commit.

### Changed
- `ExecutorDefinition` gains four optional fields: `extra_dirs`,
  `mirror_chat_to_topic`, `archive_session_full`, `plugins_dir`.
- `engage_executor` now dispatches to the `claude_code` driver for
  `driver: claude_code` executor types instead of raising NotImplementedError.
- `_finalize_engagement` now routes `driver.cancel()` to the per-engagement
  driver based on `engagement.driver`.

### Infrastructure
- Dockerfile clones superpowers at build time (adds ~30 MB to image).
- `setup-configs.sh` pre-creates `/data/casa-s6-services/` and
  `/data/engagements/`.

### Notes
- §10.2 of the design spec — `emit_completion` landing during a Casa-main
  restart's ~30s MCP blip is a known sharp edge in v0.13.0. If Plan 4a.1
  spike-milestone-3 discovers the CLI's MCP client is optimistic (silently
  drops the call on connection loss), ROADMAP 3.6 (`casa-framework` MCP
  extraction to its own s6 service) is re-prioritized as a co-requisite
  before Plan 4b's `plugin-developer` ships. Until then: accept-the-gap.
- `/hooks/resolve` endpoint routes policies through a pass-through stub
  at the HTTP boundary; HOOK_POLICIES values are SDK HookMatcher factories
  not directly HTTP-callable. Real enforcement via in-process hook
  callbacks still works. Future iteration will ship an HTTP-native policy
  layer.
- TelegramChannel now skips InCasaDriver's resume/orphan logic for
  `claude_code` engagements (which have no `sdk_session_id`).
- **MCP HTTP bridge deferred to Plan 4a.1.** The `casa-framework` MCP server
  is currently an in-process SDK server (via `create_sdk_mcp_server`) with
  no HTTP surface. `ClaudeCodeDriver` writes `.mcp.json` pointing at
  `http://127.0.0.1:8099/mcp/casa-framework`, but that route is not yet
  implemented. The v0.13.0 infrastructure (s6-rc, workspace, boot replay,
  hook bridge, hello-driver) is fully reviewed and green in CI via the
  mock CLI, but a real `claude` CLI subprocess cannot yet reach the Casa
  MCP tools. Plan 4a.1 will add an aiohttp MCP JSON-RPC bridge at
  `/mcp/casa-framework` and propagate engagement context via an
  `X-Casa-Engagement-Id` request header so `emit_completion` /
  `query_engager` can resolve the calling engagement.

## 0.12.0 — 2026-04-??

### Added — Phase 3.5 Plan 3: UC1 Configurator

- First Tier 3 Executor type: configurator - knows Casa's configuration surface and CRUDs it via engagement topic.
- ExecutorRegistry + ExecutorDefinition + agent_loader.load_all_executors.
- executor.v1.json JSON schema.
- Three new MCP tools: config_git_commit, casa_reload (Supervisor addon restart), casa_reload_triggers(role) (in-process).
- TriggerRegistry.reregister_for(role, triggers, channels) - soft-reload primitive.
- Two new hook policies: casa_config_guard (blocks /data/, /schema/, /opt/casa/, resident deletion) and commit_size_guard (ask above N files).
- engage_executor real implementation (was stub in v0.11.0).
- TELEGRAM_BOT_API_BASE env override in channels/telegram.py - retires Plan 2's deferred e2e coverage.
- Configurator defaults at defaults/agents/executors/configurator/: definition.yaml, prompt.md, hooks.yaml, observer.yaml + 20 doctrine markdown files (~3000 lines).
- Ellen prompt updates: runtime.yaml (engage_executor allowlisted), delegates.yaml (configurator entry), prompts/system.md (Configuration requests section).
- Setup-configs.sh + test override: seed agents/executors/ subtree.
- DOCS.md: "Configurator (v0.12.0)" section.
- E2E: test_engagement.sh E-1..E-8 fleshed (Plan 2 deferred cleared) + E-9 happy path + E-10 hook-blocked.
- Manual smoke: test-local/smoke/test_configurator_engagement.sh.
- Addon option: telegram_bot_api_base (default empty).

## 0.11.0 — 2026-04-22 — Engagement primitive + Tier 2 Specialist interactive mode

### Added

- **Engagements — bounded conversational threads in a Telegram forum supergroup.**
  New addon option `telegram_engagement_supergroup_id` binds Casa to a
  dedicated supergroup; each engagement spawns a forum topic via
  `createForumTopic`. See DOCS.md "Engagements" section for setup.
- **`delegate_to_specialist(mode="interactive")`**. New branch: instead of
  one-shot sync/async invocation, opens an engagement topic where the
  specialist (e.g. Alex) works with the user turn-by-turn. Completion is
  agent-driven via the new `emit_completion` tool; the user can end early
  via `/complete` or `/cancel` in the topic.
- **`engage_executor` MCP tool** (stub — returns `kind=no_executor_types`
  until Tier 3 types land in Plan 3+). Wires Ellen for the future
  engage flow; Plan 3 fleshes out with the configurator executor type.
- **`query_engager` MCP tool** — specialist-side retrieval. Bounded LLM
  synthesis over the engager's scope-filtered memory; returns `unknown`
  when context is insufficient.
- **`emit_completion` MCP tool** — specialist-side completion funnel.
  Publishes a structured summary (`text`, `artifacts`, `next_steps`),
  closes the topic (✅ icon), writes the summary to Ellen's meta-scope
  memory, and NOTIFIES Ellen for in-main-chat narration.
- **`cancel_engagement` MCP tool** — Ellen-callable. Tears down the
  driver and finalizes the record.
- **Observer module.** Static classifier + rate limiter (3 per engagement)
  + `/silent` per-engagement override. Trigger events (errors, warnings,
  idle-detected, unknown query_engager) run a bounded haiku-class LLM
  pass that may NOTIFY Ellen to interject in the main 1:1 chat.
  Per-type YAML override arrives with Plan 3.
- **Idle + suspension scheduler.** New APScheduler job
  (`engagement_idle_sweep`, daily 08:00) emits `idle_detected` bus
  events after 3 days of no user turn (specialists; 7 days for
  executors — Plan 3+); weekly re-fire. Live SDK clients torn down
  after 24h idle with `sdk_session_id` persisted for seamless resume
  on next user turn.
- **`in_casa` driver** (full impl) and **`claude_code` driver stub**
  (raises `NotImplementedError`, Plan 5 fills in).
- **Slash commands** `/cancel`, `/complete`, `/silent` registered in the
  engagement supergroup via `setMyCommands` for in-UI discoverability.
- **Addon option** `telegram_engagement_supergroup_id` (int?, 0 = disabled).

### Infrastructure

- New `casa-agent/rootfs/opt/casa/engagement_registry.py` — mirrors
  `specialist_registry.py` pattern. Persists live records to
  `/data/engagements.json`; finished records drop from disk (Ellen's
  meta-scope memory is the durable log).
- New `casa-agent/rootfs/opt/casa/drivers/` subpackage: `driver_protocol.py`,
  `in_casa_driver.py`, `claude_code_driver.py`.
- Ellen's shipped `runtime.yaml` + `delegates.yaml` + `prompts/system.md`
  updated to explain engagements and the new tools.
- Mock Telegram Bot API server at `test-local/e2e/mock_telegram/server.py`
  used by the new `test_engagement.sh` (CI).
- Manual Telegram smoke at `test-local/smoke/test_telegram_engagement.sh`
  exercises the real Bot API; not in CI — run pre-N150 deploy.
- `.github/workflows/qa.yml` adds the engagement e2e step.

### Breaking — acceptable pre-1.0.0

- `init_tools` signature adds a new kwarg `engagement_registry`. Internal
  to Casa; no external consumers.

### Deferred

- Tier 3 executor types (configurator, ha-developer, plugin-developer)
  — Plans 3, 4, 5.
- Per-type `observer.yaml` override — Plan 3.
- `claude_code` driver implementation — Plan 5.
- `next_steps` auto-chain by Ellen — Plan 3 (no Tier 3 types to chain to yet).
- Engagement topic archival/housekeeping — Plan 6+.
- `test_engagement.sh` E-1..E-8 checkpoints — scaffolded but not functional;
  flesh in follow-up commits as `TELEGRAM_BOT_API_BASE` override lands.

### Version

- `casa-agent/config.yaml`: `0.10.0` → `0.11.0`.

## 0.10.0 — 2026-04-22 — Rename: Tier 2 "Executor" → "Specialist"

Preparation for Phase 3.5 engagement primitive + Tier 3 Executors (see
`docs/superpowers/specs/2026-04-22-3.5-engagement-and-executors.md` §10).
The "Executor" term shipped in v0.6.2 is renamed to "Specialist" to
free the name for the ephemeral, task-bounded Tier 3 agents coming in
Plan 2. Zero behavior change — pure terminology refactor.

### Breaking (acceptable pre-1.0.0)

- **Directory:** `/addon_configs/casa-agent/agents/executors/` →
  `/addon_configs/casa-agent/agents/specialists/`. Migration on first
  boot under v0.10.0 is by convention — the overlay is wipe-acceptable
  per the pre-1.0.0 doctrine. An empty `agents/executors/` directory
  is now reserved for Plan 2+ Tier 3 Executor types.
- **MCP tool:** `mcp__casa-framework__delegate_to_agent` →
  `mcp__casa-framework__delegate_to_specialist`. Tool argument key
  `agent=...` → `specialist=...`. Error kind `unknown_agent` →
  `unknown_specialist`. Ellen's shipped `runtime.yaml` tool allow-list
  updated accordingly.
- **Python imports:** `from executor_registry import ExecutorRegistry` →
  `from specialist_registry import SpecialistRegistry`. Internal to
  Casa — affects nobody outside the codebase.

### Code

- `executor_registry.py` → `specialist_registry.py` (class
  `ExecutorRegistry` → `SpecialistRegistry`).
- `agent_loader.py`: `load_all_executors` → `load_all_specialists`;
  `TIER_FILES["executor"]` → `TIER_FILES["specialist"]`; `_DELEGATE_MCP_TOOL`
  constant updated; all error messages updated; `load_all_agents` now
  skips BOTH `specialists/` (Tier 2 home) and `executors/` (reserved
  for Plan 2 Tier 3).
- `tools.py`: `delegate_to_agent` handler → `delegate_to_specialist`;
  `_executor_registry` state var renamed; `_build_executor_options` →
  `_build_specialist_options`; `_run_executor` → `_run_specialist`;
  `init_tools` signature updated.
- `casa_core.py`, `agent.py`: import updates, variable renames,
  comment sweep.
- `defaults/agents/executors/` → `defaults/agents/specialists/`
  (including `finance/`). Finance prompt and character card updated.
  Ellen's character card and `runtime.yaml` tool allow-list updated.
  `defaults/schema/agent.v1.json` meta-doc updated to match the new
  TIER_FILES key.
- `setup-configs.sh` + `test-local/init-overrides/01-setup-configs.sh`:
  seed `agents/specialists/` from defaults; reserve empty
  `agents/executors/` for Plan 2.

### Tests

- `tests/test_executor_registry.py` → `test_specialist_registry.py`.
- `tests/test_delegate_to_agent.py` → `test_delegate_to_specialist.py`.
- `tests/test_agent_loader.py`, `test_agent_process.py`, `test_config.py`,
  `test_get_schedule_tool.py`, `test_notification_handling.py`,
  `test_casa_core_agent_loading.py`: reference updates.
- `test-local/mock-claude-sdk/claude_agent_sdk/__init__.py`: comment update.
- `test-local/e2e/test_delegation.sh` → `test_specialist_delegation.sh`;
  fixture dir `test-local/fixtures/delegation-enabled/agents/executors/`
  → `agents/specialists/`. `.github/workflows/qa.yml` updated to the
  new script name.

### Freed

- `agents/executors/` is now reserved for Tier 3 Executor types, arriving
  in Plan 2 (engagement primitive). Empty in v0.10.0.

## 0.9.1 — 2026-04-22 — Drop dead pre-v0.7.0 heartbeat config

### Removed

- **`heartbeat_enabled` / `heartbeat_interval_minutes` addon options.**
  Zero runtime consumers — the global heartbeat block was removed in
  v0.7.0 (Phase 4.x refactor, replaced by per-agent
  `agents/<role>/triggers.yaml`). Since then the options have been
  visible in the HA UI but had no effect. Removed from `config.yaml`
  (both `options:` and `schema:` blocks), `DOCS.md` Features table,
  `translations/en.yaml`, `test-local/options.json.example`, and the
  `test-local/init-overrides/03-export-env.sh` export loop.
- **`e2e-slow` nightly CI job + `test-local/e2e/test_heartbeat.sh`.**
  Same v0.7.0 rot — the test referenced `defaults/webhooks.yaml` and a
  top-level `schedules.yaml`/`heartbeat:` block that no longer exist.
  Also dropped the `schedule: cron "0 4 * * *"` workflow trigger and
  the `test-slow` Makefile target. (Landed earlier today on master in
  commit `2ffa4a6`; called out here for completeness.)

### Changed

- **DOCS.md "How it works" bullet 5** rewritten from the
  global-heartbeat narrative to the current per-agent trigger
  architecture.

### Migration

- **Pre-1.0.0, no migration block.** `/addon_configs/casa-agent/` is
  wipe-acceptable; if a user had explicit `heartbeat_enabled: ...` in
  their options YAML, the HA UI will surface it as "unused option" on
  next restart and they can delete it. Nothing in the runtime depended
  on the value.

## 0.9.0 — 2026-04-21 — Phase 3.3: Scheduling v2 + builder-first config ergonomics

### Added

- **`get_schedule` framework tool** on `casa-framework` MCP server.
  Returns the caller's own upcoming interval + cron triggers as a
  markdown bullet list within a configurable `within_hours` window
  (default 24, clamped to [1, 720]). Own-role visibility only.
- **Unified `<field>_file:` prose externalization idiom.** New shared
  `_resolve_prose` helper in `agent_loader.py` reads either an inline
  YAML field or a relative markdown file under the agent dir. Applies
  `_substitute_env` so external prompts see the same env-var
  substitutions as inline strings. Path traversal + non-`.md`
  extension rejected at load time.
- **Schema support** for `prompt_file` and `card_file` alternatives
  via `oneOf` branches in `character.v1.json` and `triggers.v1.json`.
- **APScheduler hardening**: explicit timezone (`resolve_tz()`:
  `CASA_TZ` → `TZ` → `Europe/Amsterdam`), `misfire_grace_time=600`,
  `coalesce=True`, `max_instances=1`. Restart-safe and wall-clock
  correct.
- **`<current_time>` system-prompt block** — every agent turn gets an
  ISO-8601 timestamp with weekday, time-of-day, and ISO week number
  injected into the composed system prompt. Same timezone source as
  the scheduler.
- **`casa_tz` addon option** in `config.yaml`. Default
  `Europe/Amsterdam`. Propagated to Python via `CASA_TZ` env var.
- **`TriggerRegistry.list_jobs_for(role, within_hours)`** public method
  backing the tool.
- **Seeded defaults**: `assistant/prompts/system.md`,
  `butler/prompts/system.md`, `executors/finance/prompts/system.md`
  — system prompts extracted from inline. `assistant/triggers.yaml`
  gains `morning-briefing` cron at `"0 8 * * 1-5"` Europe/Amsterdam
  using `prompt_file: prompts/morning-briefing.md`.

### Changed

- `_build_character` and `_build_triggers` take `agent_dir` kwarg for
  relative-path resolution.
- `init_tools` in `tools.py` takes a new optional
  `trigger_registry` kwarg.
- Scheduler + trigger registry construction moved ahead of `init_tools`
  call in `casa_core.py` so the tool has a live registry reference.
- `_check_file_set` now skips subdirectories inside an agent dir (so
  `prompts/` doesn't trigger the unknown-file guard).

### Migration

Pre-1.0.0 doctrine: no migration script. Existing
`/addon_configs/casa-agent/agents/*/character.yaml` files using
inline `prompt:` still validate and load. Users who want to benefit
from markdown-editable system prompts can either delete the overlay
(next boot re-seeds the updated defaults) or hand-move their prompt
to `<agent>/prompts/system.md` and switch `character.yaml` to
`prompt_file:`.

## 0.8.6 — 2026-04-21 — Pre-1.0.0 migration cleanup

Codebase slimming pass. Removes every version-migration block in
`setup-configs.sh` + the matching test-mode override + the v0.8.5
existing-instance e2e scenario + a pre-2.2a lazy-migration `.pop` in
`SessionRegistry`. Net -303 lines across the branch.

Driver: **pre-1.0.0 doctrine.** Casa is in full development mode
until v1.0.0. `/addon_configs/casa-agent/` is expected to be wiped
between addon updates; breaking changes ship by updating the shipped
defaults, not by migrating user state. Migration blocks + `.applied`
markers + `.pre-vX.Y.Z.bak` backups are over-engineering at this
stage — v0.8.5 proved it: the scope-corpus migration block shipped
with v0.8.5 never fired on the N150 deploy because the overlay was
fresh on update; seed-if-missing produced an identical outcome.

Removed:
- `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh` —
  the v0.8.5 `SCOPE_MIGRATION_MARKER` block (lines 62-76),
  `migrate_default_scope()` + its two invocations (lines 83-128),
  `migrate_butler_disclosure_v2()` + invocation (lines 130-153).
  Seed-if-missing blocks retained — those are idempotent seeding,
  not migrations.
- `test-local/init-overrides/01-setup-configs.sh` — same blocks
  mirrored from prod.
- `test-local/e2e/test_migration.sh` — M-7 (v0.8.5 marker absent),
  M-9 (backup absent). Reworked M-8 → M-6 as a generic seed-content
  check (`scopes.yaml == shipped defaults` on fresh install).
- `test-local/e2e/test_migration_v085_existing.sh` — whole 68-line
  script deleted (the existing-overlay → migrate → backup scenario
  is dead code).
- `casa-agent/rootfs/opt/casa/session_registry.py` — the
  `.pop("memory_session_id", None)` in `touch()` + the matching
  docstring notes about lazy migration from pre-2.2a entries.
- `tests/test_session_registry.py::TestMigration` class.

Ship-gate doctrine saved to
`memory/feedback_ship_gate_doctrine.md` (new this session):
9-gate sequence per version bump; Monitor as the default for tests
and long-running tasks; `/ha-prod-console:*` as the first choice for
N150 interaction; pre-1.0.0 = no migrations.

Unchanged (NOT migrations):
- `executor_registry.py` orphan-recovery tombstone — runtime
  crash-recovery, not version migration.
- `log_cid.py` boot-time filter cleanup — idempotence, not
  version migration.

## 0.8.5 — 2026-04-21 — Phase 3.2.2: scope-routing hardening

Scope-routing accuracy hardening + structured `scope_route` emission.
Spec at `docs/superpowers/specs/2026-04-21-3.2.2-scope-routing-hardening.md`.

- **scopes.yaml description hardening** — Replaced the v0.8.0 prose
  corpora with comma-separated keyword phrase clusters targeting the
  7 cross-cutting probe failures the v0.8.4 sweep exposed. Generic
  only — no personal names, organizations, or place names — so the
  addon stays shippable to other households. Tenant-specific signals
  belong in the per-instance overlay at
  `/addon_configs/casa-agent/policies/scopes.yaml`, which Builder
  (Phase 3.5) is authorized to edit. The new authoring contract is
  documented as a top-of-file comment block in the defaults file
  itself.
- **`ACCURACY_BASELINE` 0.80 → 0.85** in `tests/test_scope_routing_eval.py`.
  The flat-curve finding from v0.8.4 still holds — threshold tuning is
  a no-op at this fixture scale; the gain comes entirely from the
  description corpus change.
- **Structured `scope_route` log emission.** `agent.py:455` now emits
  via `logger.info("scope_route", extra={"channel": ..., "winner": ...,
  "winner_score": ..., "second_score": ..., "threshold": ...})`. New
  `_winner_pair()` helper computes the read-side winner from the
  `scores` dict.
- **Generic `extra={...}` flow in `log_cid.py`.** `JsonFormatter` now
  flattens non-standard `LogRecord` attributes into the JSON payload;
  new `HumanFormatter` appends them as `key=val` suffix. Benefits any
  future structured log call, not just `scope_route`. New
  `STANDARD_LOGRECORD_ATTRS` constant + `_record_extras()` helper.
- **`scripts/eval_scope_dist.py` works against live N150 logs** —
  the parser was always ready for this shape; the upstream emission
  is the change that unblocks it.
- **One-shot v0.8.5 migration** in `setup-configs.sh` — refreshes
  the per-instance overlay at `/addon_configs/casa-agent/policies/scopes.yaml`
  on first boot, gated by marker file
  `migrations/scope_corpus_v0.8.5.applied`. Pre-migration overlay is
  preserved as `scopes.yaml.pre-v0.8.5.bak`. Manual edits made AFTER
  the marker is written are preserved across all later boots.
- **`ScopeRegistry.threshold` exposed as a public read-only property.**
  Was `_threshold` private; agent.py needed read access for the new
  emission. Constructor signature unchanged.
- **Tests.** New `TestExtrasFlatten` (4 cases) in `tests/test_log_cid.py`;
  new `TestScopeRouteEmission` in `tests/test_agent_process_scope.py`;
  new `TestThresholdProperty` in `tests/test_scope_registry.py`. New
  e2e scenario `test-local/e2e/test_migration_v085_existing.sh` plus
  M-7..M-9 in `test_migration.sh`. 594 unit tests green; full-mode
  accuracy gate 0.943 (baseline 0.85); all local e2e scripts green
  after Dockerfile.test infra catch-up (see below).
- **Test-infra catch-up — `test-local/Dockerfile.test` migrated to
  Debian bookworm.** The main `casa-agent/Dockerfile` switched to
  `amd64-base-debian:bookworm` in v0.8.1 when fastembed pulled
  onnxruntime (no musllinux wheel) — but the test Dockerfile was
  left on Alpine/musl, breaking the local e2e harness and
  `.github/workflows/qa.yml` CI from v0.8.1 onward. v0.8.5 mirrors
  the v0.8.1 migration recipe into the test image so e2e can run
  again. Also adds the v0.8.5 migration block to
  `test-local/init-overrides/01-setup-configs.sh` (the test-mode
  setup-configs override that replaces the bashio-dependent prod
  script) — without this the test container would skip the
  migration entirely since the prod script never runs there.

Rollback: §10 of the spec. Backup file + marker removal restore v0.8.4
runtime behaviour; reverting the formatter changes and `agent.py:455`
restore prior log shape.

## 0.8.4 — 2026-04-21 — Scope-routing evaluation harness

### Added
- `casa_eval/` framework — pluggable `Tester` ABC +
  `Suite`/`Case`/`Report`/`Failure`/`Recommendation` dataclasses, all
  JSON-round-trippable. Designed so a future Builder MCP tool can call
  the same `Tester.run()` / `Tester.sweep()` / `recommend_from_sweep()`
  surface with a thin JSON wrapper.
- `ScopeRoutingTester` — evaluates scope-routing accuracy on a labelled
  probe suite with a tunable threshold. Emits `accuracy`,
  `top2_accuracy`, `fallback_rate`, `mean_winner_score`, `mean_margin`,
  `p50_latency_ms`, `p95_latency_ms`. `optimization_axes = ["threshold"]`;
  `optimization_bounds = {"threshold": (0.20, 0.50)}`. Model is frozen
  (see CHANGELOG 0.8.2 rationale).
- `tests/fixtures/eval/scope_routing/default.yaml` — 35-case probe
  suite across the four shipped scopes. Grows by hand when Nicola spots
  a misroute in prod (`metadata.source='real-misroute'`).
- Three pytest run modes: fast (mocked `_FakeEmbedder`, always-on in
  CI); full (`CASA_REAL_EMBED=1`, asserts `accuracy >= 0.85`,
  `fallback_rate <= 0.20`); sweep (`CASA_EVAL_SWEEP=1
  CASA_REAL_EMBED=1`, informational table + recommendation).
- `scripts/eval_scope_dist.py` — audits live `scope_route` log lines,
  emits per-channel winner-score histograms (text or `--json`), flags
  channels whose winners cluster within ±0.05 of the threshold.

### Changed
- `scope_threshold` promoted from a silent env-var fallback (the
  `CASA_SCOPE_THRESHOLD` default `0.35` at `casa_core.py:427`) to a
  first-class HA addon option in `config.yaml`. Default unchanged;
  users can now tune it via the HA UI and Builder will be able to tune
  it via `supervisor.addon_options_set` in 3.5. Runtime read semantics
  at `casa_core.py:427` are untouched — the env var is now sourced
  from `bashio::config 'scope_threshold'` in
  `etc/s6-overlay/s6-rc.d/svc-casa/run`. Restart-required, matching
  every other addon option (restart cost on N150 ≈ 3 sec).

### Known limitations
- `scripts/eval_scope_dist.py` expects JSON-structured `scope_route`
  log records with `winner_score`/`second_score`/`threshold` fields.
  The live addon at v0.8.4 emits `scope_route` as a formatted-string
  log line (see `agent.py:441`) without `winner_score`, so the script
  reports "total records: 0" against unmodified production logs. A
  follow-up commit will extend the upstream emission (either JSON
  `extra=` or additional score fields in the format string) to unblock
  the audit tool. Parser logic is fully tested against synthetic logs
  and will work the moment the emission ships the expected fields.
- Measured sweep on the 35-case seed fixture shows accuracy is
  **threshold-invariant over [0.20, 0.45]** — `mean_winner_score ~= 0.787`,
  so every case sits above the entire optimization range and `argmax`
  never falls back. `recommend_from_sweep` picks 0.20 by tiebreak only;
  this is not a real improvement. `scope_threshold` stays at 0.35.
  `ACCURACY_BASELINE` was measured at 0.80 on the seed fixture (not
  0.85 as initially scoped) — raising it requires either dropping
  cross-cutting probes from the default set or hardening
  `scopes.yaml` descriptions to better differentiate
  finance/business/personal. Tracked as a 3.2.2 follow-up.

### Notes — post-deploy recipe
- Full-mode pytest on the live N150:
  `sudo docker exec addon_c071ea9c_casa-agent sh -c \
   'cd /opt/casa && CASA_REAL_EMBED=1 python3 -m pytest \
    /opt/casa/tests/test_scope_routing_eval.py::TestScopeRoutingTesterFull -v'`
  (run via `/ha-prod-console:exec` after each deploy that touches
  `scopes.yaml` descriptions or the threshold).

## 0.8.3 — 2026-04-21 — Voice-latency optimizations

### Added
- Per-process LRU cache for query embeddings in `ScopeRegistry` (256
  entries, keyed on `text.strip().lower()`). Voice retriggers and
  repeat commands are frequent — hits skip the ~90 ms ONNX forward
  pass and drop `score()` cost from ~90 ms to ~1 ms (just the cosine
  dot-products).
- `scope_route` telemetry now includes `embed_cache=N/M` where `M` is
  total calls this process has seen. Use to verify the cache is
  actually paying off after a few hours of real use.
- `ScopeRegistry.cache_stats()` returns `(hits, misses)` for tests
  and telemetry.

### Changed
- Write-path classifier now short-circuits when `owned_and_readable`
  contains exactly one scope — argmax over a single candidate is
  trivially that scope. Saves ~90 ms on every butler (voice) turn,
  since Tina only owns `house`. Assistant (3 owned scopes) still
  classifies.

### Latency impact (measured on N150 with e5-large)
- Butler voice critical path: ~90 ms → ~1 ms on cache hit
- Butler voice total per-turn overhead: ~180 ms → ~0-90 ms
  (write-path classifier removed unconditionally, read-path when
  cached)
- Assistant telegram: unchanged on first call, ~90 ms saved on any
  repeat of the same user text

## 0.8.2 — 2026-04-21 — Post-deploy hotfixes (model + trust bypass)

### Fixed
- Embedding model name — `intfloat/multilingual-e5-small` is not in
  fastembed 0.4's supported-model catalog (only `-large` ships). v0.8.1
  was silently booting in degraded mode with the "model not supported"
  error on first init. Switched `_DEFAULT_MODEL_NAME` (and the
  setup-configs pre-warm invocation) to `intfloat/multilingual-e5-large`
  so the classifier comes up non-degraded. The large variant is ~500 MB
  (vs ~200 MB for small) — still well within N150 capacity.
- Write-path trust bypass — when the channel's trust tier filters out
  every scope the agent owns (`scopes_owned ∩ readable == []`), the
  write path was falling back to `default_scope` and persisting the
  exchange into a scope the channel cannot see. Now skips the write
  entirely. Regression test
  `TestWritePath::test_write_skipped_when_owned_and_readable_empty`
  covers this. Observed in v0.8.1: webhook → assistant turn was logging
  `scope_route ... active=[house] write=personal`.

## 0.8.1 — 2026-04-21 — Debian base image (onnxruntime compatibility)

### Changed
- Base image migrated from `amd64-base-python:3.12-alpine3.22` to
  `amd64-base-debian:bookworm`. Alpine ships no `musllinux` wheels for
  `onnxruntime` (a transitive dep of `fastembed>=0.4`), forcing a
  from-source build that failed under the addon's build constraints.
  Debian/glibc pulls the prebuilt `manylinux_2_17_x86_64` wheel.
- Container Python is now 3.11 (Debian bookworm default), down from
  3.12. Casa's code uses only 3.9+ features; dev-host test suite runs
  on 3.11.9 so container Python now matches.
- Python deps installed into a virtualenv at `/opt/casa/venv` (PEP 668
  "externally managed" environment on Debian prevents direct `pip
  install` to system site-packages). The venv's `bin/` is prepended to
  `PATH` so all `python3` invocations in s6 service + setup scripts
  resolve to the venv interpreter without script changes.

### Dependencies
- Node.js 18 (Debian bookworm apt) replaces Alpine's nodejs (identical
  major version; `@anthropic-ai/claude-code` engine constraint
  `>=18.0.0` still satisfied).

### Image size
- Uncompressed image grows by ~200-350 MB (Debian base + Python stack
  larger than Alpine). No impact on the N150's 120 GB storage.

## 0.8.0 — 2026-04-20 — Phase 3.2: Domain scope runtime

### Added
- Domain scope as the authoritative memory visibility layer. Four scopes
  ship by default (`personal`, `business`, `finance`, `house`) declared in
  `/addon_configs/casa-agent/policies/scopes.yaml` with editable
  natural-language descriptions and `minimum_trust` tiers.
- `ScopeRegistry` with a local `fastembed` embedding model
  (`intfloat/multilingual-e5-small`, ~200 MB, downloaded to `/data/fastembed/`
  on first boot). Scores user text per readable scope; fan-out reads above
  threshold; end-of-turn classifies the full exchange for the write target.
- Per-scope Honcho session topology: `{channel}:{chat_id}:{scope}:{role}`.
  Per-turn telemetry line `scope_route role=... channel=... active=[...]
  write=... (t=Nms)`.
- `memory.default_scope` field in resident `runtime.yaml` (required for
  residents with `scopes_readable`; forbidden on executors).
- `channel_trust()` now returns a canonical token; `channel_trust_display()`
  preserves the human-readable form for the `<channel_context>` prompt block.

### Changed
- **Breaking (internal): memory session topology.** Pre-v0.8.0 Honcho /
  SQLite sessions (keyed `{channel}:{chat_id}:{role}`) are orphaned. Fresh
  scoped sessions accumulate from turn 1 after upgrade; prior transcripts
  remain visible in the Honcho dashboard but Casa does not read from them.
- Butler `disclosure.yaml` override shortened — `categories: {}`,
  `safe_on_any_channel` and `deflection_patterns` inherit from the shared
  `standard` policy. Scope-at-retrieval enforcement makes the confidential
  category listing redundant for Tina.
- `Agent` constructor now takes `scope_registry` as a required argument.

### Environment
- New: `CASA_SCOPE_THRESHOLD` (default `0.35`). Raise to make routing
  stricter (fewer scopes pulled per turn); lower to be more inclusive.

### Dependencies
- `fastembed>=0.4,<0.5`.

### Non-goals carried forward
- No scope-aware tool gating — 3.x follow-up.
- No legacy memory migration — cold start on upgrade.
- No remote embedding provider — local only in v0.8.x.
- No `/finance ...` user-prefix syntax.

### Deployment note
- First boot downloads the embedding model (~200 MB, ~30 s). Subsequent boots
  reuse `/data/fastembed/`. Offline first-boot degrades gracefully (fan-out
  to every readable scope) with a WARNING log.

## 0.7.0 — 2026-04-20 — Agent-definition refactor (Spec X / Phase 4.x)

### Added

- **Per-agent directory format.** Each resident and executor lives in its
  own directory under `/addon_configs/casa-agent/agents/<role>/` with
  one file per concern: `character.yaml`, `voice.yaml`,
  `response_shape.yaml`, `runtime.yaml`, and optionally
  `disclosure.yaml`, `delegates.yaml`, `triggers.yaml`, `hooks.yaml`.
  Flat `agents/<role>.yaml` files are no longer loaded.
- **Strict-mode loader** (`agent_loader.py`) with JSON Schema
  validation: unknown field / unknown file / missing required
  `schema_version` / unknown `disclosure.policy` all fail-fast at boot.
- **Shared policy library** (`policies.py`) resolves
  `disclosure.policy: <name>` references against
  `/addon_configs/casa-agent/policies/disclosure.yaml`.
- **Per-agent trigger registry** (`trigger_registry.py`) replacing the
  global heartbeat block. Residents declare their own
  `interval` / `cron` / `webhook` triggers in `triggers.yaml`.
- **`HOOK_POLICIES` registry + `resolve_hooks`** in `hooks.py`; per-agent
  hook wiring via `hooks.yaml`, resolved at `Agent.__init__`. Default
  bundle (`block_dangerous_bash` + `path_scope`) applies when the file
  is absent or empty.
- **`config_git`** module — initialises a local git repo under
  `/addon_configs/casa-agent/` and snapshots manual edits on every boot
  for free history / rollback.

### Removed (breaking)

- Flat agent YAMLs: `defaults/agents/{assistant,butler,subagents}.yaml`
  and `defaults/agents/executors/finance.yaml`.
- Global schedules/webhooks files: `defaults/schedules.yaml`,
  `defaults/webhooks.yaml`.
- All one-shot migrations from `setup-configs.sh` (`migrate_rename`,
  `migrate_memory_fields`, `migrate_voice_fields`,
  `migrate_disclosure_clause`, `migrate_scope_metadata`,
  `migrate_channels`, `migrate_executor_rename`, `migrate_mcp_allowed`)
  and their six regression test modules. Migrations are no longer
  needed — the new file format is the only format the loader
  understands.
- `config.ROLE_ALIASES`, `config._normalize_role`,
  `config.load_agent_config`, the `_build_*` helpers, and the legacy
  `name` / `personality` / `description` fields on `AgentConfig`.
- `casa_core._log_subagents_deprecation_if_present`,
  `casa_core._load_agents_by_role`, `casa_core.init_heartbeat_defaults`,
  `casa_core.build_heartbeat_message`, and the inline global heartbeat
  scheduler block.
- `hooks.AGENT_PATH_RULES`, `hooks._check_path_scope`,
  `hooks.make_path_scope_hook` (replaced by the parameterized
  `make_path_scope_hook_v2`).

### Migration

No production users — this is a hard cut. Existing installations will
find their old flat YAMLs unread; seed the new tree by deleting
`/addon_configs/casa-agent/agents/*.yaml` and letting
`setup-configs.sh` copy the bundled directory defaults on next boot.

## 0.6.2 — 2026-04-20 — Phase 3.4: disabled-executor pattern (plumbing)

### Added

- **Glob-based executor seeding.** `setup-configs.sh` now discovers
  `defaults/agents/executors/*.yaml` at first boot, seeding each to the
  user's config directory if absent. Adding a new bundled-disabled
  executor is now a single-file drop — no Casa code edit. Residents
  and top-level config files stay hand-enumerated (they are individually
  required by startup). Mirrored into
  `test-local/init-overrides/01-setup-configs.sh`.

- **`n8n-workflows` MCP server registration.** New
  `_maybe_register_n8n(mcp_registry, env=None)` helper in
  `casa_core.py`, wired into `main()` after the existing
  `homeassistant` block. When `N8N_URL` is set, registers the
  `n8n-workflows` HTTP MCP server (with `Authorization: Bearer ...`
  header if `N8N_API_KEY` is also set). Generic shared infrastructure:
  any agent (resident or executor) that declares `n8n-workflows` in
  `mcp_server_names` can reach it; per-agent tool whitelisting via
  `tools.allowed` governs which workflows each agent may actually
  invoke.

- **Executor enabled/disabled summary log.**
  `ExecutorRegistry.load()` now emits one INFO line at the tail of
  loading: `Executors: enabled=[...] disabled=[...]`. Operator
  visibility into the executor landscape and a stable grep target for
  future automation.

- **User-facing docs.** New "Enabling a bundled-disabled executor"
  section in `DOCS.md` walks users through flipping
  `enabled: false` → `true` on `finance.yaml` (or any future
  disabled-by-default executor YAML) and restarting the addon.

### Tests

- 4 new unit tests in `tests/test_n8n_registration.py` covering the
  helper's env-gated behavior (URL unset, URL set with/without API
  key, whitespace-only URL).
- 2 new unit tests in `tests/test_executor_registry.py::TestSummaryLog`
  for the summary log output (mixed state + empty state).
- New `test-local/e2e/test_delegation.sh` with three scenarios
  (D-1/D-2/D-3) proving bundled-disabled, flip-to-enabled, and
  config-not-code discovery contracts. All assertions are log-line +
  file-presence; tool-behavior contracts remain at the unit level
  (`test_delegate_to_agent.py`) because the offline mock SDK doesn't
  dispatch tool calls.

### Non-breaking

- Default-env startup is unchanged for users who don't set `N8N_URL`
  and don't edit `finance.yaml`. `finance` continues to be bundled as
  `enabled: false`.

### Deferred

- `finance`'s tool whitelist, prompt polish, and n8n workflow bindings
  ship in a separate capabilities session. Plumbing only.

## 0.6.1 — 2026-04-20 — Phase 3.1 follow-ups: role-over-name + 3.4 prerequisites

### Fixed

- **Executor role/name cleanup.** v0.6.0 shipped the Alex executor with
  `role: alex`, conflating human-facing name and functional role. Every
  other resident uses `role=<function>` (assistant, butler) with
  `name=<persona>` (Ellen, Tina). Renamed the bundled default file
  `defaults/agents/executors/alex.yaml` → `finance.yaml`, set
  `role: finance` + `name: Alex`. New one-shot `migrate_executor_rename`
  in `setup-configs.sh` moves any existing
  `/addon_configs/casa-agent/agents/executors/alex.yaml` → `finance.yaml`
  and patches the `role:` + `name:` lines. Idempotent by file existence.
  Delegation API change: `delegate_to_agent(agent="finance", ...)` is
  now the canonical invocation; `agent="alex"` returns `unknown_agent`.

### Added

- **Phase 3.4 prerequisite: MCP registry wiring in
  `_build_executor_options`.** v0.6.0 hardcoded `mcp_servers={}` for
  executor invocations, which would have left a future-enabled Alex
  with zero MCP tools. `init_tools()` now accepts an optional
  `mcp_registry: McpServerRegistry`; when passed, `_build_executor_options`
  resolves `cfg.mcp_server_names` through it. `casa_core.main()`
  passes the registry. Legacy 3-arg `init_tools` still works (degrades
  to empty mcp_servers) for test harnesses.

- **Phase 3.4 prerequisite: Ellen can now call `delegate_to_agent`.**
  The Claude Agent SDK blocks MCP tools unless explicitly whitelisted
  by their `mcp__<server>__<tool>` name. v0.6.0's bundled
  `assistant.yaml::tools.allowed` didn't list
  `mcp__casa-framework__delegate_to_agent`, so Ellen refused to invoke
  it even though the tool was registered. Added both
  `mcp__casa-framework__delegate_to_agent` and
  `mcp__casa-framework__send_message` to the bundled default. New
  `migrate_mcp_allowed` one-shot in `setup-configs.sh` backfills both
  entries into existing users' `assistant.yaml::tools.allowed`, gated
  by `# casa: mcp-tools v1` marker. Handles inline list (`allowed: [...]`)
  and block list (`allowed:\n  - ...`) forms; preserves existing entries.

### Notes

- No deployment steps beyond `ha apps update`. Both new migrations
  (`migrate_executor_rename`, `migrate_mcp_allowed`) are idempotent.
  The rename migration only fires if you upgraded to v0.6.0 and had
  the Alex executor seeded at `/addon_configs/casa-agent/agents/
  executors/alex.yaml` (which is the case for anyone who ran v0.6.0).
- Updated Ellen's personality (`Delegation:` section) to reference
  `delegate_to_agent(agent="finance")` instead of the deprecated
  "spawn Alex subagent" / "spawn automation-builder subagent" wording.
  Non-functional prose — Ellen's behaviour is governed by the tool
  surface, not the prose.

---

## 0.6.0 — 2026-04-20 — Phase 3.1: Residents, Executors, delegate_to_agent

### Added

- **Phase 3.1: Residents, Executors, `delegate_to_agent`**
  (spec `docs/superpowers/specs/2026-04-20-3.1-residents-executors-delegation.md`,
  taxonomy foundation `2026-04-20-agent-taxonomy.md`).
  - Tier 1 resident loader relaxed: any `agents/<role>.yaml` with
    non-empty `channels:` loads as a resident. No code change required
    to add new user-defined residents.
  - Tier 2 executor loader + `ExecutorRegistry` at
    `casa-agent/rootfs/opt/casa/executor_registry.py`. Scans
    `agents/executors/*.yaml`; rejects Tier-1-shaped YAMLs; honours
    `enabled: false` gating.
  - New framework tool `delegate_to_agent(agent, task, context, mode)`
    in `casa-framework` MCP. Sync-with-degradation (60s
    `asyncio.wait` — never `asyncio.wait_for`) + explicit async. Late
    completions post a bus NOTIFICATION to the delegating resident;
    Ellen's NOTIFICATION branch synthesizes a fresh turn and replies
    via the origin channel.
  - In-flight delegations persisted to `/data/delegations.json`;
    orphaned records on restart fire a synthetic "lost on restart"
    NOTIFICATION exactly once.
  - Alex ships bundled at `defaults/agents/executors/alex.yaml` with
    `enabled: false`. Becomes functional when 3.4 registers the n8n MCP.
  - YAML metadata migration: `scopes_owned` + `scopes_readable` on
    Ellen + Tina, gated by `# casa: scopes v1` marker. Fields parse at
    runtime but are **unread in v0.6.0** — scope-aware retrieval ships
    in 3.2.

### Changed

- `subagents.yaml` entries (`automation-builder`, `plugin-builder`,
  inline `alex`) are no longer loaded. Re-classified as Tier 3
  Builders (deferred spec). One-time deprecation log on startup if
  the file is present; no auto-migration.

### Fixed

- Upgrade-path regression: pre-2.1 YAMLs that went through
  `migrate_rename` (ellen.yaml → assistant.yaml / tina.yaml →
  butler.yaml) lacked a `channels:` key. Task 3's tightened Tier 1
  loader would have skipped them at startup. New idempotent
  `migrate_channels` one-shot in `setup-configs.sh` backfills
  sensible defaults (`[telegram, webhook]` for assistant;
  `[ha_voice]` for butler) on upgrade, gated by `# casa: channels v1`
  marker. Non-destructive: existing channels are preserved.

### Notes

- No deployment steps for existing N150 users beyond `ha apps update`.
  The scope-metadata and channels-backfill migrations are idempotent;
  running an older Casa after upgrading YAMLs is a no-op (extra
  fields ignored by pre-3.1 loaders).

---

## 0.5.10 — 2026-04-20 — Phase 5.7: Close public dashboard

### Fixed
- On the public hostname (`agent.oudekamp.bonzanni.casa`, addon-nginx
  `:18065`), `GET /` no longer proxies to casa-core and returns
  the dashboard HTML. It now returns a static **nginx 404** via an
  exact-match `location = /` rule placed immediately before the
  existing catch-all. No Python hop, no aiohttp handler invocation.
  The dashboard remains reachable via HA ingress on the separate
  `listen $INGRESS_PORT` server block.
- Test-infra gap from v0.5.9: `test-local/mock-claude-sdk/` was
  missing `ProcessError`. v0.5.9's resume-resilience code added a
  `ProcessError` import in `agent.py`, which unit tests tolerated
  (they resolved the real pip-installed SDK on the host) but the
  Docker e2e image crashed at container start with `ImportError:
  cannot import name 'ProcessError'`. Mock now mirrors the real
  SDK's 3-arg `(message, exit_code, stderr)` signature so tests
  that construct `ProcessError(..., exit_code=N)` work unchanged.
  Unblocks all local e2e on this release; no production impact.

### Tests
- New e2e: `test-local/e2e/test_external_surface.sh` — maps both
  ingress (`:8080`) and external (`:18065`) ports, asserts three
  outcomes:
  1. Ingress `GET /` → 200 (dashboard still alive internally).
  2. External `GET /` → 404 (new contract).
  3. External `GET /healthz` → 200 (uptime contract pinned).
- Wired into `.github/workflows/qa.yml` e2e-fast as the final step.
- `test-local/e2e/common.sh::start_container()` now accepts an opt-in
  `EXT_PORT` env var that maps a second host port to container `:18065`.
  Default behaviour unchanged — all eight pre-existing e2e scripts
  still map only `:8080`.

### Not changed
- `casa-core` aiohttp routes — `app.router.add_get("/", dashboard)`
  stays intact.
- Ingress server block — the `listen $INGRESS_PORT` block is
  untouched; HA-authenticated dashboard access works as before.
- All other external routes — `/invoke/*`, `/webhook/*`,
  `/api/converse`, `/api/converse/ws`, `/telegram/update` continue to
  match the catch-all `location /` and hit their existing gates
  (HMAC, secret token, anonymous `/healthz`).
- Nginx `/terminal/` rule on the external block — pre-existing 404
  unchanged.

---

## 0.5.9 — 2026-04-19 — Phase 5.8: SDK session resume resilience

### Added
- **`SessionRegistry.clear_sdk_session(channel_key)`** — drops only
  the `sdk_session_id` field from a registry entry; keeps
  `last_active` and `agent` intact so the session sweeper and
  downstream consumers still see the scope. Idempotent and no-op on
  missing keys.
- **`Agent._process` resume fallback.** When
  `claude_agent_sdk.ProcessError` fires on a turn that attempted to
  resume a prior SDK session (`resume_session_id` was set), Casa now:
  1. Logs a `WARNING` — `SDK resume failed (key=<k> sid=<sid>); clearing and retrying fresh`.
  2. Clears the stale `sdk_session_id` via `clear_sdk_session`.
  3. Rebuilds `ClaudeAgentOptions` with `resume=None` via
     `dataclasses.replace`.
  4. Re-runs `retry_sdk_call(_attempt_sdk_turn)` once. On success the
     fresh `sdk_session_id` is persisted via the existing `register`
     path.
  If `resume_session_id` was `None` or the fresh retry also raises
  `ProcessError`, the exception propagates to the caller — no
  infinite loop.

### Fixed
- `/data/sessions.json` persists across `ha apps rebuild` (bind-
  mounted), but the claude CLI's own session state under
  `/root/.claude/` does NOT (container-local). Every rebuild
  therefore orphaned every `sdk_session_id` recorded in
  `sessions.json`. Subsequent resume attempts crashed claude CLI
  with exit 1 + `No conversation found with session ID: <uuid>`,
  manifesting in Casa as `ProcessError` and a user-facing `sdk_error`
  persona line. Tripped by the v0.5.8 post-deploy `voice-sse` smoke
  probe (`voice:probe-scope` → butler agent Tina). The fix recovers
  transparently: the first post-rebuild turn on any stale scope
  logs one `SDK resume failed` warning and proceeds on a fresh
  session. Agent memory is unaffected (Honcho / SQLite memory is
  keyed on user peer + channel key, not `sdk_session_id`).

### Tests
- New: `tests/test_session_registry.py::TestClearSdkSession` — 4
  tests (field removal, metadata preservation, missing-key no-op,
  disk persistence).
- New: `tests/test_agent_process.py::TestResumeResilience` — 5
  tests (stale resume cleared and retried, second attempt sees
  `resume=None`, no-resume re-raises, double-ProcessError re-raises
  with cleared stale id, fallback logs a single prefixed WARNING).
- Count: 426 → 435 unit tests green.

### Not changed
- `retry.py` — stays a pure policy module. `ProcessError` remains
  classified as `ErrorKind.UNKNOWN` (not in `RETRY_KINDS`). The
  fallback runs at the outer `Agent._process` layer.
- `error_kinds.py` — no classification changes.
- `SessionSweeper` — TTL-based eviction unchanged.
- `sessions.json` persistence — unchanged shape. The fallback
  mutates entries only when it fires.
- Memory providers — unchanged. Honcho / SQLite remain orthogonal
  to `sdk_session_id`.

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-19-5.8-session-resume-resilience.md`
- Plan: `docs/superpowers/plans/2026-04-19-5.8-session-resume-resilience.md`

## 0.5.8 — 2026-04-19 — Phase 5.5: log hygiene

### Added
- **aiohttp cid middleware** in new `casa_core_middleware.py`
  (`cid_middleware`). Every inbound HTTP request now gets an 8-char
  lowercase-hex correlation id at ingress. Operators may override
  via `X-Request-Cid` header (accepts 8–32 hex chars,
  case-insensitive; uppercase normalised to lowercase; invalid shape
  silently ignored in favour of a fresh allocation). The middleware
  binds `log_cid.cid_var` with a scoped ContextVar token for the
  handler's task. `asyncio.create_task` snapshots contextvars, so
  any task the handler spawns (notably `bus.request`'s inner
  dispatch task) inherits the same cid — access-log lines, ingress
  INFO lines, and the `turn_done` budget summary all share one cid.
- **Custom aiohttp `AccessLogger`** (`CasaAccessLogger`) in the same
  module. Emits one `logger.info(...)` on the `casa.access` logger,
  which picks up Casa's installed root handler (5.2-H) — so access
  lines share the active formatter (human/JSON), carry the current
  cid, and run through `RedactingFilter`. Line format:
  `access method=<M> path=<P> status=<S> duration_ms=<D> bytes=<B>`.
  Replaces aiohttp's default CLF output (which double-stamped the
  timestamp and always logged `cid=-`). Wired into `AppRunner` via
  `access_log_class=CasaAccessLogger,
  access_log=logging.getLogger("casa.access")`.
- **Telegram `_handle` inherit-or-allocate cid** — unifies webhook
  and polling transport. When the aiohttp middleware has bound
  `cid_var` (webhook mode via `/telegram/update`), PTB's `_handle`
  inherits it via contextvars. When running in polling mode (no HTTP
  ingress), `_handle` allocates a fresh cid via `new_cid()` as
  before. Pattern:
  `cid = cid_var.get(); cid = cid if cid != "-" else new_cid()`.

### Changed
- **[BREAKING] `LOG_FORMAT` default flipped from human to JSON.**
  Unset `LOG_FORMAT` now yields JSON; any value other than `"human"`
  (case-insensitive — includes typos like `LOG_FORMAT=JSON` or
  `LOG_FORMAT=true`) also resolves to JSON. **For prior behaviour,
  set `LOG_FORMAT=human`** in the addon options. Casa does not
  consume its own logs, so no internal callers break; only operators
  reading raw `docker logs` by eye are affected. Motivation:
  operators running a log aggregator (Loki+Promtail, Vector, Fluent
  Bit) no longer need to opt-in to JSON — the default is now
  parseable out of the box.
- **Addon nginx `error_log` level `info` → `warn`** in
  `casa-agent/rootfs/etc/s6-overlay/scripts/setup-nginx.sh`. The
  "closed keepalive connection" info-spam (one line per external
  request at idle) disappears; real config errors and upstream
  timeouts still surface. This is the fix the dropped-as-YAGNI 5.6
  (NPM upstream connection reuse) was reaching for — at zero
  operational debt versus a template fork or migration-off-NPM.
- **ttyd `-d 0`** in `svc-ttyd/run`. Routine connection/session
  chatter silenced; fatal errors stay visible. Only affects
  deployments that enable the `web_terminal` addon option.
- **HTTP handler cid reads** — `webhook_handler` (`casa_core.py`),
  `invoke_handler` (`casa_core.py`), voice SSE handler
  (`channels/voice/channel.py:_sse_handler`), Telegram webhook POST
  `telegram_update_handler` — all now read `request["cid"]` instead
  of calling `new_cid()` inline. `invoke_handler` additionally
  primes `payload["context"]["cid"]` from `request["cid"]` before
  calling `build_invoke_message`, so the builder's defensive
  fallback is a no-op on the normal HTTP path.

### Infra
- **Bashio ANSI strip (defensive).** Every s6 `run`/`finish` script
  plus `setup-configs.sh`, `setup-nginx.sh`, `validate-config.sh`,
  and `sync-repos.sh` now exports `BASHIO_LOG_NO_COLORS=true` and
  `NO_COLOR=1` at the top (after the shebang, before any `bashio::*`
  call). Idempotent — re-sourcing on s6 respawn costs nothing.
  Baseline ANSI count on v0.5.7 prod was not captured this session
  (SSH-agent auth failure); defensive exports ship regardless so
  future bashio/s6 TTY-detection changes cannot reintroduce ANSI.

### Not changed
- **`bus._dispatch` cid binding** — the defensive
  `cid_var.set(msg.context["cid"])` from 5.2-H stays. It's a no-op
  when the middleware already bound the same cid; authoritative
  when a non-HTTP caller sets a different cid in `context` (e.g.
  scheduler heartbeat). Removing it would silently break non-HTTP
  cid paths.
- **`CidFilter` utility class** in `log_cid.py` remains available
  for manual LogRecord construction. Not auto-wired — the
  LogRecord factory in `install_logging` handles cid tagging at
  creation time.
- **Voice WS per-utterance cid allocation** stays manual. One WS
  connection, many utterances, one cid per utterance per the 5.2-H
  contract. The middleware allocates a connection-level cid for the
  WS upgrade request; the utterance loop then overrides per frame
  via `new_cid()`.
- **Scheduler heartbeat cid** stays. It runs in the scheduler
  task, no HTTP request, so the middleware never sees it.
  `bus._dispatch` picks up `context["cid"]` as today.
- **`build_invoke_message` defensive fallback** stays. On the
  normal HTTP path `invoke_handler` primes `payload.context.cid`
  from the middleware, so the fallback is a no-op. Non-HTTP
  callers (future) can still rely on it.

### Tests
- New: `tests/test_cid_middleware.py` (12 tests across
  `TestDefaultAllocation`, `TestHeaderOverride`,
  `TestContextVarBinding`, `TestExceptionSafety`,
  `TestSpawnedTaskInherits`) — uses Casa's existing
  `TestClient(TestServer(app))` pattern, no `pytest-aiohttp`
  dependency.
- New: `tests/test_casa_access_logger.py` (6 tests across
  `TestFormat`, `TestCidInRecord`, `TestLoggerWiring`,
  `TestJsonMode`).
- Extended: `tests/test_telegram_split.py::TestInheritOrAllocateCid`
  (2 tests — inherits pre-bound cid, allocates when default).
- Extended: `tests/test_log_cid.py::TestFormatDefaultIsJson`
  (2 tests — unset env yields JSON, `LOG_FORMAT=human` yields
  human). Existing `test_human_format_default` updated to set
  `LOG_FORMAT=human` explicitly (preserves human-format
  coverage).
- Updated: `tests/test_voice_channel_sse.py` — 3 fixture
  `web.Application()` calls now register `cid_middleware` so
  handlers see `request["cid"]`.
- Count: 403 → 425 (+22 new/extended tests).

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-19-5.5-log-hygiene.md`
- Plan: `docs/superpowers/plans/2026-04-19-5.5-log-hygiene.md`

## 0.5.7 — 2026-04-18 — Phase 5.3 infra hygiene (partial: items A + K)

### Added
- `.dockerignore` at the repo root. Excludes `**/build/`,
  `**/*.egg-info/`, `**/.eggs/`, `**/dist/`, `**/__pycache__/`,
  `**/*.pyc`, `**/.pytest_cache/`, `**/.mypy_cache/`,
  `**/.ruff_cache/`, `.spike-venv/`, `.venv/`, `venv/`, `.git/`,
  `.worktrees/`, `docs/`, `.claude/`, `.env`, `.env.*`, and
  `test-local/options.json`. Docker does NOT honor `.gitignore`, so
  host-side pip-install artifacts (seen in 5.2 item F: stale
  `test-local/mock-claude-sdk/build/lib/` COPYd into the test image
  and masked product changes) now can't poison Docker builds. This
  closes the backlog item filed during 5.2-F. Verified with
  `docker buildx build --progress=plain` on a tiny `COPY . /tmp`
  probe: **context transfer 360.33 MB → 12.22 kB** (the bulk was
  the `.spike-venv` virtualenv under `.gitignore` that Docker was
  shipping to the daemon on every build).

### Changed
- `test-local/Dockerfile.test` — `ARG BUILD_FROM` now pins the HA
  base image by sha256 digest
  (`ghcr.io/home-assistant/amd64-base-python@sha256:cb37b54…`)
  instead of the floating `3.12-alpine3.22` tag. Pinned 2026-04-18
  against HA base 2026.04.0 (`org.opencontainers.image.created
  2026-04-13`). Refresh process documented in-file via a block
  comment (`docker buildx imagetools inspect … | jq
  '.manifest.digest'`). Rationale: pre-pin, a silent HA base
  republish under the same tag could change test behaviour with no
  record in our repo; CI results stop being reproducible across
  time. Spec §4 / decision H4.
  - Scope note: production `casa-agent/Dockerfile` stays unpinned
    per spec §2 non-goal / H3. It inherits `BUILD_FROM` from the HA
    builder pipeline, which pins its own base per release.
    Re-pinning would fight the HA release machinery.

### Deferred
- **Item J — narrow AppArmor `file,` rule** (spec §3). Requires the
  complain-mode discovery loop on real Linux hardware with AppArmor
  enabled (the N150 production box). Casa's dev machine is Windows
  / Docker Desktop; kernel AppArmor is unreachable. Spec decision
  H1 explicitly warns against shipping a theoretical path list
  without the `aa-logprof` / kernel-audit capture. Left on the 5.3
  roadmap entry; no code change on this release.

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-18-infra-hygiene-5.3.md`
- No separate plan file (mechanical sweep; two edits).

## 0.5.6 — 2026-04-18 — Phase 5.2 item I: inbound rate limiting

### Added
- `rate_limit.py` — pure policy module. `TokenBucket`: single-key
  refill-on-check bucket with an injectable clock; `capacity<=0`
  short-circuits every `check()` to allowed. `RateLimiter`:
  `dict[str, TokenBucket]` with lazy bucket creation AND a
  disabled-state short-circuit so disabled limiters never grow the
  per-key dict. `RateDecision` (frozen dataclass): `allowed`,
  `should_notify` (fires on the FIRST reject after any allow — the
  signal Telegram uses for its reply-once-per-streak semantic),
  `retry_after_s`. `rate_limit_response(limiter, key)` — aiohttp 429
  helper returning `None` (allowed) or a `web.Response` with
  `Retry-After` integer-seconds header rounded up from the underlying
  bucket's `retry_after_s`.
- Three `RateLimiter` instances constructed in `casa_core.main()`:
  `TELEGRAM_RATE_PER_MIN` (default 30) keyed on `chat_id`,
  `VOICE_RATE_PER_MIN` (default 20) keyed on `scope_id`,
  `WEBHOOK_RATE_PER_MIN` (default 60) on a single shared `"global"`
  key across `/webhook/{name}` + `/invoke/{agent}` per spec §8.2.
  All three env vars via the `_env_int_or` helper from item G with
  `min_value=0`; setting the value to 0 disables the limit.
- Startup log line `Rate limits: telegram=30/min, voice=20/min,
  webhook=60/min` (values rendered as `off` when the channel's limit
  is disabled).
- Centralised `telegram.*` stub install in `tests/conftest.py` with
  canonical `_FakeNetworkError` / `_FakeTimedOut` / `_FakeTelegramError`
  classes. Previously `tests/test_telegram_reconnect.py` and
  `tests/test_telegram_split.py` each installed their own stubs with
  locally-defined exception classes — pytest's alphabetical discovery
  could let one file's classes "win" and diverge from what production
  code would catch. Now all Telegram-adjacent test files share the
  same class identities regardless of load order.

### Changed
- `TelegramChannel.__init__` gains an optional
  `rate_limiter: RateLimiter | None = None` kwarg. In `_handle`,
  immediately after deriving `chat_id` (and before `_start_typing`),
  the channel consults the limiter. On reject it drops the message
  and — only on `should_notify=True` — sends one
  `"Slow down — try again in a minute."` reply via
  `bot.send_message` (wrapped in a try/except that logs at DEBUG and
  does not raise). Pre-existing callers that don't pass a limiter
  keep unlimited behaviour.
- `VoiceChannel.__init__` gains the same `rate_limiter` kwarg. On
  SSE the handler opens a 200 SSE stream and writes one
  `event: error` with `kind=rate_limit` + persona line from
  `voice_errors["rate_limit"]` (falls back to `_DEFAULT_ERROR_LINES`);
  no `event: done` is emitted. On WS `_run_ws_utterance` sends one
  `{type:"error", utterance_id, kind:"rate_limit", spoken:…}` and
  returns — no `bus.request`, no stream open.
- `casa_core.webhook_handler` and `casa_core.invoke_handler` each
  call `rate_limit_response(webhook_rate_limiter, "global")` as the
  FIRST step (before HMAC verification). On reject returns 429 with
  JSON body `{"error": "rate_limited"}` + `Retry-After` integer
  seconds. Rationale for before-HMAC: an unauthenticated flood still
  burns zero Claude quota (the real protection) and gets throttled
  cheaply without the HMAC hash.
- `tests/test_telegram_reconnect.py` aliases its local
  `_FakeNetworkError` / `_FakeTimedOut` / `_FakeTelegramError` names
  from `sys.modules["telegram.error"]` so the exceptions it raises
  via AsyncMock `side_effect=` match the class `channels.telegram`'s
  `except NetworkError:` catches, regardless of whether its own stub
  install ran first or conftest's did.

### Tests
- `tests/test_rate_limit.py` — 16 unit tests across
  `TestTokenBucket` (8), `TestRateLimiter` (5), `TestRateLimitResponse` (3).
- `tests/test_telegram_rate_limit.py` — 6 integration tests driving
  `TelegramChannel._handle` against a fake Update: burst under cap
  reaches bus; reject emits exactly ONE reply then drops silently;
  per-`chat_id` isolation; `capacity=0` disables; pre-existing
  no-limiter callers unaffected; rejected messages don't start the
  typing indicator.
- `tests/test_voice_channel_sse.py::TestRateLimit` — 3 tests
  (exhaust+reject emits `event: error kind=rate_limit` with no
  `event: done`; `capacity=0` is unlimited; per-`scope_id` isolation).
- `tests/test_voice_channel_ws.py::TestRateLimit` — 2 tests
  (exhaust+reject emits `type:error kind=rate_limit` on the socket
  with no `type:done`; `capacity=0` is unlimited).
- `tests/test_casa_core_helpers.py::TestWebhookRateLimit` — 4 tests
  (burst-then-429 with integer `Retry-After`, global bucket shared
  across `/webhook/*` and `/invoke/*`, `capacity=0` disables, 429
  body shape).
- 403 unit/integration tests green. E2E smoke, invoke-sessions, and
  concurrency scenarios still green; "Rate limits: …" startup line
  verified in a standalone container for both the default and
  all-off paths.

### Not changed
- Bus, agent, retry, memory, session_registry, session_sweeper,
  log_cid, log_redact, mcp_registry, config, tools, channel_trust,
  telegram_supervisor, voice/{session,prosodic,tts_adapter} — all
  untouched. Rate limiting is a pre-filter at each ingress; nothing
  downstream of the bus sees the reject path.
- No dashboard row, no `/metrics` endpoint (spec §5.3 precedent
  carries to §8). Logs + HTTP status codes + the Telegram reply
  text are the operator-facing surface.
- No per-agent-role override on the webhook bucket (spec §8.2 is
  explicit: "all names and agents share one bucket").
- No persistence of bucket state across restarts. A restart resets
  all three buckets to full capacity; this is intentional —
  webhook also requires valid HMAC as primary authN; rate limit is
  defense-in-depth against accidental self-DoS + a flooded
  leaked-secret.
- No eviction of idle buckets from the per-key dict. On a
  single-user Casa the set of unique keys is bounded by real
  Telegram chats + voice devices + 1 (global webhook). Add an
  idle-bucket sweep only if the dict footprint becomes a concern.
- No E2E shell scenario. The webhook 429 path is trivially
  reproducible (`for i in $(seq 1 61); do curl -X POST …/webhook/t; done`
  → last response is 429) but faithfully replaying per-chat_id
  Telegram rate limits or per-scope_id voice rate limits from a
  shell harness is out of proportion to value. Matches item D/E/G
  precedent.

## 0.5.5 — 2026-04-18 — Phase 5.2 item G: session rotation + cleanup

### Added
- `session_sweeper.py` — `SessionSweeper`: pure async policy module
  that runs a periodic TTL sweep over `SessionRegistry`. Every 6 h
  (hard-coded per spec R5) it iterates `_data` under the 5.1 lock,
  drops entries whose `last_active` is older than
  `SESSION_TTL_DAYS` (default 30), and — for `webhook:*` entries
  whose scope_id parses as a UUID (the one-shot pattern fabricated
  by `build_invoke_message`) — applies the shorter
  `WEBHOOK_SESSION_TTL_DAYS` (default 1). Non-UUID webhook scopes
  (e.g. deliberately-pinned `webhook:ha-automation-daily`) keep the
  standard TTL. Unparseable / missing `last_active` is treated as
  garbage and evicted.
- `_prune_sdk_session()` helper — forward-compat seam: `getattr`
  lookup of `claude_agent_sdk.delete_session`; no-op when absent
  (today), one-line flip when Anthropic's SDK grows it. Exceptions
  swallowed at DEBUG — the local eviction is source of truth.
- `casa_core._env_int_or` — clamping int-from-env helper matching
  `retry._env_int`'s shape; kept local until a second caller
  appears (item I will reuse it — §9.3), then promote to `env.py`.

### Changed
- `casa_core.main()` constructs a `SessionSweeper` immediately after
  the `SessionRegistry`, using env vars `SESSION_TTL_DAYS` and
  `WEBHOOK_SESSION_TTL_DAYS`. Sweeper starts alongside the
  APScheduler and stops during the shutdown sequence — before
  `channel_manager.stop_all()` — so any in-flight sweep completes
  before the registry quiesces.

### Tests
- `tests/test_session_sweeper.py` — 18 async tests across four
  classes. `TestEvictionPolicy` (9): active survive, expired
  evicted, inclusive-keep boundary, webhook UUID → short TTL,
  webhook non-UUID → standard TTL, non-webhook ignores webhook TTL,
  unparseable `last_active` evicted, no-evictions = no-save,
  one-info-log-per-pass-with-count. `TestConcurrency` (2):
  sweep + concurrent register preserves both; lock is genuinely
  held during the eviction critical section. `TestSdkSessionPrune`
  (3): forward-compat seam called when method present, no-op when
  absent, resilient to SDK exceptions. `TestLifecycle` (4): start
  schedules recurring sweeps, stop-before-start is safe, double
  start is idempotent, stop cleanly cancels the task.

### Not changed
- `SessionRegistry` public API is untouched. The sweeper uses
  underscore-prefixed attributes (`_lock`, `_data`, `_save_locked`)
  by design — the 5.1 internal-consumer seams.
- Sweep cadence is not on the env-var surface. Spec §9.3 lists
  only the two TTL knobs; the 6-h interval is hard-coded (R5: one
  pass over < 100 entries is cheap; adding a knob expands the
  support matrix for no operator benefit).
- No E2E shell scenario — a real TTL pass is days-scale; faking
  wall-clock from the harness is out of proportion. Matches item
  E / item H precedent.

## 0.5.4 — 2026-04-18 — Phase 5.2 item E: Telegram reconnect with backoff

### Added
- `channels/telegram_supervisor.py` — `ReconnectSupervisor`: pure async
  policy module that wraps a rebuild callback with 1s → 60s jittered
  exponential backoff (reuses `retry.compute_backoff_ms`). Retries
  forever per spec §4.2. Logs exactly one `ERROR` per outage and one
  `INFO` on recovery — not one line per attempt. Coalesces concurrent
  triggers (single-task design); idempotent `start()`; clean `stop()`.
- `TelegramChannel._rebuild()` — idempotent build-and-handshake: tears
  down any existing `Application` (best-effort; exceptions swallowed)
  then constructs, initializes, starts, and registers webhook or
  polling. Replaces the inline block that used to live in `start()`.
- `TelegramChannel._health_probe_loop()` — periodic `bot.get_me()`
  probe (`_PROBE_INTERVAL = 45s`, `_PROBE_TIMEOUT = 10s`). On
  `NetworkError` / `TimedOut` / `asyncio.TimeoutError`, triggers the
  supervisor. Non-transport exceptions are logged at DEBUG and the
  probe continues.
- `TelegramChannel._on_ptb_error` — registered via
  `Application.add_error_handler`. Routes `NetworkError` and
  `TimedOut` to the supervisor; other handler errors are logged at
  WARNING without triggering a rebuild.

### Changed
- `TelegramChannel.start()` no longer silently falls back from webhook
  to polling on `set_webhook` failure (that path was dead once the
  supervisor retries forever; it also downgraded a user who explicitly
  configured webhook). On `NetworkError` / `TimedOut` during initial
  bring-up, the supervisor takes over.
- `TelegramChannel.stop()` cancels the probe task and stops the
  supervisor in addition to the existing cleanup.

### Removed
- `_POLL_STALL_THRESHOLD` constant and `_poll_stall_watchdog` method —
  the old "watchdog" only refreshed its own timestamp and performed no
  actual detection. Replaced by `_health_probe_loop`.

### Tests
- `tests/test_telegram_supervisor.py` — 11 pure-asyncio tests for
  `ReconnectSupervisor` covering trigger/no-trigger, backoff on
  failure, unbounded retry, single error log per outage, single info
  log on recovery, state reset between outages, clean stop before and
  after start, idempotent start.
- `tests/test_telegram_reconnect.py` — 6 integration tests using the
  same `telegram.*` stub pattern as `test_telegram_split.py`. Covers
  initial `set_webhook` failure, probe failure, PTB error handler
  routing, non-transport errors ignored, full-cycle teardown, and
  log-once semantics at channel level.
- `tests/test_telegram_split.py` — stub module extended with
  `NetworkError` / `TimedOut` symbols (required by the new imports in
  `channels/telegram.py`).

### Not changed
- `_TYPING_BACKOFF_*` and `_TYPING_CIRCUIT_BREAK` remain as-is —
  orthogonal to reconnect (spec §4.3).
- No new env vars — reconnect schedule is hard-coded per spec §9.3.

## 0.5.3 — 2026-04-18 — Phase 5.2 item F: token budget monitoring (descoped — no cost estimate under Max)

### Added
- `tokens.py` — pure accounting module. Exports `estimate_tokens(text)`
  (`len(text) // 4`, treats `None`/`""` as 0), `extract_usage(result_msg)`
  (defensive read of `input_tokens / output_tokens / cache_read_input_tokens /
  cache_creation_input_tokens` off the SDK `ResultMessage`; missing or
  non-numeric values default to 0), `BudgetTracker` (per-`session_id`
  consecutive-overrun streak; emits one WARNING per session_id per
  process lifetime when the digest exceeds `token_budget * 1.1` for
  three turns in a row; under-budget turns reset the streak;
  `budget <= 0` short-circuits), and `format_turn_summary(role, channel,
  usage)` (renders `turn_done role=… channel=… input=… output=…
  cache_read=… cache_write=…`; cache fields kept separate so a
  `cache_write > 0` per-turn pattern surfaces as a stable-prefix
  regression).
- `Agent` instantiates a per-instance `BudgetTracker` in `__init__` so
  assistant (4000-budget) and butler (800-budget) keep independent
  warning state. After `memory.get_context` returns successfully,
  `Agent._process` records the digest size; the broken-memory branch is
  silent (no digest to measure).
- `Agent._process._attempt_sdk_turn` now captures `ResultMessage.usage`
  via `extract_usage` (resets per attempt — partial usage from a failed
  attempt cannot leak into the summary); after `retry_sdk_call`
  returns, emits one `turn_done` INFO line carrying the role, channel
  (or `-` when missing), and input/output/cache_read/cache_write token
  counts.
- `test-local/mock-claude-sdk` — `ResultMessage` gains an optional
  `usage: dict[str, int]` field populated from `MOCK_SDK_USAGE_INPUT`,
  `MOCK_SDK_USAGE_OUTPUT`, `MOCK_SDK_USAGE_CACHE_READ`,
  `MOCK_SDK_USAGE_CACHE_WRITE` (each defaults to 0). The `build/lib`
  copy is gitignored and regenerates from `setup.py`; only the
  source-tree mock is tracked.

### Descoped from spec
- **No `cost_estimate` and no `MODEL_PRICES` table.** Casa runs on a
  Claude Max subscription — Anthropic does not bill per token, so a
  USD `cost_est` log line would be theatre against list prices we
  don't pay. Operators wanting spend modelling can do it out-of-band
  against the same `turn_done` line. Spec §5.2 wording around cost is
  therefore not implemented.

### Changed
- (none — purely additive instrumentation; no env vars new per spec
  §9.3, no dashboard surface per spec §5.3.)

### Tests
- `test_tokens.py` — 23 unit tests across 4 classes (`TestEstimateTokens`,
  `TestExtractUsage`, `TestBudgetTracker`, `TestFormatTurnSummary`).
- `test_agent_process.py::TestTokenBudgetMonitoring` — 5 integration
  tests (memory recorder per turn, broken-memory skip, three-turn
  warning fires once, turn_done line carries usage, usage resets across
  retries).
- Full unit suite: 335 passed.

## 0.5.2 — 2026-04-18 — Phase 5.2 item H: structured logging with correlation IDs

### Added
- `log_cid.py` — pure logging module. `cid_var` (contextvars), `new_cid()`
  (8-char hex), `CidFilter` (standalone utility: injects `record.cid`
  from the current context var — not auto-attached by `install_logging`,
  kept for callers that construct records manually), `JsonFormatter`
  (one-line JSON with `ts/level/logger/cid/msg[/exc]` fields),
  `_human_formatter()` (ISO UTC human format `... cid=X: msg`), and
  `install_logging()` — idempotent root-logger setup that (a) installs
  a `logging.setLogRecordFactory` wrapper which tags every record with
  `record.cid = cid_var.get()` at creation time (works for all
  loggers, including caplog, because the factory runs inside
  `Logger.makeRecord`), (b) attaches a single Casa-owned StreamHandler
  with `RedactingFilter` on the handler (not root — root-level filters
  do not fire for records from descendants). Spec 5.2 §7.
- Every ingress-built `BusMessage` carries a fresh `context["cid"]`:
  Telegram `_handle`, voice SSE + WS, webhook `/webhook/{name}`,
  `/invoke/{agent}` (`build_invoke_message`), and scheduler heartbeat
  (`build_heartbeat_message`). Caller-supplied `context.cid` in
  payloads wins so external systems can thread their own trace ids.
- Env var `LOG_FORMAT` — `json` switches root formatter to one-line
  JSON; anything else (incl. unset) uses the human format. Read at
  `install_logging()` call time.

### Changed
- `MessageBus._dispatch` sets `log_cid.cid_var` from
  `msg.context["cid"]` with a scoped token before invoking the
  handler and resets it in `finally`. Cross-task contamination is
  impossible: each dispatch runs in its own `asyncio.create_task`
  whose context is a snapshot. Messages without a cid in their
  context read as `cid=-` (backward-compat).
- `casa_core.main` logging setup — a single `install_logging()` call
  replaces the prior `logging.basicConfig(...)` +
  `addFilter(RedactingFilter())` +
  `getLogger("httpx").setLevel(WARNING)` sequence. Behaviour parity
  for the single-handler case Casa ships today: same stdout stream,
  same level, same redaction, same httpx quieting. Log format gains a
  `cid=XX` field per record. Note: `RedactingFilter` now lives on
  Casa's StreamHandler rather than the root logger (which was a
  pre-existing no-op for records from descendant loggers); future
  handlers that want redaction must attach it themselves.
- Timestamps are now ISO-UTC with `Z` suffix
  (`2026-04-18T14:32:01Z`), not the previous
  `2026-04-18 14:32:01,123`. Downstream log tooling that parses the
  old format may need an update.

### Not changed
- `Agent._process`, `retry.py`, and the memory path are untouched —
  item H is strictly a logging-layer change.
- `RedactingFilter` logic unchanged; it is re-attached to Casa's
  StreamHandler via `install_logging` alongside the new factory.
- No new dependency: `json`, `uuid`, and `contextvars` are stdlib.

## 0.5.1 — 2026-04-18 — Phase 5.2 item D: SDK retry + backoff

### Added
- `retry.py` — pure policy module. `RETRY_KINDS` (TIMEOUT, RATE_LIMIT,
  SDK_ERROR), `compute_backoff_ms()` jittered exponential backoff,
  `parse_retry_after_ms()` for server-supplied Retry-After hints,
  `retry_sdk_call()` async coroutine runner. Spec 5.2 §3.
- Env vars `SDK_RETRY_MAX_ATTEMPTS` (default 3), `SDK_RETRY_INITIAL_MS`
  (500), `SDK_RETRY_CAP_MS` (8000). Read at import time — adjust via
  add-on options + restart. Malformed or below-minimum values are
  logged and clamped, never crash module import.
- Server-supplied `Retry-After` hints are clamped at `10 * CAP_MS`
  (default 80 s) to prevent a misbehaving upstream from parking the
  worker indefinitely.

### Changed
- `Agent._process` — the `ClaudeSDKClient` turn is now wrapped in
  `retry_sdk_call`. Each attempt builds a fresh client and resets
  the streaming accumulator, so `on_token` replays cumulative text
  from scratch on retry. Cancellation (e.g. voice barge-in) bypasses
  the retry loop. Non-retryable exceptions (MEMORY_ERROR,
  CHANNEL_ERROR, UNKNOWN) surface unchanged. Spec 5.2 §3.2–§3.3.
- One `logger.warning` per retry attempt emitted via the new
  `Agent._log_retry` hook; log line carries role, attempt number,
  kind, delay_ms, exc repr.
- Internal refactor: `ErrorKind`, `_classify_error`, and
  `_USER_MESSAGES` moved from `agent.py` to a new `error_kinds.py`
  module to break an `agent ↔ retry` import cycle. `agent.py`
  re-exports them so `from agent import ErrorKind` continues to
  work unchanged for all existing consumers.

### Not changed
- Memory path is still silent-degrade (spec 2.2a §11 retained — no
  retry wrapper there per spec 5.2 §2).
- Channel modules untouched; retry is strictly at the SDK layer.
- `MAX_CONCURRENT_AGENTS` / `MAX_CONCURRENT_VOICE` seams untouched.

## 0.5.0 — 2026-04-18 — Phase 5.1: Concurrency correctness + disclosure v2

### Fixed
- `SessionRegistry` — mutate+save serialised via a single `asyncio.Lock`.
  Closes the lost-register / torn-touch race reachable since v0.2.1's
  concurrent bus dispatch. Public `save()` acquires; new internal
  `_save_locked()` assumes the lock is held. Spec 5.1 §3.
- `CachedMemoryProvider` — per-key `asyncio.Lock` with double-checked
  cache in the miss path. Concurrent cold reads on the same key now
  collapse to a single backend call; cache hits remain lock-free.
  Spec 5.1 §4.

### Changed
- `butler.yaml` default personality — layer-1 `Disclosure:` clause
  tightened with concrete per-category examples, stronger deflection
  wording aligned to the `<channel_context>` trust prefix, and an
  explicit positive list of topics safe on any channel. Spec 5.1 §5.

### Migration
- `migrate_disclosure_clause` one-shot in `setup-configs.sh` replaces
  the v1 disclosure block in existing `butler.yaml` files on upgrade.
  Gated by the trailing marker comment `# casa: disclosure v2`;
  idempotent. Mirrored into `test-local/init-overrides/01-setup-configs.sh`.
- No code-level migration for 5.1 Items A and B — the asyncio locks
  are in-memory only and take effect on next process start.

### Deferred
- `MAX_CONCURRENT_AGENTS` / `MAX_CONCURRENT_VOICE` caps — seams
  preserved (`VoiceSession.gate: Semaphore(10)`, architecture §3).
  Spec 5.1 §6.
- Layer-2 post-response disclosure backstop — beyond 5.x per spec 5.1
  §9 C7.

## 0.4.0 — 2026-04-17 — Phase 2.2b: SQLite memory drop-in

### Added
- `SqliteMemoryProvider` — durable local-storage backend for the
  3-method `MemoryProvider` ABC. Single `sqlite3` connection, WAL
  journal mode, schema versioned at `1`. Stores a thin log
  (`messages`, `sessions`, `peer_cards`); no summariser, no dialectic
  (spec §3 / S1).
- `_SqliteCtx` duck-typed wrapper so the existing `_render` produces
  `## What I know about you` + `## Recent exchanges` for SQLite without
  a second rendering code path.
- `MEMORY_BACKEND` env var — `honcho` / `sqlite` / `noop`. Resolution:
  explicit value wins; else `HONCHO_API_KEY` → honcho; else sqlite.
  Invalid values fail fast at startup. `MEMORY_BACKEND=honcho` without
  an API key also fails fast.
- `MEMORY_DB_PATH` env var — SQLite file location, default
  `/data/memory.sqlite`. Parent directory is created if missing.
- Dashboard "Memory" row now renders SQLite / Honcho / none.
- `casa_core.resolve_memory_backend_choice()` + `_wrap_memory_for_strategy()` — pure helpers lifted out of `main()` and unit-tested.

### Changed (behaviour change — documented fallout)
- Fresh installs without `HONCHO_API_KEY` now persist memory to
  `/data/memory.sqlite` by default. Previously: no memory at all. Opt
  out with `MEMORY_BACKEND=noop`.
- `CachedMemoryProvider` wrap is skipped when the backend is SQLite
  (native reads are ~1 ms; caching adds staleness and a background
  task for no measurable benefit). Butler YAMLs keep
  `read_strategy: cached` unchanged — the selector silently degrades
  to bare with a one-time INFO log at startup (spec §2 / S5).

### Migration
- None. No schema changes; no YAML changes. SQLite initialises itself
  on first open via `CREATE TABLE IF NOT EXISTS`. Switching backends
  = fresh start in the new backend (spec §7 / S7).

### Deferred
- LLM summariser (2.2c seam reserved: `_SqliteCtx.summary=None`).
- `remember_fact` tool writing to `peer_cards` (4.x).
- Export/import CLI between backends.
- Retention / pruning policy.

### Tests
- New: `tests/test_memory_sqlite.py` (schema, ensure_session, add_turn
  transactional, get_context rendering, peer_card scoping, topology
  visibility), `tests/test_memory_backend_select.py`
  (resolve + wrap policy), `tests/test_agent_process_sqlite.py`
  (`Agent._process` loop integration), `test-local/e2e/test_sqlite_memory.sh`
  (persistence across restart).
- Existing Honcho unit + integration tests still green (regression
  coverage for 2.2a).

## 0.3.0 — 2026-04-17 — Phase 2.3: voice pipeline

### Added
- `VoiceChannel` — dual ingress: generic SSE at `POST /api/converse`
  and HA-optimised WebSocket at `/api/converse/ws`. Both default on.
  Toggle with `VOICE_SSE_ENABLED` / `VOICE_WS_ENABLED`; paths override
  with `VOICE_SSE_PATH` / `VOICE_WS_PATH`. Idle eviction via
  `VOICE_IDLE_TIMEOUT_SECONDS` (defaults to `butler.session.idle_timeout`).
- `ProsodicSplitter` — delta-fed, tag-opaque sentence splitter that
  treats `[…] (…) {…} <…>` as atomic. Flushes on `.`, `!`, `?`, `…`,
  paragraph break. Safety-caps at 1.5 s / 200 chars with rightmost-
  clause-mark fallback (`,`, `;`, em-dash).
- `TagDialectAdapter` — canonical `[tag]` rewriter for three dialects:
  `square_brackets` (identity), `parens` (global `[tag]→(tag)`),
  `none` (strips leading tag atoms). Agents stay in canonical form;
  rewriting happens at the transport edge.
- `VoiceSessionPool` — process-local pool keyed on `scope_id`.
  Background sweeper evicts idle sessions every 30 s at
  `butler.session.idle_timeout`. `MAX_CONCURRENT_VOICE` gate seam
  reserved (defaults to 10 slots; 5.x hardening flips to 1).
- `stt_start` WebSocket prewarm hook — calls `memory.ensure_session`
  + `memory.get_context` on `CachedMemoryProvider` so the first
  utterance lands on a warm cache. Dedup'd against repeated
  `stt_start` frames for the same scope.
- Persona-voice error lines per `ErrorKind` in `butler.yaml`
  (`voice_errors:` block with `timeout`, `rate_limit`, `sdk_error`,
  `memory_error`, `channel_error`, `unknown`). Rendered through
  `TagDialectAdapter`. Empty string = silent degrade.
- Channel-supplied error hook: `channel.emit_error_line(kind, context,
  cfg)` duck-typed method. `Agent.handle_message`'s error branch
  prefers it over plain-text delivery when present. Non-voice
  channels (Telegram) unchanged.
- `TTSConfig` on `AgentConfig` (`tts.tag_dialect`, default
  `square_brackets`).
- Dashboard row for Voice channel status (transports + on/off).

### Changed
- `MessageBus.request()` now propagates caller cancellation to the
  dispatch task (previously: only the caller's future was cancelled,
  and downstream handlers kept running). Required for voice cancel /
  barge-in semantics (spec §10.2). Backward-compatible — all existing
  bus tests still green.
- `MessageBus._dispatch` resolves the handler per-message from
  `self.handlers[name]` rather than capturing it at `run_agent_loop`
  startup. Enables dynamic handler reconfiguration at the cost of a
  dict lookup per dispatch; same-loop asyncio keeps the lookup safe.

### Migration
- `setup-configs.sh` one-shot: injects `tts:` and `voice_errors:`
  blocks into existing `butler.yaml` if absent. Idempotent. Mirrored
  in `test-local/init-overrides/`.

### Deferred to 5.x hardening
- `MAX_CONCURRENT_VOICE=1` enforcement (seam reserved via
  `VoiceSession.gate`).
- Voice-ID promotion (`voice_speaker → nicola` peer when HA voice-ID
  matures).
- Personality hot-reload.
- Concurrent-cold-key dedup in `CachedMemoryProvider`.

### Tests
- 62 new unit/integration tests (config, migration, splitter, adapter,
  pool, SSE, WS) + 2 new E2E scenarios (SSE smoke + WS smoke under
  Docker). Full voice+agent suite: 192 passed at merge.

## 0.2.2 — 2026-04-17 — Phase 2.2a: Honcho v3 memory redesign

### Changed (breaking for pre-release users)
- `MemoryProvider` is now a 3-method ABC: `ensure_session`, `get_context`,
  `add_turn`. The pre-v3 `store_message` / `create_session` /
  `close_session` surface is removed.
- `HonchoMemoryProvider` rewritten for the honcho-ai 2.1.x peer/session
  model. The pre-v3 apps/users API is gone; `.initialize()` is no
  longer needed (v3 is lazy).
- Agent YAML `memory` block: `peer_name` and `exclude_tags` are
  removed; `read_strategy` (`per_turn` | `cached` | `card_only`) is
  added. Existing user YAMLs are migrated on first boot.
- `SessionRegistry.register()` no longer takes `memory_session_id`
  (the Honcho session is derived from `{channel_key}:{role}`).
  Existing `sessions.json` entries are migrated on first write.

### Added
- `CachedMemoryProvider` — warm cache + background refresh wrapper for
  the voice path; default `read_strategy` for `butler`.
- `<channel_context>` block in every system prompt, so agents can
  condition disclosure on the ingress channel's trust level.
- Personality baselines for `assistant` and `butler` include a
  disclosure clause referencing `<channel_context>`.

### Internal
- `voice_speaker` peer for unauthenticated voice ingress; `nicola`
  peer for authenticated channels (Telegram, webhook). Future
  voice-ID can upgrade a recognised speaker's attribution without
  touching agent code.
- Storage is unconditional: write-side filtering is gone (spec §4.3).
  Visibility is enforced by session/peer topology; disclosure is
  enforced by the agent on the output side.

## 0.2.1

- Fix bus serialisation: `MessageBus.run_agent_loop` no longer awaits
  each handler inline. Each message is now dispatched via
  `asyncio.create_task`, so concurrent `/invoke` calls to a single agent
  run in parallel instead of queuing behind one another. Handler
  exceptions are logged and REQUEST callers receive an error response
  instead of hanging until the 300 s timeout.
- Test-only: added offline mock `claude_agent_sdk` package and
  Dockerized E2E suite under `test-local/e2e/`. The mock replaces the
  real SDK inside the test image so runtime tests can run without an
  OAuth token. E2E suite covers smoke, YAML migration scenarios,
  `/invoke/{agent}` session isolation, heartbeat delivery, and
  concurrent dispatch.

## 0.2.0

- Fix heartbeat silent failure: scheduled ticks now use `channel: scheduler`
  and resolve to a valid session key (`build_session_key` rejects empty
  channels, which previously swallowed every tick).
- Fix dashboard startup race: a request landing on `/` between HTTP server
  start and scheduler init no longer raises `UnboundLocalError` on
  `heartbeat_enabled` / `heartbeat_interval`.
- Fix `/invoke/{agent}` session collision: each invocation gets a distinct
  `chat_id` (caller-supplied via `context.chat_id` or a fresh UUID),
  replacing the shared `webhook:default` session key.
- Harden agent-YAML migration: the migration script now force-sets the
  canonical role on rename (no longer assumes the legacy role value) and
  strips CR first so YAMLs saved with CRLF line endings migrate cleanly.
- Pin Python runtime dependencies.

## 0.1.22

- Role-based agent refactor. Agent YAML filenames and internal identifiers
  now use structural roles (`assistant`, `butler`) instead of display names
  (Ellen, Tina). Display names remain configurable via
  `primary_agent_name` / `voice_agent_name` and are used for personality
  text and the dashboard.
- Session keys formalised as `{channel}:{scope_id}` via
  `build_session_key()`.
- One-shot migration on boot: `agents/ellen.yaml` -> `agents/assistant.yaml`
  (with `role: main` -> `role: assistant` and `peer_name` update);
  `agents/tina.yaml` -> `agents/butler.yaml`.

## 0.1.3

- Add Tina (voice agent) wiring in core startup
- Add APScheduler heartbeat with configurable interval
- Add webhook endpoints (`/webhook/{name}`, `/invoke/{agent}`) with HMAC verification
- Add Telegram message splitting for responses over 4096 characters
- Add error classification with structured user-facing messages
- Add log redaction filter for secrets and tokens
- Make SessionRegistry I/O async (non-blocking)
- Add explicit sys.path management for reliable imports
- Add `apparmor: true` and `url` to config.yaml
- Add store assets: DOCS.md, CHANGELOG.md, translations, icons

## 0.1.2

- Add safety hooks: dangerous command blocking and per-agent path scope enforcement
- Add Honcho memory provider with async SDK wrapper
- Add MCP server registry (HTTP and SDK-based servers)
- Add session registry with JSON persistence
- Add unit tests for all core modules

## 0.1.1

- Add asyncio message bus with priority queues and request/response pattern
- Add channel abstraction with Telegram implementation (python-telegram-bot v20+)
- Add agent config loading with YAML, env var substitution, and model resolution
- Add Ellen agent config with personality prompt and tool permissions

## 0.1.0

- Initial add-on scaffold
- Dockerfile with Python 3.12 Alpine base, Node.js, nginx, ttyd
- S6-overlay init scripts: config validation, default setup, nginx generation
- S6 services: casa (Python core), nginx (ingress proxy), ttyd (web terminal)
- AppArmor profile
- Multi-repo workspace sync script
- Local Docker testing setup with mock Supervisor
