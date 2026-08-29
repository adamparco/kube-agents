#!/usr/bin/env python3
"""Tests for fleet_waste.py, the fleet-wide-cost-analysis collector."""

import json
import os
import sys
import threading
import unittest
from datetime import datetime, timezone
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


class ParseTopOutputTest(unittest.TestCase):
    def test_parses_pods(self):
        text = "default   api-abc123   150m   256Mi\nkube-system   kp   5m   10Mi\n"
        out = fw.parse_top_pods(text)
        self.assertEqual(out[("default", "api-abc123")], (0.15, 256.0))

    def test_parses_nodes(self):
        text = "gke-prod-a1   500m   25%   2000Mi   40%\n"
        out = fw.parse_top_nodes(text)
        self.assertEqual(out["gke-prod-a1"], (0.5, 2000.0))

    def test_short_lines_are_skipped(self):
        self.assertEqual(fw.parse_top_pods("not enough cols"), {})
        self.assertEqual(fw.parse_top_nodes("too few"), {})


class TakeUsageSamplesTest(unittest.TestCase):
    def test_three_samples_two_sleeps(self):
        sleeps = []
        calls = {"n": 0}

        def run(argv, **kwargs):
            calls["n"] += 1
            if "pods" in argv:
                return run_of(0, "default pod-1 100m 100Mi")
            return run_of(0, "node-1 1 10% 1000Mi 10%")

        pod_samples, node_samples, ok, _ = fw.take_usage_samples(Path("/kc"), run=run, sleep=sleeps.append)
        self.assertTrue(ok)
        self.assertEqual(len(pod_samples), 3)
        self.assertEqual(len(node_samples), 3)
        self.assertEqual(sleeps, [300, 300])  # n-1 sleeps between n samples

    def test_metrics_unavailable_short_circuits(self):
        def run(argv, **kwargs):
            if "nodes" in argv:
                return run_of(1, "", "metrics not available")
            return run_of(0, "")

        pod_samples, node_samples, ok, result = fw.take_usage_samples(Path("/kc"), run=run, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertEqual(pod_samples, [])
        self.assertEqual(result.rc, 1)


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


class MachineTypeVcpusTest(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(fw._machine_type_vcpus("e2-standard-8"), 8)

    def test_highmem(self):
        self.assertEqual(fw._machine_type_vcpus("n2-highmem-16"), 16)

    def test_custom(self):
        self.assertEqual(fw._machine_type_vcpus("custom-4-16384"), 4)

    def test_small_is_unmatched(self):
        self.assertIsNone(fw._machine_type_vcpus("e2-small"))


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

    def test_flags_gross_overrequest(self):
        pod = self.deployment_pod()
        samples = [{("default", "api-1"): (0.9, 3072.0)}] * 3  # 0.9 vCPU / 3 GiB peak vs 12/48 requested
        hits = fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_does_not_flag_when_one_sample_disagrees(self):
        pod = self.deployment_pod()
        samples = [{("default", "api-1"): (0.9, 3072.0)}, {("default", "api-1"): (0.9, 3072.0)}, {("default", "api-1"): (11.0, 40000.0)}]
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=False), [])

    def test_does_not_flag_below_the_absolute_floor(self):
        pod = self.deployment_pod(cpu_req="100m", mem_req="256Mi")
        samples = [{("default", "api-1"): (0.0, 0.0)}] * 3
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=False), [])

    def test_does_not_flag_daemonset(self):
        pod = self.deployment_pod(owner_kind="DaemonSet", owner_name="ds")
        samples = [{("default", "api-1"): (0.0, 0.0)}] * 3
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=False), [])

    def test_does_not_flag_job_owned_pod(self):
        pod = self.deployment_pod(owner_kind="Job", owner_name="batch")
        samples = [{("default", "api-1"): (0.0, 0.0)}] * 3
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=False), [])

    def test_does_not_flag_a_pod_with_no_requests_at_all(self):
        pod = self.deployment_pod(cpu_req="0", mem_req="0")
        pod["spec"]["containers"][0]["resources"] = {}
        samples = [{}] * 3
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=False), [])

    def test_guaranteed_qos_is_marked_for_manual_remediation(self):
        pod = self.deployment_pod(cpu_lim="12", mem_lim="48Gi")
        samples = [{("default", "api-1"): (0.9, 3072.0)}] * 3
        hits = fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=False)
        self.assertTrue(hits[0]["_guaranteed"])

    def test_autopilot_bumps_minor_to_major(self):
        pod = self.deployment_pod(cpu_req="3", mem_req="6Gi")
        samples = [{("default", "api-1"): (0.1, 100.0)}] * 3
        hits = fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=True)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_pending_pod_is_never_flagged(self):
        pod = self.deployment_pod()
        pod["status"]["phase"] = "Pending"
        samples = [{("default", "api-1"): (0.0, 0.0)}] * 3
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, samples, now=NOW, autopilot=False), [])


class UnattachedDiskTest(unittest.TestCase):
    def disk(self, name="d1", size_gb=200, created="2026-01-01T00:00:00Z", disk_type="pd-standard", users=None):
        return {"name": name, "sizeGb": str(size_gb), "type": disk_type, "creationTimestamp": created, "zone": "us-central1-a", "users": users or []}

    def test_flags_unattached_over_30_days(self):
        self.assertEqual(len(fw.check_unattached_disk([self.disk()], set(), now=NOW)), 1)

    def test_does_not_flag_attached(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(users=["some-vm"])], set(), now=NOW), [])

    def test_does_not_flag_recently_created(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(created="2026-07-30T00:00:00Z")], set(), now=NOW), [])

    def test_does_not_flag_a_disk_matching_a_live_pv_handle(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(name="pv-handle-1")], {"pv-handle-1"}, now=NOW), [])

    def test_large_ssd_is_major(self):
        hits = fw.check_unattached_disk([self.disk(size_gb=600, disk_type="pd-ssd")], set(), now=NOW)
        self.assertEqual(hits[0]["severity"], "major")


class IdleAddressTest(unittest.TestCase):
    def address(self, name="addr1", addr_type="EXTERNAL", status="RESERVED", purpose="", created="2026-01-01T00:00:00Z", region="us-central1"):
        return {"name": name, "address": "1.2.3.4", "addressType": addr_type, "status": status, "purpose": purpose, "creationTimestamp": created, "region": region}

    def test_flags_reserved_external_over_14_days(self):
        self.assertEqual(len(fw.check_idle_address([self.address()], set(), now=NOW)), 1)

    def test_does_not_flag_internal(self):
        self.assertEqual(fw.check_idle_address([self.address(addr_type="INTERNAL")], set(), now=NOW), [])

    def test_does_not_flag_gce_endpoint_purpose(self):
        self.assertEqual(fw.check_idle_address([self.address(purpose="GCE_ENDPOINT")], set(), now=NOW), [])

    def test_does_not_flag_referenced_by_annotation(self):
        self.assertEqual(fw.check_idle_address([self.address(name="my-ip")], {"my-ip"}, now=NOW), [])

    def test_rolls_up_ten_or_more_into_one_major_finding(self):
        addrs = [self.address(name=f"a{i}") for i in range(10)]
        hits = fw.check_idle_address(addrs, set(), now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")


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

    def test_does_not_flag_recent_forwarding_rule(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-07-30T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], set(), now=NOW), [])


class CollectClusterTest(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def run_with(self, dump_items=(), pools=(), top_pods_out="", top_nodes_out="node-1 1 10% 1000Mi 10%"):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*dump_items)))
            if "top" in argv and "pods" in argv:
                return run_of(0, top_pods_out)
            if "top" in argv and "nodes" in argv:
                return run_of(0, top_nodes_out)
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, json.dumps(list(pools)))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                return fw.collect_cluster(self.CLUSTER, run=run, sleep=lambda s: None, now=NOW)

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
                entry, facts = fw.collect_cluster(self.CLUSTER, run=run, sleep=lambda s: None, now=NOW)
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
                entry, _ = fw.collect_cluster(self.CLUSTER, run=run, sleep=lambda s: None, now=NOW)
        self.assertEqual(entry["outcome"], "gate-failed")

    def test_metrics_unavailable_still_collects_object_checks(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if "top" in argv and "nodes" in argv:
                return run_of(1, "", "metrics-server unavailable")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(self.CLUSTER, run=run, sleep=lambda s: None, now=NOW)
        self.assertEqual(entry["outcome"], "collected")
        self.assertNotIn("overrequest", {c["check"] for c in entry["commands"]})
        # Dropping the check out of `commands` is only half the job: §6 raises
        # it as a gap either way, and without this the ledger named a check
        # nobody could explain.
        self.assertIn("overrequest could not be measured", entry["limitations"])
        self.assertIn("metrics-server", entry["limitations"])

    def _unreadable_pools(self, cluster=None):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if "top" in argv and "nodes" in argv:
                return run_of(0, "node-1 1 10% 1000Mi 10%")
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(1, "", "PERMISSION_DENIED: container.nodePools.list")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                return fw.collect_cluster(
                    cluster or self.CLUSTER, run=run, sleep=lambda s: None, now=NOW
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
            if "top" in argv:
                return run_of(0, "")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(cluster, run=run, sleep=lambda s: None, now=NOW)
        commands = {c["check"] for c in entry["commands"]}
        self.assertNotIn("idle-nodepool", commands)
        self.assertNotIn("scaledown-blocked", commands)

    def test_fleet_facts_carry_pv_handles_and_service_names(self):
        pv = obj("PersistentVolume", "pv1", **{"spec.csi": {"volumeHandle": "projects/p/disks/d1"}})
        svc = obj("Service", "web", ns="default")
        entry, facts = self.run_with(dump_items=[pv, svc])
        self.assertIn("d1", facts["pv_handles"])
        self.assertIn("default/web", facts["service_names"])


class FleetSamplingConcurrencyTest(unittest.TestCase):
    def test_pool_scales_to_the_fleet_not_a_fixed_cap(self):
        """A fleet bigger than the old fixed cap of 8 must still sample
        every cluster concurrently -- a `threading.Barrier` sized to the
        fleet only ever releases if that many clusters are genuinely
        in-flight at once; if the pool caps out early, the excess clusters
        never reach the barrier and every waiter times out."""
        cluster_count = 12
        clusters_json = json.dumps(
            [
                {"name": f"c{i}", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                for i in range(cluster_count)
            ]
        )
        barrier = threading.Barrier(cluster_count, timeout=2)

        def sleep_fn(_seconds):
            barrier.wait()

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if "top" in argv and "nodes" in argv:
                return run_of(0, "")
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, sleep=sleep_fn, now=NOW)

        self.assertEqual(len({c["name"] for c in manifest["clusters"]} & {f"c{i}" for i in range(cluster_count)}), cluster_count)


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
            if "top" in argv and "nodes" in argv:
                return run_of(0, "")
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet(None, run=run, sleep=lambda s: None, now=NOW)

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
            if "top" in argv and "nodes" in argv:
                return run_of(0, "")
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
                manifest = fw.collect_fleet(None, run=run, sleep=lambda s: None, now=NOW)

        beta_entry = next(c for c in manifest["clusters"] if c["name"] == "project/beta")
        self.assertIn("unattached-disk", {c["check"] for c in beta_entry["candidates"]})


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
            if "top" in argv and "nodes" in argv:
                return run_of(0, "")
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, sleep=lambda s: None, now=NOW)

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
            if "top" in argv and "nodes" in argv:
                return run_of(1, "", "metrics unavailable")
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, sleep=lambda s: None, now=NOW)

        data = {
            "audit": "fleet-wide-cost-analysis",
            "scope": {"clusters": [{"name": "c1", "checks_run": [{"check": "overrequest", "command": "x"}]}]},
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)


if __name__ == "__main__":
    unittest.main()
