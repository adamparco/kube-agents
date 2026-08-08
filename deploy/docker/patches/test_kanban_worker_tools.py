"""Unit tests for the worker-only kanban gate installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import importlib
import os
import sys
import tempfile
import types
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


def with_delegation_context(reader):
    """Patch a fake ``agent.delegation_context`` whose reader is *reader*.

    The module imports it lazily inside ``_is_delegated_child``, so a fake in
    ``sys.modules`` is enough to exercise the branch on a host with no Hermes.
    """
    fake = types.ModuleType("agent.delegation_context")
    fake.is_delegated_child_context = reader
    agent_pkg = types.ModuleType("agent")
    agent_pkg.delegation_context = fake
    return mock.patch.dict(
        sys.modules, {"agent": agent_pkg, "agent.delegation_context": fake}
    )


class DelegatedChildTest(unittest.TestCase):
    """A delegate_task child inherits ``HERMES_KANBAN_TASK`` and owns no card.

    The child runs ``run_conversation`` in the parent's own process, so the env
    var this gate keys off is the parent's and proves nothing about the child.
    Upstream's own two gates, ``_check_kanban_mode`` and
    ``_check_kanban_orchestrator_mode``, both open with the same short-circuit;
    without it this was the only kanban gate in the file that said *True* for a
    child, which would have offered it the seven worker-only tools and none of
    the five an orchestrator keeps.
    """

    def test_a_delegated_child_is_not_a_worker(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
            with with_delegation_context(lambda: True):
                self.assertFalse(check_kanban_worker_mode())

    def test_the_parent_worker_still_keeps_its_tools(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
            with with_delegation_context(lambda: False):
                self.assertTrue(check_kanban_worker_mode())

    def test_it_consults_the_real_delegation_context(self):
        """The gate takes no argument, so the module import is the only seam.

        ``check_fn`` is called with no arguments by ``tools/registry.py``, which
        means a short-circuit that did not reach ``agent.delegation_context``
        itself would be inert in the only place it matters.
        """
        module = importlib.import_module("kanban_worker_tools")
        with with_delegation_context(lambda: True):
            self.assertTrue(module._is_delegated_child())

    def test_a_host_without_hermes_is_not_a_delegated_child(self):
        """The import fails outside the image; that is not evidence of a child.

        Answering True there would hide ``kanban_complete`` and ``kanban_block``
        from every dispatcher-spawned worker in the image at once, and a worker
        with no terminal tool cannot end its run.
        """
        module = importlib.import_module("kanban_worker_tools")
        with mock.patch.dict(sys.modules, {"agent.delegation_context": None}):
            self.assertFalse(module._is_delegated_child())
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
                self.assertTrue(check_kanban_worker_mode())

    def test_a_raising_reader_leaves_the_worker_its_tools(self):
        """Uncertainty must not strand a card.

        The opposite of ``kanban_guardrail_exit._is_delegated_child``, which
        answers True here because a wrong answer there writes to the board. This
        gate only chooses which schemas ship, and
        ``_reject_delegated_child_mutation`` still refuses a child's mutations.
        """
        module = importlib.import_module("kanban_worker_tools")

        def boom():
            raise RuntimeError("no delegation context")

        with with_delegation_context(boom):
            self.assertFalse(module._is_delegated_child())
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
                self.assertTrue(check_kanban_worker_mode())

    def test_a_child_of_an_orchestrator_is_not_a_worker_either(self):
        # No HERMES_KANBAN_TASK at all: the Chat Agent delegating to a
        # specialist. The gate was already False here; the short-circuit must
        # not have turned it into a way in.
        env = {k: v for k, v in os.environ.items() if k != "HERMES_KANBAN_TASK"}
        with mock.patch.dict(os.environ, env, clear=True):
            with with_delegation_context(lambda: True):
                self.assertFalse(check_kanban_worker_mode())


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
