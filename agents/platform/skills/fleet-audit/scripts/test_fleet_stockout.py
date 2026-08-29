#!/usr/bin/env python3
"""Tests for fleet_stockout.py, the stockout-prevention collector.

The `capacity-history` and `cluster-autoscaler-visibility` fixtures below are
trimmed copies of real responses read against `adamparco-kage` on 2026-08-29,
not shapes invented to match the parser. Both APIs were the reason those two
checks stayed prose-only, so a hand-written fixture would re-create exactly the
problem converting them was meant to solve."""

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


def capacity_history(rates, machine_type="n2-standard-8", price=None):
    """A `gcloud beta compute advice capacity-history` response.

    The frame is verbatim from the live read: a bare object rather than a list,
    `preemptionRate` as a fraction, and a `listPrice` carrying `nanos` with no
    `units` — 0.110192 USD/h, which is what a Spot n2-standard-8 in us-east4
    actually cost that day and is under one currency unit, the case that made
    reading `units` alone wrong."""
    return {
        "location": "https://www.googleapis.com/compute/beta/projects/acme/regions/us-central1",
        "machineType": machine_type,
        "preemptionHistory": [
            {
                "interval": {"startTime": f"2026-08-{i + 1:02d}T07:00:00Z", "endTime": f"2026-08-{i + 2:02d}T07:00:00Z"},
                "preemptionRate": rate,
            }
            for i, rate in enumerate(rates)
        ],
        "priceHistory": [
            {
                "interval": {"startTime": "2026-08-24T07:00:00Z", "endTime": "2026-08-29T07:00:00Z"},
                "listPrice": price if price is not None else {"currencyCode": "USD", "nanos": 110192000},
            }
        ],
    }


# The `errorMsg` arm, verbatim from the entry `adam-new-cluster` logged on
# 2026-08-14 — the affected instance group arrives as a full resource URL in
# `parameters[0]`, which is why the excerpt reports only its last segment.
ERROR_MSG_ENTRY = {
    "timestamp": "2026-08-14T00:05:02.817419660Z",
    "jsonPayload": {
        "resultInfo": {
            "measureTime": "1786665900",
            "results": [
                {
                    "eventId": "63b7917e-ed60-4c15-98d1-0f74797b4c8f",
                    "errorMsg": {
                        "messageId": "scale.up.error.out.of.resources",
                        "parameters": [
                            "https://www.googleapis.com/compute/v1/projects/acme/zones/"
                            "us-central1-b/instanceGroups/gk3-prod-usc1-pool-3-b07eba62-grp"
                        ],
                    },
                }
            ],
        }
    },
}

# The node-auto-provisioning arm. A cluster failing this way never gets as far
# as a scale-up attempt, so it writes nothing under `resultInfo` at all.
NAP_ENTRY = {
    "timestamp": "2026-08-14T01:00:00Z",
    "jsonPayload": {
        "noDecisionStatus": {
            "noScaleUp": {
                "unhandledPodGroups": [
                    {
                        "napFailureReasons": [
                            {"messageId": "scale.up.error.quota.exceeded", "parameters": ["CPUS"]}
                        ]
                    }
                ]
            }
        }
    },
}

# A healthy tick. Matched by the SOP's log filter, carries neither arm.
HEALTHY_ENTRY = {"timestamp": "2026-08-14T02:00:00Z", "jsonPayload": {"status": "ok"}}


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

        clusters, not_running = fs.enumerate_clusters("acme", run=run)
        self.assertTrue(next(c for c in clusters if c["name"] == "c1")["has_nap"])
        self.assertFalse(next(c for c in clusters if c["name"] == "c2")["has_nap"])
        self.assertEqual(not_running, [])

    def test_a_cluster_that_is_not_running_comes_back_as_an_unreachable_target(self):
        # Dropped rather than recorded, a DEGRADED cluster is indistinguishable
        # from one that does not exist, and the run can publish a fleet-wide
        # all-clear over a fleet quietly missing it.
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1-a", "status": "RUNNING"},
                {"name": "sick", "location": "us-east4", "status": "DEGRADED"},
            ]
        )

        def run(argv, **kwargs):
            return run_of(0, clusters_json)

        clusters, not_running = fs.enumerate_clusters("acme", run=run)
        self.assertEqual([c["name"] for c in clusters], ["c1"])
        self.assertEqual(len(not_running), 1)
        self.assertEqual(not_running[0]["name"], "sick")
        self.assertEqual(not_running[0]["outcome"], "unreachable")
        self.assertEqual(not_running[0]["location"], "us-east4")
        self.assertIn("DEGRADED", not_running[0]["error"])


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

    def test_flags_when_no_pool_carries_the_label_at_all(self):
        """The arm's own target case, and an empty set used to turn it off. A
        Standard cluster whose pools carry no `cloud.google.com/compute-class`
        label has no pool the class can land on, which is exactly what
        `nodePoolAutoCreation: false` makes fatal."""
        cc = compute_class("cc1", [], node_pool_auto_creation=False)
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        hit = fs.check_dangling_compute_class(d, {"cc1": cc}, set())
        self.assertIsNotNone(hit)
        self.assertIn("no matching node pool", hit["excerpt"])

    def test_stays_quiet_when_the_labels_are_unknown(self):
        """`None`, not an empty set: the pools could not be read, or there are
        no user pools to read. Flagging there would accuse every workload on a
        cluster nobody could look at."""
        cc = compute_class("cc1", [], node_pool_auto_creation=False)
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, None))

    def test_a_nonexistent_class_is_still_flagged_with_labels_unknown(self):
        """Arm one needs no pool labels, so an unreadable `node-pools list`
        must not take it down with arm two."""
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "missing"})
        self.assertIsNotNone(fs.check_dangling_compute_class(d, {}, None))


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


class AutoscalerVisibilityTest(unittest.TestCase):
    def test_the_error_msg_arm_is_read(self):
        found = fs.autoscaler_message_ids([ERROR_MSG_ENTRY])
        self.assertEqual(set(found), {"scale.up.error.out.of.resources"})
        self.assertEqual(found["scale.up.error.out.of.resources"]["count"], 1)

    def test_the_nap_arm_is_read(self):
        """The SOP's own `--format` reads only this arm. A collector that
        copied it would pass every cluster that failed under the other one."""
        found = fs.autoscaler_message_ids([NAP_ENTRY])
        self.assertEqual(set(found), {"scale.up.error.quota.exceeded"})

    def test_both_arms_in_one_window_are_both_reported(self):
        found = fs.autoscaler_message_ids([ERROR_MSG_ENTRY, NAP_ENTRY, HEALTHY_ENTRY])
        self.assertEqual(
            set(found), {"scale.up.error.out.of.resources", "scale.up.error.quota.exceeded"}
        )

    def test_a_healthy_tick_is_not_a_finding(self):
        self.assertEqual(fs.autoscaler_message_ids([HEALTHY_ENTRY]), {})

    def test_an_unrelated_message_id_is_not_a_stockout(self):
        """`waiting.for.instances.timeout` is an ordinary busy cluster. Matching
        every id the autoscaler emits would turn the whole fleet critical."""
        entry = json.loads(json.dumps(ERROR_MSG_ENTRY))
        entry["jsonPayload"]["resultInfo"]["results"][0]["errorMsg"]["messageId"] = (
            "scale.up.error.waiting.for.instances.timeout"
        )
        self.assertEqual(fs.autoscaler_message_ids([entry]), {})

    def test_an_empty_read_is_not_a_crash(self):
        self.assertEqual(fs.autoscaler_message_ids(None), {})
        self.assertEqual(fs.autoscaler_message_ids([]), {})

    def test_repeats_of_one_id_collapse_to_one_finding(self):
        """A wedged cluster emits the same id every autoscaler tick, and the
        remediation branches on the id rather than the occurrence."""
        second = json.loads(json.dumps(ERROR_MSG_ENTRY))
        second["timestamp"] = "2026-08-14T06:00:00Z"
        found = fs.autoscaler_message_ids([ERROR_MSG_ENTRY, second])
        hits = fs.check_autoscaler_out_of_resources(found, "prod-usc1")
        self.assertEqual(len(hits), 1)
        self.assertIn("2 occurrences", hits[0]["excerpt"])
        self.assertIn("2026-08-14T00:05:02 .. 2026-08-14T06:00:00", hits[0]["excerpt"])

    def test_the_excerpt_does_not_claim_a_window_it_did_not_choose(self):
        """The read's `--freshness` belongs to the caller. Printing it here put
        "over the last 24h" next to timestamps thirteen days apart."""
        hits = fs.check_autoscaler_out_of_resources(
            fs.autoscaler_message_ids([ERROR_MSG_ENTRY]), "prod-usc1"
        )
        self.assertNotIn("24h", hits[0]["excerpt"])

    def test_the_excerpt_names_the_instance_group_not_its_url(self):
        hits = fs.check_autoscaler_out_of_resources(
            fs.autoscaler_message_ids([ERROR_MSG_ENTRY]), "prod-usc1"
        )
        self.assertIn("gk3-prod-usc1-pool-3-b07eba62-grp", hits[0]["excerpt"])
        self.assertNotIn("googleapis.com", hits[0]["excerpt"])
        self.assertEqual(hits[0]["object"], "Cluster/prod-usc1")


class SpotScarcityTest(unittest.TestCase):
    SHAPE = {"owners": ["ComputeClass/cc1"], "families": 1}

    def test_the_live_us_east4_response_is_not_a_finding(self):
        """26 daily intervals averaging 8.4%, which is what a healthy Spot
        shape looks like. If this ever flags, the ceiling moved."""
        hit, limitation = fs.check_spot_scarcity(
            "n2-standard-8", self.SHAPE, "us-east4", capacity_history([0.05, 0.06, 0.04, 0.05, 0.07, 0.09, 0.1, 0.07])
        )
        self.assertIsNone(hit)
        self.assertIsNone(limitation)

    def test_a_shape_over_the_ceiling_with_no_fallback_is_flagged(self):
        hit, limitation = fs.check_spot_scarcity(
            "a2-highgpu-1g", self.SHAPE, "us-central1", capacity_history([0.3] * 10)
        )
        self.assertIsNone(limitation)
        self.assertIn("30.0% per day over 10 days", hit["excerpt"])
        self.assertEqual(hit["object"], "ComputeClass/cc1")

    def test_a_multi_family_chain_over_the_ceiling_is_not_flagged(self):
        """§3.8's "without alternative family fallbacks" — a chain spanning two
        families survives its worst shape being preempted."""
        shape = {"owners": ["ComputeClass/cc1"], "families": 3}
        hit, limitation = fs.check_spot_scarcity(
            "a2-highgpu-1g", shape, "us-central1", capacity_history([0.3] * 10)
        )
        self.assertIsNone(hit)
        self.assertIsNone(limitation)

    def test_one_bad_day_inside_a_calm_month_is_not_a_finding(self):
        """The mean, not the maximum. A 90% afternoon is a zonal incident that
        already resolved; flagging the peak turns the fleet critical after it."""
        hit, _ = fs.check_spot_scarcity(
            "n2-standard-8", self.SHAPE, "us-central1", capacity_history([0.9] + [0.02] * 20)
        )
        self.assertIsNone(hit)

    def test_too_short_a_history_is_unmeasured_rather_than_clean(self):
        hit, limitation = fs.check_spot_scarcity(
            "n2-standard-8", self.SHAPE, "us-central1", capacity_history([0.01, 0.01])
        )
        self.assertIsNone(hit)
        self.assertIn("2 daily interval", limitation)

    def test_a_response_with_no_history_is_unmeasured_rather_than_clean(self):
        hit, limitation = fs.check_spot_scarcity("n2-standard-8", self.SHAPE, "us-central1", {})
        self.assertIsNone(hit)
        self.assertIn("no preemptionHistory", limitation)

    def test_the_price_below_one_dollar_survives_the_missing_units_field(self):
        self.assertEqual(fs.spot_list_price(capacity_history([0.3] * 10)), "0.1102 USD")

    def test_a_price_above_one_unit_reads_both_halves(self):
        advice = capacity_history([0.3] * 10, price={"currencyCode": "USD", "units": "3", "nanos": 500000000})
        self.assertEqual(fs.spot_list_price(advice), "3.5000 USD")

    def test_no_price_history_is_an_empty_string_not_a_crash(self):
        self.assertEqual(fs.spot_list_price({"preemptionHistory": []}), "")


class SpotShapeEnumerationTest(unittest.TestCase):
    def test_a_spot_priority_with_a_machine_type_is_a_shape(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        self.assertEqual(set(fs.spot_shapes([cc], [])), {"n2-standard-8"})

    def test_an_on_demand_priority_is_not(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8"}])
        self.assertEqual(fs.spot_shapes([cc], []), {})

    def test_a_spot_node_pool_is_a_shape_with_no_fallback_by_construction(self):
        """A node pool has no priority chain at all: when its shape runs out,
        nothing else is tried."""
        pools = [{"name": "p1", "config": {"spot": True, "machineType": "c3-standard-4"}}]
        shapes = fs.spot_shapes([], pools)
        self.assertEqual(shapes["c3-standard-4"]["families"], 1)
        self.assertEqual(shapes["c3-standard-4"]["owners"], ["NodePool/p1"])

    def test_the_family_count_spans_the_whole_chain_not_just_its_spot_arm(self):
        """The on-demand tail is exactly the fallback §3.8 asks about."""
        cc = compute_class(
            "cc1",
            [
                {"machineType": "a2-highgpu-1g", "spot": True},
                {"machineType": "n2-standard-8"},
                {"machineFamily": "c3"},
            ],
        )
        self.assertEqual(fs.spot_shapes([cc], [])["a2-highgpu-1g"]["families"], 3)

    def test_one_shape_requested_twice_is_read_once_and_names_both_owners(self):
        cc1 = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        cc2 = compute_class("cc2", [{"machineType": "n2-standard-8", "spot": True}])
        shapes = fs.spot_shapes([cc1, cc2], [])
        self.assertEqual(list(shapes), ["n2-standard-8"])
        self.assertEqual(shapes["n2-standard-8"]["owners"], ["ComputeClass/cc1", "ComputeClass/cc2"])

    def test_a_family_only_spot_priority_is_unqueryable_not_absent(self):
        """`capacity-history --machine-type` is singular and required, so this
        legal configuration has no shape to ask about. Silence would read as a
        cluster with no Spot at all."""
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        self.assertEqual(fs.spot_shapes([cc], []), {})
        self.assertEqual(fs.spot_without_a_shape([cc], []), (["cc1:c3"], []))

    def test_a_priority_with_a_machine_type_is_not_also_reported_unqueryable(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        self.assertEqual(fs.spot_without_a_shape([cc], []), ([], []))

    def test_a_shape_free_spot_priority_is_unpinned_rather_than_unqueryable(self):
        """GKE's own `autopilot-spot`, which ships on every Autopilot cluster:
        it pins neither family nor type, so every family is available to it and
        no shape can be scarce for it. Counting it as an unmeasurable gap put a
        permanent false coverage gap on every Autopilot cluster in the fleet."""
        cc = compute_class("autopilot-spot", [{"spot": True}])
        self.assertEqual(fs.spot_without_a_shape([cc], []), ([], ["ComputeClass/autopilot-spot"]))

    def test_a_spot_pool_with_no_machine_type_is_unqueryable(self):
        pools = [{"name": "p1", "config": {"spot": True}}]
        self.assertEqual(fs.spot_without_a_shape([], pools), (["NodePool/p1"], []))


class CollectClusterTest(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    # GKE's own answer when `node-pools list` is aimed at an Autopilot
    # cluster. The fake returned rc=0 for it whatever the cluster was, which
    # is why `test_autopilot_skips_single_zone_nodepool` below could pass
    # while the collector was still issuing a read the API refuses: a fake
    # that answers every argv successfully cannot tell a command the API runs
    # from one it rejects.
    AUTOPILOT_NODE_POOLS_ERROR = (
        "ERROR: (gcloud.container.node-pools.list) ResponseError: code=400, "
        "message=Autopilot node pools cannot be accessed or modified."
    )

    def run_with(
        self,
        dump_items=(),
        pools=(),
        cluster=None,
        pools_rc=0,
        pools_stderr="denied",
        log_entries=None,
        log_rc=0,
        log_stderr="denied",
        advice=None,
        advice_rc=0,
        advice_stderr="denied",
    ):
        target = cluster or self.CLUSTER
        self.issued = []

        def run(argv, **kwargs):
            self.issued.append(argv)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*dump_items)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                if target.get("autopilot"):
                    return run_of(1, "", self.AUTOPILOT_NODE_POOLS_ERROR)
                if pools_rc:
                    return run_of(pools_rc, "", pools_stderr)
                return run_of(0, json.dumps(list(pools)))
            if argv[:3] == ["gcloud", "logging", "read"]:
                if log_rc:
                    return run_of(log_rc, "", log_stderr)
                # gcloud prints nothing at all when nothing matched, which is
                # what the bare `run_of(0, "")` below stands in for elsewhere.
                return run_of(0, json.dumps(log_entries) if log_entries else "")
            if argv[:5] == ["gcloud", "beta", "compute", "advice", "capacity-history"]:
                if advice_rc:
                    return run_of(advice_rc, "", advice_stderr)
                machine_type = argv[argv.index("--machine-type") + 1]
                body = advice(machine_type) if callable(advice) else advice
                return run_of(0, json.dumps(body) if body is not None else "")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                return fs.collect_cluster(target, run=run)

    def issued_node_pools_read(self):
        return [a for a in self.issued if a[:3] == ["gcloud", "container", "node-pools"]]

    def declared_not_applicable(self, entry):
        return {e["check"] for e in entry.get("checks_not_applicable") or []}

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
        entry = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})

    def test_autopilot_declares_it_rather_than_leaving_it_absent(self):
        """Absent from `commands` is how a check nobody ran looks too, so §6
        read this as a coverage gap unless the model happened to know GKE well
        enough to excuse it by hand — which made a run's honesty about the gap
        depend on the model rather than on the cluster."""
        entry = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        declared = {e["check"]: e["reason"] for e in entry.get("checks_not_applicable") or []}
        # `spot-scarcity-risk` rides along because this fixture's cluster asks
        # for no Spot capacity at all, which is its own declared non-applicability.
        self.assertEqual(set(declared), {"single-zone-nodepool", "spot-scarcity-risk"})
        self.assertIn("Autopilot", declared["single-zone-nodepool"])

    def test_autopilot_never_issues_the_node_pools_read(self):
        """The API answers 400 for it, and its only consumers cannot apply to a
        cluster with no user node pools."""
        self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        self.assertEqual(self.issued_node_pools_read(), [])

    def test_a_standard_cluster_still_issues_it_and_declares_nothing(self):
        entry = self.run_with(pools=[{"name": "p1", "locations": ["us-central1-a", "us-central1-b"]}])
        self.assertEqual(len(self.issued_node_pools_read()), 1)
        self.assertNotIn("single-zone-nodepool", self.declared_not_applicable(entry))
        self.assertIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})

    def test_a_failed_pools_read_says_so_instead_of_dropping_the_check(self):
        """`[]` meant both "no pools" and "could not read the pools", so a
        denied read took the check out of the manifest with nothing recording
        that it had been attempted — a coverage gap §6 could name but not
        explain."""
        entry = self.run_with(pools_rc=1, pools_stderr="PERMISSION_DENIED on container.nodePools.list")
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})
        # A read that was refused is a limitation, never a non-applicability:
        # the check applies, nobody could run it.
        self.assertNotIn("single-zone-nodepool", self.declared_not_applicable(entry))
        self.assertIn("single-zone-nodepool", entry["limitations"])
        self.assertIn("PERMISSION_DENIED", entry["limitations"])
        self.assertIn("rc=1", entry["limitations"])

    def test_a_standard_cluster_with_no_pools_ran_the_check(self):
        """The other half of the same conflation: zero pools is an answer, and
        recording nothing for it made an empty cluster look unaudited."""
        entry = self.run_with(pools=[])
        self.assertIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})
        self.assertNotIn("limitations", entry)
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["candidates"]})

    def issued_advice_reads(self):
        return [a for a in self.issued if a[:5] == ["gcloud", "beta", "compute", "advice", "capacity-history"]]

    def test_a_clean_autoscaler_window_records_the_read_it_made(self):
        """"Nothing in 24h" is the answer this check exists to give. Recording
        nothing for it makes a healthy cluster look unaudited."""
        entry = self.run_with()
        self.assertIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})
        self.assertNotIn("autoscaler-out-of-resources", {c["check"] for c in entry["candidates"]})

    def test_a_stockout_in_the_window_is_reported(self):
        entry = self.run_with(log_entries=[ERROR_MSG_ENTRY, HEALTHY_ENTRY])
        hits = [c for c in entry["candidates"] if c["check"] == "autoscaler-out-of-resources"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")
        self.assertIn("scale.up.error.out.of.resources", hits[0]["excerpt"])

    def test_a_refused_logging_read_is_a_limitation_not_a_clean_cluster(self):
        entry = self.run_with(log_rc=1, log_stderr="PERMISSION_DENIED on logging.logEntries.list")
        self.assertNotIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})
        self.assertIn("autoscaler-out-of-resources", entry["limitations"])
        self.assertIn("PERMISSION_DENIED", entry["limitations"])

    def test_no_spot_anywhere_asks_no_capacity_history_and_declares_why(self):
        entry = self.run_with()
        self.assertEqual(self.issued_advice_reads(), [])
        self.assertIn("spot-scarcity-risk", self.declared_not_applicable(entry))

    def test_a_spot_shape_is_read_in_the_cluster_region(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        entry = self.run_with(dump_items=[cc], advice=lambda mt: capacity_history([0.05] * 10, mt))
        reads = self.issued_advice_reads()
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0][reads[0].index("--region") + 1], "us-central1")
        self.assertIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["candidates"]})

    def test_a_zonal_cluster_reads_its_region_not_its_zone(self):
        """`capacity-history` takes `--region` and rejects a zone."""
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        self.run_with(
            dump_items=[cc],
            cluster={**self.CLUSTER, "location": "us-central1-a"},
            advice=lambda mt: capacity_history([0.05] * 10, mt),
        )
        read = self.issued_advice_reads()[0]
        self.assertEqual(read[read.index("--region") + 1], "us-central1")

    def test_a_scarce_spot_shape_is_reported(self):
        cc = compute_class("cc1", [{"machineType": "a2-highgpu-1g", "spot": True}])
        entry = self.run_with(dump_items=[cc], advice=lambda mt: capacity_history([0.4] * 10, mt))
        hits = [c for c in entry["candidates"] if c["check"] == "spot-scarcity-risk"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_the_shape_ceiling_names_what_it_did_not_read(self):
        """A collector that quietly stops looking is the failure this whole
        stream reports on."""
        many = [{"machineType": f"n2-standard-{n}", "spot": True} for n in (2, 4, 8, 16, 32, 48, 64, 80, 96)]
        cc = compute_class("cc1", many)
        entry = self.run_with(dump_items=[cc], advice=lambda mt: capacity_history([0.05] * 10, mt))
        self.assertEqual(len(self.issued_advice_reads()), fs.SPOT_MAX_SHAPES)
        self.assertIn("8 of this cluster's 9 distinct Spot machine shapes", entry["limitations"])

    def test_a_refused_advice_read_is_a_limitation_not_a_clean_shape(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        entry = self.run_with(dump_items=[cc], advice_rc=1, advice_stderr="API [compute.googleapis.com] not enabled")
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        self.assertIn("n2-standard-8", entry["limitations"])
        self.assertIn("not enabled", entry["limitations"])

    def test_a_family_only_spot_chain_is_a_limitation_not_a_non_applicability(self):
        """The cluster does ask for Spot; the API just cannot be asked about it."""
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        entry = self.run_with(dump_items=[cc])
        self.assertEqual(self.issued_advice_reads(), [])
        self.assertNotIn("spot-scarcity-risk", self.declared_not_applicable(entry))
        self.assertIn("cc1:c3", entry["limitations"])

    def test_a_shape_free_spot_chain_says_so_rather_than_denying_the_cluster_uses_spot(self):
        """The live fleet is sixteen Autopilot-flavoured clusters all carrying
        GKE's `autopilot-spot`, so the wrong reason here is the one an operator
        reads sixteen times."""
        cc = compute_class("autopilot-spot", [{"spot": True}])
        entry = self.run_with(dump_items=[cc])
        reason = next(
            e["reason"] for e in entry["checks_not_applicable"] if e["check"] == "spot-scarcity-risk"
        )
        self.assertIn("ComputeClass/autopilot-spot", reason)
        self.assertIn("leaves the machine shape entirely to GKE", reason)
        self.assertNotIn("limitations", entry)

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
