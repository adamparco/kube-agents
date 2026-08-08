"""Unit tests for the configurable kanban wake installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import logging
import tempfile
import unittest
from pathlib import Path

from apply_kanban_wake_kinds import ANCHOR, RELATIVE, apply
from kanban_wake_kinds import DEFAULT_WAKE_KINDS, resolve_wake_kinds, wake_kinds_for

# What agents/chat/config.yaml sets: wake the front door when a card fails,
# never when it succeeds — the notifier has already delivered that summary.
FAILURE_ONLY = ["gave_up", "crashed", "timed_out", "blocked"]


def loader(kanban=None, raises=False, not_a_dict=False):
    """Build a load_config callable for a given kanban config subtree."""

    def _load():
        if raises:
            raise RuntimeError("config unreadable")
        if not_a_dict:
            return "not a mapping"
        return {"kanban": kanban} if kanban is not None else {}

    return _load


class Event:
    def __init__(self, kind):
        self.kind = kind


class ResolveWakeKindsTest(unittest.TestCase):
    def test_unset_key_keeps_upstream_behaviour(self):
        self.assertEqual(resolve_wake_kinds(loader({})), DEFAULT_WAKE_KINDS)
        self.assertEqual(resolve_wake_kinds(loader()), DEFAULT_WAKE_KINDS)

    def test_failure_only_config_drops_completed(self):
        kinds = resolve_wake_kinds(loader({"wake_on_events": FAILURE_ONLY}))
        self.assertNotIn("completed", kinds)
        self.assertEqual(set(kinds), set(FAILURE_ONLY))

    def test_explicit_empty_list_disables_the_wake(self):
        self.assertEqual(resolve_wake_kinds(loader({"wake_on_events": []})), ())

    def test_null_value_disables_the_wake(self):
        # `wake_on_events:` with nothing after it parses as None. Read that as
        # the override the user was clearly attempting, not as "unset".
        self.assertEqual(resolve_wake_kinds(loader({"wake_on_events": None})), ())

    def test_a_bare_string_is_accepted_as_one_kind(self):
        self.assertEqual(resolve_wake_kinds(loader({"wake_on_events": "crashed"})), ("crashed",))

    def test_unknown_kinds_are_dropped_and_logged(self):
        cfg = loader({"wake_on_events": ["crashed", "compleeted", "done"]})
        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            kinds = resolve_wake_kinds(cfg)
        self.assertEqual(kinds, ("crashed",))
        # The typo has to be visible: an unknown kind never matches a real
        # event, so silently dropping it looks like the wake breaking on its own.
        self.assertIn("compleeted", "\n".join(captured.output))

    def test_duplicates_collapse_and_order_is_preserved(self):
        kinds = resolve_wake_kinds(loader({"wake_on_events": ["blocked", "crashed", "blocked"]}))
        self.assertEqual(kinds, ("blocked", "crashed"))

    def test_wrong_shape_falls_back_to_the_default(self):
        with self.assertLogs("gateway.run", level=logging.WARNING):
            kinds = resolve_wake_kinds(loader({"wake_on_events": {"crashed": True}}))
        self.assertEqual(kinds, DEFAULT_WAKE_KINDS)

    def test_an_unreadable_config_still_wakes_on_failures(self):
        # Failing closed here would mean a crashed card silently never
        # escalating, which is worse than an extra turn on a healthy one.
        self.assertEqual(resolve_wake_kinds(loader(raises=True)), DEFAULT_WAKE_KINDS)
        self.assertEqual(resolve_wake_kinds(loader(not_a_dict=True)), DEFAULT_WAKE_KINDS)

    def test_default_set_matches_the_upstream_tuple(self):
        # If a base-image bump adds a terminal kind, this test is the reminder
        # to decide whether the front door should wake for it.
        self.assertEqual(
            DEFAULT_WAKE_KINDS,
            ("completed", "gave_up", "crashed", "timed_out", "blocked"),
        )


class WakeKindsForTest(unittest.TestCase):
    def test_matches_the_upstream_expression_by_default(self):
        events = [Event("completed"), Event("commented"), Event("crashed")]
        self.assertEqual(
            wake_kinds_for(events, loader({})),
            {"completed", "crashed"},
        )

    def test_completion_alone_produces_no_wake_under_failure_only(self):
        events = [Event("completed"), Event("commented")]
        self.assertEqual(wake_kinds_for(events, loader({"wake_on_events": FAILURE_ONLY})), set())

    def test_a_failure_in_the_same_batch_still_wakes(self):
        events = [Event("completed"), Event("timed_out")]
        self.assertEqual(
            wake_kinds_for(events, loader({"wake_on_events": FAILURE_ONLY})),
            {"timed_out"},
        )

    def test_events_without_a_kind_are_ignored(self):
        self.assertEqual(wake_kinds_for([object()], loader({})), set())


class Adapter:
    def __init__(self, supports_async_delivery):
        self.supports_async_delivery = supports_async_delivery


class NonPushAdapterTest(unittest.TestCase):
    """The narrowing applies to the push path only.

    Where the notifier skips its own ``send()`` it says the wake self-post IS
    the delivery, so a narrowed set there loses the result instead of saving a
    turn.
    """

    def test_a_non_push_adapter_still_wakes_on_completion(self):
        events = [Event("completed")]
        cfg = loader({"wake_on_events": FAILURE_ONLY})
        self.assertEqual(wake_kinds_for(events, cfg, adapter=Adapter(False)), {"completed"})

    def test_a_push_adapter_still_honours_the_narrowed_set(self):
        events = [Event("completed")]
        cfg = loader({"wake_on_events": FAILURE_ONLY})
        self.assertEqual(wake_kinds_for(events, cfg, adapter=Adapter(True)), set())

    def test_an_adapter_that_does_not_declare_the_flag_counts_as_push(self):
        # gateway.wake.adapter_supports_push defaults a missing attribute to
        # True; reading it as non-push would restore the redundant turn on
        # every Slack and Google Chat card.
        events = [Event("completed")]
        cfg = loader({"wake_on_events": FAILURE_ONLY})
        self.assertEqual(wake_kinds_for(events, cfg, adapter=object()), set())

    def test_an_explicit_empty_list_does_not_silence_the_non_push_path(self):
        # `wake_on_events: []` means "do not re-read an answer already
        # delivered". Nothing was delivered here, so there is no such answer.
        events = [Event("completed")]
        self.assertEqual(
            wake_kinds_for(events, loader({"wake_on_events": []}), adapter=Adapter(False)),
            {"completed"},
        )
        self.assertEqual(
            wake_kinds_for(events, loader({"wake_on_events": []}), adapter=Adapter(True)),
            set(),
        )

    def test_omitting_the_adapter_leaves_the_config_in_charge(self):
        # The notifier always passes it; the default keeps every other caller
        # (and the pre-existing tests above) on the documented config path.
        events = [Event("completed")]
        cfg = loader({"wake_on_events": FAILURE_ONLY})
        self.assertEqual(wake_kinds_for(events, cfg), set())


# The notifier loop, reduced to the two lines the patch rewrites.
UPSTREAM_WATCHER = '''\
class GatewayKanbanWatchers:
    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        for d in deliveries:
            if True:
                if True:
                    if True:
                        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
                        _wake_kinds = {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}
                        if _wake_kinds:
                            pass
'''


def patch_tree(source):
    """Write ``source`` as gateway/kanban_watchers.py under a temp root and patch it."""
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


class ApplyTest(unittest.TestCase):
    def test_the_anchor_matches_upstream_exactly_once(self):
        self.assertEqual(UPSTREAM_WATCHER.count(ANCHOR), 1)

    def test_the_hardcoded_tuple_is_replaced_by_the_helper(self):
        patched = patch_tree(UPSTREAM_WATCHER)
        # `adapter=adapter` is part of the assertion: the notifier must hand the
        # helper the adapter, or the non-push carve-out above never engages.
        self.assertIn('_wake_kinds = _wake_kinds_for(d["events"], adapter=adapter)', patched)
        # Only the prose reference in the surrounding comment may survive.
        self.assertNotIn("_WAKE_KINDS = (", patched)
        self.assertNotIn("in _WAKE_KINDS}", patched)

    def test_the_import_is_appended(self):
        patched = patch_tree(UPSTREAM_WATCHER)
        self.assertIn("from gateway.kanban_wake_kinds import wake_kinds_for", patched)

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(UPSTREAM_WATCHER))

    def test_a_drifted_anchor_fails_loudly(self):
        drifted = UPSTREAM_WATCHER.replace('"blocked")', '"blocked", "abandoned")')
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_fails_rather_than_silently_no_opping(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM_WATCHER)
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
