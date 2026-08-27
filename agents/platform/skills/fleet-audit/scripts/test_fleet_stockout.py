#!/usr/bin/env python3
"""Tests for fleet_stockout.py, the stockout-prevention collector (partial:
the ten checks this collector covers, per its own module docstring)."""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_stockout as fs  # noqa: E402


def run_of(rc: int, stdout: str = "", stderr: str = "") -> fs.Run:
    return fs.Run(["x"], rc, stdout, stderr, 0.01)


def dump_of(*items) -> dict:
    return {"items": list(items)}


def compute_class(name, priorities, node_pool_auto_creation=True):
    return {
        "kind": "ComputeClass",
        "metadata": {"name": name},
        "spec": {"priorities": priorities, "nodePoolAutoCreation": {"enabled": node_pool_auto_creation}},
    }


def deployment(name, ns="default", node_selector=None, containers=None, tolerations=None):
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"template": {"spec": {"nodeSelector": node_selector or {}, "containers": containers or [{"name": "app"}], "tolerations": tolerations or []}}},
    }


def statefulset(name, ns="default", node_selector=None, storage_class_name=None):
    vcts = [{"spec": {"storageClassName": storage_class_name}}] if storage_class_name else []
    return {
        "kind": "StatefulSet",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"template": {"spec": {"nodeSelector": node_selector or {}}}, "volumeClaimTemplates": vcts},
    }


def storage_class(name, provisioner="pd.csi.storage.gke.io", params=None):
    return {"kind": "StorageClass", "metadata": {"name": name}, "provisioner": provisioner, "parameters": params or {}}


def node(name, pool):
    return {"kind": "Node", "metadata": {"name": name, "labels": {"cloud.google.com/gke-nodepool": pool}}}


class EnumerateClustersTest(unittest.TestCase):
    def test_reads_cluster_level_node_auto_provisioning(self):
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1-a", "status": "RUNNING", "autoscaling": {"enableNodeAutoprovisioning": True}},
                {"name": "c2", "location": "us-central1-a", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            return run_of(0, clusters_json)

        clusters = fs.enumerate_clusters("acme", run=run)
        self.assertTrue(next(c for c in clusters if c["name"] == "c1")["has_nap"])
        self.assertFalse(next(c for c in clusters if c["name"] == "c2")["has_nap"])


class RegionOfTest(unittest.TestCase):
    def test_zonal_location(self):
        self.assertEqual(fs.region_of("us-central1-a"), "us-central1")

    def test_regional_location(self):
        self.assertEqual(fs.region_of("us-central1"), "us-central1")

    def test_empty(self):
        self.assertEqual(fs.region_of(""), "")


class CccMissingFallbacksTest(unittest.TestCase):
    def test_flags_single_family_single_priority(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": False}])
        self.assertIsNotNone(fs.check_ccc_missing_fallbacks(cc))

    def test_flags_family_and_spot_only_varying_one_dimension(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": False}, {"machineFamily": "c3", "spot": True}])
        # only the spot dimension varies -- family is constant, no size, no zones
        self.assertIsNotNone(fs.check_ccc_missing_fallbacks(cc))

    def test_does_not_flag_family_and_spot_both_varying(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": False}, {"machineFamily": "n4", "spot": True}])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc))

    def test_does_not_flag_multi_zone_and_family(self):
        cc = compute_class(
            "cc1",
            [
                {"machineFamily": "c3", "zones": ["us-central1-a"]},
                {"machineFamily": "n4", "zones": ["us-central1-b"]},
            ],
        )
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc))

    def test_no_priorities_is_not_a_crash(self):
        self.assertIsNone(fs.check_ccc_missing_fallbacks(compute_class("cc1", [])))


class CccNoOndemandFloorTest(unittest.TestCase):
    def test_flags_all_spot(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}, {"machineFamily": "n4", "spot": True}])
        self.assertIsNotNone(fs.check_ccc_no_ondemand_floor(cc, False))

    def test_does_not_flag_with_ondemand_floor(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}, {"machineFamily": "n4", "spot": False}])
        self.assertIsNone(fs.check_ccc_no_ondemand_floor(cc, False))

    def test_recognizes_provisioning_model_spelling(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "provisioningModel": "SPOT"}])
        self.assertIsNotNone(fs.check_ccc_no_ondemand_floor(cc, False))

    def test_default_severity_is_major(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        self.assertEqual(fs.SEVERITY["ccc-no-ondemand-floor"], "major")
        hit = fs.check_ccc_no_ondemand_floor(cc, False)
        self.assertNotIn("severity", hit)

    def test_escalates_to_critical_when_referenced_by_inference_workload(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        hit = fs.check_ccc_no_ondemand_floor(cc, True)
        self.assertEqual(hit["severity"], "critical")


class CccLargeVmScarcityTest(unittest.TestCase):
    def test_flags_large_machine_with_one_family(self):
        cc = compute_class("cc1", [{"machineFamily": "m1", "machineType": "m1-ultramem-160"}])
        hits = fs.check_ccc_large_vm_scarcity(cc)
        self.assertEqual(len(hits), 1)

    def test_does_not_flag_with_multiple_families(self):
        cc = compute_class("cc1", [{"machineFamily": "m1", "machineType": "m1-ultramem-160"}, {"machineFamily": "n4", "machineType": "n4-standard-4"}])
        self.assertEqual(fs.check_ccc_large_vm_scarcity(cc), [])

    def test_does_not_flag_small_machine(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "machineType": "n4-standard-8"}])
        self.assertEqual(fs.check_ccc_large_vm_scarcity(cc), [])


class CccPriorityStarvationTest(unittest.TestCase):
    def test_flags_over_ten_priorities(self):
        cc = compute_class("cc1", [{"machineFamily": "n4"}] * 11)
        self.assertIsNotNone(fs.check_ccc_priority_starvation(cc))

    def test_does_not_flag_ten_or_fewer(self):
        cc = compute_class("cc1", [{"machineFamily": "n4"}] * 10)
        self.assertIsNone(fs.check_ccc_priority_starvation(cc))


class CccMixedDiskGenerationsTest(unittest.TestCase):
    def test_flags_gen2_and_gen4_mix_on_stateful(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        self.assertIsNotNone(fs.check_ccc_mixed_disk_generations(cc, stateful_referencing=True))

    def test_does_not_flag_when_not_referenced_by_stateful(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        self.assertIsNone(fs.check_ccc_mixed_disk_generations(cc, stateful_referencing=False))

    def test_does_not_flag_pure_gen2(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c2"}])
        self.assertIsNone(fs.check_ccc_mixed_disk_generations(cc, stateful_referencing=True))

    def test_c3d_is_not_in_the_gen4_hyperdisk_list(self):
        """§3.5's own Gen4/Hyperdisk-compatible list is `c4, n4, c3` --
        `c3d` is not on it, even though a different check's (§3.6) list
        does include it."""
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c3d"}])
        self.assertIsNone(fs.check_ccc_mixed_disk_generations(cc, stateful_referencing=True))


class CccHyperdiskIncompatibleTest(unittest.TestCase):
    def test_flags_incompatible_fallback(self):
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "e2"}])
        self.assertIsNotNone(fs.check_ccc_hyperdisk_incompatible(cc, uses_hyperdisk=True))

    def test_does_not_flag_when_not_using_hyperdisk(self):
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "e2"}])
        self.assertIsNone(fs.check_ccc_hyperdisk_incompatible(cc, uses_hyperdisk=False))

    def test_does_not_flag_all_compatible_families(self):
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "n4"}])
        self.assertIsNone(fs.check_ccc_hyperdisk_incompatible(cc, uses_hyperdisk=True))


class DanglingComputeClassTest(unittest.TestCase):
    def test_flags_reference_to_nonexistent_class(self):
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "missing"})
        hit = fs.check_dangling_compute_class(d, {}, set())
        self.assertIsNotNone(hit)
        self.assertIn("does not exist", hit["excerpt"])

    def test_does_not_flag_valid_reference(self):
        cc = compute_class("cc1", [])
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, set()))

    def test_flags_missing_pool_label_when_auto_creation_disabled(self):
        cc = compute_class("cc1", [], node_pool_auto_creation=False)
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        hit = fs.check_dangling_compute_class(d, {"cc1": cc}, {"other-class"})
        self.assertIsNotNone(hit)

    def test_does_not_flag_missing_pool_label_when_auto_creation_enabled(self):
        cc = compute_class("cc1", [], node_pool_auto_creation=True)
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, set()))

    def test_flags_gpu_workload_without_toleration(self):
        cc = compute_class("cc1", [])
        d = deployment(
            "api",
            node_selector={"cloud.google.com/compute-class": "cc1"},
            containers=[{"name": "app", "resources": {"requests": {"nvidia.com/gpu": "1"}}}],
        )
        hit = fs.check_dangling_compute_class(d, {"cc1": cc}, set())
        self.assertIsNotNone(hit)
        self.assertIn("toleration", hit["excerpt"])

    def test_does_not_flag_gpu_workload_with_toleration(self):
        cc = compute_class("cc1", [])
        d = deployment(
            "api",
            node_selector={"cloud.google.com/compute-class": "cc1"},
            containers=[{"name": "app", "resources": {"requests": {"nvidia.com/gpu": "1"}}}],
            tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists"}],
        )
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, set()))

    def test_no_selector_is_never_flagged(self):
        d = deployment("api")
        self.assertIsNone(fs.check_dangling_compute_class(d, {}, set()))


class SingleZoneNodepoolTest(unittest.TestCase):
    def test_flags_single_zone_autoscaling_no_nap(self):
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        self.assertIsNotNone(fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1))

    def test_does_not_flag_multi_zone(self):
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1))

    def test_does_not_flag_single_zone_with_nap(self):
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=1))

    def test_flags_near_max_node_count(self):
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=9)
        self.assertIsNotNone(hit)
        self.assertIn("90%", hit["excerpt"])

    def test_does_not_flag_comfortably_under_ceiling(self):
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=3))

    def test_ignores_stale_initial_node_count_field(self):
        """A pool created with 9 nodes that the autoscaler has since scaled
        down to 1 live node must not be flagged on its stale creation-time
        field."""
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "initialNodeCount": 9}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=1))


class ReservationTest(unittest.TestCase):
    def test_flags_mostly_idle_reservation(self):
        r = {"name": "r1", "specificReservation": {"count": 10, "inUseCount": 2}}
        hit = fs.check_reservation(r)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["severity"], "major")

    def test_does_not_flag_well_utilized_reservation(self):
        r = {"name": "r1", "specificReservation": {"count": 10, "inUseCount": 8}}
        self.assertIsNone(fs.check_reservation(r))

    def test_does_not_flag_small_reservation_with_small_absolute_slack(self):
        # ratio 0/2 = 0 <= 0.5, but only 2 idle -- below the absolute floor of 4
        r = {"name": "r1", "specificReservation": {"count": 2, "inUseCount": 0}}
        self.assertIsNone(fs.check_reservation(r))

    def test_zero_count_is_not_a_crash(self):
        r = {"name": "r1", "specificReservation": {"count": 0, "inUseCount": 0}}
        self.assertIsNone(fs.check_reservation(r))


class ReservationAffinityTest(unittest.TestCase):
    def test_flags_automatic_affinity(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "reservations": {"affinity": "Automatic"}}])
        hit = fs.check_reservation_affinity(cc)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["severity"], "critical")

    def test_flags_any_best_effort_affinity(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "reservations": {"affinity": "AnyBestEffort"}}])
        self.assertIsNotNone(fs.check_reservation_affinity(cc))

    def test_does_not_flag_specific_affinity(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "reservations": {"affinity": "SpecificReservation"}}])
        self.assertIsNone(fs.check_reservation_affinity(cc))

    def test_no_reservations_field_is_not_flagged(self):
        cc = compute_class("cc1", [{"machineFamily": "n4"}])
        self.assertIsNone(fs.check_reservation_affinity(cc))


class QuotaTest(unittest.TestCase):
    def test_flags_over_90_percent(self):
        hit = fs.check_quota({"metric": "N4_CPUS", "limit": 100, "usage": 92})
        self.assertIsNotNone(hit)

    def test_does_not_flag_under_90_percent(self):
        self.assertIsNone(fs.check_quota({"metric": "N4_CPUS", "limit": 100, "usage": 70}))

    def test_zero_limit_is_not_a_crash(self):
        self.assertIsNone(fs.check_quota({"metric": "N4_CPUS", "limit": 0, "usage": 0}))


class CollectClusterTest(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def run_with(self, dump_items=(), pools=(), cluster=None):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*dump_items)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, json.dumps(list(pools)))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                return fs.collect_cluster(cluster or self.CLUSTER, run=run)

    def test_clean_cluster_collects_with_no_candidates(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "spot": False}, {"machineFamily": "c3", "spot": True}])
        entry = self.run_with(dump_items=[cc])
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry["candidates"], [])

    def test_get_credentials_failure_is_unreachable(self):
        def run(argv, **kwargs):
            return run_of(1, "", "denied") if "get-credentials" in argv else run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                entry = fs.collect_cluster(self.CLUSTER, run=run)
        self.assertEqual(entry["outcome"], "unreachable")

    def test_dump_failure_is_gate_failed(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(1, "", "forbidden")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                entry = fs.collect_cluster(self.CLUSTER, run=run)
        self.assertEqual(entry["outcome"], "gate-failed")

    def test_a_dirty_compute_class_is_reported(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        entry = self.run_with(dump_items=[cc])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("ccc-missing-fallbacks", slugs)
        self.assertIn("ccc-no-ondemand-floor", slugs)

    def test_single_zone_nodepool_reported(self):
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "initialNodeCount": 1}
        entry = self.run_with(pools=[pool])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("single-zone-nodepool", slugs)

    def test_near_max_node_count_uses_live_nodes_not_the_stale_initial_field(self):
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "initialNodeCount": 9}
        live_nodes = [node(f"n{i}", "p1") for i in range(2)]  # scaled down since creation
        entry = self.run_with(dump_items=live_nodes, pools=[pool])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertNotIn("single-zone-nodepool", slugs)

    def test_near_max_node_count_flagged_from_live_nodes(self):
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        live_nodes = [node(f"n{i}", "p1") for i in range(9)]
        entry = self.run_with(dump_items=live_nodes, pools=[pool])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("single-zone-nodepool", slugs)

    def test_cluster_level_nap_suppresses_the_finding(self):
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        entry = self.run_with(pools=[pool], cluster={**self.CLUSTER, "has_nap": True})
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["candidates"]})

    def test_autopilot_skips_single_zone_nodepool(self):
        cluster = {**self.CLUSTER, "autopilot": True}
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "initialNodeCount": 1}

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, json.dumps([pool]))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                entry = fs.collect_cluster(cluster, run=run)
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})

    def test_dangling_reference_reported(self):
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "missing"})
        entry = self.run_with(dump_items=[d])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("dangling-compute-class", slugs)

    def test_mixed_disk_generation_on_stateful_reported(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"}, storage_class_name="standard-rwo")
        entry = self.run_with(dump_items=[cc, sts])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("ccc-mixed-disk-generations", slugs)

    def test_mixed_disk_generation_not_flagged_without_persistent_volumes(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"})  # no volumeClaimTemplates
        entry = self.run_with(dump_items=[cc, sts])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertNotIn("ccc-mixed-disk-generations", slugs)

    def test_hyperdisk_incompatible_reported(self):
        sc = storage_class("hd", params={"type": "hyperdisk-balanced"})
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "e2"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"}, storage_class_name="hd")
        entry = self.run_with(dump_items=[sc, cc, sts])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("ccc-hyperdisk-incompatible", slugs)

    def test_no_ondemand_floor_escalates_for_a_referencing_inference_workload(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        gpu_container = {"name": "app", "resources": {"limits": {"nvidia.com/gpu": "1"}}}
        d = deployment("infer", node_selector={"cloud.google.com/compute-class": "cc1"}, containers=[gpu_container])
        entry = self.run_with(dump_items=[cc, d])
        hit = next(c for c in entry["candidates"] if c["check"] == "ccc-no-ondemand-floor")
        self.assertEqual(hit["severity"], "critical")

    def test_no_ondemand_floor_stays_major_for_a_non_inference_referencing_workload(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        d = deployment("web", node_selector={"cloud.google.com/compute-class": "cc1"})
        entry = self.run_with(dump_items=[cc, d])
        hit = next(c for c in entry["candidates"] if c["check"] == "ccc-no-ondemand-floor")
        self.assertEqual(hit["severity"], "major")

    def test_reservation_affinity_reported(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "reservations": {"affinity": "Automatic"}}])
        entry = self.run_with(dump_items=[cc])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("reservation-mismatch-risk", slugs)


class CollectProjectTest(unittest.TestCase):
    def test_reservation_and_quota_findings(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, json.dumps([{"name": "r1", "specificReservation": {"count": 10, "inUseCount": 1}}]))
            if argv[:3] == ["gcloud", "compute", "regions"]:
                return run_of(0, json.dumps({"quotas": [{"metric": "N4_CPUS", "limit": 100, "usage": 95}]}))
            return run_of(0, "")

        entry = fs.collect_project("acme", {"us-central1"}, run=run)
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("reservation-mismatch-risk", slugs)
        self.assertIn("quota-exhaustion-risk", slugs)

    def test_no_data_returns_none(self):
        def run(argv, **kwargs):
            return run_of(1, "", "denied")

        self.assertIsNone(fs.collect_project("acme", {"us-central1"}, run=run))


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_checks_run_copied_from_a_collected_entry_survives_cross_check(self):
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(cc)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "regions"]:
                return run_of(0, json.dumps({"quotas": []}))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet("acme", run=run)

        data = {
            "audit": "stockout-prevention",
            "scope": {
                "clusters": [
                    {"name": e["name"], "checks_run": [{"check": c["check"], "command": c["command"]} for c in e["commands"]]}
                    for e in manifest["clusters"]
                ],
                "skipped": [],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_check_absent_from_a_collected_entry_is_rejected(self):
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))  # no ComputeClasses at all
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "regions"]:
                return run_of(0, json.dumps({"quotas": []}))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet("acme", run=run)

        cluster_entry = next(c for c in manifest["clusters"] if c["name"] == "c1")
        self.assertEqual(cluster_entry["outcome"], "collected")
        self.assertNotIn("ccc-missing-fallbacks", {c["check"] for c in cluster_entry["commands"]})

        data = {
            "audit": "stockout-prevention",
            "scope": {"clusters": [{"name": "c1", "checks_run": [{"check": "ccc-missing-fallbacks", "command": "x"}]}]},
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)


if __name__ == "__main__":
    unittest.main()
