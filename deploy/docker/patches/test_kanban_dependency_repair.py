"""Unit tests for the dependency-deadlock repair installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The behavioural tests run against a miniature of the real schema and, crucially,
against a copy of the *real* gating predicate that ``claim_task`` and
``recompute_ready`` share. A repair that leaves a card unclaimable is no repair
at all, so every test asserts on ``claimable()`` rather than on link rows.
"""

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apply_kanban_dependency_repair import ANCHOR, PATCHED, RELATIVE, TRAILER, apply
from kanban_dependency_repair import (
    EVENT_KIND,
    find_deadlocked_children,
    has_unsettled_parents,
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


def board(tasks, links=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for tid, status in tasks.items():
        conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", (tid, status))
    for parent, child in links:
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent, child),
        )
    return conn


def claimable(conn, task_id):
    """The gate ``claim_task`` and ``recompute_ready`` both apply, verbatim."""
    undone = conn.execute(
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1",
        (task_id,),
    ).fetchone()
    return undone is None


def events(conn, task_id):
    return [
        (r["kind"], json.loads(r["payload"]))
        for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
    ]


class RepairTest(unittest.TestCase):
    def test_the_reported_deadlock_is_broken(self):
        """t_ab112f5b's shape: three cards parented to the card that waits on them."""
        conn = board(
            {"P": "running", "A": "todo", "B": "todo", "C": "todo"},
            [("P", "A"), ("P", "B"), ("P", "C")],
        )
        for child in ("A", "B", "C"):
            self.assertFalse(claimable(conn, child), f"{child} starts deadlocked")

        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A", "B", "C"])

        for child in ("A", "B", "C"):
            self.assertTrue(claimable(conn, child), f"{child} must now dispatch")
        self.assertFalse(claimable(conn, "P"), "P must wait for its prerequisites")

    def test_waiting_card_becomes_claimable_once_children_finish(self):
        conn = board({"P": "running", "A": "todo"}, [("P", "A")])
        repair_inverted_dependencies(conn, "P")
        self.assertFalse(claimable(conn, "P"))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'A'")
        self.assertTrue(claimable(conn, "P"), "the wait resolves on its own")

    def test_settled_children_are_left_alone(self):
        """A finished child already satisfies the gate; inverting it would un-satisfy P."""
        conn = board(
            {"P": "running", "A": "done", "B": "archived"},
            [("P", "A"), ("P", "B")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertTrue(claimable(conn, "P"))
        self.assertEqual(events(conn, "P"), [])

    def test_legitimate_continuation_card_is_untouched(self):
        """Upstream's endorsed idiom: create a child, then complete yourself.

        The repair fires only from ``block_task``, so a creator that completes
        instead of blocking never reaches it and the continuation card runs on
        the original edge. ``ApplierTest`` pins the call site; this pins that the
        idiom needs no repair to work.
        """
        conn = board({"P": "running", "K": "todo"}, [("P", "K")])
        self.assertFalse(claimable(conn, "K"))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'P'")
        self.assertTrue(claimable(conn, "K"))

    def test_real_fan_in_is_not_disturbed(self):
        """P waits on A and B the correct way round: nothing to repair."""
        conn = board(
            {"P": "running", "A": "todo", "B": "running"},
            [("A", "P"), ("B", "P")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertFalse(claimable(conn, "P"))
        self.assertTrue(claimable(conn, "A"))

    # --- guard 1: the block must be provably unsatisfiable ------------------

    def test_a_block_that_can_hold_is_left_alone(self):
        """The case that failed the in-image gate.

        A researcher fans out to a synthesizer and then blocks for its own
        reasons. Its successor is not what it is waiting for, and inverting the
        pipeline would be a rewrite, not a repair.
        """
        conn = board(
            {"UP": "running", "A": "running", "SINK": "todo"},
            [("UP", "A"), ("A", "SINK")],
        )
        self.assertTrue(has_unsettled_parents(conn, "A"))
        self.assertEqual(repair_inverted_dependencies(conn, "A"), [])
        links = set(
            (r["parent_id"], r["child_id"])
            for r in conn.execute("SELECT parent_id, child_id FROM task_links")
        )
        self.assertEqual(links, {("UP", "A"), ("A", "SINK")})

    def test_settled_parents_do_not_count_as_a_real_wait(self):
        conn = board(
            {"UP": "done", "P": "running", "A": "todo"},
            [("UP", "P"), ("P", "A")],
        )
        self.assertFalse(has_unsettled_parents(conn, "P"))
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])

    def test_repair_is_idempotent_via_guard_one(self):
        """After the first repair the card has real parents, so a re-block is a no-op."""
        conn = board({"P": "running", "A": "todo"}, [("P", "A")])
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        self.assertTrue(has_unsettled_parents(conn, "P"))
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])

    # --- guard 2: inverting must actually free the child --------------------

    def test_child_with_another_unfinished_parent_is_left_alone(self):
        """Releasing one of two blockers frees nothing, so do not rewrite the edge."""
        conn = board(
            {"P": "running", "OTHER": "todo", "SINK": "todo"},
            [("P", "SINK"), ("OTHER", "SINK")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertFalse(claimable(conn, "SINK"))

    def test_child_whose_other_parents_are_finished_is_repaired(self):
        conn = board(
            {"P": "running", "OLD": "done", "SINK": "todo"},
            [("P", "SINK"), ("OLD", "SINK")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["SINK"])
        self.assertTrue(claimable(conn, "SINK"))

    def test_mixed_graph_repairs_only_the_deadlocking_edges(self):
        conn = board(
            {"P": "running", "A": "todo", "DONE": "done", "SHARED": "todo",
             "OTHER": "todo"},
            [("P", "A"), ("P", "DONE"), ("P", "SHARED"), ("OTHER", "SHARED")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        self.assertTrue(claimable(conn, "A"))
        links = set(
            (r["parent_id"], r["child_id"])
            for r in conn.execute("SELECT parent_id, child_id FROM task_links")
        )
        self.assertEqual(
            links, {("A", "P"), ("P", "DONE"), ("P", "SHARED"), ("OTHER", "SHARED")}
        )

    def test_cycle_is_refused_when_the_new_edge_is_the_one_that_closes_it(self):
        """T -> X -> C and T -> C, X settled. Inverting T->C would close a loop.

        This is the shape the probe exists for, and the only one that produces a
        cycle: the edge being *inserted* is C -> T, so what matters is whether T
        can still reach C after T -> C is dropped. Here it can, via X.

        Both guards pass first, which is what makes the probe load-bearing — T
        has no unsettled parent, and C's only other parent (X) is ``done``, so C
        is genuinely deadlocked on T alone.
        """
        conn = board(
            {"T": "running", "X": "done", "C": "todo"},
            [("T", "X"), ("X", "C"), ("T", "C")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "T"), [])
        links = set(
            (r["parent_id"], r["child_id"])
            for r in conn.execute("SELECT parent_id, child_id FROM task_links")
        )
        self.assertEqual(
            links, {("T", "X"), ("X", "C"), ("T", "C")}, "graph restored unchanged"
        )
        kind, payload = events(conn, "T")[0]
        self.assertEqual(kind, EVENT_KIND)
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
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        links = set(
            (r["parent_id"], r["child_id"])
            for r in conn.execute("SELECT parent_id, child_id FROM task_links")
        )
        self.assertEqual(links, {("A", "M"), ("M", "P"), ("A", "P")})
        self.assertTrue(claimable(conn, "A"))
        kind, payload = events(conn, "P")[0]
        self.assertEqual(kind, EVENT_KIND)
        self.assertEqual(payload["inverted"], ["A"])
        self.assertEqual(payload["skipped_would_cycle"], [])

    def test_self_link_would_cycle_and_is_skipped(self):
        conn = board({"P": "running"}, [("P", "P")])
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])

    def test_reachability_walk_terminates_on_a_pre_existing_cycle(self):
        """n0..n49 is a ring. The walk must not spin looking for P.

        The ring is ``done`` so guard 2 lets n0 through — the point of the test
        is the 50-hop traversal, not the guards.
        """
        tasks = {f"n{i}": "done" for i in range(50)}
        tasks["n0"] = "todo"
        tasks["P"] = "running"
        links = [(f"n{i}", f"n{i + 1}") for i in range(49)]
        links += [("n49", "n0"), ("P", "n0")]
        conn = board(tasks, links)
        # P is not reachable from the ring, so the edge inverts.
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["n0"])

    def test_no_children_writes_nothing(self):
        conn = board({"P": "running"})
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertEqual(events(conn, "P"), [])

    def test_event_records_the_repair(self):
        conn = board({"P": "running", "A": "todo"}, [("P", "A")])
        repair_inverted_dependencies(conn, "P", reason="waiting on cluster audits")
        kind, payload = events(conn, "P")[0]
        self.assertEqual(kind, EVENT_KIND)
        self.assertEqual(payload["inverted"], ["A"])
        self.assertEqual(payload["reason"], "waiting on cluster audits")

    def test_long_reason_is_clipped(self):
        conn = board({"P": "running", "A": "todo"}, [("P", "A")])
        repair_inverted_dependencies(conn, "P", reason="x" * 5000)
        _, payload = events(conn, "P")[0]
        self.assertEqual(len(payload["reason"]), 200)

    def test_find_deadlocked_children_ignores_settled(self):
        conn = board(
            {"P": "running", "A": "todo", "B": "done", "C": "blocked"},
            [("P", "A"), ("P", "B"), ("P", "C")],
        )
        self.assertEqual(find_deadlocked_children(conn, "P"), ["A", "C"])

    def test_has_unsettled_parents_matches_the_claim_gate(self):
        conn = board({"P": "todo", "D": "done", "R": "running"})
        self.assertFalse(has_unsettled_parents(conn, "P"))
        conn.execute("INSERT INTO task_links VALUES ('D', 'P')")
        self.assertFalse(has_unsettled_parents(conn, "P"))
        self.assertTrue(claimable(conn, "P"))
        conn.execute("INSERT INTO task_links VALUES ('R', 'P')")
        self.assertTrue(has_unsettled_parents(conn, "P"))
        self.assertFalse(claimable(conn, "P"))


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

    def test_applier_is_not_idempotent_and_that_is_by_design(self):
        """PATCHED still contains ANCHOR, so a second run would stack the call.

        The Dockerfile runs each applier exactly once against pristine upstream
        source. Pinning the behaviour here so nobody assumes a re-run is free.
        """
        root, _ = self._tree(PREAMBLE + PATCHED + EPILOGUE + TRAILER)
        apply(root)
        body = (root / RELATIVE).read_text()
        self.assertEqual(
            body.count("_kanban_repair_inverted_deps(conn, task_id, reason)"), 2
        )


if __name__ == "__main__":
    unittest.main()
