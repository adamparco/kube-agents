#!/usr/bin/env python3
"""Tests for patch_readiness.py, the security-patch-orchestrator collector."""

import json
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import audit_report  # noqa: E402
import patch_readiness as pr  # noqa: E402


def run_of(rc: int, stdout: str = "", stderr: str = "") -> pr.Run:
    return pr.Run(["gcloud"], rc, stdout, stderr, 0.01)


def cluster(name="prod-usc1", location="us-central1", master="1.30.5-gke.100", channel="REGULAR", autopilot=False, node_pools=None, **overrides):
    doc = {
        "name": name,
        "location": location,
        "status": "RUNNING",
        "currentMasterVersion": master,
        "releaseChannel": {"channel": channel} if channel else {},
        "autopilot": {"enabled": autopilot},
        "nodePools": node_pools if node_pools is not None else [pool("default-pool", master)],
        "maintenancePolicy": {"window": {"recurringWindow": {}}},
        "notificationConfig": {"pubsub": {"enabled": True}},
    }
    doc.update(overrides)
    return doc


def pool(name="default-pool", version="1.30.5-gke.100", status="RUNNING", auto_upgrade=True, auto_repair=True, image_type="COS_CONTAINERD", **overrides):
    doc = {
        "name": name,
        "version": version,
        "status": status,
        "management": {"autoUpgrade": auto_upgrade, "autoRepair": auto_repair},
        "config": {"imageType": image_type},
    }
    doc.update(overrides)
    return doc


def server_config(channel="REGULAR", default="1.30.5-gke.100", valid_versions=None, valid_image_types=None):
    return {
        "channels": [{"channel": channel, "defaultVersion": default, "validVersions": valid_versions or [default]}],
        "validMasterVersions": valid_versions or [default],
        "validImageTypes": valid_image_types or ["COS_CONTAINERD", "UBUNTU_CONTAINERD", "WINDOWS_LTSC_CONTAINERD"],
    }


BASELINE = pr.normalize_server_config(server_config())
NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)


class VersionArithmeticTest(unittest.TestCase):
    def test_parses_with_build(self):
        self.assertEqual(pr.parse_version("1.30.5-gke.1355000"), (1, 30, 5, 1355000))

    def test_parses_without_build(self):
        self.assertEqual(pr.parse_version("1.30.5"), (1, 30, 5, 0))

    def test_unparseable_is_none(self):
        self.assertIsNone(pr.parse_version("not-a-version"))
        self.assertIsNone(pr.parse_version(""))

    def test_never_string_compares(self):
        # 1.30.9 < 1.30.10 numerically, the opposite of a string compare.
        self.assertLess(pr.parse_version("1.30.9"), pr.parse_version("1.30.10"))

    def test_minor_of(self):
        self.assertEqual(pr.minor_of("1.30.5-gke.1"), (1, 30))


class MasterBehindTest(unittest.TestCase):
    def test_no_baseline_is_not_a_finding(self):
        self.assertIsNone(pr.check_master_behind(cluster(), None))

    def test_absent_from_valid_versions_is_critical(self):
        c = cluster(master="1.28.0-gke.1")
        hit = pr.check_master_behind(c, BASELINE)
        self.assertEqual(hit["severity"], "critical")

    def test_a_minor_behind_default_is_major(self):
        baseline = pr.normalize_server_config(server_config(default="1.31.0-gke.1", valid_versions=["1.30.5-gke.100", "1.31.0-gke.1"]))
        hit = pr.check_master_behind(cluster(master="1.30.5-gke.100"), baseline)
        self.assertEqual(hit["severity"], "major")

    def test_same_minor_older_patch_is_minor(self):
        baseline = pr.normalize_server_config(server_config(default="1.30.9-gke.1", valid_versions=["1.30.5-gke.100", "1.30.9-gke.1"]))
        hit = pr.check_master_behind(cluster(master="1.30.5-gke.100"), baseline)
        self.assertEqual(hit["severity"], "minor")

    def test_equal_to_default_is_not_flagged(self):
        self.assertIsNone(pr.check_master_behind(cluster(master="1.30.5-gke.100"), BASELINE))

    def test_newer_than_default_is_not_flagged(self):
        baseline = pr.normalize_server_config(server_config(default="1.30.0-gke.1", valid_versions=["1.30.5-gke.100", "1.30.0-gke.1"]))
        self.assertIsNone(pr.check_master_behind(cluster(master="1.30.5-gke.100"), baseline))

    def test_no_channel_uses_valid_master_versions(self):
        baseline = pr.normalize_server_config(server_config(valid_versions=["1.30.5-gke.100"]))
        self.assertIsNone(pr.check_master_behind(cluster(channel=""), baseline))
        c = cluster(channel="", master="9.9.9-gke.1")
        self.assertEqual(pr.check_master_behind(c, baseline)["severity"], "critical")

    def test_unknown_channel_spelling_is_not_a_crash(self):
        c = cluster(channel="MYSTERY")
        self.assertIsNone(pr.check_master_behind(c, BASELINE))


class PoolSkewTest(unittest.TestCase):
    def test_autopilot_is_never_flagged(self):
        c = cluster(autopilot=True, node_pools=[pool(version="1.20.0-gke.1")])
        self.assertEqual(pr.check_pool_skew(c), [])

    def test_three_minors_behind_is_critical(self):
        c = cluster(master="1.33.0-gke.1", node_pools=[pool(version="1.30.0-gke.1")])
        hits = pr.check_pool_skew(c)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_different_major_is_critical(self):
        c = cluster(master="2.0.0-gke.1", node_pools=[pool(version="1.30.0-gke.1")])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "critical")

    def test_two_minors_behind_is_major(self):
        c = cluster(master="1.32.0-gke.1", node_pools=[pool(version="1.30.0-gke.1")])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "major")

    def test_one_minor_behind_with_autoupgrade_off_is_major(self):
        c = cluster(master="1.31.0-gke.1", node_pools=[pool(version="1.30.0-gke.1", auto_upgrade=False)])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "major")

    def test_one_minor_behind_with_autoupgrade_on_is_minor(self):
        c = cluster(master="1.31.0-gke.1", node_pools=[pool(version="1.30.0-gke.1", auto_upgrade=True)])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "minor")

    def test_one_patch_behind_is_not_flagged(self):
        c = cluster(master="1.30.5-gke.100", node_pools=[pool(version="1.30.4-gke.100")])
        self.assertEqual(pr.check_pool_skew(c), [])

    def test_several_patches_behind_is_minor(self):
        c = cluster(master="1.30.9-gke.100", node_pools=[pool(version="1.30.4-gke.100")])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "minor")

    def test_ahead_of_control_plane_is_major(self):
        c = cluster(master="1.30.0-gke.1", node_pools=[pool(version="1.31.0-gke.1")])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "major")

    def test_reconciling_pool_is_suppressed(self):
        c = cluster(master="1.33.0-gke.1", node_pools=[pool(version="1.30.0-gke.1", status="RECONCILING")])
        self.assertEqual(pr.check_pool_skew(c), [])

    def test_reconciling_cluster_suppresses_every_pool(self):
        c = cluster(master="1.33.0-gke.1", node_pools=[pool(version="1.30.0-gke.1")], status="RECONCILING")
        self.assertEqual(pr.check_pool_skew(c), [])

    def test_same_version_is_never_flagged(self):
        c = cluster(master="1.30.5-gke.100", node_pools=[pool(version="1.30.5-gke.100")])
        self.assertEqual(pr.check_pool_skew(c), [])


class FleetSpreadTest(unittest.TestCase):
    def test_a_two_minor_spread_is_flagged_once_on_the_laggard(self):
        clusters = [cluster(name="old", master="1.28.0-gke.1"), cluster(name="new", master="1.30.0-gke.1")]
        hits = pr.check_fleet_spread(clusters)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Cluster/old")

    def test_a_one_minor_spread_is_not_flagged(self):
        clusters = [cluster(name="a", master="1.29.0-gke.1"), cluster(name="b", master="1.30.0-gke.1")]
        self.assertEqual(pr.check_fleet_spread(clusters), [])

    def test_a_single_cluster_is_never_a_spread(self):
        self.assertEqual(pr.check_fleet_spread([cluster()]), [])


class NoChannelTest(unittest.TestCase):
    def test_flags_empty_channel(self):
        self.assertIsNotNone(pr.check_no_channel(cluster(channel="")))

    def test_flags_unspecified(self):
        self.assertIsNotNone(pr.check_no_channel(cluster(channel="UNSPECIFIED")))

    def test_does_not_flag_regular(self):
        self.assertIsNone(pr.check_no_channel(cluster(channel="REGULAR")))


class NoAutoupgradeAutorepairTest(unittest.TestCase):
    def test_autopilot_is_never_flagged(self):
        c = cluster(autopilot=True, node_pools=[pool(auto_upgrade=False, auto_repair=False)])
        self.assertEqual(pr.check_no_autoupgrade(c), [])
        self.assertEqual(pr.check_no_autorepair(c), [])

    def test_flags_disabled_autoupgrade(self):
        c = cluster(node_pools=[pool(auto_upgrade=False)])
        self.assertEqual(len(pr.check_no_autoupgrade(c)), 1)

    def test_flags_disabled_autorepair(self):
        c = cluster(node_pools=[pool(auto_repair=False)])
        self.assertEqual(len(pr.check_no_autorepair(c)), 1)

    def test_does_not_flag_enabled(self):
        c = cluster(node_pools=[pool(auto_upgrade=True, auto_repair=True)])
        self.assertEqual(pr.check_no_autoupgrade(c), [])
        self.assertEqual(pr.check_no_autorepair(c), [])


class NoMaintenanceWindowTest(unittest.TestCase):
    def test_flags_no_window(self):
        c = cluster(**{"maintenancePolicy": {}})
        self.assertIsNotNone(pr.check_no_maintenance_window(c))

    def test_does_not_flag_recurring_window(self):
        c = cluster(**{"maintenancePolicy": {"window": {"recurringWindow": {}}}})
        self.assertIsNone(pr.check_no_maintenance_window(c))

    def test_does_not_flag_daily_window(self):
        c = cluster(**{"maintenancePolicy": {"window": {"dailyMaintenanceWindow": {}}}})
        self.assertIsNone(pr.check_no_maintenance_window(c))


class BlockingExclusionTest(unittest.TestCase):
    def exclusion_cluster(self, start, end, scope="NO_UPGRADES"):
        return cluster(
            maintenancePolicy={
                "window": {
                    "recurringWindow": {},
                    "maintenanceExclusions": {"freeze": {"startTime": start, "endTime": end, "maintenanceExclusionOptions": {"scope": scope}}},
                }
            }
        )

    def test_a_long_freeze_is_flagged_even_without_a_version_finding(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z")
        hit = pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["severity"], "minor")

    def test_a_short_freeze_with_no_version_finding_is_not_flagged(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-01-20T00:00:00Z")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False))

    def test_a_short_freeze_holding_back_a_version_finding_is_major(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-01-20T00:00:00Z")
        hit = pr.check_blocking_exclusion(c, now=NOW, has_version_finding=True)
        self.assertEqual(hit["severity"], "major")

    def test_an_expired_exclusion_is_not_flagged(self):
        c = self.exclusion_cluster("2025-01-01T00:00:00Z", "2025-06-01T00:00:00Z")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=True))

    def test_a_future_exclusion_is_not_flagged(self):
        c = self.exclusion_cluster("2027-01-01T00:00:00Z", "2027-06-01T00:00:00Z")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=True))

    def test_no_minor_upgrades_scope_is_never_flagged(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z", scope="NO_MINOR_UPGRADES")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=True))

    def test_exclusions_is_a_map_not_a_list(self):
        # Regression: iterating the raw dict without .items() would walk
        # the exclusion *names* as if they were the exclusion dicts.
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z")
        exclusions = c["maintenancePolicy"]["window"]["maintenanceExclusions"]
        self.assertIsInstance(exclusions, dict)
        self.assertIn("freeze", exclusions)


class StaleImageTypeTest(unittest.TestCase):
    def test_autopilot_is_never_flagged(self):
        c = cluster(autopilot=True, node_pools=[pool(image_type="COS")])
        self.assertEqual(pr.check_stale_image_type(c, BASELINE), [])

    def test_flags_deprecated_cos(self):
        c = cluster(node_pools=[pool(image_type="COS")])
        self.assertEqual(len(pr.check_stale_image_type(c, BASELINE)), 1)

    def test_flags_absent_from_valid_types(self):
        c = cluster(node_pools=[pool(image_type="SOME_FUTURE_TYPE")])
        self.assertEqual(len(pr.check_stale_image_type(c, BASELINE)), 1)

    def test_does_not_flag_current_containerd_variant(self):
        c = cluster(node_pools=[pool(image_type="COS_CONTAINERD")])
        self.assertEqual(pr.check_stale_image_type(c, BASELINE), [])

    def test_case_insensitive(self):
        c = cluster(node_pools=[pool(image_type="cos_containerd")])
        self.assertEqual(pr.check_stale_image_type(c, BASELINE), [])

    def test_no_baseline_is_not_a_crash(self):
        c = cluster(node_pools=[pool(image_type="COS")])
        self.assertEqual(pr.check_stale_image_type(c, None), [])


class NoNotificationsTest(unittest.TestCase):
    def test_flags_disabled(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": False}}})
        self.assertIsNotNone(pr.check_no_notifications(c))

    def test_flags_absent(self):
        c = cluster(**{"notificationConfig": {}})
        self.assertIsNotNone(pr.check_no_notifications(c))

    def test_enabled_with_no_filter_is_not_flagged(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": True}}})
        self.assertIsNone(pr.check_no_notifications(c))

    def test_enabled_with_filter_excluding_upgrade_event_is_flagged(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": True, "filter": {"eventType": ["SECURITY_BULLETIN_EVENT"]}}}})
        self.assertIsNotNone(pr.check_no_notifications(c))

    def test_enabled_with_filter_including_upgrade_event_is_not_flagged(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": True, "filter": {"eventType": ["UPGRADE_AVAILABLE_EVENT"]}}}})
        self.assertIsNone(pr.check_no_notifications(c))


class CollectProjectTest(unittest.TestCase):
    def fake_run(self, responses):
        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(f"unstubbed command: {joined}")

        return run

    def test_a_clean_project_collects_with_no_candidates(self):
        c = cluster()
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "collected")
        self.assertEqual(entries[0]["candidates"], [])
        self.assertEqual({c["check"] for c in entries[0]["commands"]}, set(pr.SEVERITY))

    def test_clusters_list_failure_yields_no_entries(self):
        entries = pr.collect_project("acme", run=self.fake_run({"clusters list": run_of(1, "", "denied")}), now=NOW)
        self.assertEqual(entries, [])

    def test_get_server_config_failure_drops_only_the_baseline_checks(self):
        c = cluster()
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(1, "", "denied"),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        self.assertEqual(entries[0]["outcome"], "collected")
        slugs = {cmd["check"] for cmd in entries[0]["commands"]}
        self.assertNotIn("master-behind", slugs)
        self.assertNotIn("stale-image-type", slugs)
        self.assertIn("pool-skew", slugs)

    def test_a_dirty_cluster_reports_findings(self):
        c = cluster(node_pools=[pool(auto_upgrade=False, auto_repair=False)])
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        slugs = {cand["check"] for cand in entries[0]["candidates"]}
        self.assertIn("no-autoupgrade", slugs)
        self.assertIn("no-autorepair", slugs)

    def test_only_the_laggard_carries_the_fleet_spread_finding(self):
        clusters = [cluster(name="old", master="1.28.0-gke.1"), cluster(name="new", master="1.30.0-gke.1")]
        responses = {
            "clusters list": run_of(0, json.dumps(clusters)),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        old_entry = next(e for e in entries if e["name"] == "old")
        new_entry = next(e for e in entries if e["name"] == "new")
        self.assertIn("fleet-spread", {c["check"] for c in old_entry["candidates"]})
        self.assertNotIn("fleet-spread", {c["check"] for c in new_entry["candidates"]})

    def test_both_clusters_record_the_fleet_spread_command(self):
        """§3.3 emits one finding, but it reads every cluster to get there.

        The clean-fleet half of this is the one that used to be wrong: a fleet
        with no spread produced no hit, so no cluster recorded the command, and
        §6 scored `fleet-spread` as never run on all of them."""
        spread = [cluster(name="old", master="1.28.0-gke.1"), cluster(name="new", master="1.30.0-gke.1")]
        tight = [cluster(name="a", master="1.30.0-gke.1"), cluster(name="b", master="1.30.1-gke.2")]
        for label, clusters in (("spread", spread), ("tight", tight)):
            with self.subTest(fleet=label):
                responses = {
                    "clusters list": run_of(0, json.dumps(clusters)),
                    "get-server-config": run_of(0, json.dumps(server_config())),
                }
                entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
                for entry in entries:
                    self.assertIn("fleet-spread", {c["check"] for c in entry["commands"]})

    def test_a_clean_fleet_reports_no_coverage_gap_for_fleet_spread(self):
        """End-to-end: §6's arithmetic over a tight fleet, which is the shape
        every healthy run of this audit takes."""
        clusters = [cluster(name="a", master="1.30.0-gke.1"), cluster(name="b", master="1.30.1-gke.2")]
        responses = {
            "clusters list": run_of(0, json.dumps(clusters)),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        roster = set(audit_report.audit_target_checks("security-patch-orchestrator", "a"))
        for entry in entries:
            self.assertEqual(roster - {c["check"] for c in entry["commands"]}, set())


class CollectFleetTest(unittest.TestCase):
    def test_project_override_skips_discovery(self):
        def run(argv, **kwargs):
            if "clusters" in argv and "list" in argv:
                return run_of(0, json.dumps([cluster()]))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config()))
            raise AssertionError(f"unexpected discovery call: {argv}")

        manifest = pr.collect_fleet("acme-only", run=run, now=NOW)
        self.assertEqual({c["project"] for c in manifest["clusters"]}, {"acme-only"})
        self.assertEqual(manifest["audit"], "security-patch-orchestrator")


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_checks_run_copied_from_a_collected_cluster_survives_cross_check(self):
        import audit_report

        c = cluster()
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(joined)

        manifest = pr.collect_fleet("acme", run=run, now=NOW)
        entry = manifest["clusters"][0]
        data = {
            "audit": "security-patch-orchestrator",
            "scope": {"clusters": [{"name": entry["name"], "checks_run": [{"check": c["check"], "command": c["command"]} for c in entry["commands"]]}]},
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_check_absent_from_the_manifest_is_rejected(self):
        import audit_report

        c = cluster()
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(1, "", "denied"),
        }

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(joined)

        manifest = pr.collect_fleet("acme", run=run, now=NOW)
        entry = manifest["clusters"][0]
        data = {
            "audit": "security-patch-orchestrator",
            "scope": {"clusters": [{"name": entry["name"], "checks_run": [{"check": "master-behind", "command": "x"}]}]},
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)


if __name__ == "__main__":
    unittest.main()
