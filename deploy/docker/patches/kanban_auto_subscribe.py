"""Give a kanban worker's child cards the chat subscription of its own card.

Installed into the image at ``/opt/hermes/tools/kanban_auto_subscribe.py`` and
wired into ``tools/kanban_tools.py`` (the ``kanban_create`` handler) by
``deploy/docker/patches/apply_kanban_auto_subscribe.py``.

The gap, measured
-----------------
The gateway notifier only sees cards with a ``kanban_notify_subs`` row. Two
things write those rows at creation time, and both missed the worker case on
the 2026-08-07 live run (12:58):

* ``_maybe_auto_subscribe`` reads the originating chat identity
  (``HERMES_SESSION_CHAT_ID`` / ``_THREAD_ID`` / ``_PLATFORM``) from session
  context that exists only on inbound user messages. The coordinator card
  ``t_b9659077`` was created in-session by the chat agent and got its row; the
  four cards the coordinator created *as a dispatcher-spawned worker* (three
  sleep tasks and the synthesizer ``t_f54bd6b5``) had no session context and
  got nothing.
* Upstream ``create_task`` does inherit subscriptions — but only from the
  explicit ``parents=[...]`` graph edges (``_inherit_notify_subs``). A
  worker's own card is deliberately not a graph parent of the work it fans
  out (``parents=[<itself>]`` is the self-parenting deadlock the
  ``kanban_dependency_repair`` patch exists to untangle), so the chain from
  the user's thread to the fan-out is severed at the first worker.

Result: the synthesizer's answer sat undelivered for 91.3s until the user
asked after it — ``gateway.log`` shows zero Slack sends between 12:59:02 and
13:02:15. The manual remedy,
``agents/platform/scripts/kanban_notify_propagate.py``, relies on the worker
remembering to run it; it didn't.

The fix
-------
:func:`maybe_inherit_worker_subscriptions` runs right after
``_maybe_auto_subscribe`` in the ``kanban_create`` tool handler. When the
creating process is a kanban worker (``HERMES_KANBAN_TASK`` set, non-empty,
and not the new card itself — the dispatcher pins it into every worker), the
new card inherits every subscription row of the card the worker is running.
So the user's thread follows the work wherever a worker fans it out, one hop
at a time: coordinator → sleep tasks → synthesizer, each inheriting from the
card that created it.

Same contract as the manual script it automates: the copied column set is
``platform, chat_id, thread_id, user_id, notifier_profile`` (also exactly
what upstream ``_inherit_notify_subs`` copies), ``INSERT OR IGNORE`` on the
subscription primary key makes it idempotent — the script remains in place
as a manual/back-fill tool and double-writes are harmless — ``last_event_id``
resets to 0 (a fresh card has no terminal events to replay; the notifier
claims only terminal kinds), and ``created_at`` is re-stamped.

Fail-soft throughout: any exception is logged and swallowed. A notification
bookkeeping failure must never fail the ``kanban_create`` the worker is
mid-flight on — the same posture ``_maybe_auto_subscribe`` itself takes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Union

logger = logging.getLogger(__name__)

#: The subscription columns copied parent -> child. ``task_id`` is rewritten,
#: ``created_at`` re-stamped, ``last_event_id`` reset to 0. Matches both the
#: manual propagate script and upstream ``_inherit_notify_subs`` so a schema
#: drift shows up here as a clear error instead of a silently wrong copy.
COPY_COLUMNS = ("platform", "chat_id", "thread_id", "user_id", "notifier_profile")

#: The env var the dispatcher pins into every worker it spawns: the id of the
#: card the worker is running. Presence (with a different id than the new
#: card) is the definition of "this create came from a worker".
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"


def inherit_subscriptions(
    conn_or_db_path: Union[sqlite3.Connection, str, "os.PathLike[str]"],
    child_task_id: str,
    parent_task_id: str,
) -> int:
    """Copy ``kanban_notify_subs`` rows from parent to child. Fail-soft.

    Returns the number of rows written (0 on any failure, no-op, or when the
    child already has them — idempotent via ``INSERT OR IGNORE`` on the
    ``(task_id, platform, chat_id, thread_id)`` primary key). Never raises.

    Refuses to write for a child card not on the board, for the reason the
    manual script documents: the notifier unsubscribes only when a task turns
    terminal, so a row for a card that does not exist would be scanned on
    every notifier tick for the life of the board.
    """
    try:
        if not child_task_id or not parent_task_id:
            return 0
        if child_task_id == parent_task_id:
            return 0

        own_conn = not isinstance(conn_or_db_path, sqlite3.Connection)
        if own_conn:
            # `timeout=` IS the busy timeout; the gateway and CLI write this
            # same board, and a nudge lost to a busy DB is a lost delivery.
            conn = sqlite3.connect(str(conn_or_db_path), timeout=10)
        else:
            conn = conn_or_db_path
        try:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (child_task_id,)
            ).fetchone()
            if row is None:
                logger.warning(
                    "kanban_auto_subscribe: child card %r not on this board; "
                    "refusing to write an uncollectable subscription row",
                    child_task_id,
                )
                return 0
            cols = ", ".join(COPY_COLUMNS)
            cur = conn.execute(
                f"""
                INSERT OR IGNORE INTO kanban_notify_subs
                    (task_id, {cols}, created_at, last_event_id)
                SELECT ?, {cols}, ?, 0
                  FROM kanban_notify_subs
                 WHERE task_id = ?
                """,
                (child_task_id, int(time.time()), parent_task_id),
            )
            written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            # kanban_db connections run autocommit with explicit BEGIN
            # IMMEDIATE for multi-statement writes; a plain connect() opens a
            # deferred txn on the INSERT instead. Commit whichever applies.
            if getattr(conn, "in_transaction", False):
                conn.commit()
            if written:
                logger.info(
                    "kanban_auto_subscribe: %s inherited %d subscription "
                    "row(s) from %s",
                    child_task_id, written, parent_task_id,
                )
            return written
        finally:
            if own_conn:
                conn.close()
    except Exception as exc:  # noqa: BLE001 — never break the create
        logger.warning(
            "kanban_auto_subscribe: inherit %r -> %r failed (continuing): %r",
            parent_task_id, child_task_id, exc,
        )
        return 0


def maybe_inherit_worker_subscriptions(conn, child_task_id: str) -> int:
    """Inherit from the worker's own card when this process is a worker.

    The single call the patched ``kanban_create`` handler makes. A process
    with no ``HERMES_KANBAN_TASK`` (chat session, CLI, cron) is left exactly
    as upstream — ``_maybe_auto_subscribe`` and ``_inherit_notify_subs``
    already cover those paths. Never raises.
    """
    try:
        parent = (os.environ.get(WORKER_TASK_ENV) or "").strip()
    except Exception:
        return 0
    if not parent or parent == child_task_id:
        return 0
    return inherit_subscriptions(conn, child_task_id, parent)
