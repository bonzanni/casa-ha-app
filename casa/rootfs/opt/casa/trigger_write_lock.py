"""One lock serialising every writer of a role ``triggers.yaml`` (#458).

``config_sync`` reconciles ``agents/<role>/triggers.yaml`` with a
read → decide → write that runs on a WORKER THREAD (``reload.reload_config_sync``
hands ``config_sync.run`` to ``asyncio.to_thread``). The reminder tools write the
same file on the EVENT LOOP. #403 removed the *agent* writers of that file but
left this cross-thread pair: a ``set_reminder`` that lands between the sync's
read and its write is discarded by the write, and is absent from the pre-sync
git snapshot / ``.casabak`` too, so it is unrecoverable — the same lost-update
#403 described, on a different writer.

Two review rounds tried to lock each of ``config_sync``'s write sites and each
round found another (the two reconcile loops, then ``_post_sync_validate_and_heal``);
the write goes through both ``_copy``/``shutil`` and ``atomic_write_text``, so
there is no single chokepoint to guard. This module cuts that: ONE lock, held by
``config_sync`` across the WHOLE pass and taken by every loop-side mutator around
its read → write, so no per-site coverage can be missed and a future write site
inside the pass is covered for free.

Contract, load-bearing:

* ``config_sync.run`` holds ``PASS_LOCK`` for the entire reconcile pass.
* Every ``reminders`` mutator (``add_entry`` / ``remove_entry`` / ``upsert_entry``
  / ``delete_entry``) takes ``PASS_LOCK`` around its own read → write.
* Because the mutators BLOCK on the lock, a caller ON THE EVENT LOOP must invoke
  them via ``asyncio.to_thread`` — never directly — or a held pass would stall
  the loop. Callers off the loop (boot, tests) may call them directly.

The lock is process-local. The boot run of ``config_sync`` is a separate process
with no reminder writer, so it is uncontended there; the live-reload pass is the
only contended case, and the one this closes.
"""
from __future__ import annotations

import threading

# A single lock for the whole triggers.yaml-writing surface. Not per-path: the
# reconcile pass is sequential and spans every role, so per-path locking buys no
# real concurrency and reintroduces the "did every site take the right lock?"
# fragility this module exists to remove.
PASS_LOCK = threading.Lock()
