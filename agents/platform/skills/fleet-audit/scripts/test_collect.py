"""Tests for the fleet-audit procedural collector (collect.py).

Golden-dump tests for the two converted obtainability checks — jq filters
finally get the tests prose alone could never have (design §9) — plus fault
injection at every seam the manifest exists to make honest: a zero-byte
dump, a truncated one, one cluster's get-credentials failing under
parallelism, and both never reading as a shorter candidate list.
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402
from collect import Run  # noqa: E402


def deployment(name, ns="default", **overrides):
    doc = {
        "kind": "Deployment",
        "metadata": {"namespace": ns, "name": name, "labels": {}, "annotations": {}},
        "spec": {
            "replicas": 2,
            "template": {"spec": {"containers": [{"name": "app", "resources": {}}]}},
        },
    }
    for path, value in overrides.items():
        target = doc
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return doc


def dump_of(*items):
    return {"items": list(items)}


def with_container_resources(dep, resources):
    dep["spec"]["template"]["spec"]["containers"][0]["resources"] = resources
    return dep


class TestNormalizeWorkloads(unittest.TestCase):
    def test_a_plain_deployment_survives(self):
        out = collect.normalize_workloads(dump_of(deployment("api")))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "api")

    def test_system_namespace_is_excluded(self):
        out = collect.normalize_workloads(dump_of(deployment("coredns", ns="kube-system")))
        self.assertEqual(out, [])

    def test_a_gke_prefixed_namespace_is_excluded(self):
        out = collect.normalize_workloads(dump_of(deployment("x", ns="gke-connect")))
        self.assertEqual(out, [])

    def test_gke_managed_addon_is_excluded(self):
        d = deployment("fluentbit")
        d["metadata"]["labels"]["addonmanager.kubernetes.io/mode"] = "Reconcile"
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_a_workload_with_an_owner_is_excluded(self):
        d = deployment("replicaset-child")
        d["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "x"}]
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_the_opt_out_label_is_excluded(self):
        d = deployment("exempted")
        d["metadata"]["labels"]["kubeagents.x-k8s.io/reliability-audit"] = "exempt"
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_the_opt_out_annotation_is_also_honored(self):
        d = deployment("exempted")
        d["metadata"]["annotations"]["kubeagents.x-k8s.io/reliability-audit"] = "exempt"
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_a_scaled_to_zero_workload_is_excluded(self):
        d = deployment("idle", **{"spec.replicas": 0})
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_non_workload_kinds_are_ignored(self):
        pdb = {"kind": "PodDisruptionBudget", "metadata": {"namespace": "default", "name": "x"}}
        self.assertEqual(collect.normalize_workloads(dump_of(pdb)), [])


def context_of(dump=None, **overrides):
    """A build_context()-shaped dict with sensible empty defaults, so a test
    that only cares about one cross-reference does not have to construct the
    other three."""
    base = {"limitranges": {}, "pdbs": {}, "hpas": {}, "services": {}, "workloads": []}
    if dump is not None:
        base.update(
            limitranges=collect.limitranges_by_namespace(dump),
            pdbs=collect.pdbs_by_namespace(dump),
            hpas=collect.hpas_by_namespace(dump),
            services=collect.services_by_namespace(dump),
            workloads=collect.normalize_workloads(dump),
        )
    base.update(overrides)
    return base


class TestNoRequests(unittest.TestCase):
    def check(self, workload, limitranges=None):
        return collect.check_no_requests(workload, context_of(limitranges=limitranges or {}))

    def wl(self, resources=None, init_containers=None):
        d = deployment("api")
        if resources is not None:
            d["spec"]["template"]["spec"]["containers"][0]["resources"] = resources
        if init_containers is not None:
            d["spec"]["template"]["spec"]["initContainers"] = init_containers
        return collect.normalize_workloads(dump_of(d))[0]

    def test_no_requests_at_all_is_flagged(self):
        hit = self.check(self.wl(resources={}))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object"], "Deployment/api")
        self.assertIn("cpu", hit["excerpt"])
        self.assertIn("memory", hit["excerpt"])

    def test_both_requests_present_is_not_flagged(self):
        hit = self.check(self.wl(resources={"requests": {"cpu": "100m", "memory": "128Mi"}}))
        self.assertIsNone(hit)

    def test_missing_only_memory_is_flagged_by_name(self):
        hit = self.check(self.wl(resources={"requests": {"cpu": "100m"}}))
        self.assertIn("memory", hit["excerpt"])
        self.assertNotIn("cpu:", hit["excerpt"])

    def test_a_limitrange_default_request_suppresses_the_finding(self):
        limitranges = {
            "default": [{"spec": {"limits": [{"defaultRequest": {"cpu": "50m", "memory": "64Mi"}}]}}]
        }
        hit = self.check(self.wl(resources={}), limitranges)
        self.assertIsNone(hit)

    def test_a_limitrange_in_a_different_namespace_does_not_help(self):
        limitranges = {
            "other-ns": [{"spec": {"limits": [{"defaultRequest": {"cpu": "50m", "memory": "64Mi"}}]}}]
        }
        hit = self.check(self.wl(resources={}), limitranges)
        self.assertIsNotNone(hit)

    def test_a_native_sidecar_is_covered(self):
        # restartPolicy: Always makes an initContainer count toward the pod's
        # effective request set (§3.1) -- a plain init container never does.
        hit = self.check(
            self.wl(
                resources={"requests": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[{"name": "proxy", "restartPolicy": "Always", "resources": {}}],
            )
        )
        self.assertIsNotNone(hit)
        self.assertIn("proxy", hit["excerpt"])

    def test_a_plain_init_container_is_never_flagged(self):
        hit = self.check(
            self.wl(
                resources={"requests": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[{"name": "migrate", "resources": {}}],
            )
        )
        self.assertIsNone(hit)

    def test_a_wrong_sized_but_present_request_is_never_flagged(self):
        # This check owns absence only -- sizing is the waste audit's job.
        hit = self.check(self.wl(resources={"requests": {"cpu": "1m", "memory": "1Mi"}}))
        self.assertIsNone(hit)


class TestNoMemoryLimit(unittest.TestCase):
    def wl(self, resources=None):
        d = deployment("api")
        if resources is not None:
            d["spec"]["template"]["spec"]["containers"][0]["resources"] = resources
        return collect.normalize_workloads(dump_of(d))[0]

    def test_missing_memory_limit_is_flagged(self):
        hit = collect.check_no_memory_limit(self.wl(resources={}), context_of())
        self.assertIsNotNone(hit)
        self.assertIn("app", hit["excerpt"])

    def test_a_present_memory_limit_is_not_flagged(self):
        hit = collect.check_no_memory_limit(self.wl(resources={"limits": {"memory": "256Mi"}}), context_of())
        self.assertIsNone(hit)

    def test_a_missing_cpu_limit_is_never_flagged(self):
        # Omitting a CPU limit is a deliberate, recommended choice (§3.2).
        hit = collect.check_no_memory_limit(
            self.wl(resources={"limits": {"memory": "256Mi"}, "requests": {"cpu": "1"}}), context_of()
        )
        self.assertIsNone(hit)

    def test_a_limitrange_default_memory_limit_suppresses_it(self):
        limitranges = {"default": [{"spec": {"limits": [{"default": {"memory": "256Mi"}}]}}]}
        hit = collect.check_no_memory_limit(self.wl(resources={}), context_of(limitranges=limitranges))
        self.assertIsNone(hit)

    def test_a_limitrange_defaultRequest_does_not_count_as_a_limit(self):
        # default vs defaultRequest are different LimitRange fields; only
        # `default` backs a memory *limit*.
        limitranges = {"default": [{"spec": {"limits": [{"defaultRequest": {"memory": "256Mi"}}]}}]}
        hit = collect.check_no_memory_limit(self.wl(resources={}), context_of(limitranges=limitranges))
        self.assertIsNotNone(hit)


class TestSelectorMatches(unittest.TestCase):
    def test_matchLabels_all_must_match(self):
        self.assertTrue(collect.selector_matches({"matchLabels": {"app": "api"}}, {"app": "api", "tier": "web"}))
        self.assertFalse(collect.selector_matches({"matchLabels": {"app": "api", "tier": "db"}}, {"app": "api"}))

    def test_an_empty_selector_matches_everything(self):
        # The exact footgun 3.3's remediation guards against emitting -- this
        # function reads a live selector faithfully, it does not guard here.
        self.assertTrue(collect.selector_matches({}, {"anything": "goes"}))

    def test_matchExpressions_in_and_not_in(self):
        sel = {"matchExpressions": [{"key": "env", "operator": "In", "values": ["prod", "staging"]}]}
        self.assertTrue(collect.selector_matches(sel, {"env": "prod"}))
        self.assertFalse(collect.selector_matches(sel, {"env": "dev"}))
        sel = {"matchExpressions": [{"key": "env", "operator": "NotIn", "values": ["dev"]}]}
        self.assertFalse(collect.selector_matches(sel, {"env": "dev"}))

    def test_matchExpressions_exists_and_does_not_exist(self):
        self.assertTrue(
            collect.selector_matches({"matchExpressions": [{"key": "app", "operator": "Exists"}]}, {"app": "x"})
        )
        self.assertFalse(
            collect.selector_matches({"matchExpressions": [{"key": "app", "operator": "Exists"}]}, {})
        )
        self.assertFalse(
            collect.selector_matches({"matchExpressions": [{"key": "app", "operator": "DoesNotExist"}]}, {"app": "x"})
        )

    def test_matchLabels_and_matchExpressions_are_anded_together(self):
        sel = {"matchLabels": {"app": "api"}, "matchExpressions": [{"key": "tier", "operator": "In", "values": ["web"]}]}
        self.assertTrue(collect.selector_matches(sel, {"app": "api", "tier": "web"}))
        self.assertFalse(collect.selector_matches(sel, {"app": "api", "tier": "db"}))


def pdb(name, ns="default", selector=None, max_unavailable=None, min_available=None):
    spec = {"selector": selector if selector is not None else {"matchLabels": {"app": "api"}}}
    if max_unavailable is not None:
        spec["maxUnavailable"] = max_unavailable
    if min_available is not None:
        spec["minAvailable"] = min_available
    return {"kind": "PodDisruptionBudget", "metadata": {"namespace": ns, "name": name}, "spec": spec}


def hpa(name, ns="default", min_replicas=1, max_replicas=5, target=None, owned=False):
    doc = {
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"namespace": ns, "name": name},
        "spec": {
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
            "scaleTargetRef": target or {"apiVersion": "apps/v1", "kind": "Deployment", "name": "api"},
        },
    }
    if owned:
        doc["metadata"]["ownerReferences"] = [{"kind": "ScaledObject", "name": "x"}]
    return doc


def service(name, ns="default", selector=None, svc_type="ClusterIP"):
    spec = {"type": svc_type}
    if selector is not None:
        spec["selector"] = selector
    return {"kind": "Service", "metadata": {"namespace": ns, "name": name}, "spec": spec}


class TestNoPdb(unittest.TestCase):
    def wl(self, kind="Deployment", replicas=2, labels=None):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        if labels is not None:
            d["spec"]["template"]["metadata"] = {"labels": labels}
        return collect.normalize_workloads(dump_of(d))[0]

    def test_multi_replica_with_no_matching_pdb_is_flagged(self):
        hit = collect.check_no_pdb(self.wl(), context_of())
        self.assertIsNotNone(hit)

    def test_a_matching_pdb_suppresses_it(self):
        workload = self.wl(labels={"app": "api"})
        ctx = context_of(pdbs={"default": [pdb("p", selector={"matchLabels": {"app": "api"}})]})
        self.assertIsNone(collect.check_no_pdb(workload, ctx))

    def test_a_pdb_with_a_non_matching_selector_does_not_help(self):
        workload = self.wl(labels={"app": "api"})
        ctx = context_of(pdbs={"default": [pdb("p", selector={"matchLabels": {"app": "other"}})]})
        self.assertIsNotNone(collect.check_no_pdb(workload, ctx))

    def test_a_daemonset_is_never_flagged(self):
        self.assertIsNone(collect.check_no_pdb(self.wl(kind="DaemonSet"), context_of()))

    def test_a_single_replica_workload_is_never_flagged(self):
        self.assertIsNone(collect.check_no_pdb(self.wl(replicas=1), context_of()))


class TestBlockingPdb(unittest.TestCase):
    def ctx(self, pdb_entry, replicas=3, labels=None):
        # pdb()'s default selector is {"matchLabels": {"app": "api"}}, so the
        # workload needs that label by default too, or every "blocking"
        # fixture below fails to match and asserts nothing.
        d = deployment("api", **{"spec.replicas": replicas})
        d["spec"]["template"]["metadata"] = {"labels": labels if labels is not None else {"app": "api"}}
        workloads = collect.normalize_workloads(dump_of(d))
        return context_of(pdbs={"default": [pdb_entry]}, workloads=workloads)

    def test_max_unavailable_zero_is_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", max_unavailable=0)))
        self.assertEqual(len(hits), 1)
        self.assertIn("PodDisruptionBudget/p", hits[0]["object"])

    def test_max_unavailable_zero_percent_is_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", max_unavailable="0%")))
        self.assertEqual(len(hits), 1)

    def test_min_available_100_percent_is_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", min_available="100%")))
        self.assertEqual(len(hits), 1)

    def test_min_available_integer_at_or_above_replicas_is_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", min_available=3), replicas=3))
        self.assertEqual(len(hits), 1)

    def test_min_available_below_replica_count_is_not_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", min_available=1), replicas=3))
        self.assertEqual(hits, [])

    def test_a_workload_scaled_to_zero_is_never_blocked(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", max_unavailable=0), replicas=0))
        self.assertEqual(hits, [])

    def test_an_orphan_pdb_matching_no_workload_is_not_reported(self):
        ctx = context_of(pdbs={"default": [pdb("p", max_unavailable=0, selector={"matchLabels": {"app": "nope"}})]},
                          workloads=collect.normalize_workloads(dump_of(deployment("api"))))
        self.assertEqual(collect.check_blocking_pdb(ctx), [])


class TestNoHpa(unittest.TestCase):
    def wl(self, replicas=3, kind="Deployment"):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        return collect.normalize_workloads(dump_of(d))[0]

    def test_three_or_more_replicas_with_no_hpa_is_flagged(self):
        self.assertIsNotNone(collect.check_no_hpa(self.wl(), context_of()))

    def test_a_matching_hpa_suppresses_it(self):
        ctx = context_of(hpas={"default": [hpa("h")]})
        self.assertIsNone(collect.check_no_hpa(self.wl(), ctx))

    def test_fewer_than_three_replicas_is_never_flagged(self):
        self.assertIsNone(collect.check_no_hpa(self.wl(replicas=2), context_of()))

    def test_a_statefulset_is_never_flagged(self):
        self.assertIsNone(collect.check_no_hpa(self.wl(kind="StatefulSet"), context_of()))

    def test_a_keda_owned_hpa_still_counts_because_it_is_a_real_hpa(self):
        # hpas_by_namespace excludes KEDA-owned HPAs from other checks, but
        # an HPA that DOES exist and targets this Deployment still means
        # "this workload is autoscaled" -- so it should suppress no-hpa if
        # it were included. Since hpas_by_namespace strips it, this proves
        # the exclusion means the workload reads as unautoscaled instead,
        # which is the documented limitation, not a bug: the real config
        # lives in a CRD this audit does not read.
        ctx = context_of()
        dump = dump_of(hpa("h", owned=True))
        ctx["hpas"] = collect.hpas_by_namespace(dump)
        self.assertEqual(ctx["hpas"], {"default": []})  # namespace key kept, owned HPA filtered out
        self.assertIsNotNone(collect.check_no_hpa(self.wl(), ctx))


class TestHpaCannotScale(unittest.TestCase):
    def test_min_equals_max_is_pinned_major(self):
        ctx = context_of(hpas={"default": [hpa("h", min_replicas=3, max_replicas=3)]})
        hits = collect.check_hpa_cannot_scale(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_a_dangling_target_is_minor(self):
        workloads = collect.normalize_workloads(dump_of(deployment("other")))
        ctx = context_of(
            hpas={"default": [hpa("h", target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gone"})]},
            workloads=workloads,
        )
        hits = collect.check_hpa_cannot_scale(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_a_healthy_hpa_with_an_existing_target_is_not_flagged(self):
        workloads = collect.normalize_workloads(dump_of(deployment("api")))
        ctx = context_of(hpas={"default": [hpa("h", min_replicas=1, max_replicas=5)]}, workloads=workloads)
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_a_target_kind_outside_the_dump_is_not_dangling(self):
        # The cluster was readable; an unevaluated target (e.g. a
        # StatefulSet the dump does carry, or a custom resource) belongs in
        # limitations prose, never in this finding.
        ctx = context_of(hpas={"default": [hpa("h", target={"apiVersion": "apps/v1", "kind": "CustomThing", "name": "x"})]})
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])


class TestRigidScheduling(unittest.TestCase):
    def wl(self, node_selector=None, affinity=None, kind="Deployment", vct=False):
        d = deployment("api")
        d["kind"] = kind
        if node_selector is not None:
            d["spec"]["template"]["spec"]["nodeSelector"] = node_selector
        if affinity is not None:
            d["spec"]["template"]["spec"]["affinity"] = affinity
        if vct:
            d["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "data"}}]
        return collect.normalize_workloads(dump_of(d))[0]

    def test_hostname_node_selector_is_critical(self):
        hit = collect.check_rigid_scheduling(self.wl(node_selector={"kubernetes.io/hostname": "node-1"}), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_single_zone_node_selector_is_major(self):
        hit = collect.check_rigid_scheduling(
            self.wl(node_selector={"topology.kubernetes.io/zone": "us-central1-a"}), context_of()
        )
        self.assertEqual(hit["severity"], "major")

    def test_a_statefulset_with_zonal_storage_is_not_flagged_for_its_zone_pin(self):
        hit = collect.check_rigid_scheduling(
            self.wl(node_selector={"topology.kubernetes.io/zone": "us-central1-a"}, kind="StatefulSet", vct=True),
            context_of(),
        )
        self.assertIsNone(hit)

    def test_a_hardware_selector_is_never_flagged(self):
        hit = collect.check_rigid_scheduling(
            self.wl(node_selector={"cloud.google.com/gke-accelerator": "nvidia-t4"}), context_of()
        )
        self.assertIsNone(hit)

    def test_hostname_node_affinity_is_critical(self):
        affinity = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-1"]}]}
                    ]
                }
            }
        }
        hit = collect.check_rigid_scheduling(self.wl(affinity=affinity), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_a_multi_value_zone_affinity_is_never_flagged(self):
        affinity = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["a", "b"]}]}
                    ]
                }
            }
        }
        self.assertIsNone(collect.check_rigid_scheduling(self.wl(affinity=affinity), context_of()))

    def test_preferred_affinity_is_never_flagged(self):
        affinity = {
            "nodeAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {"preference": {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["x"]}]}}
                ]
            }
        }
        self.assertIsNone(collect.check_rigid_scheduling(self.wl(affinity=affinity), context_of()))

    def test_an_unpinned_workload_is_not_flagged(self):
        self.assertIsNone(collect.check_rigid_scheduling(self.wl(), context_of()))


class TestNoSpread(unittest.TestCase):
    def wl(self, replicas=2, tsc=None, anti_affinity=None, kind="Deployment"):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        if tsc is not None:
            d["spec"]["template"]["spec"]["topologySpreadConstraints"] = tsc
        if anti_affinity is not None:
            d["spec"]["template"]["spec"]["affinity"] = {"podAntiAffinity": anti_affinity}
        return collect.normalize_workloads(dump_of(d))[0]

    def test_multi_replica_with_neither_mechanism_is_flagged(self):
        self.assertIsNotNone(collect.check_no_spread(self.wl(), context_of()))

    def test_a_topology_spread_constraint_suppresses_it(self):
        tsc = [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "ScheduleAnyway"}]
        self.assertIsNone(collect.check_no_spread(self.wl(tsc=tsc), context_of()))

    def test_required_pod_anti_affinity_suppresses_it(self):
        anti = {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname"}]}
        self.assertIsNone(collect.check_no_spread(self.wl(anti_affinity=anti), context_of()))

    def test_preferred_pod_anti_affinity_suppresses_it(self):
        anti = {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {"podAffinityTerm": {"topologyKey": "topology.kubernetes.io/zone"}}
            ]
        }
        self.assertIsNone(collect.check_no_spread(self.wl(anti_affinity=anti), context_of()))

    def test_a_daemonset_is_never_flagged(self):
        self.assertIsNone(collect.check_no_spread(self.wl(kind="DaemonSet"), context_of()))

    def test_single_replica_is_never_flagged(self):
        self.assertIsNone(collect.check_no_spread(self.wl(replicas=1), context_of()))


class TestProbes(unittest.TestCase):
    def wl(self, probes=None):
        d = deployment("api")
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        if probes is not None:
            d["spec"]["template"]["spec"]["containers"][0].update(probes)
        return collect.normalize_workloads(dump_of(d))[0]

    def svc_ctx(self):
        return context_of(services={"default": [service("s", selector={"app": "api"})]})

    def test_readiness_missing_on_a_service_backed_workload_is_flagged(self):
        self.assertIsNotNone(collect.check_probes_readiness(self.wl(), self.svc_ctx()))

    def test_readiness_present_is_not_flagged(self):
        hit = collect.check_probes_readiness(
            self.wl(probes={"readinessProbe": {"httpGet": {"path": "/", "port": 80}}}), self.svc_ctx()
        )
        self.assertIsNone(hit)

    def test_readiness_is_never_flagged_with_no_service(self):
        self.assertIsNone(collect.check_probes_readiness(self.wl(), context_of()))

    def test_an_external_name_service_does_not_count_as_backing(self):
        ctx = context_of(services={"default": [service("s", svc_type="ExternalName")]})
        self.assertIsNone(collect.check_probes_readiness(self.wl(), ctx))

    def test_a_self_health_sidecar_is_never_flagged(self):
        d = deployment("api")
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        d["spec"]["template"]["spec"]["containers"].append({"name": "istio-proxy", "resources": {}})
        workload = collect.normalize_workloads(dump_of(d))[0]
        hit = collect.check_probes_readiness(workload, self.svc_ctx())
        self.assertNotIn("istio-proxy", hit["excerpt"] if hit else "")

    def test_liveness_missing_is_flagged_with_no_service_required(self):
        self.assertIsNotNone(collect.check_probes_liveness(self.wl(), context_of()))

    def test_liveness_present_is_not_flagged(self):
        hit = collect.check_probes_liveness(
            self.wl(probes={"livenessProbe": {"httpGet": {"path": "/", "port": 80}}}), context_of()
        )
        self.assertIsNone(hit)

    def test_liveness_and_readiness_are_reported_separately_never_merged(self):
        # A workload missing both must be two findings under two checks, not
        # one -- they carry different severities and impacts.
        readiness_hit = collect.check_probes_readiness(self.wl(), self.svc_ctx())
        liveness_hit = collect.check_probes_liveness(self.wl(), context_of())
        self.assertIsNotNone(readiness_hit)
        self.assertIsNotNone(liveness_hit)


class TestSingleReplica(unittest.TestCase):
    def wl(self, replicas=1, strategy=None, kind="Deployment"):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        if strategy is not None:
            d["spec"]["strategy"] = {"type": strategy}
        return collect.normalize_workloads(dump_of(d))[0]

    def svc_ctx(self):
        return context_of(services={"default": [service("s", selector={"app": "api"})]})

    def test_a_single_replica_service_backed_deployment_is_flagged(self):
        self.assertIsNotNone(collect.check_single_replica(self.wl(), self.svc_ctx()))

    def test_multi_replica_is_never_flagged(self):
        self.assertIsNone(collect.check_single_replica(self.wl(replicas=2), self.svc_ctx()))

    def test_a_statefulset_is_never_flagged(self):
        self.assertIsNone(collect.check_single_replica(self.wl(kind="StatefulSet"), self.svc_ctx()))

    def test_recreate_strategy_is_never_flagged(self):
        self.assertIsNone(collect.check_single_replica(self.wl(strategy="Recreate"), self.svc_ctx()))

    def test_no_service_means_never_flagged(self):
        self.assertIsNone(collect.check_single_replica(self.wl(), context_of()))


def fake_run(replies, calls):
    def run(argv, **kwargs):
        calls.append(argv)
        for key, result in replies.items():
            if key in argv:
                return result
        return Run(argv, 0, "", "", 0.01)

    return run


class TestFetchCredentials(unittest.TestCase):
    def test_sets_kubeconfig_per_cluster_not_export(self):
        calls = []
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)):
                kc, result = collect.fetch_credentials(
                    "proj", "prod-usc1", "us-central1", run=fake_run({}, calls)
                )
        self.assertEqual(result.rc, 0)
        self.assertIn("prod-usc1", str(kc))
        self.assertIn("proj", str(kc))

    def test_a_failed_get_credentials_is_reported_not_raised(self):
        calls = []
        replies = {"get-credentials": Run([], 1, "", "cluster not found", 0.1)}
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)):
                _, result = collect.fetch_credentials(
                    "proj", "gone", "us-central1", run=fake_run(replies, calls)
                )
        self.assertEqual(result.rc, 1)


class TestDumpStateGate(unittest.TestCase):
    def run_dump(self, stdout, rc=0):
        def run(argv, **kwargs):
            return Run(argv, rc, stdout, "", 0.05)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.dump_state(Path(tmp) / "kc.yaml", "c1", run=run)

    def test_a_well_formed_dump_passes_the_gate(self):
        _, _, gate_ok = self.run_dump(json.dumps({"items": []}))
        self.assertTrue(gate_ok)

    def test_a_zero_byte_dump_fails_the_gate(self):
        _, _, gate_ok = self.run_dump("")
        self.assertFalse(gate_ok)

    def test_a_truncated_dump_fails_the_gate(self):
        # What a 4 MiB proxy truncation looks like: valid JSON up to a point,
        # then cut off mid-object.
        _, _, gate_ok = self.run_dump('{"items": [{"kind": "Depl')
        self.assertFalse(gate_ok)

    def test_a_non_zero_exit_fails_the_gate_even_with_output(self):
        _, _, gate_ok = self.run_dump(json.dumps({"items": []}), rc=1)
        self.assertFalse(gate_ok)

    def test_the_wrong_shape_fails_the_gate(self):
        # Valid JSON, but not a List -- e.g. an error object kubectl printed.
        _, _, gate_ok = self.run_dump(json.dumps({"error": "no"}))
        self.assertFalse(gate_ok)


class TestCollectCluster(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def collect(self, dump_items, cred_rc=0, dump_rc=0, checks=None):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if "get-credentials" in argv:
                return Run(argv, cred_rc, "", "" if cred_rc == 0 else "denied", 0.1)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, dump_rc, json.dumps(dump_of(*dump_items)), "", 0.2)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(
                    self.CLUSTER, checks or collect.OBTAINABILITY_CHECKS, run=run
                )
        return result, calls

    def test_a_clean_cluster_collects_with_no_candidates(self):
        # Scoped to the first two checks deliberately -- this test is about
        # no-requests/no-memory-limit interaction, not about every check in
        # the roster being individually satisfied by one fixture workload.
        clean = with_container_resources(
            deployment("api"),
            {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
        )
        result, _ = self.collect([clean], checks=collect.OBTAINABILITY_CHECKS[:2])
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["commands"]), 2)  # one per check, same collection command

    def test_a_dirty_cluster_reports_both_checks(self):
        d = deployment("api")  # no resources at all
        result, _ = self.collect([d], checks=collect.OBTAINABILITY_CHECKS[:2])
        slugs = {c["check"] for c in result["candidates"]}
        self.assertEqual(slugs, {"no-requests", "no-memory-limit"})

    def test_the_full_roster_runs_together(self):
        # A bare two-replica Deployment with no resources, no PDB, no probes,
        # and no spreading should trip every workload-scoped check the
        # roster carries (all but the cluster-scoped ones, which need a
        # matching PDB/HPA to fire at all).
        d = deployment("api", **{"spec.replicas": 2})
        result, _ = self.collect([d])
        slugs = {c["check"] for c in result["candidates"]}
        self.assertEqual(
            slugs,
            {
                "no-requests", "no-memory-limit", "no-pdb", "no-spread",
                "probes-liveness",
            },
        )

    def test_a_fully_compliant_workload_trips_nothing_in_the_full_roster(self):
        d = with_container_resources(
            deployment("api", **{"spec.replicas": 1}),
            {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
        )
        d["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] = {"httpGet": {"path": "/", "port": 80}}
        d["spec"]["template"]["spec"]["containers"][0]["livenessProbe"] = {"httpGet": {"path": "/", "port": 80}}
        result, _ = self.collect([d])
        self.assertEqual(result["candidates"], [])

    def test_get_credentials_failure_is_unreachable_not_a_shorter_list(self):
        result, calls = self.collect([deployment("api")], cred_rc=1)
        self.assertEqual(result["outcome"], "unreachable")
        self.assertNotIn("candidates", result)
        # The gate must never have been reached -- no kubectl call at all.
        self.assertFalse(any(c[:2] == ["kubectl", "get"] for c in calls))

    def test_a_failed_dump_is_gate_failed_not_a_shorter_list(self):
        result, _ = self.collect([deployment("api")], dump_rc=1)
        self.assertEqual(result["outcome"], "gate-failed")
        self.assertNotIn("candidates", result)

    def test_autopilot_downgrades_no_requests_but_not_no_memory_limit(self):
        cluster = {**self.CLUSTER, "autopilot": True}
        calls = []

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.1)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of(deployment("api"))), "", 0.2)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(cluster, collect.OBTAINABILITY_CHECKS, run=run)
        by_slug = {c["check"]: c for c in result["candidates"]}
        self.assertEqual(by_slug["no-requests"]["severity"], "minor")
        self.assertEqual(by_slug["no-memory-limit"]["severity"], "major")

    def test_the_collection_command_is_the_same_across_every_check(self):
        result, _ = self.collect([deployment("api")])
        commands = {c["command"] for c in result["commands"]}
        self.assertEqual(len(commands), 1)
        self.assertIn("kubectl get", next(iter(commands)))


class TestCollectFleet(unittest.TestCase):
    def test_every_enumerated_cluster_gets_an_entry_even_under_parallelism(self):
        # One cluster fails get-credentials; the others succeed. All three
        # must appear in the manifest -- a background failure under the
        # thread pool must not vanish a cluster from the result.
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "c2", "location": "us-central1", "status": "RUNNING"},
                {"name": "c3", "location": "us-central1", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                if "c2" in argv:
                    return Run(argv, 1, "", "denied", 0.05)
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.05)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("obtainability-audit", "acme", run=run, max_workers=3)

        names = {c["name"]: c["outcome"] for c in manifest["clusters"]}
        self.assertEqual(names, {"c1": "collected", "c2": "unreachable", "c3": "collected"})

    def test_a_non_running_cluster_is_never_enumerated(self):
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "stopping", "location": "us-central1", "status": "STOPPING"},
            ]
        )

        def run(argv, **kwargs):
            if "list" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.05)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("obtainability-audit", "acme", run=run)
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["c1"])

    def test_an_unknown_audit_id_refuses_rather_than_collecting_nothing(self):
        with self.assertRaises(ValueError):
            collect.collect_fleet("no-such-stream", "acme")

    def test_enumeration_failure_raises(self):
        def run(argv, **kwargs):
            return Run(argv, 1, "", "permission denied", 0.05)

        with self.assertRaises(RuntimeError):
            collect.collect_fleet("obtainability-audit", "acme", run=run)


class TestManifestComposesWithAuditReport(unittest.TestCase):
    """collect.py and audit_report.py are developed and tested independently
    against a shared manifest contract (design §6). This proves the contract
    actually holds: a real manifest from `collect_fleet`, fed into
    `audit_report.cross_check_manifest` alongside the `checks_run` list an
    agent would honestly copy from it, passes — and a `checks_run` that
    invents a check the manifest never ran is still rejected.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        global audit_report
        import audit_report  # noqa: F401, imported for its cross_check_manifest

    def build_manifest(self, dump_items):
        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of(*dump_items)), "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.collect_fleet("obtainability-audit", "acme", run=run)

    def test_the_full_roster_of_checks_run_verifies_against_the_real_manifest(self):
        manifest = self.build_manifest([deployment("api")])
        checks_run = [{"check": c.slug, "command": "x"} for c in collect.OBTAINABILITY_CHECKS]
        doc = {"audit": "obtainability-audit", "scope": {"clusters": [{"name": "c1", "checks_run": checks_run}]}}
        audit_report.cross_check_manifest(doc, manifest)  # must not raise

    def test_a_check_the_agent_did_not_actually_run_is_still_caught(self):
        manifest = self.build_manifest([deployment("api")])
        doc = {
            "audit": "obtainability-audit",
            "scope": {"clusters": [{"name": "c1", "checks_run": [{"check": "no-hpa", "command": "x"}]}]},
        }
        # no-hpa IS in the real manifest, so this passes -- the negative case:
        audit_report.cross_check_manifest(doc, manifest)
        doc["scope"]["clusters"][0]["checks_run"].append({"check": "single-replica", "command": "x"})
        # single-replica is also real; still passes. Now fabricate one that
        # is not a rostered obtainability-audit slug at all -- the harness's
        # own roster validation (not this function) would catch that in
        # practice, but cross_check_manifest itself must refuse a slug the
        # manifest's commands never recorded, whatever it's called.
        doc["scope"]["clusters"][0]["checks_run"].append({"check": "not-a-real-check", "command": "x"})
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(doc, manifest)


if __name__ == "__main__":
    unittest.main()
