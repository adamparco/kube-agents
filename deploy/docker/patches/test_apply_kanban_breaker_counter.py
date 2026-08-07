"""Unit tests for the kanban breaker-counter patch applied by the Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import tempfile
import unittest
from pathlib import Path

from apply_kanban_breaker_counter import (
    BUILD_MARKER,
    CRASH_BIND_ANCHOR,
    RELATIVE,
    SPAWN_BIND_ANCHOR,
    TRIP_ANCHOR,
    apply,
)

# The shape of the region being patched, reproduced from
# hermes_cli/kanban_db.py at the pinned Hermes version. Only the anchors
# matter, but the surroundings keep the fixture honest about indentation and
# about the two sibling UPDATEs in the else-branch that must NOT be touched.
FIXTURE = '''\
def _record_task_failure(conn, task_id, error, *, outcome, failure_limit=None,
                        force_trip=False, release_claim=False, end_run=False,
                        event_payload_extra=None):
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    blocked = False
    with write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        failures = int(row["consecutive_failures"]) + 1
        task_override = (
            row["max_retries"] if "max_retries" in row.keys() else None
        )
        if task_override is not None:
            effective_limit = int(task_override)
            limit_source = "task"
        else:
            effective_limit = int(failure_limit)
            limit_source = "dispatcher"

        if force_trip or failures >= effective_limit:
            # Trip the breaker.
            if release_claim:
                # Spawn path: still running, also clear claim state.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('running', 'ready')",
                    (failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready``
                # with claim cleared; just flip to blocked + update
                # counter fields.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('ready', 'running')",
                    (failures, error[:500], task_id),
                )
            payload = {
                "failures": failures,
                "effective_limit": effective_limit,
                "limit_source": limit_source,
            }
            blocked = True
        else:
            # Below threshold. These two must keep binding the raw count.
            if release_claim:
                conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (failures, error[:500], task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error[:500], task_id),
                )
    return blocked
'''


def write_source(root, body):
    """Materialise a fake Hermes tree containing ``body``."""
    path = Path(root) / RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class ApplyTest(unittest.TestCase):
    def test_floors_the_persisted_counter_on_the_trip_path_only(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_source(root, FIXTURE)
            apply(Path(root))
            patched = path.read_text()

        # The floor landed, and it consults the per-task override.
        self.assertIn(BUILD_MARKER, patched)
        self.assertIn("if task_override is None:", patched)
        self.assertIn("persisted_failures, DEFAULT_FAILURE_LIMIT", patched)

        # Both blocked-branch UPDATEs now bind the floored value...
        self.assertEqual(
            patched.count("(persisted_failures, error[:500], task_id),"), 2
        )
        # ...and the two below-threshold UPDATEs still bind the raw count.
        self.assertEqual(
            patched.count("(failures, error[:500], task_id),"), 2
        )

        # The gave_up payload keeps reporting the true attempt count, so the
        # audit trail does not silently change meaning.
        self.assertIn('"failures": failures,', patched)

    def test_floor_is_computed_before_the_updates_that_use_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_source(root, FIXTURE)
            apply(Path(root))
            patched = path.read_text()

        assign = patched.index(BUILD_MARKER)
        first_use = patched.index("(persisted_failures, error[:500], task_id),")
        self.assertLess(assign, first_use)

    def test_is_not_idempotent_and_says_so(self):
        # Re-running must fail loudly rather than double-apply.
        with tempfile.TemporaryDirectory() as root:
            write_source(root, FIXTURE)
            apply(Path(root))
            with self.assertRaises(SystemExit):
                apply(Path(root))

    def test_missing_anchor_raises(self):
        for anchor in (TRIP_ANCHOR, SPAWN_BIND_ANCHOR, CRASH_BIND_ANCHOR):
            with self.subTest(anchor=anchor.splitlines()[0]):
                with tempfile.TemporaryDirectory() as root:
                    write_source(root, FIXTURE.replace(anchor, ""))
                    with self.assertRaises(SystemExit):
                        apply(Path(root))


if __name__ == "__main__":
    unittest.main()
