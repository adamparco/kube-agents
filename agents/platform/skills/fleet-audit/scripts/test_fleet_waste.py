#!/usr/bin/env python3
"""Tests for fleet_waste.py, the fleet-wide-cost-analysis collector."""

import json
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_waste as fw  # noqa: E402

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def run_of(rc: int, stdout: str = "", stderr: str = "") -> fw.Run:
    return fw.Run(["x"], rc, stdout, stderr, 0.01)


def dump_of(*items) -> dict:
    return {"items": list(items)}


def obj(kind, name, ns=None, **overrides):
    meta = {"name": name, "creationTimestamp": "2026-01-01T00:00:00Z", "labels": {}, "annotations": {}}
    if ns is not None:
        meta["namespace"] = ns
    doc = {"kind": kind, "metadata": meta, "spec": {}, "status": {}}
    for path, value in overrides.items():
        target = doc
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return doc


class ParseCpuMemTest(unittest.TestCase):
    def test_millicores(self):
        self.assertEqual(fw.parse_cpu_cores("150m"), 0.15)

    def test_whole_cores(self):
        self.assertEqual(fw.parse_cpu_cores("2"), 2.0)

    def test_mebibytes(self):
        self.assertEqual(fw.parse_mem_mib("512Mi"), 512.0)

    def test_gibibytes(self):
        self.assertEqual(fw.parse_mem_mib("2Gi"), 2048.0)

    def test_kibibytes(self):
        self.assertAlmostEqual(fw.parse_mem_mib("2048Ki"), 2.0)

    def test_bare_number_is_bytes_not_mebibytes(self):
        self.assertAlmostEqual(fw.parse_mem_mib(str(512 * 1024 * 1024)), 512.0)

    def test_unparseable_is_none(self):
        self.assertIsNone(fw.parse_cpu_cores("garbage"))
        self.assertIsNone(fw.parse_mem_mib("garbage"))


MIB = 1024 * 1024


def series_of(ns, pod, *values, key="doubleValue"):
    return {
        "resource": {"labels": {"namespace_name": ns, "pod_name": pod}},
        "points": [{"value": {key: v}} for v in values],
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text

    def json(self):
        return self._payload


class FakeSession:
    """Answers the two metric queries `fetch_usage_peaks` issues."""

    def __init__(self, cpu=(), mem=(), status=200, text="", raises=None):
        self.cpu, self.mem, self.status, self.text, self.raises = list(cpu), list(mem), status, text, raises
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        if self.raises:
            raise self.raises
        if self.status != 200:
            return FakeResponse(self.status, text=self.text)
        is_cpu = "cpu/core_usage_time" in params["filter"]
        return FakeResponse(200, {"timeSeries": self.cpu if is_cpu else self.mem})


def usage_session(*pods, **kwargs):
    """A `FakeSession` answering with one series per `(ns, pod, cores, mib)`.

    Collector tests need *some* usage data or `overrequest` reads as degraded,
    which is a different code path from the one they are exercising.
    """
    pods = pods or (("default", "idle-1", 0.01, 8.0),)
    return FakeSession(
        cpu=[series_of(ns, pod, cores) for ns, pod, cores, _ in pods],
        mem=[series_of(ns, pod, mib * MIB) for ns, pod, _, mib in pods],
        **kwargs,
    )


NO_USAGE = dict(cpu=[], mem=[])


class FetchUsagePeaksTest(unittest.TestCase):
    def fetch(self, session, **kwargs):
        return fw.fetch_usage_peaks("acme", "prod-usc1", session=session, now=NOW, **kwargs)

    def test_cpu_and_memory_merge_into_one_peak_per_pod(self):
        session = FakeSession(
            cpu=[series_of("default", "api-1", 0.15)],
            mem=[series_of("default", "api-1", 256 * MIB)],
        )
        peaks, ok, result = self.fetch(session)
        self.assertTrue(ok)
        self.assertEqual(result.rc, 0)
        # Cores and MiB -- the units `check_overrequest` compares against
        # parsed `resources.requests`, not the API's cores and raw bytes.
        self.assertEqual(peaks[("default", "api-1")], (0.15, 256.0))

    def test_the_peak_is_the_max_not_the_last_or_the_mean(self):
        session = FakeSession(
            cpu=[series_of("default", "api-1", 0.1, 4.0, 0.2)],
            mem=[series_of("default", "api-1", MIB, 8 * MIB, 2 * MIB)],
        )
        peaks, _, _ = self.fetch(session)
        self.assertEqual(peaks[("default", "api-1")], (4.0, 8.0))

    def test_int64_values_are_read_as_well_as_double(self):
        # Monitoring returns memory as an integer type; a reader that only
        # understood doubleValue would see every pod using zero bytes.
        session = FakeSession(
            cpu=[series_of("default", "api-1", 0.5)],
            mem=[series_of("default", "api-1", str(512 * MIB), key="int64Value")],
        )
        peaks, _, _ = self.fetch(session)
        self.assertEqual(peaks[("default", "api-1")], (0.5, 512.0))

    def test_a_pod_in_one_metric_only_still_appears(self):
        session = FakeSession(cpu=[series_of("default", "api-1", 0.3)], mem=[])
        peaks, ok, _ = self.fetch(session)
        self.assertTrue(ok)
        self.assertEqual(peaks[("default", "api-1")], (0.3, 0.0))

    def test_an_empty_answer_is_unavailable_rather_than_zero_usage(self):
        # The one failure mode that turns this check into a fleet-wide false
        # positive. An empty result read as "every pod used nothing" flags
        # every workload on the cluster as pure waste, with a plausible-looking
        # peak of 0.00 vCPU behind it.
        peaks, ok, result = self.fetch(FakeSession(cpu=[], mem=[]))
        self.assertEqual(peaks, {})
        self.assertFalse(ok)
        self.assertIn("no time series", result.stderr)

    def test_an_api_error_is_unavailable_and_keeps_the_status(self):
        peaks, ok, result = self.fetch(FakeSession(status=403, text="caller lacks monitoring.timeSeries.list"))
        self.assertFalse(ok)
        self.assertEqual(peaks, {})
        self.assertEqual(result.rc, 403)
        self.assertIn("monitoring.timeSeries.list", result.stderr)

    def test_a_transport_exception_is_unavailable_not_a_crash(self):
        peaks, ok, result = self.fetch(FakeSession(raises=OSError("connection reset")))
        self.assertFalse(ok)
        self.assertEqual(result.rc, -1)
        self.assertIn("connection reset", result.stderr)

    def test_no_session_degrades_instead_of_raising(self):
        # `collect_fleet` passes None when ADC could not be resolved. Every
        # object-state check still has to run.
        peaks, ok, result = self.fetch(None)
        self.assertFalse(ok)
        self.assertEqual(peaks, {})
        self.assertIn("ADC", result.stderr)

    def test_pagination_follows_the_next_page_token(self):
        pages = {
            "cpu": [
                {"timeSeries": [series_of("default", "api-1", 0.1)], "nextPageToken": "more"},
                {"timeSeries": [series_of("default", "api-2", 0.2)]},
            ],
            "mem": [{"timeSeries": [series_of("default", "api-1", MIB)]}],
        }
        seen = []

        class Paged:
            def get(self, url, params=None, timeout=None):
                seen.append(params.get("pageToken"))
                key = "cpu" if "cpu/core_usage_time" in params["filter"] else "mem"
                queue = pages[key]
                return FakeResponse(200, queue.pop(0))

        peaks, ok, _ = self.fetch(Paged())
        self.assertTrue(ok)
        self.assertEqual(sorted(peaks), [("default", "api-1"), ("default", "api-2")])
        self.assertEqual(seen, [None, "more", None])

    def test_the_query_asks_for_a_peak_per_pod_over_the_whole_window(self):
        # These four parameters are the whole method. Without the secondary
        # ALIGN_MAX the response is one point per alignment period and the
        # caller would have to reduce it itself; without REDUCE_SUM grouped by
        # namespace and pod the figures stay per-container and compare against
        # a pod's summed requests as if each container were the whole pod.
        session = FakeSession(cpu=[series_of("d", "p", 1.0)], mem=[series_of("d", "p", MIB)])
        self.fetch(session, window_hours=24)
        params = session.calls[0]
        self.assertEqual(params["secondaryAggregation.perSeriesAligner"], "ALIGN_MAX")
        self.assertEqual(params["secondaryAggregation.alignmentPeriod"], "86400s")
        self.assertEqual(params["aggregation.crossSeriesReducer"], "REDUCE_SUM")
        self.assertEqual(
            params["aggregation.groupByFields"],
            ["resource.labels.namespace_name", "resource.labels.pod_name"],
        )
        self.assertIn('resource.labels.cluster_name="prod-usc1"', params["filter"])
        self.assertEqual(params["interval.startTime"], "2026-07-31T00:00:00Z")
        self.assertEqual(params["interval.endTime"], "2026-08-01T00:00:00Z")

    def test_cpu_is_a_rate_and_memory_is_not(self):
        # `core_usage_time` is a cumulative counter in core-seconds: aligned
        # any way but ALIGN_RATE it reports seconds of CPU consumed since the
        # container started, which is not cores and grows without bound.
        session = FakeSession(cpu=[series_of("d", "p", 1.0)], mem=[series_of("d", "p", MIB)])
        self.fetch(session)
        aligners = {c["filter"].split('"')[1]: c["aggregation.perSeriesAligner"] for c in session.calls}
        self.assertEqual(aligners[fw.CPU_METRIC], "ALIGN_RATE")
        self.assertEqual(aligners[fw.MEM_METRIC], "ALIGN_MAX")


class OrphanPvTest(unittest.TestCase):
    def pv(self, phase, reclaim="Retain", **overrides):
        doc = obj("PersistentVolume", "pv-1", **{"spec.persistentVolumeReclaimPolicy": reclaim, "status.phase": phase, "spec.capacity": {"storage": "10Gi"}})
        for path, value in overrides.items():
            target = doc
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        return doc

    def context(self, pvs, pvcs=None, sts=None):
        return {"pvs": pvs, "pvcs": pvcs or [], "statefulsets": sts or []}

    def test_flags_released_over_7_days(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"})
        hits = fw.check_orphan_pv(self.context([pv]), now=NOW)
        self.assertEqual(len(hits), 1)

    def test_does_not_flag_released_under_7_days(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-07-30T00:00:00Z"})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_delete_policy_is_never_flagged(self):
        pv = self.pv("Released", reclaim="Delete", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_falls_back_to_object_age_when_transition_time_absent(self):
        pv = self.pv("Failed")  # creationTimestamp is 2026-01-01, > 7 days before NOW
        hits = fw.check_orphan_pv(self.context([pv]), now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("lastPhaseTransitionTime absent", hits[0]["excerpt"])

    def test_available_unclaimed_over_30_days_is_flagged(self):
        pv = self.pv("Available")  # created 2026-01-01, unclaimed
        self.assertEqual(len(fw.check_orphan_pv(self.context([pv]), now=NOW)), 1)

    def test_available_with_claim_ref_is_not_flagged(self):
        pv = self.pv("Available", **{"spec.claimRef": {"namespace": "default", "name": "x"}})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_claim_ref_naming_a_live_pvc_is_suppressed(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z", "spec.claimRef": {"namespace": "default", "name": "data"}})
        pvc = obj("PersistentVolumeClaim", "data", ns="default")
        self.assertEqual(fw.check_orphan_pv(self.context([pv], pvcs=[pvc]), now=NOW), [])

    def test_scaled_to_zero_statefulset_claim_is_suppressed(self):
        pv = self.pv(
            "Released",
            **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z", "spec.claimRef": {"namespace": "default", "name": "data-mydb-0"}},
        )
        sts = obj("StatefulSet", "mydb", ns="default")
        self.assertEqual(fw.check_orphan_pv(self.context([pv], sts=[sts]), now=NOW), [])

    def test_backup_annotated_pv_is_suppressed(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"})
        pv["metadata"]["annotations"]["velero.io/backup-name"] = "nightly"
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_large_disk_is_major(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z", "spec.capacity": {"storage": "500Gi"}})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW)[0]["severity"], "major")

    def test_a_part_day_over_the_gate_reports_the_gate_not_the_day_after(self):
        # 30d 14h. The gate is 30, so this is the shape that matters: rounding
        # to nearest printed "31d" and handed the model a number a day past a
        # threshold it is entitled to quote back in a title.
        pv = self.pv("Available", **{"metadata.creationTimestamp": "2026-07-01T10:00:00Z"})
        self.assertIn("AGE=30d", fw.check_orphan_pv(self.context([pv]), now=NOW)[0]["excerpt"])


class UnconsumedPvcTest(unittest.TestCase):
    def pvc(self, name="data", ns="default", phase="Bound", created="2026-01-01T00:00:00Z", capacity="10Gi"):
        return obj("PersistentVolumeClaim", name, ns=ns, **{"status.phase": phase, "status.capacity": {"storage": capacity}}, **{"metadata.creationTimestamp": created})

    def test_flags_bound_unreferenced_over_14_days(self):
        context = {"pods": [], "pvcs": [self.pvc()], "statefulsets": []}
        self.assertEqual(len(fw.check_unconsumed_pvc(context, now=NOW)), 1)

    def test_does_not_flag_referenced_by_a_pod(self):
        pod = obj("Pod", "p", ns="default", **{"spec.volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]})
        context = {"pods": [pod], "pvcs": [self.pvc()], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_under_14_days(self):
        context = {"pods": [], "pvcs": [self.pvc(created="2026-07-25T00:00:00Z")], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_scaled_to_zero_statefulset_claim(self):
        sts = obj("StatefulSet", "mydb", ns="default")
        context = {"pods": [], "pvcs": [self.pvc(name="data-mydb-0")], "statefulsets": [sts]}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_system_namespace(self):
        context = {"pods": [], "pvcs": [self.pvc(ns="kube-system")], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_unbound(self):
        context = {"pods": [], "pvcs": [self.pvc(phase="Pending")], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_a_part_day_over_the_gate_reports_the_gate_not_the_day_after(self):
        # 14d 14h, against a 14-day gate. See the matching case in OrphanPvTest.
        context = {"pods": [], "pvcs": [self.pvc(created="2026-07-17T10:00:00Z")], "statefulsets": []}
        self.assertIn("AGE=14d", fw.check_unconsumed_pvc(context, now=NOW)[0]["excerpt"])


class IdleNodepoolTest(unittest.TestCase):
    def node(self, name, pool, cpu_alloc="4", mem_alloc="8Gi", unschedulable=False):
        return obj(
            "Node", name,
            **{
                "metadata.labels": {"cloud.google.com/gke-nodepool": pool},
                "status.allocatable": {"cpu": cpu_alloc, "memory": mem_alloc},
                "spec.unschedulable": unschedulable,
                "metadata.creationTimestamp": "2026-01-01T00:00:00Z",
            },
        )

    def pod_on(self, node, cpu_req="0", mem_req="0Mi", daemonset=False, phase="Running"):
        owners = [{"kind": "DaemonSet", "name": "ds"}] if daemonset else []
        return obj(
            "Pod", f"p-{node}", ns="default",
            **{
                "spec.nodeName": node,
                "spec.containers": [{"resources": {"requests": {"cpu": cpu_req, "memory": mem_req}}}],
                "metadata.ownerReferences": owners,
                "status.phase": phase,
            },
        )

    def pool(self, name, min_nodes=1, autoscaling_enabled=True, machine_type="e2-standard-8", accelerators=None):
        return {
            "name": name,
            "autoscaling": {"enabled": autoscaling_enabled, "minNodeCount": min_nodes},
            "config": {"machineType": machine_type, "accelerators": accelerators or []},
        }

    def test_flags_idle_pool_with_nonzero_floor(self):
        nodes = [self.node("n1", "idle-pool")]
        pods = [self.pod_on("n1", cpu_req="200m", mem_req="200Mi")]  # well under 15% of 4 vCPU/8Gi
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("idle-pool"), self.pool("other-pool")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "NodePool/idle-pool")

    def test_does_not_flag_the_only_pool_in_the_cluster(self):
        nodes = [self.node("n1", "only-pool")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("only-pool")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_does_not_flag_pool_at_min_zero(self):
        nodes = [self.node("n1", "burst")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("burst", min_nodes=0), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_daemonset_pods_are_excluded_from_the_allocation_math(self):
        nodes = [self.node("n1", "pool")]
        pods = [self.pod_on("n1", cpu_req="3", mem_req="6Gi", daemonset=True)]
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("pool"), self.pool("other")]
        # DaemonSet-only allocation still reads as idle -- 90% of a node
        # would look "used" if the filter did not exclude it.
        self.assertEqual(len(fw.check_idle_nodepool(context, pools, now=NOW)), 1)

    def test_does_not_flag_a_well_utilized_pool(self):
        nodes = [self.node("n1", "pool")]
        pods = [self.pod_on("n1", cpu_req="3", mem_req="6Gi")]
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_accelerator_pool_is_major_even_with_few_nodes(self):
        nodes = [self.node("n1", "gpu-pool")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("gpu-pool", machine_type="a2-highgpu-1g", accelerators=[{"acceleratorType": "nvidia-tesla-a100"}]), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_small_machine_few_nodes_is_minor(self):
        nodes = [self.node("n1", "pool", cpu_alloc="2", mem_alloc="4Gi")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("pool", machine_type="e2-small"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_a_tpu_pool_is_major_without_an_accelerators_list(self):
        """A TPU node pool carries its topology, not `config.accelerators`, so
        `has_accelerator` is False there and the machine type is the only
        signal. §3.7: an idle accelerator pool with a non-zero floor is the
        largest reclaimable item this audit can find."""
        nodes = [self.node("n1", "tpu-pool")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("tpu-pool", machine_type="ct5lp-hightpu-4t"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_one_busy_node_stops_the_pool_being_called_idle(self):
        """§3.7 flags when *every* node in the pool is under 15%, not when the
        pool averages under 15%. Nine empty nodes and one full one average 10%,
        and the full one is exactly what stops the pool shrinking."""
        nodes = [self.node(f"n{i}", "pool") for i in range(10)]
        pods = [self.pod_on("n0", cpu_req="3800m", mem_req="7Gi")]
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_a_pool_where_every_node_is_idle_is_still_flagged(self):
        nodes = [self.node(f"n{i}", "pool") for i in range(10)]
        pods = [self.pod_on(f"n{i}", cpu_req="100m", mem_req="100Mi") for i in range(10)]
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(len(fw.check_idle_nodepool(context, pools, now=NOW)), 1)

    def test_a_node_reporting_no_allocatable_is_not_evidence_of_idleness(self):
        nodes = [self.node("n1", "pool"), self.node("n2", "pool", cpu_alloc="0", mem_alloc="0")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_pool_age_comes_from_the_oldest_node_not_the_first_listed(self):
        """`kubectl get nodes` comes back name-sorted, so `nodes[0]` was the
        alphabetically first node. A months-old pool that autoscaled up a node
        named `a-...` yesterday exempted itself from the whole check."""
        old = self.node("z-old", "pool")  # creationTimestamp 2026-01-01
        new = self.node("a-new", "pool")
        new["metadata"]["creationTimestamp"] = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        context = {"nodes": [new, old], "pods": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(len(fw.check_idle_nodepool(context, pools, now=NOW)), 1)

    def test_a_genuinely_new_pool_is_still_skipped(self):
        new = self.node("n1", "pool")
        new["metadata"]["creationTimestamp"] = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        context = {"nodes": [new], "pods": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])


class MachineTypeVcpusTest(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(fw._machine_type_vcpus("e2-standard-8"), 8)

    def test_highmem(self):
        self.assertEqual(fw._machine_type_vcpus("n2-highmem-16"), 16)

    def test_custom(self):
        self.assertEqual(fw._machine_type_vcpus("custom-4-16384"), 4)

    def test_small_is_unmatched(self):
        self.assertIsNone(fw._machine_type_vcpus("e2-small"))

    def test_a_local_ssd_variant_still_parses(self):
        self.assertEqual(fw._machine_type_vcpus("c3-standard-8-lssd"), 8)

    def test_an_accelerator_type_is_not_read_as_a_vcpu_count(self):
        """`a3-highgpu-8g`'s `8` counts GPUs, not vCPUs -- it is a 208-vCPU
        machine. Answering 8 would be luck; answering None and letting
        `_is_big_machine` decide is the honest split."""
        for machine_type in ("a2-highgpu-1g", "a2-ultragpu-8g", "a3-megagpu-8g", "ct5lp-hightpu-4t"):
            with self.subTest(machine_type=machine_type):
                self.assertIsNone(fw._machine_type_vcpus(machine_type))


class IsBigMachineTest(unittest.TestCase):
    def test_eight_vcpus_is_the_line(self):
        self.assertTrue(fw._is_big_machine("e2-standard-8"))
        self.assertFalse(fw._is_big_machine("e2-standard-4"))

    def test_every_accelerator_family_is_big(self):
        for machine_type in ("a2-highgpu-1g", "a2-ultragpu-8g", "a3-megagpu-8g", "a3-highgpu-8g", "ct5lp-hightpu-4t"):
            with self.subTest(machine_type=machine_type):
                self.assertTrue(fw._is_big_machine(machine_type))

    def test_an_unparseable_type_is_not_assumed_big(self):
        self.assertFalse(fw._is_big_machine("e2-micro"))
        self.assertFalse(fw._is_big_machine(""))


class ScaledownBlockedTest(unittest.TestCase):
    def test_bare_pod_with_local_storage_is_critical(self):
        pod = obj("Pod", "debug", ns="ci", **{"spec.nodeName": "n1", "spec.volumes": [{"emptyDir": {}}], "metadata.ownerReferences": []})
        context = {"pods": [pod], "pdbs": []}
        hits = fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])
        self.assertEqual(hits[0]["severity"], "critical")

    def test_safe_to_evict_false_on_owned_pod_is_critical(self):
        pod = obj(
            "Pod", "app", ns="default",
            **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}], "metadata.annotations": {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"}},
        )
        context = {"pods": [pod], "pdbs": []}
        hits = fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])
        self.assertEqual(hits[0]["severity"], "critical")

    def test_pdb_backed_pod_is_never_flagged_here(self):
        pod = obj("Pod", "app", ns="default", **{"spec.nodeName": "n1", "metadata.labels": {"app": "web"}, "metadata.ownerReferences": []})
        pdb = obj("PodDisruptionBudget", "pdb1", ns="default", **{"spec.selector": {"matchLabels": {"app": "web"}}})
        context = {"pods": [pod], "pdbs": [pdb]}
        self.assertEqual(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}]), [])

    def test_no_idle_pool_hits_means_nothing_to_check(self):
        self.assertEqual(fw.check_scaledown_blocked({"pods": [], "pdbs": []}, []), [])

    def test_ordinary_evictable_pod_is_not_flagged(self):
        pod = obj("Pod", "app", ns="default", **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}]})
        context = {"pods": [pod], "pdbs": []}
        self.assertEqual(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}]), [])

    def evict_pod(self, value, node="n1"):
        return obj(
            "Pod", "app", ns="default",
            **{
                "spec.nodeName": node,
                "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}],
                "metadata.annotations": {fw.SAFE_TO_EVICT_ANNOTATION: value},
            },
        )

    def test_every_spelling_the_autoscaler_accepts_pins_the_node(self):
        """The autoscaler parses this annotation with `strconv.ParseBool`, so
        `"False"`, `"FALSE"` and `"0"` pin a pod exactly as `"false"` does. An
        exact match against `"false"` read all three as the annotation being
        absent and let the node through unreported."""
        for value in ("false", "False", "FALSE", "0", "f", " false "):
            with self.subTest(value=value):
                context = {"pods": [self.evict_pod(value)], "pdbs": []}
                hits = fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])
                self.assertEqual(len(hits), 1, value)
                self.assertEqual(hits[0]["severity"], "critical")

    def test_a_capitalised_true_clears_the_local_storage_pin(self):
        """The mirror-image error: `!= "true"` read `"True"` as unset, so a pod
        whose owner had explicitly cleared the annotation was still reported."""
        for value in ("true", "True", "TRUE", "1"):
            with self.subTest(value=value):
                pod = self.evict_pod(value)
                pod["spec"]["volumes"] = [{"emptyDir": {}}]
                context = {"pods": [pod], "pdbs": []}
                self.assertEqual(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}]), [])

    def test_an_unparseable_value_is_treated_as_unset(self):
        pod = self.evict_pod("maybe")
        context = {"pods": [pod], "pdbs": []}
        self.assertEqual(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}]), [])

    def test_the_excerpt_quotes_the_raw_annotation(self):
        context = {"pods": [self.evict_pod("False")], "pdbs": []}
        hits = fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])
        self.assertIn("safe-to-evict=False", hits[0]["excerpt"])


class OwnerKeyTest(unittest.TestCase):
    def test_no_owners_is_none(self):
        self.assertIsNone(fw._owner_key([]))

    def test_the_controller_wins_wherever_it_sits_in_the_list(self):
        owners = [
            {"kind": "ThingBinding", "name": "zzz"},
            {"kind": "ReplicaSet", "name": "web-abc", "controller": True},
        ]
        self.assertEqual(fw._owner_key(owners), ("ReplicaSet", "web-abc"))
        self.assertEqual(fw._owner_key(list(reversed(owners))), ("ReplicaSet", "web-abc"))

    def test_with_no_controller_the_answer_is_still_order_independent(self):
        owners = [{"kind": "B", "name": "b"}, {"kind": "A", "name": "a"}]
        self.assertEqual(fw._owner_key(owners), fw._owner_key(list(reversed(owners))))


class JobFinishedAtTest(unittest.TestCase):
    def test_picks_the_terminal_condition_not_the_first_one(self):
        status = {
            "conditions": [
                {"type": "Suspended", "status": "False", "lastTransitionTime": "2026-01-01T00:00:00Z"},
                {"type": "Failed", "status": "True", "lastTransitionTime": "2026-07-01T00:00:00Z"},
            ]
        }
        self.assertEqual(fw._job_finished_at(status), "2026-07-01T00:00:00Z")

    def test_a_condition_that_is_not_true_does_not_count(self):
        status = {"conditions": [{"type": "Failed", "status": "False", "lastTransitionTime": "2026-01-01T00:00:00Z"}]}
        self.assertEqual(fw._job_finished_at(status), "")

    def test_success_criteria_met_counts(self):
        status = {"conditions": [{"type": "SuccessCriteriaMet", "status": "True", "lastTransitionTime": "2026-02-02T00:00:00Z"}]}
        self.assertEqual(fw._job_finished_at(status), "2026-02-02T00:00:00Z")

    def test_no_conditions_is_empty(self):
        self.assertEqual(fw._job_finished_at({}), "")


class TerminalPodsTest(unittest.TestCase):
    def terminal_pod(self, ns="default", name="p", phase="Succeeded", created="2026-01-01T00:00:00Z"):
        return obj("Pod", name, ns=ns, **{"status.phase": phase, "metadata.creationTimestamp": created})

    def test_flags_a_namespace_with_50_or_more(self):
        pods = [self.terminal_pod(name=f"p{i}") for i in range(50)]
        context = {"pods": pods, "jobs": [], "cronjobs": []}
        hits = fw.check_terminal_pods(context, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("50 terminal pods", hits[0]["excerpt"])

    def test_flags_a_single_old_pod(self):
        pods = [self.terminal_pod(created="2026-01-01T00:00:00Z")]
        context = {"pods": pods, "jobs": [], "cronjobs": []}
        self.assertEqual(len(fw.check_terminal_pods(context, now=NOW)), 1)

    def test_does_not_flag_recent_small_backlog(self):
        pods = [self.terminal_pod(created="2026-07-30T00:00:00Z") for _ in range(3)]
        context = {"pods": pods, "jobs": [], "cronjobs": []}
        self.assertEqual(fw.check_terminal_pods(context, now=NOW), [])

    def test_flags_standalone_job_without_ttl(self):
        job = obj("Job", "batch", ns="default", **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z"})
        context = {"pods": [], "jobs": [job], "cronjobs": []}
        hits = fw.check_terminal_pods(context, now=NOW)
        self.assertTrue(any(h["object"] == "Job/batch" for h in hits))

    def test_does_not_flag_job_with_ttl_set(self):
        job = obj("Job", "batch", ns="default", **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z", "spec.ttlSecondsAfterFinished": 3600})
        context = {"pods": [], "jobs": [job], "cronjobs": []}
        self.assertEqual(fw.check_terminal_pods(context, now=NOW), [])

    def test_does_not_flag_cronjob_owned_job(self):
        job = obj(
            "Job", "cron-123", ns="default",
            **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z", "metadata.ownerReferences": [{"kind": "CronJob", "name": "cron"}]},
        )
        context = {"pods": [], "jobs": [job], "cronjobs": []}
        self.assertEqual(fw.check_terminal_pods(context, now=NOW), [])

    def test_flags_cronjob_with_excessive_history_limit(self):
        cj = obj("CronJob", "chatty", ns="default", **{"spec.successfulJobsHistoryLimit": 20})
        context = {"pods": [], "jobs": [], "cronjobs": [cj]}
        hits = fw.check_terminal_pods(context, now=NOW)
        self.assertTrue(any(h["object"] == "CronJob/chatty" for h in hits))


class IdleNamespaceTest(unittest.TestCase):
    def ns(self, name, created="2026-01-01T00:00:00Z"):
        return obj("Namespace", name, **{"metadata.creationTimestamp": created})

    def test_flags_idle_ns_with_loadbalancer(self):
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        hits = fw.check_idle_namespace(context, now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_flags_idle_ns_with_pvc(self):
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "10Gi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertEqual(len(fw.check_idle_namespace(context, now=NOW)), 1)

    def test_does_not_flag_active_namespace(self):
        pod = obj("Pod", "p", ns="demo", **{"status.phase": "Running"})
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        context = {"pods": [pod], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_does_not_flag_namespace_with_nothing_billable(self):
        context = {"pods": [], "pvcs": [], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_does_not_flag_gitops_synced_namespace(self):
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        ns_doc = self.ns("demo")
        ns_doc["metadata"]["annotations"]["configsync.gke.io/sync-name"] = "x"
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [ns_doc]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_a_part_day_over_the_gate_reports_the_gate_not_the_day_after(self):
        # 30d 14h, against a 30-day gate. See the matching case in OrphanPvTest.
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "10Gi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo", created="2026-07-01T10:00:00Z")]}
        self.assertIn("for 30d;", fw.check_idle_namespace(context, now=NOW)[0]["excerpt"])

    def test_capacity_just_under_the_severity_gate_is_not_printed_as_the_gate(self):
        # 102000Mi is 99.6 GiB. Rounding to nearest printed "100 GiB" while the
        # gate read the raw value and graded `minor`, so one finding said the
        # threshold was met and denied it in the same breath.
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "102000Mi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        hit = fw.check_idle_namespace(context, now=NOW)[0]
        self.assertIn("99 GiB of PVCs", hit["excerpt"])
        self.assertEqual(hit["severity"], "minor")

    def test_a_sub_gibibyte_claim_is_not_floored_away_to_zero(self):
        # Flooring must not print "0 GiB of PVCs" about the very PVC that made
        # the namespace billable; only a namespace billable through something
        # other than storage gets a literal 0.
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "500Mi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertIn("<1 GiB of PVCs", fw.check_idle_namespace(context, now=NOW)[0]["excerpt"])

    def test_a_namespace_billable_only_by_a_loadbalancer_holds_zero(self):
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertIn("a LoadBalancer Service, 0 GiB of PVCs", fw.check_idle_namespace(context, now=NOW)[0]["excerpt"])

    def test_a_resourcequota_does_not_make_an_empty_namespace_billable(self):
        # A quota reserves nothing and bills nothing -- it gates admission on
        # the requests of the pods in its own namespace, of which there are
        # none. This namespace is the live `gitops-managed`: no pods, no PVCs,
        # no Services, one quota with `used` all zeroes, costing zero. Flagged,
        # it drew the impact "reserving 10 vCPU / 20 GiB of request headroom
        # ... that no other namespace on this Autopilot cluster can use", which
        # is false in all three of its claims.
        rq = obj("ResourceQuota", "platform-baseline-quota", ns="demo", **{"status.hard": {"requests.cpu": "10"}})
        context = {"pods": [], "pvcs": [], "services": [], "resourcequotas": [rq], "namespaces": [self.ns("demo")]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_a_resourcequota_beside_a_real_billable_object_still_flags(self):
        # Dropping the quota arm must not suppress a namespace that a PVC or a
        # LoadBalancer would have flagged on its own.
        rq = obj("ResourceQuota", "q", ns="demo", **{"status.hard": {"requests.cpu": "10"}})
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "10Gi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [rq], "namespaces": [self.ns("demo")]}
        hits = fw.check_idle_namespace(context, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertNotIn("uota", hits[0]["excerpt"])


class OverrequestTest(unittest.TestCase):
    def deployment_pod(self, ns="default", name="api-1", cpu_req="12", mem_req="48Gi", cpu_lim=None, mem_lim=None, started="2026-01-01T00:00:00Z", owner_kind="ReplicaSet", owner_name="api"):
        resources = {"requests": {"cpu": cpu_req, "memory": mem_req}}
        if cpu_lim or mem_lim:
            resources["limits"] = {"cpu": cpu_lim or cpu_req, "memory": mem_lim or mem_req}
        return obj(
            "Pod", name, ns=ns,
            **{
                "spec.containers": [{"resources": resources}],
                "status.startTime": started,
                "status.phase": "Running",
                "metadata.ownerReferences": [{"kind": owner_kind, "name": owner_name}],
            },
        )

    IDLE = {("default", "api-1"): (0.0, 0.0)}

    def test_flags_gross_overrequest(self):
        pod = self.deployment_pod()
        peaks = {("default", "api-1"): (0.9, 3072.0)}  # 0.9 vCPU / 3 GiB peak vs 12/48 requested
        hits = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_does_not_flag_when_the_peak_clears_the_bar(self):
        # The window used to be three ten-minute samples and this rule used to
        # be "every one of them agrees". A week-long peak is the same rule
        # without the sampling error: a workload that reached 11 vCPU once is
        # not over-requesting at 12, however idle it looked when we last
        # happened to run `kubectl top`.
        pod = self.deployment_pod()
        peaks = {("default", "api-1"): (11.0, 40000.0)}
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False), [])

    def test_does_not_flag_below_the_absolute_floor(self):
        pod = self.deployment_pod(cpu_req="100m", mem_req="256Mi")
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), [])

    def test_does_not_flag_daemonset(self):
        pod = self.deployment_pod(owner_kind="DaemonSet", owner_name="ds")
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), [])

    def test_does_not_flag_job_owned_pod(self):
        pod = self.deployment_pod(owner_kind="Job", owner_name="batch")
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), [])

    def test_does_not_flag_a_pod_with_no_requests_at_all(self):
        pod = self.deployment_pod(cpu_req="0", mem_req="0")
        pod["spec"]["containers"][0]["resources"] = {}
        # Peaks for some *other* pod, so the no-requests skip is what makes
        # this pass rather than the empty-usage guard at the top.
        peaks = {("default", "unrelated"): (0.0, 0.0)}
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False), [])

    def test_no_usage_data_flags_nothing_rather_than_everything(self):
        # `fetch_usage_peaks` returns `{}` for a cluster it could not read.
        # Reading that as "this Deployment used no CPU and no memory" would
        # flag every workload in the fleet as reclaimable waste.
        pod = self.deployment_pod()
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, {}, now=NOW, autopilot=False), [])

    def test_guaranteed_qos_is_marked_for_manual_remediation(self):
        pod = self.deployment_pod(cpu_lim="12", mem_lim="48Gi")
        peaks = {("default", "api-1"): (0.9, 3072.0)}
        hits = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False)
        self.assertTrue(hits[0]["_guaranteed"])

    def test_autopilot_bumps_minor_to_major(self):
        pod = self.deployment_pod(cpu_req="3", mem_req="6Gi")
        peaks = {("default", "api-1"): (0.1, 100.0)}
        hits = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=True)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_the_excerpt_names_the_window_it_rests_on(self):
        pod = self.deployment_pod()
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", hits[0]["excerpt"])

    def test_the_window_shrinks_to_a_controller_younger_than_it(self):
        # Monitoring holds a week of history; a Deployment rolled six hours ago
        # has six hours of it. Reporting "over the trailing 168h" there claims
        # to have watched something that did not exist for 162 of them.
        pod = self.deployment_pod(started="2026-07-31T18:00:00Z")
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False)
        self.assertIn("trailing 6h", hits[0]["excerpt"])

    def test_the_window_follows_the_longest_lived_pod_of_the_controller(self):
        old = self.deployment_pod(name="api-1", started="2026-07-01T00:00:00Z")
        fresh = self.deployment_pod(name="api-2", started="2026-07-31T18:00:00Z")
        peaks = {("default", "api-1"): (0.4, 1536.0), ("default", "api-2"): (0.5, 1536.0)}
        hits = fw.check_overrequest({"pods": [old, fresh]}, peaks, now=NOW, autopilot=False)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", hits[0]["excerpt"])

    def test_an_unknown_start_time_falls_back_to_the_full_window(self):
        pod = self.deployment_pod(started="")
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", hits[0]["excerpt"])

    def test_pending_pod_is_never_flagged(self):
        pod = self.deployment_pod()
        pod["status"]["phase"] = "Pending"
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), [])


class UnattachedDiskTest(unittest.TestCase):
    def disk(self, name="d1", size_gb=200, created="2026-01-01T00:00:00Z", disk_type="pd-standard", users=None):
        return {"name": name, "sizeGb": str(size_gb), "type": disk_type, "creationTimestamp": created, "zone": "us-central1-a", "users": users or []}

    def test_flags_unattached_over_30_days(self):
        self.assertEqual(len(fw.check_unattached_disk([self.disk()], set(), now=NOW)), 1)

    def test_does_not_flag_attached(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(users=["some-vm"])], set(), now=NOW), [])

    def test_does_not_flag_recently_created(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(created="2026-07-30T00:00:00Z")], set(), now=NOW), [])

    def test_a_disk_detached_yesterday_is_churn_however_old_it_is(self):
        """The 30 days are justified as outliving a maintenance cycle, and a
        maintenance cycle detaches disks — it does not create them. Reading the
        age off `creationTimestamp` flagged a year-old boot disk that PD-CSI
        released this morning, which is the churn the threshold excludes."""
        disk = self.disk(created="2025-01-01T00:00:00Z")
        disk["lastDetachTimestamp"] = "2026-08-27T00:00:00Z"
        self.assertEqual(fw.check_unattached_disk([disk], set(), now=NOW), [])

    def test_the_excerpt_dates_the_detach_not_the_creation(self):
        disk = self.disk(created="2025-01-01T00:00:00Z")
        disk["lastDetachTimestamp"] = "2026-05-30T00:00:00Z"
        hits = fw.check_unattached_disk([disk], set(), now=NOW)
        self.assertIn("unattached since 2026-05-30T00:00:00Z", hits[0]["excerpt"])
        self.assertNotIn("2025-01-01", hits[0]["excerpt"])

    def test_a_disk_never_attached_says_so_rather_than_implying_a_detach(self):
        """No `lastDetachTimestamp` means GCE never attached it, so creation is
        genuinely when it went idle — but "unattached since" would assert a
        detach that never happened."""
        hits = fw.check_unattached_disk([self.disk()], set(), now=NOW)
        self.assertIn("never attached, created 2026-01-01T00:00:00Z", hits[0]["excerpt"])

    def test_does_not_flag_a_disk_matching_a_live_pv_handle(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(name="pv-handle-1")], {"pv-handle-1"}, now=NOW), [])

    def test_large_ssd_is_major(self):
        hits = fw.check_unattached_disk([self.disk(size_gb=600, disk_type="pd-ssd")], set(), now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_a_zonal_disk_carries_its_zone_scope_flag(self):
        """The excerpt used to print `zone=` with gcloud's raw selfLink in it.

        A URL is not something you can paste after `--zone`, so the describe and
        delete in §3.2's chain went out unscoped and resolved against gcloud's
        configured zone.
        """
        url = "https://www.googleapis.com/compute/v1/projects/p/zones/us-east4-a"
        hits = fw.check_unattached_disk([{**self.disk(), "zone": url}], set(), now=NOW)
        self.assertIn("--zone=us-east4-a", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_a_regional_disk_carries_a_region_flag_not_a_zone_one(self):
        """A regional PD has `region` and no `zone`; `--zone` would not find it."""
        disk = {k: v for k, v in self.disk().items() if k != "zone"}
        disk["region"] = "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"
        hits = fw.check_unattached_disk([disk], set(), now=NOW)
        self.assertIn("--region=us-east4", hits[0]["excerpt"])
        self.assertNotIn("--zone", hits[0]["excerpt"])


class IdleAddressTest(unittest.TestCase):
    def address(self, name="addr1", addr_type="EXTERNAL", status="RESERVED", purpose="", created="2026-01-01T00:00:00Z", region="us-central1"):
        return {"name": name, "address": "1.2.3.4", "addressType": addr_type, "status": status, "purpose": purpose, "creationTimestamp": created, "region": region}

    def test_flags_reserved_external_over_14_days(self):
        self.assertEqual(len(fw.check_idle_address([self.address()], set(), project="p", now=NOW)), 1)

    def test_does_not_flag_internal(self):
        self.assertEqual(fw.check_idle_address([self.address(addr_type="INTERNAL")], set(), project="p", now=NOW), [])

    def test_does_not_flag_gce_endpoint_purpose(self):
        self.assertEqual(fw.check_idle_address([self.address(purpose="GCE_ENDPOINT")], set(), project="p", now=NOW), [])

    def test_does_not_flag_referenced_by_annotation(self):
        self.assertEqual(fw.check_idle_address([self.address(name="my-ip")], {"my-ip"}, project="p", now=NOW), [])

    def test_rolls_up_ten_or_more_into_one_major_finding(self):
        addrs = [self.address(name=f"a{i}") for i in range(10)]
        hits = fw.check_idle_address(addrs, set(), project="p", now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_a_regional_address_carries_the_region_scope_flag(self):
        hits = fw.check_idle_address([self.address()], set(), project="p", now=NOW)
        self.assertIn("--region=us-central1", hits[0]["excerpt"])

    def test_a_global_address_carries_the_global_scope_flag(self):
        """gcloud omits `region` entirely for a global address.

        Left to infer it, an agent writes the remediation with no scope flag at
        all, gcloud resolves it against its configured default region, and the
        command answers `was not found` on an address that is really there.
        """
        hits = fw.check_idle_address([self.address(region=None)], set(), project="p", now=NOW)
        self.assertIn("--global", hits[0]["excerpt"])
        self.assertNotIn("--region", hits[0]["excerpt"])

    def test_a_region_selflink_is_reduced_to_its_name(self):
        """The list API returns `region` as a full URL, not `us-central1`."""
        url = "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"
        hits = fw.check_idle_address([self.address(region=url)], set(), project="p", now=NOW)
        self.assertIn("--region=us-east4", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_the_rollup_names_a_location_not_a_url(self):
        url = "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"
        addrs = [self.address(name=f"a{i}", region=url) for i in range(10)]
        hits = fw.check_idle_address(addrs, set(), project="p", now=NOW)
        self.assertIn("us-east4", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_the_rollup_is_named_after_the_project_not_one_members_region(self):
        """§3.5's roll-up is per project and §5 forbids naming it after a member.

        The old `Address/rollup-<region of the first address>` was both: it
        claimed a region for addresses that were not in it, and it moved --
        releasing that one address renamed the finding, so the ledger announced
        the same leak as resolved and then as new.
        """
        addrs = [self.address(name=f"a{i}", region="us-east4") for i in range(6)]
        addrs += [self.address(name=f"b{i}", region="europe-west1") for i in range(6)]
        hits = fw.check_idle_address(addrs, set(), project="adamparco-kage", now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Project/adamparco-kage")
        # Both regions named, with their own counts -- not one region's name
        # over a total that spans two.
        self.assertIn("us-east4 (6)", hits[0]["excerpt"])
        self.assertIn("europe-west1 (6)", hits[0]["excerpt"])
        self.assertTrue(hits[0]["excerpt"].startswith("12 external addresses"))

    def test_the_rollup_identity_survives_releasing_one_member(self):
        many = [self.address(name=f"a{i}", region="us-east4") for i in range(11)]
        first = fw.check_idle_address(many, set(), project="p", now=NOW)
        # Release the one that used to decide the name, in the region that used
        # to decide the name.
        rest = [a for a in many if a["name"] != "a0"]
        rest[0]["region"] = "europe-west1"
        second = fw.check_idle_address(rest, set(), project="p", now=NOW)
        self.assertEqual(first[0]["object"], second[0]["object"])

    def test_the_rollup_excerpt_names_its_members(self):
        addrs = [self.address(name=f"a{i:02d}") for i in range(10)]
        hits = fw.check_idle_address(addrs, set(), project="p", now=NOW)
        self.assertIn("a00", hits[0]["excerpt"])
        self.assertIn("a09", hits[0]["excerpt"])

    def test_a_long_rollup_says_how_many_it_did_not_name(self):
        addrs = [self.address(name=f"a{i:03d}") for i in range(40)]
        hits = fw.check_idle_address(addrs, set(), project="p", now=NOW)
        self.assertIn(f"and {40 - fw.ROLLUP_EXCERPT_MEMBERS} more", hits[0]["excerpt"])


class OrphanLbTest(unittest.TestCase):
    def test_flags_forwarding_rule_targeting_deleted_service(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z"}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertEqual(len(hits), 1)

    def test_does_not_flag_when_service_still_exists(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], {"staging/checkout"}, now=NOW), [])

    def test_does_not_flag_multicluster_ingress(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout multiclusteringress", "creationTimestamp": "2026-01-01T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], set(), now=NOW), [])

    def test_flags_empty_target_pool(self):
        hits = fw.check_orphan_lb([], [{"name": "tp1", "instances": []}], [], set(), now=NOW)
        self.assertEqual(hits[0]["object"], "TargetPool/tp1")

    def test_flags_empty_backend_service(self):
        hits = fw.check_orphan_lb([], [], [{"name": "bs1", "backends": []}], set(), now=NOW)
        self.assertEqual(hits[0]["object"], "BackendService/bs1")

    #: What the GKE service controller actually writes into a forwarding rule's
    #: description. Every test above uses the bare `key: value` form, which is
    #: what the SOP's prose example shows and what no live rule carries -- so
    #: the whole leg passed its tests while matching nothing in the fleet.
    GKE_DESC = '{"kubernetes.io/service-name":"staging/checkout","kubernetes.io/api-version":"v1"}'

    def test_the_json_description_gke_really_writes_is_matched(self):
        rule = {"name": "fr1", "description": self.GKE_DESC, "creationTimestamp": "2026-01-01T00:00:00Z"}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("staging/checkout", hits[0]["excerpt"])

    def test_a_live_service_still_suppresses_the_json_form(self):
        rule = {"name": "fr1", "description": self.GKE_DESC, "creationTimestamp": "2026-01-01T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], {"staging/checkout"}, now=NOW), [])

    def test_a_rule_with_no_kubernetes_description_is_still_skipped(self):
        rule = {"name": "tf-managed", "description": "managed by terraform", "creationTimestamp": "2026-01-01T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], set(), now=NOW), [])

    def test_does_not_flag_recent_forwarding_rule(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-07-30T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], set(), now=NOW), [])

    REGION = "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"

    def test_a_regional_forwarding_rule_carries_its_scope_flag(self):
        """The remediation chain deletes the rule, so it needs to find it.

        Every resource in §3.6's chain is regional-or-global, and an unscoped
        `gcloud compute` verb resolves against whatever region gcloud is
        configured for -- answering `was not found` for a rule that is really
        there, which reads as already-remediated.
        """
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z", "region": self.REGION}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertIn("--region=us-east4", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_a_global_forwarding_rule_carries_the_global_flag(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z"}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertIn("--global", hits[0]["excerpt"])

    def test_a_target_pool_carries_its_region(self):
        hits = fw.check_orphan_lb([], [{"name": "tp1", "instances": [], "region": self.REGION}], [], set(), now=NOW)
        self.assertIn("--region=us-east4", hits[0]["excerpt"])

    def test_a_backend_service_carries_its_scope_flag(self):
        """A backend service is regional or global, and the listing tells them
        apart only by whether `region` is there at all."""
        regional = fw.check_orphan_lb([], [], [{"name": "bs1", "backends": [], "region": self.REGION}], set(), now=NOW)
        self.assertIn("--region=us-east4", regional[0]["excerpt"])
        glob = fw.check_orphan_lb([], [], [{"name": "bs2", "backends": []}], set(), now=NOW)
        self.assertIn("--global", glob[0]["excerpt"])


class AgeInExcerptTest(unittest.TestCase):
    """Every check that gates on age says the age it gated on.

    A check computes an age, decides with it, and then quoted the raw ISO
    timestamp. The model reading the manifest still has to say how long the
    thing has been idle -- that is the finding -- so it did the date arithmetic
    itself and got it wrong: `argocd-webhook-ip`, reserved 2026-08-02 and read
    2026-09-01, was published as "unused for 28 days". `adopt_collector_evidence`
    replaces the model's evidence with the collector's and leaves the title
    alone, so the wrong number outlives the correct evidence beside it.

    Each timestamp below is 2026-01-01 against a NOW of 2026-08-01: 212 days.
    """

    AGO = "(212d ago)"

    def test_orphan_pv_dates_the_phase_transition(self):
        pv = obj("PersistentVolume", "pv-1", **{"spec.persistentVolumeReclaimPolicy": "Retain", "status.phase": "Released", "spec.capacity": {"storage": "10Gi"}, "status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"})
        hits = fw.check_orphan_pv({"pvs": [pv], "pvcs": [], "statefulsets": []}, now=NOW)
        self.assertIn(self.AGO, hits[0]["excerpt"])

    def test_terminal_pods_dates_the_oldest_pod(self):
        pods = [obj("Pod", "p", ns="default", **{"status.phase": "Succeeded", "metadata.creationTimestamp": "2026-01-01T00:00:00Z"})]
        hits = fw.check_terminal_pods({"pods": pods, "jobs": [], "cronjobs": []}, now=NOW)
        self.assertIn(self.AGO, hits[0]["excerpt"])

    def test_a_ttl_less_job_dates_its_completion(self):
        job = obj("Job", "batch", ns="default", **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z"})
        hits = fw.check_terminal_pods({"pods": [], "jobs": [job], "cronjobs": []}, now=NOW)
        self.assertIn(self.AGO, hits[0]["excerpt"])

    def test_an_unattached_disk_dates_the_detach(self):
        disk = {"name": "d1", "sizeGb": "200", "type": "pd-standard", "creationTimestamp": "2020-01-01T00:00:00Z", "lastDetachTimestamp": "2026-01-01T00:00:00Z", "zone": "us-central1-a", "users": []}
        hits = fw.check_unattached_disk([disk], set(), now=NOW)
        self.assertIn(f"unattached since 2026-01-01T00:00:00Z {self.AGO}", hits[0]["excerpt"])

    def test_an_idle_address_dates_its_reservation(self):
        addr = {"name": "a1", "address": "1.2.3.4", "addressType": "EXTERNAL", "status": "RESERVED", "purpose": "", "creationTimestamp": "2026-01-01T00:00:00Z", "region": "us-central1"}
        hits = fw.check_idle_address([addr], set(), project="p", now=NOW)
        self.assertIn(f"since 2026-01-01T00:00:00Z {self.AGO}", hits[0]["excerpt"])

    def test_an_orphan_forwarding_rule_dates_its_creation(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z"}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertIn(self.AGO, hits[0]["excerpt"])

    def test_the_age_belongs_to_the_address_it_is_printed_beside(self):
        """`idle` was a list of addresses and the age a loop variable left over
        from the filter pass, so reading it in the emit loop would have stamped
        every address with the last one's age. Two addresses of different ages,
        emitted separately, is the only shape that catches it."""
        old = {"name": "old", "address": "1.1.1.1", "addressType": "EXTERNAL", "status": "RESERVED", "creationTimestamp": "2026-01-01T00:00:00Z", "region": "us-central1"}
        new = {"name": "new", "address": "2.2.2.2", "addressType": "EXTERNAL", "status": "RESERVED", "creationTimestamp": "2026-07-01T00:00:00Z", "region": "us-central1"}
        by_name = {h["object"]: h["excerpt"] for h in fw.check_idle_address([old, new], set(), project="p", now=NOW)}
        self.assertIn("(212d ago)", by_name["Address/old"])
        self.assertIn("(31d ago)", by_name["Address/new"])

    def test_an_unreadable_timestamp_prints_no_age_rather_than_zero(self):
        """`_age_days` returns None for a timestamp it cannot parse, and "(0d
        ago)" would assert the thing went idle today."""
        self.assertEqual(fw._ago(None), "")

    def test_a_part_day_rounds_down_so_a_title_cannot_claim_the_threshold(self):
        """The second half of the same bug, one release later.

        Dating the excerpt stopped the model doing its own arithmetic, but
        `{age:.0f}` rounds to nearest, so the same `argocd-webhook-ip` -- 29.69
        days old when the 2026-09-01 cost run read it -- was published as "(30d
        ago)" and the model titled the finding "unused for 30+ days". A claim
        about a threshold, derived from a rounded number, and false. Anything
        under a whole day has to round down, or the collector hands the model
        the licence to state the threshold.
        """
        self.assertEqual(fw._ago(29.69), " (29d ago)")
        self.assertEqual(fw._ago(29.999), " (29d ago)")
        self.assertEqual(fw._ago(30.0), " (30d ago)")

    def test_an_age_that_predates_the_read_prints_zero_not_a_negative(self):
        """A clock that moved backwards between creation and read gives a
        negative age. "(-1d ago)" would say the object is from the future."""
        self.assertEqual(fw._ago(-0.5), " (0d ago)")


class CollectProjectComputeTest(unittest.TestCase):
    """The five project-scope reads, and what happens when one of them fails.

    The `run` here emulates gcloud's *argument parser*, not just its API, which
    is the whole point: the disks read spent its entire life failing on a filter
    value gcloud would not accept, and a fake that answers every argv with `[]`
    cannot tell the difference between a command gcloud runs and one it rejects.
    """

    FACTS = {"pv_handles": set(), "referenced_addresses": set(), "service_names": set()}

    def run_with(self, fail: dict | None = None):
        fail = fail or {}

        def run(argv, **kwargs):
            # gcloud reads `--filter` followed by a token starting with `-` as
            # two flags and rejects the command for the argument it thinks is
            # missing. `--filter=-users:*` is one token and parses fine.
            for i, tok in enumerate(argv):
                if tok == "--filter" and (i + 1 >= len(argv) or argv[i + 1].startswith("-")):
                    return run_of(2, "", f"ERROR: (gcloud.compute.{argv[2]}.list) argument --filter: expected one argument")
            resource = argv[2] if len(argv) > 2 else ""
            if resource in fail:
                return run_of(1, "", fail[resource])
            return run_of(0, "[]")

        return run

    def test_the_disks_read_survives_gcloud_argument_parsing(self):
        target = fw.collect_project_compute("acme", True, self.FACTS, run=self.run_with(), now=NOW)
        self.assertEqual(target["outcome"], "collected")

    def test_the_collector_does_not_pass_a_filter_gcloud_would_reject(self):
        """Guards the fake as much as the collector. If the parser emulation
        above stopped rejecting the old spelling it would pass everything, and
        the test above would go green against a disks read that never ran."""
        broken = ["gcloud", "compute", "disks", "list", "--project", "acme", "--filter", "-users:*", "--format", "json"]
        self.assertEqual(self.run_with()(broken).rc, 2)

        seen = []

        def recording(argv, **kwargs):
            seen.append(argv)
            return self.run_with()(argv, **kwargs)

        fw.collect_project_compute("acme", True, self.FACTS, run=recording, now=NOW)
        disks = next(argv for argv in seen if argv[2] == "disks")
        self.assertIn("--filter=-users:*", disks)
        self.assertNotIn("--filter", disks)

    def test_a_failed_read_names_the_command_and_what_it_said(self):
        target = fw.collect_project_compute(
            "acme", True, self.FACTS, run=self.run_with(fail={"addresses": "PERMISSION_DENIED: compute.addresses.list"}), now=NOW
        )
        self.assertEqual(target["outcome"], "gate-failed")
        self.assertIn("1 of 5", target["error"])
        self.assertIn("gcloud compute addresses list", target["error"])
        self.assertIn("PERMISSION_DENIED", target["error"])

    def test_the_error_names_every_read_that_failed_not_just_the_first(self):
        target = fw.collect_project_compute(
            "acme", True, self.FACTS, run=self.run_with(fail={"addresses": "denied-a", "target-pools": "denied-t"}), now=NOW
        )
        self.assertIn("2 of 5", target["error"])
        self.assertIn("denied-a", target["error"])
        self.assertIn("denied-t", target["error"])

    def test_a_read_that_returns_no_stderr_still_names_its_command(self):
        target = fw.collect_project_compute("acme", True, self.FACTS, run=self.run_with(fail={"disks": ""}), now=NOW)
        self.assertIn("gcloud compute disks list", target["error"])
        self.assertIn("no stderr", target["error"])

    def test_withholding_orphan_lb_says_why_rather_than_just_dropping_it(self):
        """§6's roster half names the missing check on its own, so the gap reads
        "orphan-lb did not run" whatever this entry says. Without a reason that
        sends a reader hunting a broken gcloud read: all three compute reads
        succeeded here and the check was withheld deliberately."""
        target = fw.collect_project_compute("acme", False, self.FACTS, run=self.run_with(), now=NOW)
        self.assertEqual(target["outcome"], "collected")
        self.assertNotIn("orphan-lb", [c["check"] for c in target["commands"]])
        self.assertIn("orphan-lb", target["limitations"])

    def test_a_project_whose_clusters_all_read_carries_no_limitation(self):
        """The other half of the pair. A limitation set unconditionally would
        make every healthy run partial, which costs more than the silence did."""
        target = fw.collect_project_compute("acme", True, self.FACTS, run=self.run_with(), now=NOW)
        self.assertIn("orphan-lb", [c["check"] for c in target["commands"]])
        self.assertNotIn("limitations", target)


class CollectClusterTest(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def run_with(self, dump_items=(), pools=(), session=None, cluster=None):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*dump_items)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, json.dumps(list(pools)))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                return fw.collect_cluster(cluster or self.CLUSTER, run=run, session=session or usage_session(), now=NOW)

    def test_every_outcome_publishes_the_mode(self):
        # The mode is a cluster property `enumerate_clusters` already resolved,
        # so it rides on the error shapes too: a cluster does not stop being
        # Autopilot because this run failed to read inside it.
        cluster = {**self.CLUSTER, "autopilot": True}

        def denied(argv, **kwargs):
            return run_of(1, "", "denied") if "get-credentials" in argv else run_of(0, "")

        def gated(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(1, "", "forbidden")
            return run_of(0, "")

        entries = [self.run_with(cluster=cluster)[0]]
        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                for runner in (denied, gated):
                    entries.append(
                        fw.collect_cluster(cluster, run=runner, session=usage_session(), now=NOW)[0]
                    )
        self.assertEqual(
            [e["outcome"] for e in entries], ["collected", "unreachable", "gate-failed"]
        )
        for entry in entries:
            with self.subTest(outcome=entry["outcome"]):
                self.assertIs(entry["autopilot"], True)

    def test_a_cluster_that_never_ran_still_publishes_the_mode(self):
        entry = fw.not_running_entry(
            {"name": "dr-west", "location": "us-west1", "status": "DEGRADED",
             "autopilot": {"enabled": True}},
            "acme",
        )
        self.assertEqual(entry["outcome"], "unreachable")
        self.assertIs(entry["autopilot"], True)
        self.assertIs(
            fw.not_running_entry({"name": "c", "status": "STOPPING"}, "acme")["autopilot"], False
        )

    def test_clean_cluster_collects_with_no_candidates(self):
        entry, facts = self.run_with()
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry["candidates"], [])
        self.assertIn("overrequest", {c["check"] for c in entry["commands"]})

    def test_get_credentials_failure_is_unreachable(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(1, "", "denied")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, facts = fw.collect_cluster(self.CLUSTER, run=run, session=usage_session(), now=NOW)
        self.assertEqual(entry["outcome"], "unreachable")
        self.assertEqual(facts, {"pv_handles": set(), "service_names": set(), "referenced_addresses": set()})

    def test_object_dump_failure_is_gate_failed(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(1, "", "forbidden")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(self.CLUSTER, run=run, session=usage_session(), now=NOW)
        self.assertEqual(entry["outcome"], "gate-failed")

    def _metrics_down(self, *dump_items, session=None):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*dump_items)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(
                    self.CLUSTER, run=run, session=session or FakeSession(**NO_USAGE), now=NOW
                )
        return entry

    def test_metrics_unavailable_still_collects_object_checks(self):
        # The node matters: without one this is the empty-cluster case below,
        # where the check is not applicable rather than degraded.
        entry = self._metrics_down(obj("Node", "node-1"))
        self.assertEqual(entry["outcome"], "collected")
        self.assertNotIn("overrequest", {c["check"] for c in entry["commands"]})
        # Dropping the check out of `commands` is only half the job: §6 raises
        # it as a gap either way, and without this the ledger named a check
        # nobody could explain.
        self.assertIn("overrequest could not be measured", entry["limitations"])
        self.assertIn("Cloud Monitoring", entry["limitations"])

    def test_a_denied_usage_read_says_so_rather_than_saying_no_data(self):
        # An IAM gap and a cluster that ships no metrics both stop the check,
        # but only one of them is something an operator can fix, so the
        # limitation has to carry which it was.
        entry = self._metrics_down(
            obj("Node", "node-1"),
            session=FakeSession(status=403, text="caller lacks monitoring.timeSeries.list"),
        )
        self.assertIn("rc=403", entry["limitations"])
        self.assertIn("monitoring.timeSeries.list", entry["limitations"])

    def test_a_cluster_with_no_nodes_cannot_be_over_requesting(self):
        # An empty cluster is not a degraded one. The usage read comes back
        # empty there because nothing ran to report any, and reading that as
        # lost coverage published `partial: true` over two freshly created
        # Autopilot peers on 2026-08-29 -- a gap naming a check that had no
        # object to run against.
        entry = self._metrics_down()
        self.assertEqual(entry["outcome"], "collected")
        self.assertNotIn("limitations", entry)
        self.assertEqual(
            {na["check"] for na in entry["checks_not_applicable"]}, {"overrequest"}
        )
        self.assertIn("no nodes", entry["checks_not_applicable"][0]["reason"])

    def _unreadable_pools(self, cluster=None):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(1, "", "PERMISSION_DENIED: container.nodePools.list")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                return fw.collect_cluster(
                    cluster or self.CLUSTER, run=run, session=usage_session(), now=NOW
                )

    def test_an_unreadable_node_pool_list_is_not_an_absence_of_idle_pools(self):
        """The purest silent-clean shape this collector had.

        `node-pools list` was run bare, so a denied or throttled read parsed to
        `[]`, and a cluster with no node pools has no idle ones. Both 3.7 and
        3.8 recorded their command and reported nothing found. The evidence
        line carried `rc=1` and nothing downstream reads it.
        """
        entry, _ = self._unreadable_pools()
        commands = {c["check"] for c in entry["commands"]}
        self.assertNotIn("idle-nodepool", commands)
        self.assertNotIn("scaledown-blocked", commands)
        self.assertEqual(entry["outcome"], "collected")

    def test_the_unreadable_pool_list_says_why(self):
        entry, _ = self._unreadable_pools()
        self.assertIn("idle-nodepool and scaledown-blocked", entry["limitations"])
        self.assertIn("rc=1", entry["limitations"])
        self.assertIn("PERMISSION_DENIED", entry["limitations"])

    def test_an_unreadable_pool_list_leaves_the_object_checks_alone(self):
        """A degradation, not a gate failure: the object dump still backs 3.1–3.4."""
        entry, _ = self._unreadable_pools()
        commands = {c["check"] for c in entry["commands"]}
        for slug in ("orphan-pv", "unconsumed-pvc", "terminal-pods", "idle-namespace", "overrequest"):
            self.assertIn(slug, commands)

    def test_autopilot_with_unreadable_pools_claims_no_pool_limitation(self):
        """Autopilot owns its pools, so 3.7/3.8 are inapplicable rather than
        unmeasured — naming them in `limitations` would raise a gap for a check
        the cluster does not owe."""
        entry, _ = self._unreadable_pools({**self.CLUSTER, "autopilot": True})
        self.assertNotIn("limitations", entry)

    def test_a_readable_empty_pool_list_still_records_the_checks(self):
        """Zero pools is a measurement. It must not look like the failure above."""
        entry, _ = self.run_with(pools=[])
        commands = {c["check"] for c in entry["commands"]}
        self.assertIn("idle-nodepool", commands)
        self.assertIn("scaledown-blocked", commands)
        self.assertNotIn("limitations", entry)

    def test_autopilot_skips_idle_nodepool_and_scaledown_blocked(self):
        cluster = {**self.CLUSTER, "autopilot": True}

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(cluster, run=run, session=usage_session(), now=NOW)
        commands = {c["check"] for c in entry["commands"]}
        self.assertNotIn("idle-nodepool", commands)
        self.assertNotIn("scaledown-blocked", commands)

    def test_fleet_facts_carry_pv_handles_and_service_names(self):
        pv = obj("PersistentVolume", "pv1", **{"spec.csi": {"volumeHandle": "projects/p/disks/d1"}})
        svc = obj("Service", "web", ns="default")
        entry, facts = self.run_with(dump_items=[pv, svc])
        self.assertIn("d1", facts["pv_handles"])
        self.assertIn("default/web", facts["service_names"])


class AutopilotNotApplicableTest(unittest.TestCase):
    """The two node-pool checks Autopilot cannot owe, declared by the collector.

    Leaving this to the model cost three false coverage gaps a week: it declared
    `idle-nodepool` not-applicable and forgot `scaledown-blocked`, and a check
    that is neither run nor dispositioned reads as one nobody performed.
    """

    def collect(self, autopilot: bool):
        cluster = {"name": "ap-1", "project": "acme", "location": "us-central1", "autopilot": autopilot}

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(cluster, run=run, session=usage_session(), now=NOW)
        return entry

    def test_autopilot_declares_both_node_pool_checks_not_applicable(self):
        entry = self.collect(autopilot=True)
        self.assertEqual(
            {na["check"] for na in entry["checks_not_applicable"]},
            {"idle-nodepool", "scaledown-blocked"},
        )

    def test_neither_check_is_also_reported_as_having_run(self):
        """A check cannot be both dispositioned and performed -- that is the
        double-count `_limitation_restates_na` exists to catch downstream."""
        entry = self.collect(autopilot=True)
        ran = {c["check"] for c in entry["commands"]}
        self.assertNotIn("idle-nodepool", ran)
        self.assertNotIn("scaledown-blocked", ran)

    def test_every_not_applicable_entry_carries_a_reason(self):
        for na in self.collect(autopilot=True)["checks_not_applicable"]:
            self.assertTrue(na.get("reason", "").strip(), na)

    def test_a_standard_cluster_declares_nothing_not_applicable(self):
        """The disposition is Autopilot's alone. A Standard cluster owes both
        checks, so declaring them here would hide a real gap."""
        entry = self.collect(autopilot=False)
        self.assertNotIn("checks_not_applicable", entry)
        self.assertIn("idle-nodepool", {c["check"] for c in entry["commands"]})


class FleetConcurrencyTest(unittest.TestCase):
    def test_clusters_are_collected_in_parallel_up_to_the_pool_size(self):
        """Per-cluster work runs concurrently, not one cluster after another.

        This used to rendezvous on the injected `sleep` and assert the pool
        grew to the whole fleet, because each cluster held a ten-minute
        sampling window and serializing those would have taken hours. Nothing
        sleeps now, so the pool is back to the stream's usual 8 and the
        invariant worth holding is the plain one: a fleet larger than the pool
        still saturates it.

        The gate counts callers inside the Monitoring read and releases them
        once `max_workers` are in flight at once, so it cannot deadlock on an
        uneven split of clusters across workers the way a `threading.Barrier`
        sized to a wave would.
        """
        cluster_count, workers = 12, 4
        clusters_json = json.dumps(
            [
                {"name": f"c{i}", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                for i in range(cluster_count)
            ]
        )
        lock, saturated = threading.Lock(), threading.Event()
        state = {"live": 0, "peak": 0}

        class GatedSession:
            def get(self, url, params=None, timeout=None):
                with lock:
                    state["live"] += 1
                    state["peak"] = max(state["peak"], state["live"])
                    if state["live"] >= workers:
                        saturated.set()
                saturated.wait(timeout=10)
                with lock:
                    state["live"] -= 1
                is_cpu = "cpu/core_usage_time" in params["filter"]
                return FakeResponse(200, {"timeSeries": [series_of("d", "p", 1.0 if is_cpu else MIB)]})

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=GatedSession(), max_workers=workers, now=NOW)

        self.assertTrue(saturated.is_set(), "never reached the pool size; collection was serialized")
        self.assertGreaterEqual(state["peak"], workers)
        self.assertEqual(len({c["name"] for c in manifest["clusters"]} & {f"c{i}" for i in range(cluster_count)}), cluster_count)


class CrashIsolationTest(unittest.TestCase):
    def test_one_cluster_crashing_costs_that_cluster_and_no_other(self):
        """`future.result()` re-raises, and the SOP redirects this collector's
        stdout into the manifest — so an unmodelled exception on one cluster
        used to leave a zero-byte file and lose the whole fleet."""
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "boom", "location": "us-central1", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                if any("boom" in str(v) for v in kwargs.get("env", {}).values()):
                    raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        outcomes = {c["name"]: c["outcome"] for c in manifest["clusters"] if c["name"] in ("c1", "boom")}
        self.assertEqual(outcomes, {"c1": "collected", "boom": "gate-failed"})
        boom = next(c for c in manifest["clusters"] if c["name"] == "boom")
        self.assertIn("TypeError", boom["error"])


class GetTargetProjectsTest(unittest.TestCase):
    def test_project_override_skips_discovery(self):
        def run(argv, **kwargs):
            raise AssertionError(f"unexpected discovery call: {argv}")

        self.assertEqual(fw.get_target_projects("acme-only", run=run), ["acme-only"])

    def test_discovers_every_project_with_a_cluster(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nother\nempty\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                project = argv[argv.index("--project") + 1]
                return run_of(0, json.dumps([{"name": "c1"}]) if project == "other" else "[]")
            raise AssertionError(argv)

        self.assertEqual(fw.get_target_projects(None, run=run), ["acme", "other"])

    def test_project_list_failure_falls_back_to_the_base_project(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(1, "", "permission denied")
            raise AssertionError(argv)

        self.assertEqual(fw.get_target_projects(None, run=run), ["acme"])


class MultiProjectCollectFleetTest(unittest.TestCase):
    def test_discovers_and_audits_every_project_with_a_cluster(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                project = argv[argv.index("--project") + 1]
                name = "c1" if project == "acme" else "c2"
                cluster = {"name": name, "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                return run_of(0, json.dumps([cluster]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)

        names = {c["name"] for c in manifest["clusters"]}
        self.assertEqual(names, {"c1", "c2", "project/acme", "project/beta"})

    def test_cross_project_facts_do_not_leak(self):
        """A PV handle live in project acme's cluster must not suppress a
        genuinely unattached disk of the same name in project beta -- the
        cross-cluster fact union is scoped per project, not fleet-wide.
        Beta gets its own (PV-less) cluster so project discovery includes
        it at all; a project with zero clusters is out of scope entirely,
        matching `patch_readiness.py`'s sibling discovery rule."""

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                project = argv[argv.index("--project") + 1]
                name = "c1" if project == "acme" else "c2"
                cluster = {"name": name, "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                return run_of(0, json.dumps([cluster]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                kc = str(kwargs.get("env", {}).get("KUBECONFIG", ""))
                if "_acme_" in kc:
                    pv = obj("PersistentVolume", "pv1", **{"spec.csi": {"volumeHandle": "projects/acme/disks/shared-disk-id"}})
                    return run_of(0, json.dumps(dump_of(pv)))
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "compute", "disks"]:
                project = argv[argv.index("--project") + 1]
                if project == "beta":
                    disk = {"name": "shared-disk-id", "creationTimestamp": "2020-01-01T00:00:00Z", "sizeGb": "10", "type": "pd-standard", "zone": "z"}
                    return run_of(0, json.dumps([disk]))
                return run_of(0, "[]")
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)

        beta_entry = next(c for c in manifest["clusters"] if c["name"] == "project/beta")
        self.assertIn("unattached-disk", {c["check"] for c in beta_entry["candidates"]})

    def test_a_project_whose_clusters_cannot_be_listed_is_recorded_not_skipped(self):
        # `project/beta`'s compute entry still arrives as `collected`, so
        # without a second entry for the enumeration itself the document sees a
        # project with two of three checks and no clusters -- exactly what a
        # genuinely cluster-free project looks like.
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                project = argv[argv.index("--project") + 1]
                if project == "beta":
                    return run_of(1, "", "PERMISSION_DENIED: container.clusters.list")
                cluster = {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                return run_of(0, json.dumps([cluster]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)

        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertIn("project/beta/clusters", by_name)
        self.assertEqual(by_name["project/beta/clusters"]["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", by_name["project/beta/clusters"]["error"])

    def test_a_cluster_that_is_not_running_is_recorded_as_an_unreachable_target(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(
                    0,
                    json.dumps(
                        [
                            {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}},
                            {"name": "sick", "location": "us-east4", "status": "DEGRADED"},
                        ]
                    ),
                )
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["sick"]["outcome"], "unreachable")
        self.assertIn("DEGRADED", by_name["sick"]["error"])
        self.assertEqual(by_name["c1"]["outcome"], "collected")

    def fleet_with(self, clusters, orphan_rule=True):
        rule = {
            "name": "fr1",
            "description": '{"kubernetes.io/service-name":"gone/svc"}',
            "creationTimestamp": "2026-01-01T00:00:00Z",
        }

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, json.dumps(clusters))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:4] == ["gcloud", "compute", "forwarding-rules", "list"]:
                return run_of(0, json.dumps([rule] if orphan_rule else []))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        return {c["name"]: c for c in manifest["clusters"]}

    RUNNING = {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
    DEGRADED = {"name": "sick", "location": "us-east4", "status": "DEGRADED"}

    def test_a_readable_fleet_evaluates_orphan_lb(self):
        by_name = self.fleet_with([self.RUNNING])
        project = by_name["project/acme"]
        self.assertIn("orphan-lb", {c["check"] for c in project["commands"]})
        self.assertIn("orphan-lb", {c["check"] for c in project["candidates"]})
        self.assertNotIn("limitations", project)

    def test_a_degraded_cluster_closes_the_orphan_lb_gate_for_its_project(self):
        """§3.6 runs only if *every* cluster in the project was enumerated, and
        a DEGRADED cluster goes straight into `scope.skipped` per §2. The gate
        was read off the RUNNING clusters alone, so a project with a cluster in
        `scope.skipped` still published `orphan-lb` findings against a Service
        list it had not finished collecting -- the false positive §3.6 calls the
        highest-risk cross-check in the audit."""
        by_name = self.fleet_with([self.RUNNING, self.DEGRADED])
        project = by_name["project/acme"]
        self.assertNotIn("orphan-lb", {c["check"] for c in project["commands"]})
        self.assertNotIn("orphan-lb", {c["check"] for c in project["candidates"]})
        self.assertIn("orphan-lb was not evaluated", project["limitations"])
        # The other two project checks still run: only 3.6 needs the union.
        self.assertEqual({c["check"] for c in project["commands"]}, {"unattached-disk", "idle-address"})

    def test_an_unlistable_project_also_closes_the_gate(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(1, "", "PERMISSION_DENIED")
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        project = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertNotIn("orphan-lb", {c["check"] for c in project["commands"]})


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_checks_run_copied_from_a_collected_cluster_survives_cross_check(self):
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        cluster_entry = next(c for c in manifest["clusters"] if c["name"] == "c1")
        project_entry = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        data = {
            "audit": "fleet-wide-cost-analysis",
            "scope": {
                "clusters": [
                    {"name": "c1", "checks_run": [{"check": c["check"], "command": c["command"]} for c in cluster_entry["commands"]]},
                    {"name": "project/acme", "checks_run": [{"check": c["check"], "command": c["command"]} for c in project_entry["commands"]]},
                ],
                "skipped": [],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_check_absent_from_the_manifest_is_rejected(self):
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        data = {
            "audit": "fleet-wide-cost-analysis",
            "scope": {"clusters": [{"name": "c1", "checks_run": [{"check": "overrequest", "command": "x"}]}]},
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)


if __name__ == "__main__":
    unittest.main()
