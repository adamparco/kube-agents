"""Unit tests for the dependency-deadlock repair installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The behavioural tests run against a miniature of the real schema and, crucially,
against a copy of the *real* gating predicate that ``claim_task`` and
``recompute_ready`` share. A repair that leaves a card unclaimable is no repair
at all, so every test asserts on ``claimable()`` rather than on link rows.

A board carries an event log as well as tasks and links, because the repair
decides what it may touch from the order of the ``created`` and ``claimed`` rows
in ``task_events``: an edge is a fan-out of this card's run only if the child
was created after the card first started. ``fanned_out`` and ``planned`` spell
the two histories that matter — the worker that creates its own children, and
the planner that lays out a pipeline before anything runs.
"""

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apply_kanban_dependency_repair import ANCHOR, PATCHED, RELATIVE, apply
from kanban_dependency_repair import (
    EVENT_KIND,
    find_deadlocked_children,
    repair_inverted_dependencies,
)

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER
);
"""


def board(tasks, links=(), log=()):
    """A miniature board. ``log`` is the ``task_events`` history, in order.

    The log is inserted before the links so its ids are the low ones, matching
    a real board where an edge is written in the same transaction as the
    ``created`` event that records it.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for tid, status in tasks.items():
        conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", (tid, status))
    for task_id, kind in log:
        conn.execute(
            "INSERT INTO task_events (task_id, kind) VALUES (?, ?)", (task_id, kind)
        )
    for parent, child in links:
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent, child),
        )
    return conn


def fanned_out(parent, *children):
    """The history a worker writes: claimed first, then it creates its cards."""
    return [(parent, "created"), (parent, "claimed")] + [
        (child, "created") for child in children
    ]


def planned(*task_ids):
    """The history a planner writes: every card exists before anything is claimed."""
    return [(task_id, "created") for task_id in task_ids]


def claimable(conn, task_id):
    """The gate ``claim_task`` and ``recompute_ready`` both apply, verbatim."""
    undone = conn.execute(
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1",
        (task_id,),
    ).fetchone()
    return undone is None


def links_of(conn):
    return {
        (r["parent_id"], r["child_id"])
        for r in conn.execute("SELECT parent_id, child_id FROM task_links")
    }


def repair_events(conn, task_id):
    """The decoded payloads of whatever this module wrote about ``task_id``."""
    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, EVENT_KIND),
        )
    ]


class RepairTest(unittest.TestCase):
    def test_the_reported_deadlock_is_broken(self):
        """t_ab112f5b's shape: three cards parented to the card that waits on them."""
        conn = board(
            {"P": "running", "A": "todo", "B": "todo", "C": "todo"},
            [("P", "A"), ("P", "B"), ("P", "C")],
            log=fanned_out("P", "A", "B", "C"),
        )
        for child in ("A", "B", "C"):
            self.assertFalse(claimable(conn, child), f"{child} starts deadlocked")

        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A", "B", "C"])

        for child in ("A", "B", "C"):
            self.assertTrue(claimable(conn, child), f"{child} must now dispatch")
        self.assertFalse(claimable(conn, "P"), "P must wait for its prerequisites")

    def test_waiting_card_becomes_claimable_once_children_finish(self):
        conn = board(
            {"P": "running", "A": "todo"}, [("P", "A")], log=fanned_out("P", "A")
        )
        repair_inverted_dependencies(conn, "P")
        self.assertFalse(claimable(conn, "P"))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'A'")
        self.assertTrue(claimable(conn, "P"), "the wait resolves on its own")

    def test_settled_children_are_left_alone(self):
        """A finished child already satisfies the gate; inverting it would un-satisfy P."""
        conn = board(
            {"P": "running", "A": "done", "B": "archived"},
            [("P", "A"), ("P", "B")],
            log=fanned_out("P", "A", "B"),
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertTrue(claimable(conn, "P"))
        self.assertEqual(repair_events(conn, "P"), [])

    def test_legitimate_continuation_card_is_untouched(self):
        """Upstream's endorsed idiom: create a child, then complete yourself.

        The repair fires only from ``block_task``, so a creator that completes
        instead of blocking never reaches it and the continuation card runs on
        the original edge. ``ApplierTest`` pins the call site; this pins that the
        idiom needs no repair to work.
        """
        conn = board(
            {"P": "running", "K": "todo"}, [("P", "K")], log=fanned_out("P", "K")
        )
        self.assertFalse(claimable(conn, "K"))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'P'")
        self.assertTrue(claimable(conn, "K"))

    def test_real_fan_in_is_not_disturbed(self):
        """P waits on A and B the correct way round: nothing to repair."""
        conn = board(
            {"P": "running", "A": "todo", "B": "running"},
            [("A", "P"), ("B", "P")],
            log=planned("A", "B", "P") + [("B", "claimed"), ("P", "claimed")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertFalse(claimable(conn, "P"))
        self.assertTrue(claimable(conn, "A"))

    # --- the run window: only cards this card fanned out are repaired --------

    def test_a_pipeline_stage_keeps_its_direction_when_it_blocks(self):
        """The bug this module shipped with, reproduced against the live engine.

        A is done, B was claimed, B -> C. Nothing here is a fan-out of B's run:
        C was planned before B ever started, so B's dependency block is about
        something else and the pipeline has to survive it. The parent-set guard
        this replaces said B had no unsettled parents — true of every card
        ``block_task`` accepts — and inverted B -> C, dispatching C ahead of the
        stage it exists to follow.
        """
        conn = board(
            {"A": "done", "B": "running", "C": "todo"},
            [("A", "B"), ("B", "C")],
            log=planned("A", "B", "C") + [("A", "claimed"), ("B", "claimed")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "B"), [])
        self.assertEqual(links_of(conn), {("A", "B"), ("B", "C")})
        self.assertFalse(claimable(conn, "C"), "C must still wait its turn")
        self.assertEqual(repair_events(conn, "B"), [])

    def test_a_fan_in_keeps_its_direction_after_one_input_finishes(self):
        """The same false positive in the fan-in shape.

        Once B is done it stops gating C, so "is this card the last unfinished
        parent" says yes and A's edge was inverted. C predates A's run, so the
        window says no.
        """
        conn = board(
            {"A": "running", "B": "done", "C": "todo"},
            [("A", "C"), ("B", "C")],
            log=planned("A", "B", "C") + [("B", "claimed"), ("A", "claimed")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "A"), [])
        self.assertEqual(links_of(conn), {("A", "C"), ("B", "C")})

    def test_a_card_that_was_never_claimed_repairs_nothing(self):
        """No ``claimed`` row means no run window, so the comparison is NULL.

        A raw ``UPDATE tasks SET status='running'`` is not evidence that the card
        fanned anything out. verify_kanban_scheduling.py builds this board.
        """
        conn = board(
            {"P": "running", "A": "todo"},
            [("P", "A")],
            log=[("P", "created"), ("A", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])

    def test_a_fan_out_from_a_run_that_died_is_still_repaired(self):
        """The watermark is the first claim, not the current one.

        A worker that creates A and is then killed leaves it as deadlocked as one
        that blocks. When the card is re-claimed, creates B and blocks for real,
        both children have to be released — matching ``claim_task``'s own
        ``started_at = COALESCE(started_at, ?)``, which likewise records the
        first start and not the latest.
        """
        conn = board(
            {"P": "running", "A": "todo", "B": "todo"},
            [("P", "A"), ("P", "B")],
            log=[
                ("P", "created"),
                ("P", "claimed"),
                ("A", "created"),
                ("P", "reclaimed"),
                ("P", "claimed"),
                ("B", "created"),
            ],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A", "B"])

    def test_find_deadlocked_children_ignores_children_older_than_the_run(self):
        conn = board(
            {"P": "running", "OLDER": "todo", "NEWER": "todo"},
            [("P", "OLDER"), ("P", "NEWER")],
            log=planned("P", "OLDER") + [("P", "claimed"), ("NEWER", "created")],
        )
        self.assertEqual(find_deadlocked_children(conn, "P"), ["NEWER"])

    def test_a_second_repair_finds_nothing_to_do(self):
        """The first pass turned the edge around, so the second finds no successor."""
        conn = board(
            {"P": "running", "A": "todo"}, [("P", "A")], log=fanned_out("P", "A")
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])

    # --- inverting must actually free the child -----------------------------

    def test_child_with_another_unfinished_parent_is_left_alone(self):
        """Releasing one of two blockers frees nothing, so do not rewrite the edge."""
        conn = board(
            {"P": "running", "OTHER": "todo", "SINK": "todo"},
            [("P", "SINK"), ("OTHER", "SINK")],
            log=planned("P", "OTHER") + [("P", "claimed"), ("SINK", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertFalse(claimable(conn, "SINK"))

    def test_child_whose_other_parents_are_finished_is_repaired(self):
        conn = board(
            {"P": "running", "OLD": "done", "SINK": "todo"},
            [("P", "SINK"), ("OLD", "SINK")],
            log=planned("P", "OLD") + [("P", "claimed"), ("SINK", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["SINK"])
        self.assertTrue(claimable(conn, "SINK"))

    def test_mixed_graph_repairs_only_the_deadlocking_edges(self):
        """One board, all three reasons to leave an edge alone.

        DONE is settled, SHARED has another unfinished parent, and PRIOR was
        planned before P ever ran. Only A is a deadlocked fan-out of P's run.
        """
        conn = board(
            {"P": "running", "A": "todo", "DONE": "done", "SHARED": "todo",
             "OTHER": "todo", "PRIOR": "todo"},
            [("P", "A"), ("P", "DONE"), ("P", "SHARED"), ("OTHER", "SHARED"),
             ("P", "PRIOR")],
            log=planned("P", "OTHER", "PRIOR")
            + [("P", "claimed"), ("A", "created"), ("DONE", "created"),
               ("SHARED", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        self.assertTrue(claimable(conn, "A"))
        self.assertEqual(
            links_of(conn),
            {("A", "P"), ("P", "DONE"), ("P", "SHARED"), ("OTHER", "SHARED"),
             ("P", "PRIOR")},
        )

    # --- the cycle probe ----------------------------------------------------

    def test_cycle_is_refused_when_the_new_edge_is_the_one_that_closes_it(self):
        """T -> X -> C and T -> C, X settled. Inverting T->C would close a loop.

        This is the shape the probe exists for, and the only one that produces a
        cycle: the edge being *inserted* is C -> T, so what matters is whether T
        can still reach C after T -> C is dropped. Here it can, via X.

        Everything else passes first, which is what makes the probe load-bearing
        — C was created inside T's run and its only other parent (X) is ``done``,
        so C is genuinely deadlocked on T alone.
        """
        conn = board(
            {"T": "running", "X": "done", "C": "todo"},
            [("T", "X"), ("X", "C"), ("T", "C")],
            log=planned("T", "X") + [("T", "claimed"), ("C", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "T"), [])
        self.assertEqual(
            links_of(conn),
            {("T", "X"), ("X", "C"), ("T", "C")},
            "graph restored unchanged",
        )
        payload = repair_events(conn, "T")[0]
        self.assertEqual(payload["skipped_would_cycle"], ["C"])
        self.assertEqual(payload["inverted"], [])

    def test_a_pre_existing_cycle_through_the_gated_child_is_repaired(self):
        """P -> A -> M -> P, M ``done``. Inverting P->A breaks the loop, not makes one.

        The old probe walked from the child, which answered a question about the
        edge being deleted rather than the one being inserted, and refused this
        repair. Nothing here closes a loop: after P->A is dropped, P reaches
        nothing, so A -> P is safe and A becomes claimable.
        """
        conn = board(
            {"P": "running", "A": "todo", "M": "done"},
            [("P", "A"), ("A", "M"), ("M", "P")],
            log=planned("M", "P") + [("P", "claimed"), ("A", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        self.assertEqual(links_of(conn), {("A", "M"), ("M", "P"), ("A", "P")})
        self.assertTrue(claimable(conn, "A"))
        payload = repair_events(conn, "P")[0]
        self.assertEqual(payload["inverted"], ["A"])
        self.assertEqual(payload["skipped_would_cycle"], [])

    def test_a_card_is_never_a_fan_out_of_its_own_run(self):
        """A self-link cannot pass the window: a card is created before it is claimed.

        ``link_tasks`` refuses ``parent_id == child_id`` outright, so this only
        arrives through raw SQL, and the window turns it away before the cycle
        probe has to.
        """
        conn = board({"P": "running"}, [("P", "P")], log=fanned_out("P"))
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])

    def test_reachability_walk_terminates_on_a_pre_existing_cycle(self):
        """n0..n49 is a ring. The walk must not spin looking for P.

        The ring is ``done`` apart from n0, so the last-unfinished-parent test
        lets n0 through — the point of the test is the 50-hop traversal.
        """
        tasks = {f"n{i}": "done" for i in range(50)}
        tasks["n0"] = "todo"
        tasks["P"] = "running"
        links = [(f"n{i}", f"n{i + 1}") for i in range(49)]
        links += [("n49", "n0"), ("P", "n0")]
        log = planned(*[f"n{i}" for i in range(1, 50)])
        log += [("P", "created"), ("P", "claimed"), ("n0", "created")]
        conn = board(tasks, links, log=log)
        # P is not reachable from the ring, so the edge inverts.
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["n0"])

    # --- the event ----------------------------------------------------------

    def test_no_children_writes_nothing(self):
        conn = board({"P": "running"}, log=fanned_out("P"))
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertEqual(repair_events(conn, "P"), [])

    def test_event_records_the_repair(self):
        conn = board(
            {"P": "running", "A": "todo"}, [("P", "A")], log=fanned_out("P", "A")
        )
        repair_inverted_dependencies(conn, "P", reason="waiting on cluster audits")
        payload = repair_events(conn, "P")[0]
        self.assertEqual(payload["inverted"], ["A"])
        self.assertEqual(payload["reason"], "waiting on cluster audits")

    def test_long_reason_is_clipped(self):
        conn = board(
            {"P": "running", "A": "todo"}, [("P", "A")], log=fanned_out("P", "A")
        )
        repair_inverted_dependencies(conn, "P", reason="x" * 5000)
        self.assertEqual(len(repair_events(conn, "P")[0]["reason"]), 200)

    def test_find_deadlocked_children_ignores_settled(self):
        conn = board(
            {"P": "running", "A": "todo", "B": "done", "C": "blocked"},
            [("P", "A"), ("P", "B"), ("P", "C")],
            log=fanned_out("P", "A", "B", "C"),
        )
        self.assertEqual(find_deadlocked_children(conn, "P"), ["A", "C"])


# A stand-in for block_task's dependency branch with the same indentation as
# upstream, so the applier's ast.parse guard is exercised for real.
PREAMBLE = (
    "def block_task(conn, task_id, kind, reason, expected_run_id=None):\n"
    "    with write_txn(conn):\n"
    '        if kind == "dependency":\n'
    "            cur = conn.execute(SQL)\n"
    "            if cur.rowcount != 1:\n"
    "                return False\n"
    "            run_id = _end_run(conn, task_id)\n"
)
EPILOGUE = "            )\n            return True\n"


class ApplierTest(unittest.TestCase):
    """The applier is the thing that fails the build, so exercise it directly."""

    def _tree(self, body):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return root, target

    def test_applies_and_stays_parseable(self):
        root, target = self._tree(PREAMBLE + ANCHOR + EPILOGUE)
        apply(root)
        out = target.read_text()
        self.assertIn("_kanban_repair_inverted_deps(conn, task_id, reason)", out)
        self.assertIn("from hermes_cli.kanban_dependency_repair import", out)
        ast.parse(out)

    def test_call_precedes_the_event(self):
        root, target = self._tree(PREAMBLE + ANCHOR + EPILOGUE)
        apply(root)
        out = target.read_text()
        self.assertLess(
            out.index("_kanban_repair_inverted_deps"),
            out.index('"dependency_wait"'),
            "the repair must land before the event that reports the wait",
        )

    def test_missing_anchor_fails_the_build(self):
        root, _ = self._tree(PREAMBLE + EPILOGUE)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_missing_anchor_does_not_write(self):
        body = PREAMBLE + EPILOGUE
        root, target = self._tree(body)
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), body)

    def test_duplicate_anchor_fails_the_build(self):
        root, _ = self._tree(PREAMBLE + ANCHOR + ANCHOR + EPILOGUE)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_missing_target_file_fails_the_build(self):
        with self.assertRaises(SystemExit):
            apply(Path(tempfile.mkdtemp()))

    def test_the_patched_text_still_contains_the_anchor(self):
        """Which is exactly why counting the anchor cannot detect a re-run."""
        self.assertIn(ANCHOR, PATCHED)

    def test_a_second_run_is_refused_instead_of_stacking_the_call(self):
        """Replayed against the running gateway's kanban_db.py, the unguarded
        applier exited 0 three times in a row and left three copies of the call
        and three trailer imports behind.
        """
        root, target = self._tree(PREAMBLE + ANCHOR + EPILOGUE)
        apply(root)
        once = target.read_text()
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), once, "a refused run must not write")


if __name__ == "__main__":
    unittest.main()
