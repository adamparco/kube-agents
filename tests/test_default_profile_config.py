"""Tests for deploy/shared/default_profile_config.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching tests/test_profile_overlay.py: PyYAML is the only
import beyond the standard library, and it already ships in every environment that runs
the agent.

This runs once per pod start against the front door's own config.yaml, and both of its
failure modes are silent. Lose a runtime key and `/sethome` forgets the home channel on
the next restart with no log line; lose an operator key and the agent quietly talks to
the wrong model endpoint. The cases below are the ones the three-way merge exists for.
"""

import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

_SHARED = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared"

# default_profile_config imports profile_overlay by name, so load that first under the
# name it expects. Same trick the module itself uses in the image, where both files sit
# in /opt/defaults/scripts.
for _name in ("profile_overlay", "default_profile_config"):
    _spec = importlib.util.spec_from_file_location(_name, _SHARED / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)

dpc = sys.modules["default_profile_config"]


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True))


def read(path):
    return yaml.safe_load(path.read_text()) or {}


class ReconcileTest(unittest.TestCase):
    """The per-key rules, exercised directly."""

    def test_untouched_key_takes_the_new_baseline(self):
        self.assertEqual({"a": 9}, dpc.reconcile({"a": 1}, {"a": 1}, {"a": 9}))

    def test_runtime_edit_wins_when_the_baseline_held_still(self):
        self.assertEqual({"a": 5}, dpc.reconcile({"a": 1}, {"a": 5}, {"a": 1}))

    def test_operator_wins_when_both_changed(self):
        self.assertEqual({"a": 7}, dpc.reconcile({"a": 1}, {"a": 5}, {"a": 7}))

    def test_runtime_only_key_survives(self):
        # Nothing else can restore it: neither baseline has ever mentioned it.
        self.assertEqual({"a": 9, "b": 2}, dpc.reconcile({"a": 1}, {"a": 1, "b": 2}, {"a": 9}))

    def test_absence_is_not_a_deletion(self):
        # save_config drops keys whose value equals a Hermes default, so a missing key
        # carries no information and must not remove anything.
        self.assertEqual({"a": 7}, dpc.reconcile({"a": 1}, {}, {"a": 7}))

    def test_baseline_dropping_a_key_removes_it(self):
        self.assertEqual({}, dpc.reconcile({"a": 1}, {"a": 1}, {}))

    def test_nested_dicts_merge_key_by_key(self):
        base = {"platforms": {"google_chat": {"typing_status_text": "x"}}}
        live = {
            "platforms": {
                "google_chat": {"typing_status_text": "x"},
                "slack": {"enabled": True, "home_channel": {"id": "C1"}},
            }
        }
        baseline = {
            "platforms": {
                "google_chat": {"typing_status_text": "x", "enabled": False},
                "slack": {"enabled": True},
            }
        }
        self.assertEqual(
            {
                "platforms": {
                    "google_chat": {"typing_status_text": "x", "enabled": False},
                    "slack": {"enabled": True, "home_channel": {"id": "C1"}},
                }
            },
            dpc.reconcile(base, live, baseline),
        )

    def test_runtime_list_replaces_when_the_baseline_held_still(self):
        # A toolset switched off in the dashboard writes a SHORTER list. Unioning would
        # put the entry back on every restart.
        self.assertEqual(
            {"t": ["a", "b"]},
            dpc.reconcile({"t": ["a", "b", "c"]}, {"t": ["a", "b"]}, {"t": ["a", "b", "c"]}),
        )

    def test_moved_baseline_list_keeps_runtime_additions(self):
        self.assertEqual(
            {"t": ["a", "b", "c", "d", "x"]},
            dpc.reconcile(
                {"t": ["a", "b", "c"]}, {"t": ["a", "b", "x"]}, {"t": ["a", "b", "c", "d"]}
            ),
        )

    def test_inputs_are_not_mutated(self):
        base, live, baseline = {"a": 1}, {"a": 1, "b": {"c": 2}}, {"a": 9}
        dpc.reconcile(base, live, baseline)
        self.assertEqual({"a": 1}, base)
        self.assertEqual({"a": 1, "b": {"c": 2}}, live)
        self.assertEqual({"a": 9}, baseline)


class RebuildTest(unittest.TestCase):
    """End-to-end against files, the way the entrypoint calls it."""

    IMAGE = {
        "model": {"provider": "custom", "base_url": "http://litellm/v1"},
        "platforms": {"google_chat": {"typing_status_text": "Kage is thinking…"}},
        "plugins": {"enabled": ["hermes_otel", "session_store"]},
    }
    OVERLAY = {
        "model": {"provider": "custom", "base_url": "http://litellm/v1"},
        "platforms": {"google_chat": {"enabled": False}, "slack": {"enabled": True}},
        "plugins": {"enabled": ["hermes_otel", "session_store", "legacy_slash_commands"]},
        "approvals": {"cron_mode": "approve"},
    }

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.image = self.tmp / "chat-template" / "config.yaml"
        self.overlay = self.tmp / "agent-config" / "profile-default.overlay.yaml"
        self.config = self.tmp / "data" / "config.yaml"
        self.baseline = self.config.with_name(dpc.BASELINE_FILENAME)
        write(self.image, self.IMAGE)
        write(self.overlay, self.OVERLAY)

    def rebuild(self, overlays=None):
        return dpc.rebuild(
            self.config,
            self.image,
            [self.overlay] if overlays is None else overlays,
        )

    def test_fresh_volume_gets_image_plus_overlay(self):
        self.rebuild()
        got = read(self.config)
        # The operator's keys land...
        self.assertTrue(got["platforms"]["slack"]["enabled"])
        self.assertEqual("approve", got["approvals"]["cron_mode"])
        # ...and the image's own keys are not dropped by them.
        self.assertEqual("Kage is thinking…", got["platforms"]["google_chat"]["typing_status_text"])
        self.assertEqual(
            ["hermes_otel", "session_store", "legacy_slash_commands"], got["plugins"]["enabled"]
        )

    def test_first_start_on_an_existing_volume_loses_nothing(self):
        # Before this code shipped, the entrypoint force-copied the image config over the
        # live file on every start, so that is what a pre-upgrade volume holds. With no
        # recorded baseline it must not be mistaken for a pile of runtime edits.
        write(self.config, self.IMAGE)
        self.assertFalse(self.baseline.exists())
        self.rebuild()
        self.assertEqual(read(self.config), read(self.baseline))

    def test_home_channel_survives_a_restart(self):
        self.rebuild()
        live = read(self.config)
        live["platforms"]["slack"]["home_channel"] = {"id": "C123", "platform": "slack"}
        live["monitoring"] = {"install_id": "d3adbeef"}
        write(self.config, live)

        self.rebuild()  # the restart

        got = read(self.config)
        self.assertEqual({"id": "C123", "platform": "slack"}, got["platforms"]["slack"]["home_channel"])
        self.assertEqual("d3adbeef", got["monitoring"]["install_id"])
        self.assertTrue(got["platforms"]["slack"]["enabled"])

    def test_a_changed_overlay_wins_over_a_stale_runtime_edit(self):
        self.rebuild()
        live = read(self.config)
        live["approvals"]["cron_mode"] = "never"
        write(self.config, live)

        overlay = dict(self.OVERLAY, approvals={"cron_mode": "auto"})
        write(self.overlay, overlay)
        self.rebuild()

        self.assertEqual("auto", read(self.config)["approvals"]["cron_mode"])

    def test_an_unchanged_overlay_leaves_a_runtime_edit_alone(self):
        self.rebuild()
        live = read(self.config)
        live["approvals"]["cron_mode"] = "never"
        write(self.config, live)

        self.rebuild()

        self.assertEqual("never", read(self.config)["approvals"]["cron_mode"])

    def test_an_image_change_reaches_the_agent(self):
        self.rebuild()
        write(self.image, dict(self.IMAGE, toolsets=["kanban"]))
        self.rebuild()
        self.assertEqual(["kanban"], read(self.config)["toolsets"])

    def test_a_withdrawn_overlay_key_disappears(self):
        self.rebuild()
        overlay = {k: v for k, v in self.OVERLAY.items() if k != "approvals"}
        write(self.overlay, overlay)
        self.rebuild()
        self.assertNotIn("approvals", read(self.config))

    def test_no_overlay_is_not_an_error(self):
        # Plain docker, or the kustomize bases: there is no operator and no ConfigMap.
        self.overlay.unlink()
        self.rebuild()
        self.assertEqual(self.IMAGE, read(self.config))

    def test_rebuild_is_idempotent(self):
        self.rebuild()
        first = read(self.config)
        self.rebuild()
        self.assertEqual(first, read(self.config))

    def test_missing_image_config_is_loud(self):
        self.image.unlink()
        with self.assertRaises(FileNotFoundError):
            self.rebuild()

    def test_baseline_records_no_runtime_state(self):
        self.rebuild()
        live = read(self.config)
        live["monitoring"] = {"install_id": "d3adbeef"}
        write(self.config, live)
        self.rebuild()
        self.assertNotIn("monitoring", read(self.baseline))


class ArgvUnionTest(unittest.TestCase):
    """`args` is the one list the image and the operator must not merely agree about.

    Every other list here is a set — plugins, toolsets, disabled_toolsets — where a
    union is the intended behaviour and a stray extra entry is at worst inert. A
    command line is a sequence: union two differing declarations and you get two argv
    words, and the process runs the first. The router MCP would then start against a
    path that does not exist and the Chat Agent would have no specialist roster.
    """

    CHAT_CONFIG = pathlib.Path(__file__).resolve().parents[1] / "agents" / "chat" / "config.yaml"

    def test_differing_args_concatenate_rather_than_replace(self):
        # The mechanism, stated once so the guard below has a reason attached.
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        image, overlay = tmp / "image.yaml", tmp / "overlay.yaml"
        write(image, {"mcp_servers": {"router": {"args": ["/opt/data/scripts/router_server.py"]}}})
        write(overlay, {"mcp_servers": {"router": {"args": ["/var/lib/kage/scripts/router_server.py"]}}})
        baseline = dpc.build_baseline(image, [overlay])
        self.assertEqual(
            baseline["mcp_servers"]["router"]["args"],
            ["/opt/data/scripts/router_server.py", "/var/lib/kage/scripts/router_server.py"],
        )

    def test_the_image_declares_the_router_script_home_independently(self):
        # So the operator's declaration can be byte-identical at ANY agentHome and the
        # union above collapses to one entry. renderConfigYAML holds the other half;
        # the operator's TestRenderConfigYAMLListsMatchChatConfig compares the two,
        # under a custom agentHome as well as the default.
        args = yaml.safe_load(self.CHAT_CONFIG.read_text())["mcp_servers"]["router"]["args"]
        self.assertEqual(args, ["${HERMES_HOME}/scripts/router_server.py"])


if __name__ == "__main__":
    unittest.main()
