"""Let a kanban board mutation wake its watchers instead of waiting out a poll.

Installed into the image at ``/opt/hermes/hermes_cli/kanban_wake_nudge.py`` and
wired into ``hermes_cli/kanban_db.py`` (producers) and
``gateway/kanban_watchers.py`` (consumers) by
``deploy/docker/patches/apply_kanban_wake_nudge.py``. It lives in
``hermes_cli`` rather than ``gateway`` because both sides must import it and
the dependency only points one way: the gateway already imports
``hermes_cli.kanban_db`` freely, while the CLI/worker processes never import
``gateway``.

The problem, measured
---------------------
Both gateway watchers are pure polls. The dispatcher ticks every
``kanban.dispatch_interval_seconds`` (5s on the live cluster; the sleep loop
at the bottom of ``_kanban_dispatcher_watcher``) and the notifier every 5s
(the ``interval`` default on ``_kanban_notifier_watcher``, spawned with no
args from ``gateway/run.py``). So every hop of a task chain — card created →
dispatcher claims it; card completed → notifier delivers and children promote
— pays an average of half a tick and up to a whole one, for work that took
microseconds to commit.

Measured on the 2026-08-07 live run: 4.9s for the coordinator card to be
claimed, 2.2s for the synthesizer, plus notify delays, on a request whose
whole round trip was 87s. None of that latency bought anything; the rows were
sitting committed in SQLite the entire time.

The nudge
---------
Workers run in separate processes from the gateway, so an in-process event is
useless — the signal has to cross process boundaries and survive none of the
participants sharing anything but the board file. The cheapest primitive with
those properties is a sibling file: :func:`record_wake` bumps the mtime of
``<db_path>.wake`` (derived from the board DB path, never hardcoded — every
board gets its own) after the mutations that give a watcher something to do:

* ``create_task`` commit — a claimable card exists;
* ``complete_task`` (after its ``recompute_ready``) — a terminal event to
  deliver, and possibly children promoted to ``ready``;
* ``unblock_task`` commit — a card returned to the claimable pool.

Deliberately NOT hooked: claims, heartbeats, and everything else the
dispatcher itself writes during a tick — hooking those would let the
dispatcher wake itself and turn the 0.25s reaction slice into a busy loop.

Consumers construct one :class:`WakeMonitor` per watcher loop and replace
their fixed sleep with :func:`wait_interval`, which dozes in ≤0.25s slices and
breaks early when the wake file's mtime moves. Reaction to a nudge is
therefore ≤0.25s, while the full-interval scan remains the unconditional
fallback — the wake file is an accelerator, never a correctness dependency.

Failure posture
---------------
Everything here fails towards upstream behaviour, and that is a tested
property, not an aspiration. ``record_wake`` swallows every exception (a
read-only filesystem must not fail the board write it rides on);
``WakeMonitor.changed`` returns False when the file cannot be statted; and
``wait_interval`` with a dead monitor is exactly the sleep loop it replaced.
The mtime is bumped with an explicit ``time.time_ns()`` value so two nudges
inside one coarse filesystem-timestamp tick still compare unequal.

Multi-board note: each watcher monitors the wake file of the board DB that
``kanban_db_path()`` resolves for it (the ``HERMES_KANBAN_DB`` pin on the
cluster, which is also what the dispatcher injects into every worker).
Mutations on any *other* board still land within one full interval, exactly
as upstream.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Optional, Union

#: Longest a watcher dozes between wake-file checks. The reaction ceiling for
#: a nudge, and the cost ceiling for an idle board: one stat() per slice.
SLICE_SECONDS = 0.25

_WAKE_SUFFIX = ".wake"


def wake_path(db_path: Union[str, "os.PathLike[str]"]) -> str:
    """Return the wake-file path for a board DB path.

    Always derived, never fixed: ``/opt/data/kanban.db`` →
    ``/opt/data/kanban.db.wake``, and a test board in a tmp dir gets its wake
    file in that same tmp dir.
    """
    return str(db_path) + _WAKE_SUFFIX


def record_wake(db_path: Union[str, "os.PathLike[str]"]) -> bool:
    """Bump the wake file next to ``db_path``. Fail-soft, cheap, atomic.

    Returns True when the nudge landed, False when it could not — and never
    raises: this runs on the tail of successful board writes, and a full disk
    or read-only filesystem must degrade to plain polling, not fail the write
    the caller already committed.

    The mtime is set to an explicit ``time.time_ns()`` pair rather than left
    to the filesystem clock so that two nudges within one coarse timestamp
    granule still produce distinct ``st_mtime_ns`` values.
    """
    try:
        if db_path is None or not str(db_path):
            # str(None) is a real (relative!) filename; refuse rather than
            # scatter "None.wake" files wherever the process's cwd happens
            # to be.
            return False
        path = wake_path(db_path)
        now_ns = time.time_ns()
        try:
            os.utime(path, ns=(now_ns, now_ns))
        except FileNotFoundError:
            # First nudge for this board. open("ab") is create-if-missing and
            # race-tolerant: two concurrent creators both succeed.
            with open(path, "ab"):
                pass
            os.utime(path, ns=(now_ns, now_ns))
        return True
    except Exception:
        return False


def record_wake_for_conn(conn) -> bool:
    """:func:`record_wake` against the board an open connection writes to.

    The producer hooks in ``kanban_db`` only have ``conn`` in scope, so the
    path is read back from SQLite itself (``PRAGMA database_list``) — which
    also guarantees the nudge lands next to the file the mutation actually
    went to, whatever mix of ``HERMES_KANBAN_DB`` / board slug resolved it.
    Fail-soft like everything else here: an in-memory database (no file) or a
    closed connection is a False, never an exception.
    """
    try:
        for row in conn.execute("PRAGMA database_list"):
            # (seq, name, file) — index access works for tuples and
            # sqlite3.Row alike.
            if row[1] == "main":
                db_file = row[2]
                if db_file:
                    return record_wake(db_file)
                return False
        return False
    except Exception:
        return False


class WakeMonitor:
    """Watches one board's wake file for nudges.

    ``db_path`` may be the path itself or a zero-arg callable returning it
    (the watchers pass ``kanban_db.kanban_db_path`` uncalled, so a resolver
    error degrades this monitor instead of killing the watcher coroutine).

    ``changed()`` is edge-triggered: True exactly once per observed mtime
    move, so a burst of nudges between two polls costs one early scan, not
    one per nudge. The first call after construction returns False unless the
    file changed *since* construction — the monitor reports news, it does not
    replay history.
    """

    def __init__(
        self,
        db_path: Union[str, "os.PathLike[str]", Callable[[], object], None],
    ) -> None:
        self._path: Optional[str] = None
        try:
            resolved = db_path() if callable(db_path) else db_path
            if resolved is not None:
                self._path = wake_path(resolved)
        except Exception:
            self._path = None
        self._last = self._stat()

    def _stat(self) -> Optional[int]:
        if self._path is None:
            return None
        try:
            return os.stat(self._path).st_mtime_ns
        except OSError:
            return None
        except Exception:
            return None

    def changed(self) -> bool:
        """True iff the wake file moved since the last observation."""
        current = self._stat()
        if current == self._last:
            return False
        self._last = current
        return True


async def wait_interval(
    monitor: Optional[WakeMonitor],
    interval: float,
    running: Optional[Callable[[], object]] = None,
) -> bool:
    """Doze up to ``interval`` seconds, waking early on a recorded nudge.

    Drop-in for the watchers' fixed sleep loops: sleeps in ≤0.25s slices,
    returning early — True — when ``monitor.changed()``, and True after the
    full interval otherwise. Returns False iff ``running()`` went falsy, so a
    caller that used ``if not self._running: return`` keeps its snappy
    shutdown (``running`` is checked before every slice, exactly where the
    upstream loops checked it).

    A monitor that is None, half-constructed, or raising is treated as "no
    nudge ever": the wait then IS the upstream fixed sleep.
    """
    try:
        seconds = float(interval)
    except (TypeError, ValueError):
        seconds = 5.0
    if seconds <= 0:
        # Upstream floors its sleeps too (the notifier at 1s per slice, the
        # dispatcher at interval >= 1.0); a zero interval here would turn the
        # watcher into a hot loop. One slice is the smallest honest wait.
        seconds = SLICE_SECONDS
    deadline = time.monotonic() + seconds
    while True:
        if running is not None and not running():
            return False
        try:
            if monitor is not None and monitor.changed():
                return True
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(SLICE_SECONDS, remaining))
