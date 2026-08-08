"""Unit tests for the claim fencing installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The scenario under test is the 2026-08-07 gateway SIGBUS: the container restarted
and kept its pod name, so the replacement dispatcher adjudicated its
predecessor's worker PIDs — every one of them belonging to a process it never
spawned — and abandoned six cards. Every test here is written in that vocabulary
— OLD is the process life that died, NEW is the one that came up.

The end-to-end consequences of charging a reclaim (a poison card reaching
``blocked``, the breaker's own threshold being the one that decides) belong to
verify_kanban_scheduling.py, which drives the real engine inside the image.
What is pinned here is the contract this module offers it.
"""

import ast
import json
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from apply_kanban_claim_fencing import (
    CHARGE_ANCHOR,
    FENCE_ANCHOR,
    RELATIVE,
    apply,
)
from kanban_claim_fencing import (
    EVENT_KIND,
    RECLAIM_ERROR,
    charge_reclaimed_cards,
    claim_is_self,
    release_dead_foreign_claims,
)

POD = "platform-agent-gateway-75b5f6ddf6-7dkd7"
OLD = f"{POD}:4"  # the dispatcher that took the bus error
NEW = f"{POD}:9"  # the one that replaced it, same pod name
OTHER_POD = "platform-agent-gateway-595bbd777f-5vlzk:7"

# The fingerprint as ``hermes_cli/kanban_db.py`` ships it, and as this patch
# deliberately leaves it. Mirrored rather than imported because kanban_db only
# exists inside the image.
UPSTREAM_FINGERPRINT_SOURCE = (
    "def _error_fingerprint(error_text):\n"
    "    fp = re.sub(r'\\bpid \\d+\\b', 'pid N', error_text[:80])\n"
    "    fp = re.sub(r'\\b\\d{10,}\\b', '<TS>', fp)\n"
    "    return fp.lower().strip()\n"
)


def upstream_fingerprint(error_text):
    fp = re.sub(r"\bpid \d+\b", "pid N", error_text[:80])
    fp = re.sub(r"\b\d{10,}\b", "<TS>", fp)
    return fp.lower().strip()


SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    worker_pid INTEGER,
    claim_lock TEXT,
    claim_expires INTEGER,
    current_run_id INTEGER,
    started_at INTEGER
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
            self.assertEqual(row["status"], "ready")
            self.assertIsNone(row["claim_lock"])
            self.assertIsNone(row["claim_expires"])
            self.assertIsNone(row["worker_pid"])
            self.assertIsNone(row["current_run_id"])

    def test_the_card_comes_back_ready_not_todo(self):
        """``todo`` is how a card used to walk past the breaker.

        ``recompute_ready`` applies its failure-limit guard only to ``blocked``
        rows and promotes ``todo`` unconditionally, and
        ``_record_task_failure``'s trip branch is gated on
        ``status IN ('ready', 'running')`` — so a reclaim that lands in ``todo``
        can be neither stopped nor even counted.
        """
        conn = board([("t1", "running", 1044, OLD)])
        release_dead_foreign_claims(conn, NEW, DEAD)
        self.assertEqual(task(conn, "t1")["status"], "ready")

    def test_release_is_reclaimed_not_crashed(self):
        """The run outcome names the infrastructure event, not a worker fault."""
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

    def test_a_card_inside_the_grace_window_is_left_alone(self):
        """The sweep must not be stricter than the per-row loop it runs ahead of.

        A worker claimed one second ago may not be on ``/proc`` yet, so a dead
        PID reading proves nothing — for a foreign claim exactly as much as for
        our own.
        """
        conn = board([("t1", "running", 1044, OLD)])
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = 't1'", (time.time(),))
        self.assertEqual(
            release_dead_foreign_claims(conn, NEW, DEAD, lambda: 30), []
        )
        self.assertEqual(task(conn, "t1")["status"], "running")

    def test_a_card_past_the_grace_window_is_released(self):
        conn = board([("t1", "running", 1044, OLD)])
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = 't1'", (time.time() - 31,)
        )
        self.assertEqual(
            release_dead_foreign_claims(conn, NEW, DEAD, lambda: 30), ["t1"]
        )

    def test_a_null_started_at_does_not_confer_grace(self):
        """``board()`` leaves ``started_at`` NULL, as an un-migrated row would."""
        conn = board([("t1", "running", 1044, OLD)])
        self.assertEqual(
            release_dead_foreign_claims(conn, NEW, DEAD, lambda: 30), ["t1"]
        )

    def test_the_grace_resolver_is_consulted_at_most_once(self):
        conn = board([("t1", "running", 1044, OLD), ("t2", "running", 1045, OLD)])
        conn.execute("UPDATE tasks SET started_at = ?", (time.time() - 31,))
        calls = []

        def resolver():
            calls.append(1)
            return 30

        self.assertEqual(
            release_dead_foreign_claims(conn, NEW, DEAD, resolver), ["t1", "t2"]
        )
        self.assertEqual(len(calls), 1)

    def test_an_already_closed_run_is_not_rewritten(self):
        conn = board([("t1", "running", 1044, OLD)])
        conn.execute(
            "UPDATE task_runs SET ended_at = 123, outcome = 'completed' "
            "WHERE task_id = 't1'"
        )
        self.assertEqual(release_dead_foreign_claims(conn, NEW, DEAD), ["t1"])
        row = run_row(conn, "t1")
        self.assertEqual(row["outcome"], "completed")
        self.assertEqual(row["ended_at"], 123)


class ChargeReclaimedCardsTest(unittest.TestCase):
    """``_record_task_failure`` lives in kanban_db, so record what it is handed."""

    def setUp(self):
        self.calls = []

    def recorder(self, trips=()):
        def record_failure(conn, task_id, **kwargs):
            self.calls.append((task_id, kwargs))
            return task_id in trips

        return record_failure

    def test_every_released_card_is_charged_one_failure(self):
        parked = charge_reclaimed_cards(None, ["t1", "t2"], self.recorder())
        self.assertEqual(parked, [])
        self.assertEqual([tid for tid, _ in self.calls], ["t1", "t2"])

    def test_the_cards_the_breaker_parked_are_returned(self):
        """``dispatch_once`` reports these as auto-blocked, so a human hears."""
        parked = charge_reclaimed_cards(
            None, ["t1", "t2", "t3"], self.recorder(trips={"t2"})
        )
        self.assertEqual(parked, ["t2"])

    def test_nothing_released_means_nothing_charged(self):
        self.assertEqual(charge_reclaimed_cards(None, [], self.recorder()), [])
        self.assertEqual(self.calls, [])

    def test_the_charge_uses_the_dispatchers_own_threshold(self):
        """No second retry budget: no ``failure_limit``, no ``force_trip``.

        ``_record_task_failure`` then resolves per-task ``max_retries``, else
        ``kanban.failure_limit``, else ``DEFAULT_FAILURE_LIMIT`` — the same
        ladder every other failure kind is judged on.
        """
        charge_reclaimed_cards(None, ["t1"], self.recorder())
        _, kwargs = self.calls[0]
        self.assertNotIn("failure_limit", kwargs)
        self.assertNotIn("force_trip", kwargs)

    def test_the_charge_neither_releases_a_claim_nor_ends_a_run(self):
        """The sweep already did both, inside its own transaction."""
        charge_reclaimed_cards(None, ["t1"], self.recorder())
        _, kwargs = self.calls[0]
        self.assertFalse(kwargs["release_claim"])
        self.assertFalse(kwargs["end_run"])

    def test_the_gave_up_event_says_why_the_card_was_parked(self):
        charge_reclaimed_cards(None, ["t1"], self.recorder(trips={"t1"}))
        _, kwargs = self.calls[0]
        self.assertEqual(kwargs["error"], RECLAIM_ERROR)
        self.assertEqual(kwargs["outcome"], EVENT_KIND)
        self.assertEqual(
            kwargs["event_payload_extra"],
            {"reason": "owner_process_gone", "fenced": True},
        )


class FingerprintTest(unittest.TestCase):
    """Why ``_error_fingerprint`` is left exactly as upstream ships it."""

    def test_normalising_the_pid_is_what_makes_a_burst_detectable(self):
        """Every message on this path is PID-prefixed, so the sub is load-bearing.

        ``detect_crashed_workers`` builds all three of these itself. Without the
        substitution no two concurrent workers can share a bucket, ``_fp_counts``
        never reaches the ``>= 3`` the systemic heuristic tests, and the
        detector is dead code.
        """
        for template in (
            "pid {} not alive",
            "pid {} exited with code 137",
            "pid {} killed by signal 9",
        ):
            messages = [template.format(p) for p in (1044, 1045, 1046, 1047)]
            with self.subTest(template=template):
                self.assertEqual(
                    len({upstream_fingerprint(m) for m in messages}),
                    1,
                    "four workers felled by one event must land in one bucket",
                )
                self.assertEqual(
                    len({m[:80] for m in messages}),
                    4,
                    "and the unnormalised text is what used to split them",
                )

    def test_distinct_faults_still_keep_their_own_buckets(self):
        self.assertNotEqual(
            upstream_fingerprint("pid 1044 exited with code 137"),
            upstream_fingerprint("pid 1044 killed by signal 9"),
        )

    def test_timestamp_normalisation_is_untouched(self):
        self.assertEqual(
            upstream_fingerprint("failed at 1754539230"),
            upstream_fingerprint("failed at 1754539999"),
        )

    def test_a_reclaim_never_reaches_the_fingerprint_at_all(self):
        """Six identical reclaim texts would otherwise read as one systemic fault.

        ``charge_reclaimed_cards`` runs its own loop precisely so that a rollout
        handing back every in-flight card cannot collapse into ``failure_limit=1``
        and abandon the lot — the 2026-08-07 outcome by another route.
        """
        self.assertEqual(
            len({upstream_fingerprint(RECLAIM_ERROR) for _ in range(6)}), 1
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
            + "            pass\n"
            "    auto_blocked = []\n"
            + CHARGE_ANCHOR
            + "    detect_crashed_workers._last_auto_blocked = auto_blocked\n"
            "    return crashed\n\n"
            + UPSTREAM_FINGERPRINT_SOURCE
        )

    def test_applies_both_edits_and_stays_parseable(self):
        root, target = self._tree(self._pristine())
        apply(root)
        out = target.read_text()
        self.assertIn(
            "_kanban_reclaimed = _kanban_release_dead_foreign_claims(\n"
            "            conn, _kanban_claimer, _pid_alive, "
            "_resolve_crash_grace_seconds\n        )",
            out,
        )
        self.assertIn("_kanban_claim_is_self(lock, _kanban_claimer)", out)
        self.assertIn(
            "_kanban_charge_reclaimed_cards(\n"
            "            conn, _kanban_reclaimed, _record_task_failure\n        )",
            out,
        )
        self.assertIn("from hermes_cli.kanban_claim_fencing import", out)
        ast.parse(out)

    def test_host_prefix_comparison_is_gone_from_the_crash_reaper(self):
        root, target = self._tree(self._pristine())
        apply(root)
        self.assertNotIn("lock.startswith(host_prefix)", target.read_text())

    def test_the_fingerprint_is_left_exactly_as_upstream_wrote_it(self):
        """The regression this file used to carry, pinned so it cannot return."""
        root, target = self._tree(self._pristine())
        apply(root)
        out = target.read_text()
        self.assertIn(UPSTREAM_FINGERPRINT_SOURCE, out)
        self.assertNotIn("fp = error_text[:80]", out)

    def test_sweep_runs_before_the_rows_are_read(self):
        root, target = self._tree(self._pristine())
        apply(root)
        out = target.read_text()
        self.assertLess(
            out.index("_kanban_release_dead_foreign_claims"),
            out.index('"SELECT id, worker_pid, claim_lock, started_at FROM tasks "'),
        )

    def test_the_charge_runs_after_the_sweeps_transaction_has_closed(self):
        """``_record_task_failure`` opens its own txn, and ``write_txn`` cannot nest."""
        root, target = self._tree(self._pristine())
        apply(root)
        out = target.read_text()
        charge = out.index("_kanban_charge_reclaimed_cards")
        self.assertLess(out.index("with write_txn(conn):"), charge)
        self.assertLess(charge, out.index("_last_auto_blocked"))
        # Nothing the charge does may sit at the transaction's indentation.
        self.assertIn("\n    auto_blocked.extend(\n", out)

    def test_missing_fence_anchor_fails_the_build(self):
        root, _ = self._tree("import re\n" + CHARGE_ANCHOR)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_missing_charge_anchor_fails_the_build(self):
        root, _ = self._tree("import re\n" + FENCE_ANCHOR)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_partial_apply_does_not_write(self):
        """The fence anchor matches, the charge one does not: leave the file be."""
        body = "import re\n" + FENCE_ANCHOR
        root, target = self._tree(body)
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), body)

    def test_duplicate_anchor_fails_the_build(self):
        root, _ = self._tree(
            "import re\n" + FENCE_ANCHOR + FENCE_ANCHOR + CHARGE_ANCHOR
        )
        with self.assertRaises(SystemExit):
            apply(root)


if __name__ == "__main__":
    unittest.main()
