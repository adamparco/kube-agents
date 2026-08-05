"""Unit tests for the worker-only kanban gate installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apply_kanban_worker_tools import HANDLERS, RELATIVE, apply, build_patches
from kanban_worker_tools import WORKER_ONLY_TOOLS, check_kanban_worker_mode

# Tools an orchestrator profile keeps — the surface agents/chat/SOUL.md §1.5
# permits the front door: create, read, route, comment, unblock.
ORCHESTRATOR_TOOLS = (
    "kanban_show",
    "kanban_comment",
    "kanban_create",
)

# Reproduces the shape of upstream tools/kanban_tools.py closely enough that the
# anchors have to be right: the two gate functions, then the registrations.
GATES = '''\
import os


def _profile_has_kanban_toolset() -> bool:
    return False


def _check_kanban_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()
'''


def registration(tool, check_fn, handler=None):
    handler = handler or HANDLERS.get(tool, f"_handle_{tool[len('kanban_'):]}")
    return (
        "\n\nregistry.register(\n"
        f'    name="{tool}",\n'
        '    toolset="kanban",\n'
        f"    schema={tool.upper()}_SCHEMA,\n"
        f"    handler={handler},\n"
        f"    check_fn={check_fn},\n"
        '    emoji="x",\n'
        ")\n"
    )


def upstream_source():
    src = GATES
    src += registration("kanban_list", "_check_kanban_orchestrator_mode")
    src += registration("kanban_unblock", "_check_kanban_orchestrator_mode")
    for tool in ORCHESTRATOR_TOOLS:
        src += registration(tool, "_check_kanban_mode")
    for tool in WORKER_ONLY_TOOLS:
        src += registration(tool, "_check_kanban_mode")
    return src


def patch_tree(source):
    """Write ``source`` as tools/kanban_tools.py under a temp root and patch it."""
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


class CheckWorkerModeTest(unittest.TestCase):
    def test_true_only_inside_a_dispatched_run(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
            self.assertTrue(check_kanban_worker_mode())

    def test_false_for_an_orchestrator_profile(self):
        env = {k: v for k, v in os.environ.items() if k != "HERMES_KANBAN_TASK"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(check_kanban_worker_mode())

    def test_an_empty_task_id_is_not_a_worker(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": ""}):
            self.assertFalse(check_kanban_worker_mode())

    def test_the_forbidden_four_are_all_covered(self):
        # The four agents/chat/SOUL.md §1.5 names explicitly. If a future edit
        # trims WORKER_ONLY_TOOLS, the prose and the schema set diverge again.
        for tool in ("kanban_complete", "kanban_block", "kanban_heartbeat", "kanban_link"):
            self.assertIn(tool, WORKER_ONLY_TOOLS)


class ApplyTest(unittest.TestCase):
    def test_worker_only_tools_are_regated(self):
        patched = patch_tree(upstream_source())
        for tool in WORKER_ONLY_TOOLS:
            self.assertIn(
                registration(tool, "_check_kanban_worker_mode"),
                patched,
                f"{tool} was not re-gated",
            )

    def test_orchestrator_tools_are_left_alone(self):
        patched = patch_tree(upstream_source())
        for tool in ORCHESTRATOR_TOOLS:
            self.assertIn(registration(tool, "_check_kanban_mode"), patched)
        for tool in ("kanban_list", "kanban_unblock"):
            self.assertIn(registration(tool, "_check_kanban_orchestrator_mode"), patched)

    def test_the_import_lands_above_the_registrations(self):
        patched = patch_tree(upstream_source())
        import_at = patched.index("from tools.kanban_worker_tools import")
        first_use = patched.index("check_fn=_check_kanban_worker_mode")
        # check_fn= is evaluated at import time, so a trailing import would
        # NameError at module load rather than at first tool call.
        self.assertLess(import_at, first_use)

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(upstream_source()))

    def test_a_drifted_anchor_fails_loudly(self):
        drifted = upstream_source().replace(
            registration("kanban_complete", "_check_kanban_mode"),
            registration("kanban_complete", "_check_kanban_mode", handler="_handle_finish"),
        )
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_fails_rather_than_silently_no_opping(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(upstream_source())
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))

    def test_every_worker_only_tool_has_a_handler_mapping(self):
        self.assertEqual(set(WORKER_ONLY_TOOLS) - set(HANDLERS), set())
        # One import edit plus one per tool.
        self.assertEqual(len(build_patches()), len(WORKER_ONLY_TOOLS) + 1)


if __name__ == "__main__":
    unittest.main()
