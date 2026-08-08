"""Keep a kanban worker on the board when its turn ends without a terminal call.

The contract and the hole
-------------------------
A dispatcher-spawned worker must finish with ``kanban_complete`` or
``kanban_block``. Upstream Hermes already knows this and guards it: at the end
of ``run_conversation``'s *no-tool-calls* branch it calls
``build_kanban_stop_nudge`` and, if the worker is about to exit without a
terminal call, appends a synthetic user turn and continues the loop
(``agent/conversation_loop.py``, "Kanban worker terminal-tool stop guard").

That guard sits in one branch of one loop. Every other way out of the loop is a
bare ``break`` several hundred lines earlier, and each one jumps straight over
it. The worker then exits ``rc=0`` having never touched the board, and the
dispatcher's reaper stamps a ``protocol_violation`` after the fact — a failure
with no explanation in it, because by the time it is detected the reason is
gone.

What that cost us on 2026-08-07
-------------------------------
Card ``t_d3efabef`` ("Evaluate Config Connector Setup Patterns") burned four
runs and 38 minutes producing nothing. Every run ended the same way::

    ⚠️ Tool guardrail halted web_search: loop_web_search_cap

``ToolCallGuardrailController._check_loop_cap`` blocks the 51st ``web_search`` of
a turn, ``_guardrail_block_result`` records the decision on the agent, and
``conversation_loop.py`` breaks out of the tool-call branch. ``failed`` stays
``False``. The model never sees the block result, never gets another turn, and
never learns that it is one sentence away from a clean completion — it had 173
successful searches in hand.

The aggravating half is that the 50-search cap is documented as *per turn* and
resets in ``reset_for_turn``, but a non-goal-mode kanban worker is a single
``run_conversation`` for the whole card. For those workers the per-turn cap is
really a per-card research budget, and a genuine research card blows through it.
That part is addressed in ``agents/platform/config.yaml``; this module addresses
the exit path, which is the defect. Raising the cap alone would only move when
the bug fires.

The two fixes here
------------------
1. **Make the halt kanban-aware** (``guardrail_halt_nudge``). At the halt site
   the decision is visible and ``messages`` is still live and mutable, which is
   what makes it the only workable seam. Reuse upstream's own nudge helper — it
   is already bounded at two attempts and already returns ``None`` for
   non-workers, so non-kanban sessions are untouched by construction — and add
   the one thing the stock text does not say: the tool is gone for this run,
   stop trying to use it.

   The caller must clear ``agent._tool_guardrail_halt_decision`` before
   continuing. ``ToolCallGuardrailController.reset_for_turn`` clears it once per
   *turn*, not per iteration, so leaving it set makes the next iteration
   re-enter the halt branch and break anyway.

   The halt is re-armable and the search counter is still at the cap, so a model
   that ignores the instruction and searches again will re-halt and spend
   another nudge. Two nudges bounds that at three halts per run, after which
   today's behaviour resumes. Do **not** paper over it by resetting
   ``_turn_web_search_count`` — that hands out another 50 searches.

2. **Backstop every other exit** (``should_record_missing_terminal`` /
   ``record_missing_terminal_call``). ``finalize_turn`` is the single funnel
   every ``break`` passes through, so one check there covers the halt path and
   the six siblings that leak identically: ``all_retries_exhausted_no_response``,
   ``partial_stream_recovery``, ``fallback_prior_turn_content``,
   ``empty_response_exhausted``, ``local_processing_error`` and
   ``error_near_max_iterations``. Even the guarded ``text_response`` path leaks
   once its two nudges are spent. All of them leave ``failed=False`` with a
   non-``text_response`` exit reason and no board write.

   This converts an unexplained ``protocol_violation`` into a counted
   ``timed_out`` that names the exit reason in its payload, using the same
   ``_record_task_failure`` call the iteration-budget path directly above it
   already uses.

Three exclusions matter, and all of them are load-bearing:

* **Goal mode.** ``_run_kanban_goal_loop_q`` calls ``run_conversation`` once per
  turn, so ``finalize_turn`` runs many times for one card and intermediate turns
  legitimately end without a terminal call. The goal loop has its own bounded
  turn budget and its own ``block_fn``. Recording a failure on turn 1 of N would
  release the claim and close the run underneath it.

* **Cron runs.** ``cronjob(action='run')`` executes the job in-process inside the
  dispatching worker, and ``tools/cron_run_scope.py`` deliberately leaves
  ``HERMES_KANBAN_TASK`` pointing at the dispatcher's card for the duration —
  ``heartbeat_current_worker_from_env`` reads it on every kanban call to keep a
  15-minute claim alive across a 20-minute audit. So the env var this backstop
  keys off names a card the cron run is explicitly *barred* from terminating
  (``resolve_default_task_id`` returns ``None`` inside a run, and
  ``cron_ownership_violation`` rejects it by name). Its not having called a
  terminal tool is the design, not a leak. Record it and the dispatcher — still
  blocked on the run — has its claim released and a ``timed_out`` charged to its
  failure budget by its own child.

* **The task's own status.** ``session_called_kanban_terminal`` reads
  ``messages``, which compaction can rewrite. The board is the authority: if the
  card is no longer ``running``, a terminal tool already moved it and there is
  nothing to record. This is what makes a false positive impossible rather than
  merely unlikely.

What is deliberately *not* excluded is the halt nudge. It fires during a cron run
too — ``kanban_stop_nudge_enabled()`` is on whenever ``HERMES_KANBAN_TASK`` is
set, and upstream interpolates that id straight into the text — so the run is
told by name to complete a card it will be refused. That wastes at most two
turns and is upstream's behaviour in its own stop guard either way. The reason to
leave it is that ``_in_cron_run`` can only over-report: the marker's environment
half is process-wide, so a second worker running concurrently with somebody
else's dispatch reads it as its own. For the board write that direction is free
(skip a record, never write a wrong one); for the nudge it would silence the fix
on an unrelated worker, which is the whole thing this module exists to prevent.
"""

from __future__ import annotations

import os

# Mirrors ``agent.kanban_stop._DEFAULT_MAX_ATTEMPTS``. Duplicated rather than
# imported so this module stays importable on a host without Hermes, which is
# what lets the tests run outside the image.
DEFAULT_MAX_NUDGES = 2

OUTCOME = "timed_out"

DETECTOR = "kanban_guardrail_exit"

_HALT_SUFFIX = (
    "\n\n[System: `{tool}` is exhausted for the rest of this run "
    "({code}) — every further call will be blocked, so do not try again. "
    "Write up what you already have and call `kanban_complete`, or call "
    "`kanban_block` if what you have is genuinely unusable.]"
)


def guardrail_halt_nudge(build_nudge, *, messages, attempts, decision):
    """Return the follow-up for a kanban worker whose turn a guardrail halted.

    ``build_nudge`` is ``agent.kanban_stop.build_kanban_stop_nudge``, injected so
    this module never imports Hermes. It already returns ``None`` when the guard
    should not fire — not a kanban worker, a terminal tool was already called, or
    the nudge budget is spent — and those are exactly the cases where the caller
    should fall through to the original ``break``.

    Returns ``None`` on any failure. A broken nudge must never wedge a worker;
    the old exit path is always the safe answer.
    """
    try:
        base = build_nudge(messages=messages, attempts=attempts)
    except Exception:
        return None
    if not base:
        return None
    tool = getattr(decision, "tool_name", None) or "that tool"
    code = getattr(decision, "code", None) or "tool guardrail"
    return base + _HALT_SUFFIX.format(tool=tool, code=code)


def missing_terminal_error(turn_exit_reason) -> str:
    """The ``last_failure_error`` text, which names how the turn actually ended."""
    return (
        "worker ended without a terminal kanban call "
        f"(turn_exit_reason={turn_exit_reason})"
    )


#: The marker ``tools/cron_run_scope.py`` holds for the duration of a dispatched
#: run, as a context variable and an environment variable both. Named here only
#: for the fallback below; ``current_cron_job`` is the reader that matters.
CRON_RUN_ENV = "HERMES_KANBAN_CRON_RUN"


def _in_cron_run() -> bool:
    """Whether this turn is a cron job borrowing a worker's environment.

    Delegates to ``cron_run_scope.current_cron_job`` so the *context variable*
    is consulted first. That distinction is the point: ``cron_run_scope`` sets
    both halves of the marker, and the environment half is process-wide, so with
    ``dispatch_in_gateway`` several workers share it. Only the context variable
    is scoped to the thread ``run_job`` submitted the run on.

    Reading the env var directly is the fallback for a host where neither module
    resolves — the tests, mostly, since ``cron_run_scope`` is a sibling there.

    Answers ``True`` if the reader raises. Every uncertain answer in this module
    means "do not write to the board", and this is the same rule stated from the
    other side.
    """
    try:  # In the image, /opt/hermes is on sys.path.
        from tools.cron_run_scope import current_cron_job
    except Exception:
        try:  # Local unittest discovery: the patches dir is top-level.
            from cron_run_scope import current_cron_job
        except Exception:
            return bool(os.environ.get(CRON_RUN_ENV))
    try:
        return bool(current_cron_job())
    except Exception:
        return True


def should_record_missing_terminal(
    *,
    task_id,
    interrupted,
    failed,
    iteration_limit_fallback,
    messages,
    session_called_terminal,
    goal_mode=None,
    cron_run=None,
) -> bool:
    """Whether ``finalize_turn`` is about to leak a kanban worker.

    ``session_called_terminal`` is
    ``agent.kanban_stop.session_called_kanban_terminal`` — a **module function**,
    not a method on the agent. ``goal_mode`` defaults to reading
    ``HERMES_KANBAN_GOAL_MODE`` from the environment, and ``cron_run`` to
    ``tools.cron_run_scope.current_cron_job()``.

    Every uncertain answer is ``False``. This function decides whether to write a
    failure to a live board, so anything it cannot prove is a leak is not one.
    """
    if not task_id:
        return False
    if interrupted or failed or iteration_limit_fallback:
        # Interrupts are the caller's business, ``failed`` turns already exit
        # nonzero, and the iteration-budget path records its own failure
        # immediately above this check.
        return False
    if cron_run is None:
        cron_run = _in_cron_run()
    if cron_run:
        # task_id here is the DISPATCHER's card, inherited: this run has none of
        # its own and may not terminate that one. See the docstring.
        return False
    if goal_mode is None:
        goal_mode = os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1"
    if goal_mode:
        return False
    try:
        if session_called_terminal(messages):
            return False
    except Exception:
        return False
    return True


def task_is_still_running(conn, task_id: str) -> bool:
    """Whether the board still shows this card as ``running``.

    ``kanban_complete`` moves it to ``done`` and ``kanban_block`` to ``blocked``,
    so anything else means a terminal tool ran and the transcript check was
    wrong — most plausibly because compaction dropped the tool call out of
    ``messages``.
    """
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    try:
        status = row["status"]
    except (TypeError, IndexError, KeyError):
        status = row[0]
    return str(status or "") == "running"


def record_missing_terminal_call(
    *,
    task_id: str,
    turn_exit_reason,
    connect,
    record_failure,
) -> bool:
    """Charge a leaked exit to the card's failure budget. Returns whether it did.

    ``connect`` is ``hermes_cli.kanban_db.connect`` and ``record_failure`` is
    ``hermes_cli.kanban_db._record_task_failure``, both injected. The
    ``release_claim`` / ``end_run`` pair is the same one the iteration-budget
    path uses: the card is still ``running`` with an open run, and this hands
    both back.

    ``event_payload_extra`` reaches only the ``gave_up`` event, on the attempt
    that trips the breaker — the per-attempt ``timed_out`` event carries a fixed
    ``{error, failures}`` payload. So the exit reason has to travel in the error
    *text* to be legible on every attempt, which is what
    ``missing_terminal_error`` is for; the structured copy is a convenience for
    the one event a human is most likely to read.
    """
    conn = connect()
    try:
        if not task_is_still_running(conn, task_id):
            return False
        record_failure(
            conn,
            task_id,
            error=missing_terminal_error(turn_exit_reason),
            outcome=OUTCOME,
            release_claim=True,
            end_run=True,
            event_payload_extra={
                "turn_exit_reason": str(turn_exit_reason),
                "detector": DETECTOR,
            },
        )
        return True
    finally:
        try:
            conn.close()
        except Exception:
            pass
