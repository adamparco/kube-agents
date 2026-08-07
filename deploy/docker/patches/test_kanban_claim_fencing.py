"""Unit tests for the claim fencing installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The scenario under test is the 2026-08-07 gateway SIGBUS: the container restarted,
kept its pod name, and reset its PID namespace, so the replacement dispatcher
adjudicated its predecessor's worker PIDs and abandoned six cards. Every test
here is written in that vocabulary — OLD is the process life that died, NEW is
the one that came up.
"""

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apply_kanban_claim_fencing import (
    FENCE_ANCHOR,
    FP_ANCHOR,
    RELATIVE,
    apply,
)
from kanban_claim_fencing import (
    EVENT_KIND,
    RECLAIM_ERROR,
    claim_is_self,
    release_dead_foreign_claims,
)

POD = "platform-agent-gateway-75b5f6ddf6-7dkd7"
OLD = f"{POD}:4"  # the dispatcher that took the bus error
NEW = f"{POD}:9"  # the one that replaced it, same pod name
OTHER_POD = "platform-agent-gateway-595bbd777f-5vlzk:7"

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    worker_pid INTEGER,
    claim_lock TEXT,
    claim_expires INTEGER,
    current_run_id INTEGER
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    status TEXT,
    outcome TEXT,
    ended_at INTEGER,
    error TEXT
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


def board(rows):
    """rows: (id, status, worker_pid, claim_lock). Each gets a running run row."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for tid, status, pid, lock in rows:
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, status) VALUES (?, 'running')", (tid,)
        )
        conn.execute(
            "INSERT INTO tasks (id, status, worker_pid, claim_lock, claim_expires, "
            "current_run_id) VALUES (?, ?, ?, ?, 9999999999, ?)",
            (tid, status, pid, lock, cur.lastrowid),
        )
    return conn


def task(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()


def run_row(conn, tid):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC", (tid,)
    ).fetchone()


def events(conn, tid):
    return [
        (r["kind"], json.loads(r["payload"]))
        for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        )
    ]


DEAD = lambda pid: False  # noqa: E731
ALIVE = lambda pid: True  # noqa: E731


class ClaimIsSelfTest(unittest.TestCase):
    def test_same_pod_different_process_is_not_self(self):
        """The whole bug in one assertion: upstream's prefix test said True here."""
        self.assertTrue(OLD.startswith(POD + ":"))
        self.assertFalse(claim_is_self(OLD, NEW))

    def test_exact_token_is_self(self):
        self.assertTrue(claim_is_self(NEW, NEW))

    def test_other_pod_is_not_self(self):
        self.assertFalse(claim_is_self(OTHER_POD, NEW))

    def test_empty_and_none_are_not_self(self):
        self.assertFalse(claim_is_self("", NEW))
        self.assertFalse(claim_is_self(None, NEW))

    def test_prefix_of_our_token_is_not_self(self):
        """`pod:4` must not match `pod:41` in either direction."""
        self.assertFalse(claim_is_self(f"{POD}:4", f"{POD}:41"))
        self.assertFalse(claim_is_self(f"{POD}:41", f"{POD}:4"))


class ReleaseDeadForeignClaimsTest(unittest.TestCase):
    def test_predecessors_dead_claims_come_back(self):
        conn = board([("t1", "running", 1044, OLD), ("t2", "running", 1046, OLD)])
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), ["t1", "t2"])
        for tid in ("t1", "t2"):
            row = task(conn, tid)
            self.assertEqual(row["status"], "todo")
            self.assertIsNone(row["claim_lock"])
            self.assertIsNone(row["claim_expires"])
            self.assertIsNone(row["worker_pid"])
            self.assertIsNone(row["current_run_id"])

    def test_release_is_reclaimed_not_crashed(self):
        """An infrastructure event must not spend the card's retry budget."""
        conn = board([("t1", "running", 1044, OLD)])
        release_dead_foreign_claims(conn, NEW, DEAD)
        run = run_row(conn, "t1")
        self.assertEqual(run["status"], "reclaimed")
        self.assertEqual(run["outcome"], "reclaimed")
        self.assertEqual(run["error"], RECLAIM_ERROR)
        self.assertIsNotNone(run["ended_at"])
        kind, payload = events(conn, "t1")[0]
        self.assertEqual(kind, EVENT_KIND)
        self.assertNotEqual(kind, "crashed")
        self.assertEqual(payload["stale_lock"], OLD)
        self.assertEqual(payload["worker_pid"], 1044)
        self.assertEqual(payload["reason"], "owner_process_gone")

    def test_our_own_claims_are_never_touched(self):
        conn = board([("mine", "running", 2001, NEW)])
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), [])
        self.assertEqual(task(conn, "mine")["status"], "running")

    def test_a_live_foreign_worker_is_left_to_the_ttl(self):
        """This is what makes the sweep safe from a CLI process.

        A CLI has its own claimer id and sees the gateway's claims as foreign,
        but the gateway's workers are alive, so nothing is taken from them.
        """
        conn = board([("gw", "running", 2001, OLD)])
        self.assertEqual(release_dead_foreign_claims(conn, OTHER_POD, ALIVE), [])
        self.assertEqual(task(conn, "gw")["status"], "running")

    def test_rolled_pods_claims_come_back_without_waiting_out_the_ttl(self):
        """claim_expires is far in the future; the sweep does not consult it."""
        conn = board([("t1", "running", 2017, OTHER_POD)])
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), ["t1"])

    def test_only_running_rows_are_candidates(self):
        conn = board([("d", "done", 1044, OLD), ("b", "blocked", 1045, OLD)])
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), [])

    def test_rows_without_a_pid_are_left_for_the_ttl(self):
        conn = board([("t1", "running", None, OLD)])
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), [])
        self.assertEqual(task(conn, "t1")["status"], "running")

    def test_mixed_board_releases_only_the_dead_foreign_rows(self):
        conn = board(
            [
                ("mine_live", "running", 3001, NEW),
                ("mine_dead", "running", 3002, NEW),
                ("prev_dead", "running", 1044, OLD),
                ("other_live", "running", 2001, OTHER_POD),
            ]
        )
        alive = {3001, 2001}
        released = release_dead_foreign_claims(conn, NEW, lambda p: p in alive)
        self.assertEqual(released, ["prev_dead"])
        # mine_dead is ours: detect_crashed_workers still owns that decision.
        self.assertEqual(task(conn, "mine_dead")["status"], "running")

    def test_sweep_is_idempotent(self):
        conn = board([("t1", "running", 1044, OLD)])
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), ["t1"])
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), [])

    def test_non_integer_pid_is_skipped_not_fatal(self):
        conn = board([("t1", "running", 1044, OLD)])
        conn.execute("UPDATE tasks SET worker_pid = 'x' WHERE id = 't1'")
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), [])


class FingerprintTest(unittest.TestCase):
    """The fingerprint change lives in kanban_db.py, so assert on the patched text."""

    def test_patched_fingerprint_keeps_distinct_pids_distinct(self):
        import re

        def upstream(text):
            fp = re.sub(r"\bpid \d+\b", "pid N", text[:80])
            fp = re.sub(r"\b\d{10,}\b", "<TS>", fp)
            return fp.lower().strip()

        def patched(text):
            fp = text[:80]
            fp = re.sub(r"\b\d{10,}\b", "<TS>", fp)
            return fp.lower().strip()

        crashes = [f"pid {p} not alive" for p in (1044, 1045, 1046, 1047, 1048, 1049)]
        self.assertEqual(
            len({upstream(c) for c in crashes}),
            1,
            "upstream collapses six workers into one systemic fingerprint",
        )
        self.assertEqual(
            len({patched(c) for c in crashes}),
            6,
            "each worker must count as its own failure",
        )

    def test_genuinely_systemic_errors_still_group(self):
        import re

        def patched(text):
            fp = text[:80]
            fp = re.sub(r"\b\d{10,}\b", "<TS>", fp)
            return fp.lower().strip()

        same = ["model provider returned 503"] * 4
        self.assertEqual(len({patched(c) for c in same}), 1)

    def test_timestamp_normalisation_is_preserved(self):
        import re

        def patched(text):
            fp = text[:80]
            fp = re.sub(r"\b\d{10,}\b", "<TS>", fp)
            return fp.lower().strip()

        self.assertEqual(
            patched("failed at 1754539230"), patched("failed at 1754539999")
        )


class ApplierTest(unittest.TestCase):
    def _tree(self, body):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return root, target

    def _pristine(self):
        return (
            "import re\n\n"
            "def detect_crashed_workers(conn):\n"
            "    with write_txn(conn):\n"
            + FENCE_ANCHOR
            + "            pass\n\n"
            "def _error_fingerprint(error_text):\n"
            + FP_ANCHOR
            + "    return fp\n"
        )

    def test_applies_both_edits_and_stays_parseable(self):
        root, target = self._tree(self._pristine())
        apply(root)
        out = target.read_text()
        self.assertIn("_kanban_release_dead_foreign_claims(conn, _kanban_claimer", out)
        self.assertIn("_kanban_claim_is_self(lock, _kanban_claimer)", out)
        self.assertIn("fp = error_text[:80]", out)
        self.assertIn("from hermes_cli.kanban_claim_fencing import", out)
        ast.parse(out)

    def test_host_prefix_comparison_is_gone_from_the_crash_reaper(self):
        root, target = self._tree(self._pristine())
        apply(root)
        out = target.read_text()
        self.assertNotIn("lock.startswith(host_prefix)", out)
        self.assertNotIn("re.sub(r'\\bpid \\d+\\b'", out)

    def test_sweep_runs_before_the_rows_are_read(self):
        root, target = self._tree(self._pristine())
        apply(root)
        out = target.read_text()
        self.assertLess(
            out.index("_kanban_release_dead_foreign_claims"),
            out.index('"SELECT id, worker_pid, claim_lock, started_at FROM tasks "'),
        )

    def test_missing_fence_anchor_fails_the_build(self):
        root, _ = self._tree("import re\n" + FP_ANCHOR)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_missing_fingerprint_anchor_fails_the_build(self):
        root, _ = self._tree("import re\n" + FENCE_ANCHOR)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_partial_apply_does_not_write(self):
        """The fence anchor matches, the fingerprint one does not: leave the file be."""
        body = "import re\n" + FENCE_ANCHOR
        root, target = self._tree(body)
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), body)

    def test_duplicate_anchor_fails_the_build(self):
        root, _ = self._tree("import re\n" + FENCE_ANCHOR + FENCE_ANCHOR + FP_ANCHOR)
        with self.assertRaises(SystemExit):
            apply(root)


if __name__ == "__main__":
    unittest.main()
