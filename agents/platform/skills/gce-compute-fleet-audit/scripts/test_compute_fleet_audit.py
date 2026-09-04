#!/usr/bin/env python3
"""Unit tests for compute_fleet_audit.py."""

import datetime
import hashlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import compute_fleet_audit as cf  # noqa: E402

FLEET_AUDIT_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "..", "fleet-audit", "scripts")
sys.path.insert(0, os.path.abspath(FLEET_AUDIT_SCRIPTS))

NOW = datetime.datetime(2026, 9, 4, tzinfo=datetime.timezone.utc)
OLD_STAMP = "2020-01-01T00:00:00.000-08:00"
FRESH_STAMP = "2026-09-01T00:00:00.000-08:00"


def run_of(rc: int, stdout: str = "", stderr: str = "") -> cf.Run:
    return cf.Run(["gcloud"], rc, stdout, stderr, 0.01)


def one_running_instance(name: str = "vm-1", zone: str = "us-central1-a") -> str:
    return (
        f'[{{"name": "{name}", "status": "RUNNING", '
        f'"zone": "https://x/projects/proj-1/zones/{zone}"}}]'
    )


class RunAndGateTest(unittest.TestCase):
    def test_gate_closes_on_nonzero_rc(self):
        parsed, result = cf.run_and_gate(["gcloud"], run=lambda argv, **kw: run_of(1, "[]", "boom"))
        self.assertIsNone(parsed)
        self.assertEqual(result.rc, 1)

    def test_gate_closes_on_empty_stdout(self):
        parsed, _ = cf.run_and_gate(["gcloud"], run=lambda argv, **kw: run_of(0, "   "))
        self.assertIsNone(parsed)

    def test_gate_closes_on_non_json(self):
        parsed, _ = cf.run_and_gate(["gcloud"], run=lambda argv, **kw: run_of(0, "ERROR: nope"))
        self.assertIsNone(parsed)

    def test_gate_opens_on_clean_json(self):
        parsed, _ = cf.run_and_gate(["gcloud"], run=lambda argv, **kw: run_of(0, '[{"name": "a"}]'))
        self.assertEqual(parsed, [{"name": "a"}])


class HelpersTest(unittest.TestCase):
    def test_last_segment(self):
        self.assertEqual(cf._last_segment("https://x/zones/us-central1-a"), "us-central1-a")
        self.assertEqual(cf._last_segment(""), "")

    def test_redact_leaves_the_marker_line_readable(self):
        """The excerpt has to survive redaction or the finding says nothing.

        `adopt_collector_evidence` overwrites the model's excerpt with this
        string, so a redactor that swallowed §2.1's own marker would publish a
        `critical` finding whose evidence line is `[REDACTED]`.
        """
        self.assertEqual(cf.redact("startup-script exit status 1"), "startup-script exit status 1")

    def test_redact_removes_an_oauth_token(self):
        """The SOP's red line, enforced here rather than by the model.

        Before the manifest conversion the model retyped every excerpt and
        could drop a secret on the way; the collector's excerpt now ships
        verbatim, so scrubbing is this file's job.
        """
        out = cf.redact("curl -H ya29.a0AfH6SMBxxxxxxxxxxxxxxxx failed: startup-script exit status 1")
        self.assertNotIn("ya29.a0AfH6SMB", out)
        self.assertIn(cf.REDACTED, out)
        self.assertIn("startup-script exit status 1", out)

    def test_redact_removes_a_private_key_header(self):
        self.assertNotIn("PRIVATE KEY", cf.redact("-----BEGIN RSA PRIVATE KEY-----"))

    def test_redact_removes_a_named_secret_assignment(self):
        self.assertNotIn("hunter2hunter2", cf.redact("password=hunter2hunter2"))

    def test_redact_clips_to_the_document_excerpt_ceiling(self):
        self.assertLessEqual(len(cf.redact("x" * 5000)), cf.MAX_EXCERPT_CHARS)


class StartupScriptTest(unittest.TestCase):
    def test_flags_the_exit_status_marker(self):
        hit = cf.check_startup_script("vm-1", "us-central1-a", "boot\nstartup-script exit status 1\nmore\n")
        self.assertEqual(hit["object"], "ComputeInstance/us-central1-a/vm-1")
        self.assertEqual(hit["excerpt"], "startup-script exit status 1")

    def test_flags_the_finished_with_error_marker(self):
        hit = cf.check_startup_script("vm-2", "us-central1-a", "Finished running startup scripts with error\n")
        self.assertEqual(hit["object"], "ComputeInstance/us-central1-a/vm-2")

    def test_does_not_flag_a_clean_boot(self):
        self.assertIsNone(cf.check_startup_script("vm-1", "us-central1-a", "Finished running startup scripts.\n"))

    def test_the_match_is_case_sensitive(self):
        """Unchanged from the pre-manifest revision: the markers are matched as
        literal substrings, so a differently-cased line is not a hit. Pinned so
        a later 'improvement' to the matcher is a deliberate change to what
        gets flagged rather than a side effect."""
        self.assertIsNone(cf.check_startup_script("vm-1", "us-central1-a", "STARTUP-SCRIPT EXIT STATUS 1\n"))

    def test_first_match_wins_and_the_scan_stops(self):
        hit = cf.check_startup_script("vm-1", "us-central1-a",
            "startup-script exit status 1\nFinished running startup scripts with error\n",
        )
        self.assertEqual(hit["excerpt"], "startup-script exit status 1")

    def test_empty_serial_output_is_not_a_crash(self):
        self.assertIsNone(cf.check_startup_script("vm-1", "us-central1-a", ""))

    def test_the_hit_hands_the_gke_exclusion_back_to_the_model(self):
        """§2.1's Do-NOT-flag limb excludes GKE-managed nodes and this
        collector does not apply it, so the candidate says so rather than
        presenting the hit as fully mechanical."""
        hit = cf.check_startup_script("gke-pool-1-abcd", "us-central1-a", "startup-script exit status 1\n")
        self.assertEqual(hit["needs_triage"], cf.TRIAGE_GKE_NODE)

    def test_a_secret_in_the_serial_line_never_reaches_the_excerpt(self):
        hit = cf.check_startup_script("vm-1", "us-central1-a", "token: ya29.c.b0Aaekm1Jxxxxxxxxxxxxxxxxxxxxxx startup-script exit status 1\n"
        )
        self.assertNotIn("ya29.c.b0Aaekm1J", hit["excerpt"])


class RunningInstancesTest(unittest.TestCase):
    def test_keeps_running_instances_and_splits_the_zone(self):
        self.assertEqual(
            cf.running_instances(
                [{"name": "vm-1", "status": "RUNNING", "zone": "https://x/zones/us-central1-a"}]
            ),
            [("vm-1", "us-central1-a")],
        )

    def test_drops_a_terminated_instance(self):
        self.assertEqual(
            cf.running_instances(
                [{"name": "vm-1", "status": "TERMINATED", "zone": "https://x/zones/us-central1-a"}]
            ),
            [],
        )

    def test_drops_an_instance_with_no_addressable_zone(self):
        self.assertEqual(cf.running_instances([{"name": "vm-1", "status": "RUNNING"}]), [])

    def test_a_non_object_item_is_not_a_crash(self):
        self.assertEqual(cf.running_instances(["vm-1", None]), [])


class OrphanedSnapshotTest(unittest.TestCase):
    def snapshot(self, **overrides) -> dict:
        base = {
            "name": "snap-1",
            "sourceDisk": "https://x/projects/proj-1/zones/us-central1-a/disks/gone",
            "creationTimestamp": OLD_STAMP,
        }
        base.update(overrides)
        return base

    def test_flags_an_old_snapshot_of_a_deleted_disk(self):
        hit = cf.check_orphaned_snapshot(self.snapshot(), set(), NOW)
        self.assertEqual(hit["object"], "Snapshot/snap-1")
        self.assertIn('"sourceDisk": "gone"', hit["excerpt"])
        self.assertEqual(hit["needs_triage"], cf.TRIAGE_RETENTION_HOLD)

    def test_does_not_flag_when_the_disk_is_still_there_by_name(self):
        self.assertIsNone(cf.check_orphaned_snapshot(self.snapshot(), {"gone"}, NOW))

    def test_does_not_flag_when_the_disk_is_still_there_by_self_link(self):
        """A `sourceDisk` arrives as a bare name in some payloads and as a full
        `selfLink` in others; indexing only one form deletes live disks'
        snapshots."""
        link = "https://x/projects/proj-1/zones/us-central1-a/disks/gone"
        self.assertIsNone(cf.check_orphaned_snapshot(self.snapshot(), {link}, NOW))

    def test_does_not_flag_a_snapshot_under_a_resource_policy(self):
        self.assertIsNone(
            cf.check_orphaned_snapshot(self.snapshot(resourcePolicies=["daily"]), set(), NOW)
        )

    def test_does_not_flag_a_snapshot_inside_the_ninety_day_window(self):
        self.assertIsNone(
            cf.check_orphaned_snapshot(self.snapshot(creationTimestamp=FRESH_STAMP), set(), NOW)
        )

    def test_does_not_flag_a_snapshot_with_no_source_disk(self):
        self.assertIsNone(cf.check_orphaned_snapshot(self.snapshot(sourceDisk=""), set(), NOW))

    def test_an_unparseable_timestamp_is_not_a_crash_and_not_a_finding(self):
        self.assertIsNone(
            cf.check_orphaned_snapshot(self.snapshot(creationTimestamp="whenever"), set(), NOW)
        )

    def test_a_naive_timestamp_is_not_a_crash(self):
        """`now` is offset-aware, so subtracting a naive stamp raises
        `TypeError` rather than `ValueError`. Catching only the parse error
        lost the whole project to one badly-stamped snapshot."""
        self.assertIsNone(
            cf.check_orphaned_snapshot(
                self.snapshot(creationTimestamp="2020-01-01T00:00:00"), set(), NOW
            )
        )

    def test_a_missing_timestamp_is_not_a_finding(self):
        self.assertIsNone(
            cf.check_orphaned_snapshot(self.snapshot(creationTimestamp=""), set(), NOW)
        )

    def test_active_disk_index_carries_both_spellings(self):
        index = cf.active_disk_index([{"name": "d1", "selfLink": "https://x/disks/d1"}])
        self.assertEqual(index, {"d1", "https://x/disks/d1"})

    def test_active_disk_index_tolerates_a_non_object(self):
        self.assertEqual(cf.active_disk_index([None, "d1"]), set())


class CollectProjectTest(unittest.TestCase):
    def fake_run(self, responses: dict) -> cf.RunFn:
        """`responses` maps a substring of the joined argv to a `Run`. First
        match wins, in insertion order, so a test can stub only the calls it
        cares about."""

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(f"unstubbed command: {joined}")

        return run

    def clean(self, **overrides) -> dict:
        responses = {
            "instances list": run_of(0, one_running_instance()),
            "get-serial-port-output": run_of(0, "boot ok\n"),
            "disks list": run_of(0, "[]"),
            "snapshots list": run_of(0, "[]"),
        }
        responses.update(overrides)
        return responses

    # --- schema conformance ------------------------------------------------ #

    def test_a_collected_target_carries_every_required_key(self):
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        self.assertEqual(entry["name"], "project/proj-1")
        self.assertEqual(entry["project"], "proj-1")
        self.assertEqual(entry["location"], "global")
        self.assertEqual(entry["outcome"], "collected")
        for key in ("commands", "candidates", "checks_not_applicable"):
            self.assertIsInstance(entry[key], list, key)

    def test_a_project_target_carries_no_autopilot_key(self):
        """§6: the mode is a property of a cluster, and `false` on a target
        that stands for a project reads as a fleet of Standard clusters."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        self.assertNotIn("autopilot", entry)

    def test_every_commands_entry_is_an_object_with_a_string_check_and_int_rc(self):
        """`cross_check_manifest` reads `entry.get("check")` with no isinstance
        guard and compares `rc == 0` as an integer: a bare string in the list
        crashes `finish`, and a `"0"` string silently fails the claim."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        for record in entry["commands"]:
            self.assertIsInstance(record, dict)
            self.assertIsInstance(record["check"], str)
            self.assertEqual(record["check"], record["check"].strip())
            self.assertIsInstance(record["rc"], int)
            self.assertNotIsInstance(record["rc"], bool)
            self.assertIsInstance(record["command"], str)
            self.assertGreaterEqual(len(record["command"]), 8)
            self.assertLessEqual(len(record["command"]), cf.MAX_COMMAND_CHARS)

    def test_every_not_applicable_entry_is_an_object_with_a_usable_reason(self):
        """`validate_na_reason` refuses a reason under sixteen characters, and
        `cross_check_manifest` calls `.get` on the entry without a guard."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        for record in entry["checks_not_applicable"]:
            self.assertIsInstance(record, dict)
            self.assertIsInstance(record["check"], str)
            self.assertGreaterEqual(len(record["reason"]), 16)

    def test_every_candidate_carries_the_identity_fields(self):
        responses = self.clean(
            **{
                "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
                "snapshots list": run_of(
                    0,
                    '[{"name": "snap-1", "sourceDisk": "https://x/disks/gone", '
                    f'"creationTimestamp": "{OLD_STAMP}"}}]',
                ),
            }
        )
        entry = cf.collect_project("proj-1", run=self.fake_run(responses))
        self.assertEqual(len(entry["candidates"]), 2)
        for candidate in entry["candidates"]:
            self.assertIn(candidate["check"], cf.SEVERITY)
            self.assertEqual(candidate["namespace"], "")
            self.assertTrue(candidate["object"])
            self.assertIn(candidate["severity"], ("critical", "major", "minor"))
            self.assertTrue(candidate["excerpt"])
            self.assertTrue(candidate["impact"])
            self.assertIn("needs_triage", candidate)

    def test_candidate_identities_are_unique_within_the_target(self):
        """`finish` refuses a document holding two findings that agree on
        (check, cluster, namespace, object)."""
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
                        "snapshots list": run_of(
                            0,
                            '[{"name": "snap-1", "sourceDisk": "https://x/disks/a", '
                            f'"creationTimestamp": "{OLD_STAMP}"}}, '
                            '{"name": "snap-2", "sourceDisk": "https://x/disks/b", '
                            f'"creationTimestamp": "{OLD_STAMP}"}}]',
                        ),
                    }
                )
            ),
        )
        ids = [(c["check"], c["namespace"], c["object"]) for c in entry["candidates"]]
        self.assertEqual(len(ids), len(set(ids)))

    # --- detection through the collector ----------------------------------- #

    def test_the_severities_are_the_sop_severities(self):
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
                        "snapshots list": run_of(
                            0,
                            '[{"name": "snap-1", "sourceDisk": "https://x/disks/gone", '
                            f'"creationTimestamp": "{OLD_STAMP}"}}]',
                        ),
                    }
                )
            ),
        )
        by_check = {c["check"]: c["severity"] for c in entry["candidates"]}
        self.assertEqual(by_check["gce-startup-script-status"], "critical")
        self.assertEqual(by_check["orphaned-snapshots"], "minor")

    def test_a_snapshot_of_a_live_disk_is_not_flagged_through_the_collector(self):
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "disks list": run_of(0, '[{"name": "gone"}]'),
                        "snapshots list": run_of(
                            0,
                            '[{"name": "snap-1", "sourceDisk": "https://x/disks/gone", '
                            f'"creationTimestamp": "{OLD_STAMP}"}}]',
                        ),
                    }
                )
            ),
        )
        self.assertEqual(entry["candidates"], [])

    def test_a_project_with_nothing_wrong_is_collected_with_no_candidates(self):
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry["candidates"], [])
        self.assertNotIn("limitations", entry)

    def test_zero_instances_is_a_reading_not_a_gap(self):
        """Absence that provably means zero is a result. A project with no VM
        still records the enumeration against §2.1 and declares nothing."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(0, "[]")})))
        self.assertIn(cf.STARTUP_SLUG, [c["check"] for c in entry["commands"]])
        self.assertNotIn(cf.STARTUP_SLUG, [c["check"] for c in entry["checks_not_applicable"]])
        self.assertNotIn("limitations", entry)

    # --- the three checks nobody implemented -------------------------------- #

    def test_the_three_unimplemented_checks_are_declared_unevaluated(self):
        """Silence would be read as a clean result. The declaration is what
        stops `finish` accepting a `checks_run` claim on a check no code
        performs, and what stops a previous finding on one being announced
        resolved because this run said nothing about it.
        """
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        declared = {e["check"]: e["reason"] for e in entry["checks_not_applicable"]}
        for slug in ("mig-autoscaler-flapping", "ops-agent-guest-health", "sole-tenant-headroom"):
            self.assertIn(slug, declared)
            self.assertTrue(declared[slug].startswith(cf.UNEVALUATED_MARKER), slug)

    def test_no_command_is_recorded_for_a_check_declared_inapplicable(self):
        """Rule 6.5 exists because one broad read recorded against every slug
        it feeds corroborates a claim the collector meant to refuse. The
        collector filters at the source rather than relying on the reader."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        declared = {e["check"] for e in entry["checks_not_applicable"]}
        recorded = {c["check"] for c in entry["commands"]}
        self.assertEqual(declared & recorded, set())

    def test_no_candidate_is_emitted_for_an_unimplemented_check(self):
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(self.clean(**{"get-serial-port-output": run_of(0, "startup-script exit status 1\n")})),
        )
        emitted = {c["check"] for c in entry["candidates"]}
        self.assertEqual(emitted - {cf.STARTUP_SLUG, cf.SNAPSHOT_SLUG}, set())

    # --- gcloud failure handling -------------------------------------------- #

    def test_a_failed_instances_list_gate_fails_the_whole_target(self):
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(1, "", "PERMISSION_DENIED")}))
        )
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn(cf.STARTUP_SLUG, entry["error"])
        self.assertIn("rc=1", entry["error"])
        self.assertIn("PERMISSION_DENIED", entry["error"])
        self.assertNotIn("candidates", entry)

    def test_a_failed_disks_list_gate_fails_the_target_rather_than_skipping_a_check(self):
        """The pre-manifest revision skipped the snapshot check silently and
        left the project in scope carrying one check, with nothing saying the
        other had been abandoned."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean(**{"disks list": run_of(1, "", "denied")})))
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn(cf.SNAPSHOT_SLUG, entry["error"])

    def test_a_failed_snapshots_list_gate_fails_the_target(self):
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"snapshots list": run_of(1, "", "denied")}))
        )
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn(cf.SNAPSHOT_SLUG, entry["error"])

    def test_non_json_output_gates_closed(self):
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(0, "Updates are available")}))
        )
        self.assertEqual(entry["outcome"], "gate-failed")

    def test_a_json_object_where_an_array_was_expected_gates_closed(self):
        """A dict passes `json.loads` and iterating it yields keys, so reading
        zero items off an error envelope would report an empty, healthy
        project."""
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"snapshots list": run_of(0, '{"error": "nope"}')}))
        )
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("not a JSON array", entry["error"])

    def test_the_error_is_clipped_at_the_source(self):
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(1, "", "x" * 5000)}))
        )
        self.assertLessEqual(len(entry["error"]), cf.ERROR_CLIP_CHARS)

    def test_a_gate_failed_target_still_appears_in_the_manifest(self):
        """A target missing from `clusters[]` is indistinguishable from a
        target that does not exist, and the document is then free to publish a
        fleet-wide all-clear over it."""
        manifest = cf.collect_fleet(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(1, "", "denied")}))
        )
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["project/proj-1"])

    def test_one_failed_serial_read_costs_coverage_not_the_project(self):
        seen = []

        def run(argv, **kwargs):
            joined = " ".join(argv)
            seen.append(joined)
            if "instances list" in joined:
                return run_of(
                    0,
                    '[{"name": "vm-1", "status": "RUNNING", "zone": "https://x/zones/z1"}, '
                    '{"name": "vm-2", "status": "RUNNING", "zone": "https://x/zones/z1"}]',
                )
            if "get-serial-port-output vm-1" in joined:
                return run_of(1, "", "instance not ready")
            if "get-serial-port-output vm-2" in joined:
                return run_of(0, "startup-script exit status 1\n")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual([c["object"] for c in entry["candidates"]], ["ComputeInstance/z1/vm-2"])
        self.assertIn("vm-1 (z1)", entry["limitations"])
        self.assertIn(cf.STARTUP_SLUG, [c["check"] for c in entry["commands"]])

    def test_every_serial_read_failing_is_unevaluated_not_a_clean_fleet(self):
        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, one_running_instance())
            if "get-serial-port-output" in joined:
                return run_of(1, "", "instance not ready")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        self.assertEqual(entry["outcome"], "collected")
        declared = {e["check"]: e["reason"] for e in entry["checks_not_applicable"]}
        self.assertIn(cf.STARTUP_SLUG, declared)
        self.assertTrue(declared[cf.STARTUP_SLUG].startswith(cf.UNEVALUATED_MARKER))
        self.assertNotIn(cf.STARTUP_SLUG, [c["check"] for c in entry["commands"]])
        self.assertIn("could not be evaluated", entry["limitations"])

    def test_an_empty_serial_body_counts_as_unread(self):
        """`rc == 0` with nothing on stdout is not a clean read of a console
        with no errors in it — it is a read that returned nothing."""

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, one_running_instance())
            if "get-serial-port-output" in joined:
                return run_of(0, "   ")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        self.assertIn(cf.STARTUP_SLUG, {e["check"] for e in entry["checks_not_applicable"]})

    # --- provenance ---------------------------------------------------------- #

    def test_the_recorded_command_is_the_argv_that_ran(self):
        """The pre-manifest revision recorded `--project=<id>` while running
        `--project <id>`, and recorded the project-wide enumeration for a
        finding produced by a per-instance read."""
        seen = []

        def run(argv, **kwargs):
            joined = " ".join(argv)
            seen.append(joined)
            if "instances list" in joined:
                return run_of(0, one_running_instance())
            if "get-serial-port-output" in joined:
                return run_of(0, "boot ok\n")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        for record in entry["commands"]:
            for part in record["command"].split(" && "):
                self.assertIn(part.strip(), seen)

    def test_the_serial_read_names_port_one(self):
        """§2.1's command and its evidence example both spell `--port=1`, and
        the model copies this string into `checks_run`."""
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(self.clean()),
        )
        startup = next(c for c in entry["commands"] if c["check"] == cf.STARTUP_SLUG)
        self.assertIn("--port=1", startup["command"])

    def test_a_joined_command_stays_under_the_harness_ceiling(self):
        """An over-length `command` is not a clipped field: `validate_check_command`
        refuses the whole document, so a project with enough VMs would publish
        nothing at all."""
        instances = ",".join(
            f'{{"name": "vm-{i}", "status": "RUNNING", "zone": "https://x/zones/us-central1-a"}}'
            for i in range(200)
        )

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, f"[{instances}]")
            if "get-serial-port-output" in joined:
                return run_of(0, "boot ok\n")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        startup = next(c for c in entry["commands"] if c["check"] == cf.STARTUP_SLUG)
        self.assertLessEqual(len(startup["command"]), cf.MAX_COMMAND_CHARS)
        self.assertIn("more read(s) of the same shape", startup["command"])


class CollectFleetTest(unittest.TestCase):
    def stub(self):
        def run(argv, **kwargs):
            return run_of(0, "[]")

        return run

    def test_sweeps_every_monitored_project(self):
        os.environ["MONITORED_PROJECT_IDS"] = "p-a, p-b"
        try:
            manifest = cf.collect_fleet(run=self.stub())
        finally:
            os.environ.pop("MONITORED_PROJECT_IDS", None)
        self.assertEqual(
            sorted(c["name"] for c in manifest["clusters"]), ["project/p-a", "project/p-b"]
        )

    def test_single_project_override_bypasses_the_environment(self):
        os.environ["MONITORED_PROJECT_IDS"] = "p-a,p-b"
        try:
            manifest = cf.collect_fleet("only-this", run=self.stub())
        finally:
            os.environ.pop("MONITORED_PROJECT_IDS", None)
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["project/only-this"])

    def test_one_project_crashing_costs_that_project_and_no_other(self):
        """The SOP redirects stdout into the manifest file, so an exception
        escaping `collect_fleet` truncates it — the run loses every project to
        one bad object instead of one."""
        os.environ["MONITORED_PROJECT_IDS"] = "p-a,p-b"

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "p-a" in joined:
                raise TypeError("unmodelled")
            return run_of(0, "[]")

        try:
            manifest = cf.collect_fleet(run=run)
        finally:
            os.environ.pop("MONITORED_PROJECT_IDS", None)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/p-a"]["outcome"], "gate-failed")
        self.assertIn("TypeError", by_name["project/p-a"]["error"])
        self.assertEqual(by_name["project/p-b"]["outcome"], "collected")

    def test_no_project_resolved_still_produces_a_target(self):
        """An empty `clusters` list reads as a fleet with nothing in it, which
        is a clean, fully covered scope."""
        for var in ("MONITORED_PROJECT_IDS", "GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID"):
            os.environ.pop(var, None)
        manifest = cf.collect_fleet(run=lambda argv, **kw: run_of(1, "", "no auth"))
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["project/unknown"])
        self.assertEqual(manifest["clusters"][0]["outcome"], "gate-failed")

    def test_the_manifest_carries_the_top_level_contract(self):
        manifest = cf.collect_fleet("proj-1", run=self.stub())
        self.assertEqual(manifest["version"], cf.MANIFEST_VERSION)
        self.assertEqual(manifest["audit"], "gce-compute-fleet-audit")
        self.assertEqual(manifest["checks_revision"], cf.CHECKS_REVISION)
        for key in ("started_at", "finished_at"):
            self.assertRegex(manifest[key], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_target_names_are_unique_across_the_manifest(self):
        """`cross_check_manifest` builds `{c["name"]: c}`, so a duplicate name
        makes the earlier target invisible to all six rejection rules."""
        os.environ["MONITORED_PROJECT_IDS"] = "p-a,p-b,p-c"
        try:
            manifest = cf.collect_fleet(run=self.stub())
        finally:
            os.environ.pop("MONITORED_PROJECT_IDS", None)
        names = [c["name"] for c in manifest["clusters"]]
        self.assertEqual(len(names), len(set(names)))


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    """`collect_fleet`'s real output, run through `audit_report`'s own
    `cross_check_manifest` — the same integration shape
    `test_networking_audit.py` uses for the networking stream."""

    AUDIT = "gce-compute-fleet-audit"

    def fleet(self, **overrides) -> dict:
        responses = {
            "instances list": run_of(0, one_running_instance()),
            "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
            "disks list": run_of(0, "[]"),
            "snapshots list": run_of(0, "[]"),
        }
        responses.update(overrides)

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(f"unstubbed command: {joined}")

        return cf.collect_fleet("proj-1", run=run)

    @staticmethod
    def _scope_entry(entry: dict) -> dict:
        """The SOP's copy rule, applied literally: copy `commands` minus any
        slug this same target declares not-applicable, and carry the
        collector's `checks_not_applicable` and `limitations` through
        untouched."""
        na_slugs = {d["check"] for d in entry.get("checks_not_applicable") or []}
        out = {
            "name": entry["name"],
            "location": entry["location"],
            "project": entry["project"],
            "checks_run": [
                {"check": c["check"], "command": c["command"]}
                for c in entry["commands"]
                if c["check"] not in na_slugs
            ],
            "checks_not_applicable": entry.get("checks_not_applicable") or [],
        }
        if entry.get("limitations"):
            out["limitations"] = entry["limitations"]
        return out

    @staticmethod
    def _finding(candidate: dict, cluster: str) -> dict:
        """A candidate turned into the document finding the SOP asks the model
        to write: the collector's fields verbatim, plus the recommendation and
        remediation only the model authors."""
        return {
            "check": candidate["check"],
            "severity": candidate["severity"],
            "title": "Boot-time configuration never applied on a running instance",
            "cluster": cluster,
            "namespace": candidate["namespace"],
            "object": candidate["object"],
            "impact": candidate["impact"],
            "evidence": {
                "command": "gcloud compute instances list --project proj-1 --format=json",
                "excerpt": candidate["excerpt"],
            },
            "recommendation": {
                "action": "Correct the boot metadata on the instance template that produced this VM.",
                "rationale": (
                    "Resetting the instance would clear the symptom and reproduce it on the next "
                    "boot, because the failing script is baked into the template."
                ),
                "risk": "The instance has to reboot before the corrected script runs.",
            },
            "remediation": {"kind": "manual", "path": "", "note": "Update the instance template."},
        }

    def _document(self, manifest: dict) -> dict:
        entries = [self._scope_entry(e) for e in manifest["clusters"]]
        findings = []
        for entry in manifest["clusters"]:
            for candidate in entry.get("candidates") or []:
                findings.append(self._finding(candidate, entry["name"]))
        return {
            "audit": self.AUDIT,
            "scope": {"clusters": entries, "skipped": []},
            "findings": findings,
        }

    def test_the_copy_recipe_publishes_on_the_first_attempt(self):
        import audit_report

        manifest = self.fleet()
        data = self._document(manifest)
        audit_report.cross_check_manifest(data, manifest)
        audit_report.validate_findings(data, self.AUDIT)

    def test_the_run_is_not_partial_when_every_readable_check_ran(self):
        """The three unimplemented checks leave the coverage denominator via
        `checks_not_applicable`, so a fleet the collector fully covered does
        not report a gap it can never close."""
        import audit_report

        manifest = self.fleet(**{"get-serial-port-output": run_of(0, "boot ok\n")})
        data = self._document(manifest)
        audit_report.cross_check_manifest(data, manifest)
        self.assertEqual(audit_report.coverage_gaps(data), [])

    def test_the_collectors_evidence_is_adopted_onto_the_model_finding(self):
        """The whole point of the conversion: the excerpt that ships is the
        collector's observed line, not the model's retyped one."""
        import audit_report

        manifest = self.fleet()
        data = self._document(manifest)
        data["findings"][0]["evidence"]["excerpt"] = "the model made this up"
        audit_report.adopt_collector_evidence(data["findings"], manifest)
        self.assertEqual(data["findings"][0]["evidence"]["excerpt"], "startup-script exit status 1")

    def test_claiming_an_unimplemented_check_is_rejected(self):
        """Rule 6.5. The collector declared the slug inapplicable, so a
        document asserting it ran is refused however the command is spelled."""
        import audit_report

        manifest = self.fleet()
        data = self._document(manifest)
        data["scope"]["clusters"][0]["checks_run"].append(
            {
                "check": "mig-autoscaler-flapping",
                "command": "gcloud compute instance-groups managed list --project proj-1 --format=json",
            }
        )
        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.cross_check_manifest(data, manifest)
        self.assertIn("mig-autoscaler-flapping", str(caught.exception))

    def test_a_gate_failed_target_left_out_of_the_document_is_rejected(self):
        """Rule 6.2. A project the collector could not read has to be
        accounted for somewhere, or the run publishes an all-clear over it."""
        import audit_report

        manifest = self.fleet(**{"instances list": run_of(1, "", "PERMISSION_DENIED")})
        data = {"audit": self.AUDIT, "scope": {"clusters": [], "skipped": []}, "findings": []}
        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.cross_check_manifest(data, manifest)
        self.assertIn("project/proj-1", str(caught.exception))

    def test_a_gate_failed_target_routed_to_skipped_publishes(self):
        import audit_report

        manifest = self.fleet(**{"instances list": run_of(1, "", "PERMISSION_DENIED")})
        data = {
            "audit": self.AUDIT,
            "scope": {
                "clusters": [],
                "skipped": [
                    {
                        "cluster": "project/proj-1",
                        "reason": manifest["clusters"][0]["error"],
                    }
                ],
            },
            "findings": [],
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_collected_target_dropped_from_the_document_is_rejected(self):
        """Rule 6.1. On 2026-08-29 a collector read four clusters, the document
        named one, and `finish` published a full-fleet all-clear over a quarter
        of the fleet."""
        import audit_report

        manifest = self.fleet()
        data = {"audit": self.AUDIT, "scope": {"clusters": [], "skipped": []}, "findings": []}
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)

    def test_a_check_the_collector_never_recorded_is_rejected(self):
        """Rule 6.4."""
        import audit_report

        manifest = self.fleet()
        data = self._document(manifest)
        data["scope"]["clusters"][0]["checks_run"] = [
            {"check": "gce-startup-script-status", "command": "gcloud compute instances list --project proj-1"},
            {"check": "orphaned-snapshots", "command": "gcloud compute snapshots list --project proj-1"},
        ]
        manifest["clusters"][0]["commands"] = []
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)

    def test_an_unreadable_target_claiming_checks_without_limitations_is_rejected(self):
        """Rule 6.3. Hand-collection is legal; reporting it as a clean full
        read is not."""
        import audit_report

        manifest = self.fleet(**{"instances list": run_of(1, "", "PERMISSION_DENIED")})
        data = {
            "audit": self.AUDIT,
            "scope": {
                "clusters": [
                    {
                        "name": "project/proj-1",
                        "location": "global",
                        "project": "proj-1",
                        "checks_run": [
                            {
                                "check": "gce-startup-script-status",
                                "command": "gcloud compute instances list --project proj-1 --format=json",
                            }
                        ],
                    }
                ],
                "skipped": [],
            },
            "findings": [],
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)

    def test_every_candidate_slug_is_on_the_streams_roster(self):
        import audit_report

        manifest = self.fleet()
        roster = set(audit_report.audit_checks(self.AUDIT))
        for entry in manifest["clusters"]:
            for candidate in entry.get("candidates") or []:
                self.assertIn(candidate["check"], roster)

    def test_every_declared_slug_is_on_the_streams_roster(self):
        """A declaration naming a slug the roster does not carry subtracts
        nothing and reads as a check somebody removed."""
        import audit_report

        manifest = self.fleet()
        roster = set(audit_report.audit_checks(self.AUDIT))
        for entry in manifest["clusters"]:
            for declared in entry.get("checks_not_applicable") or []:
                self.assertIn(declared["check"], roster)

    def test_the_collector_covers_or_declares_every_roster_slug(self):
        """Nothing on the roster may go unmentioned: a slug that is neither
        recorded nor declared is a coverage gap the ledger cannot explain."""
        import audit_report

        manifest = self.fleet()
        entry = manifest["clusters"][0]
        accounted = {c["check"] for c in entry["commands"]} | {
            d["check"] for d in entry["checks_not_applicable"]
        }
        self.assertEqual(set(audit_report.audit_checks(self.AUDIT)), accounted)


class ChecksRevisionTest(unittest.TestCase):
    """This collector tells the harness which version of itself ran.

    `audit_report.py` compares this run's revision with the previous run's to
    decide whether a finding that stopped appearing was fixed or merely stopped
    being looked for. Publishing nothing gives it no signal, and it falls back
    to claiming a fix.
    """

    def test_the_revision_is_a_digest_of_this_collectors_source(self):
        path = Path(cf.__file__).resolve()
        expected = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        self.assertEqual(cf.CHECKS_REVISION, expected)

    def test_the_manifest_carries_it(self):
        """Driven, not grepped: the constant is inert unless it reaches the
        manifest `audit_report.py` reads."""
        manifest = cf.collect_fleet("acme", run=lambda argv, **kwargs: cf.Run(argv, 0, "[]", "", 0.01))
        self.assertEqual(manifest["checks_revision"], cf.CHECKS_REVISION)
        self.assertEqual(manifest["version"], cf.MANIFEST_VERSION)


class AdversarialReviewRegressionTest(unittest.TestCase):
    """Four defects the pre-PR adversarial pass confirmed against the first
    draft of this collector. Each is pinned by the failure it actually caused,
    not by the shape of the fix."""

    def fake_run(self, responses: dict) -> cf.RunFn:
        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(f"unstubbed command: {joined}")

        return run

    def test_two_same_named_instances_in_different_zones_stay_distinct(self):
        """A GCE instance name is unique per *zone*, so one project can hold
        `web-1` in two zones at once. Unqualified, both candidates derived the
        finding id `...computeinstance-web-1` and `validate_findings` refused
        the entire document — the run published nothing at all, rather than
        publishing one of the two.
        """
        two_zones = (
            '[{"name": "web-1", "status": "RUNNING", '
            '"zone": "https://x/projects/p1/zones/us-central1-a"},'
            '{"name": "web-1", "status": "RUNNING", '
            '"zone": "https://x/projects/p1/zones/us-central1-b"}]'
        )
        entry = cf.collect_project(
            "p1",
            run=self.fake_run(
                {
                    "instances list": run_of(0, two_zones),
                    "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
                    "disks list": run_of(0, "[]"),
                    "snapshots list": run_of(0, "[]"),
                }
            ),
        )
        objects = [c["object"] for c in entry["candidates"]]
        self.assertEqual(len(objects), 2)
        self.assertEqual(len(set(objects)), 2, f"collided: {objects}")
        self.assertEqual(
            sorted(objects),
            ["ComputeInstance/us-central1-a/web-1", "ComputeInstance/us-central1-b/web-1"],
        )

    def test_the_target_name_is_classified_as_a_project_not_a_cluster(self):
        """`audit_report.target_kind` recognises a `project/` prefix. The
        hyphenated `project-<id>` this collector used to emit fell through to
        the bare-name branch, so a sweep of one GCP project published the scope
        line "1 cluster"."""
        import audit_report as ar

        entry = cf.collect_project(
            "p1",
            run=self.fake_run(
                {
                    "instances list": run_of(0, "[]"),
                    "disks list": run_of(0, "[]"),
                    "snapshots list": run_of(0, "[]"),
                }
            ),
        )
        self.assertEqual(ar.target_kind(entry["name"]), "project")
        self.assertIn("project", ar.scope_phrase([entry]))

    def test_redaction_spares_a_gke_node_name_and_a_path(self):
        """The catch-all pattern matched any 40-char run of lowercase, digits
        and separators — which is a GKE node name and a deep filesystem path.
        `adopt_collector_evidence` overwrites the model's excerpt with this
        string, so the degraded line was what shipped: the node name is what
        `needs_triage: gke-managed-node` asks the model to judge, and the path
        is the only part of the excerpt saying *what* failed."""
        node = "gke-prod-cluster-default-pool-9f8a7b6c-abcd"
        self.assertGreaterEqual(len(node), 40, "fixture must exceed the pattern's floor")
        line = f"Sep  4 19:00:00 {node} startup-script: /opt/bootstrap/very/long/path/to/installer.sh: line 12: startup-script exit status 1"
        out = cf.redact(line)
        self.assertIn(node, out)
        self.assertIn("/opt/bootstrap/very/long/path/to/installer.sh", out)
        self.assertNotIn(cf.REDACTED, out)

    def test_redaction_still_catches_encoded_material_and_hex_digests(self):
        """The other side of the same fix: narrowing the catch-all must not
        open a hole. Both shapes carry no keyword for the named-secret pattern
        to key on, so the catch-all is the only thing covering them."""
        blob = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4"
        self.assertNotIn(blob, cf.redact(f"echo {blob} >> /etc/x"))
        digest = "d41d8cd98f00b204e9800998ecf8427e" + "a" * 32
        self.assertNotIn(digest, cf.redact(f"checksum {digest} ok"))

    def test_the_read_behind_a_candidate_survives_the_command_clip(self):
        """`_joined_record` clips the join at MAX_COMMAND_CHARS and
        `adopt_collector_evidence` writes that one string onto every finding of
        the (target, check). Tail-ordered, a project with more RUNNING
        instances than the budget holds shipped instance #30's excerpt under a
        command naming only instances #1-#16."""
        many = ",".join(
            f'{{"name": "vm-{i:03d}", "status": "RUNNING", '
            f'"zone": "https://x/projects/p1/zones/us-central1-a"}}'
            for i in range(60)
        )

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, f"[{many}]")
            if "get-serial-port-output" in joined:
                if "vm-059" in joined:
                    return run_of(0, "startup-script exit status 1\n")
                return run_of(0, "boot ok\n")
            return run_of(0, "[]")

        entry = cf.collect_project("p1", run=run)
        self.assertEqual(
            [c["object"] for c in entry["candidates"]],
            ["ComputeInstance/us-central1-a/vm-059"],
        )
        startup = [c for c in entry["commands"] if c["check"] == cf.STARTUP_SLUG]
        self.assertEqual(len(startup), 1)
        command = startup[0]["command"]
        self.assertLessEqual(len(command), cf.MAX_COMMAND_CHARS)
        self.assertIn("more read(s)", command, "fixture must overflow the budget")
        self.assertIn("vm-059", command, "the read behind the published excerpt was clipped away")


if __name__ == "__main__":
    unittest.main()
