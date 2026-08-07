"""Unit tests for worker subscription inheritance installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kanban_auto_subscribe as kas
from apply_kanban_auto_subscribe import ANCHOR, RELATIVE, apply
from kanban_auto_subscribe import (
    COPY_COLUMNS,
    WORKER_TASK_ENV,
    inherit_subscriptions,
    maybe_inherit_worker_subscriptions,
)

# The real table shape from hermes_cli/kanban_db.py SCHEMA_SQL — including the
# two columns (chat_type, delivery_metadata) that are deliberately NOT copied,
# matching both upstream _inherit_notify_subs and the manual propagate script.
SUBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    chat_type     TEXT,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT);
"""

PARENT = "t_b9659077"  # the coordinator card from the 2026-08-07 incident
CHILD = "t_f54bd6b5"   # the synthesizer whose answer sat undelivered 91.3s


def board(path=None):
    conn = sqlite3.connect(path if path is not None else ":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SUBS_SCHEMA)
    conn.execute("INSERT INTO tasks VALUES (?, 'running')", (PARENT,))
    conn.execute("INSERT INTO tasks VALUES (?, 'ready')", (CHILD,))
    conn.commit()
    return conn


def subscribe(conn, task_id, chat="C123", thread="171234.5678", platform="slack"):
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, chat_type,"
        " thread_id, user_id, notifier_profile, delivery_metadata, created_at,"
        " last_event_id) VALUES (?, ?, ?, 'group', ?, 'U1', 'default',"
        " '{\"thread_id\": \"x\"}', 1000, 42)",
        (task_id, platform, chat, thread),
    )
    conn.commit()


def child_rows(conn):
    return conn.execute(
        "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (CHILD,)
    ).fetchall()


class InheritSubscriptionsTest(unittest.TestCase):
    def test_the_child_gets_the_parents_subscription(self):
        conn = board()
        subscribe(conn, PARENT)
        self.assertEqual(inherit_subscriptions(conn, CHILD, PARENT), 1)
        rows = child_rows(conn)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["platform"], "slack")
        self.assertEqual(row["chat_id"], "C123")
        self.assertEqual(row["thread_id"], "171234.5678")
        self.assertEqual(row["user_id"], "U1")
        self.assertEqual(row["notifier_profile"], "default")

    def test_last_event_id_resets_and_created_at_is_restamped(self):
        conn = board()
        subscribe(conn, PARENT)
        inherit_subscriptions(conn, CHILD, PARENT)
        row = child_rows(conn)[0]
        self.assertEqual(row["last_event_id"], 0, "the child's own events start fresh")
        self.assertGreater(row["created_at"], 1000)

    def test_only_the_documented_columns_are_copied(self):
        # chat_type / delivery_metadata stay NULL — the same set upstream's
        # _inherit_notify_subs and the manual propagate script copy.
        self.assertEqual(
            COPY_COLUMNS,
            ("platform", "chat_id", "thread_id", "user_id", "notifier_profile"),
        )
        conn = board()
        subscribe(conn, PARENT)
        inherit_subscriptions(conn, CHILD, PARENT)
        row = child_rows(conn)[0]
        self.assertIsNone(row["chat_type"])
        self.assertIsNone(row["delivery_metadata"])

    def test_every_parent_row_is_copied(self):
        conn = board()
        subscribe(conn, PARENT, chat="C123")
        subscribe(conn, PARENT, chat="C456", platform="google_chat")
        self.assertEqual(inherit_subscriptions(conn, CHILD, PARENT), 2)
        self.assertEqual(len(child_rows(conn)), 2)

    def test_idempotent_like_the_script_it_automates(self):
        conn = board()
        subscribe(conn, PARENT)
        self.assertEqual(inherit_subscriptions(conn, CHILD, PARENT), 1)
        self.assertEqual(inherit_subscriptions(conn, CHILD, PARENT), 0)
        self.assertEqual(len(child_rows(conn)), 1)

    def test_an_unsubscribed_parent_writes_nothing(self):
        conn = board()
        self.assertEqual(inherit_subscriptions(conn, CHILD, PARENT), 0)
        self.assertEqual(child_rows(conn), [])

    def test_parent_equal_to_child_is_a_no_op(self):
        conn = board()
        subscribe(conn, PARENT)
        self.assertEqual(inherit_subscriptions(conn, PARENT, PARENT), 0)

    def test_a_phantom_child_is_refused_not_written(self):
        # A row for a card that isn't on the board is scanned by every
        # notifier tick forever — the manual script refuses it, so do we.
        conn = board()
        subscribe(conn, PARENT)
        with self.assertLogs(kas.logger, level="WARNING"):
            self.assertEqual(inherit_subscriptions(conn, "t_typo", PARENT), 0)

    def test_a_db_path_is_accepted_in_place_of_a_connection(self):
        db = Path(tempfile.mkdtemp()) / "kanban.db"
        conn = board(str(db))
        subscribe(conn, PARENT)
        conn.close()
        self.assertEqual(inherit_subscriptions(str(db), CHILD, PARENT), 1)
        check = sqlite3.connect(str(db))
        try:
            count = check.execute(
                "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?",
                (CHILD,),
            ).fetchone()[0]
        finally:
            check.close()
        self.assertEqual(count, 1)

    def test_a_broken_board_is_logged_and_swallowed(self):
        # Fail-soft is the contract: this runs inside the kanban_create the
        # worker is mid-flight on, and must never break it.
        with self.assertLogs(kas.logger, level="WARNING"):
            self.assertEqual(
                inherit_subscriptions("/no/such/dir/kanban.db", CHILD, PARENT), 0
            )

    def test_empty_ids_are_a_no_op(self):
        conn = board()
        subscribe(conn, PARENT)
        self.assertEqual(inherit_subscriptions(conn, "", PARENT), 0)
        self.assertEqual(inherit_subscriptions(conn, CHILD, ""), 0)


class MaybeInheritWorkerSubscriptionsTest(unittest.TestCase):
    def test_a_worker_process_propagates_its_own_cards_subscription(self):
        conn = board()
        subscribe(conn, PARENT)
        with mock.patch.dict("os.environ", {WORKER_TASK_ENV: PARENT}):
            self.assertEqual(maybe_inherit_worker_subscriptions(conn, CHILD), 1)
        self.assertEqual(len(child_rows(conn)), 1)

    def test_a_non_worker_create_is_untouched(self):
        conn = board()
        subscribe(conn, PARENT)
        with mock.patch.dict("os.environ", clear=False) as env:
            env.pop(WORKER_TASK_ENV, None)
            self.assertEqual(maybe_inherit_worker_subscriptions(conn, CHILD), 0)
        self.assertEqual(child_rows(conn), [])

    def test_a_blank_env_value_is_not_a_worker(self):
        conn = board()
        subscribe(conn, PARENT)
        with mock.patch.dict("os.environ", {WORKER_TASK_ENV: "   "}):
            self.assertEqual(maybe_inherit_worker_subscriptions(conn, CHILD), 0)

    def test_a_card_never_inherits_from_itself(self):
        conn = board()
        subscribe(conn, CHILD)
        with mock.patch.dict("os.environ", {WORKER_TASK_ENV: CHILD}):
            self.assertEqual(maybe_inherit_worker_subscriptions(conn, CHILD), 0)


# The create handler, reduced to the two lines the patch anchors on.
UPSTREAM_TOOLS = '''\
def _handle_create(args, **kw):
    try:
        kb, conn = _connect(board=board)
        try:
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
            )
            new_task = kb.get_task(conn, new_tid)
            subscribed = _maybe_auto_subscribe(conn, new_tid)
            return _ok(
                task_id=new_tid,
                subscribed=subscribed,
            )
        finally:
            conn.close()
    except Exception as e:
        return tool_error(f"kanban_create: {e}")
'''


def patch_tree(source):
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


class ApplyTest(unittest.TestCase):
    def test_the_anchor_matches_upstream_exactly_once(self):
        self.assertEqual(UPSTREAM_TOOLS.count(ANCHOR), 1)

    def test_the_hook_lands_after_the_auto_subscribe_attempt(self):
        patched = patch_tree(UPSTREAM_TOOLS)
        auto_sub = patched.index("subscribed = _maybe_auto_subscribe(conn, new_tid)")
        hook = patched.index("_kanban_inherit_worker_subs(conn, new_tid)")
        result = patched.index("return _ok(")
        self.assertTrue(auto_sub < hook < result)

    def test_the_import_trailer_is_appended(self):
        patched = patch_tree(UPSTREAM_TOOLS)
        self.assertIn(
            "from tools.kanban_auto_subscribe import (  # noqa: E402", patched
        )
        self.assertIn(
            "maybe_inherit_worker_subscriptions as _kanban_inherit_worker_subs",
            patched,
        )

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(UPSTREAM_TOOLS))

    def test_a_drifted_anchor_fails_loudly(self):
        drifted = UPSTREAM_TOOLS.replace(
            "_maybe_auto_subscribe(conn, new_tid)",
            "_maybe_auto_subscribe(conn, new_tid, board)",
        )
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_fails_rather_than_silently_no_opping(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM_TOOLS)
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
