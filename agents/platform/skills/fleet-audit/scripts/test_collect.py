"""Tests for the fleet-audit procedural collector (collect.py).

Golden-dump tests for the two converted obtainability checks — jq filters
finally get the tests prose alone could never have (design §9) — plus fault
injection at every seam the manifest exists to make honest: a zero-byte
dump, a truncated one, one cluster's get-credentials failing under
parallelism, and both never reading as a shorter candidate list.
"""

import inspect
import json
import re
import shlex
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

    def test_a_workload_owned_by_a_job_is_excluded(self):
        d = deployment("batch-child")
        d["metadata"]["ownerReferences"] = [{"apiVersion": "batch/v1", "kind": "Job", "name": "x"}]
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_a_workload_owned_by_a_crd_is_still_audited(self):
        """S3 defers to the owning controller. A CRD is not a controller this
        audit ever reads, so deferring to it drops the finding instead of
        moving it — which is how the harness's own gateway went permanently
        unaudited in the one namespace S1 keeps in scope on purpose."""
        d = deployment("platform-agent-gateway", ns="kubeagents-system")
        d["metadata"]["ownerReferences"] = [
            {"apiVersion": "kubeagents.x-k8s.io/v1alpha1", "kind": "PlatformAgent", "name": "platform-agent"}
        ]
        out = collect.normalize_workloads(dump_of(d))
        self.assertEqual([w["name"] for w in out], ["platform-agent-gateway"])

    def test_a_crd_that_borrows_a_builtin_kind_name_does_not_suppress(self):
        """`Job` in someone else's API group is a custom resource wearing the
        name, and nothing about it is reachable from this dump."""
        d = deployment("look-alike")
        d["metadata"]["ownerReferences"] = [{"apiVersion": "acme.example.com/v1", "kind": "Job", "name": "x"}]
        self.assertEqual([w["name"] for w in collect.normalize_workloads(dump_of(d))], ["look-alike"])

    def test_one_builtin_owner_is_enough_to_suppress(self):
        d = deployment("two-owners")
        d["metadata"]["ownerReferences"] = [
            {"apiVersion": "acme.example.com/v1", "kind": "Widget", "name": "w"},
            {"apiVersion": "apps/v1", "kind": "Deployment", "name": "d"},
        ]
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
    base = {
        "limitranges": {},
        "pdbs": {},
        "hpas": {},
        "services": {},
        "workloads": [],
        "workload_keys": set(),
        "pod_namespaces": set(),
        "cluster_name": "test-cluster",
    }
    if dump is not None:
        base.update(
            limitranges=collect.limitranges_by_namespace(dump),
            pdbs=collect.pdbs_by_namespace(dump),
            hpas=collect.hpas_by_namespace(dump),
            services=collect.services_by_namespace(dump),
            workloads=collect.normalize_workloads(dump),
            workload_keys=collect.workload_keys(dump),
        )
    base.update(overrides)
    if "workload_keys" not in overrides and dump is None:
        # A test that hands over `workloads` and no dump is saying "this is
        # what the cluster holds", so the two have to agree. Deriving keeps
        # those tests about the check they name; the S4/S5 cases that need the
        # two sets to *differ* pass a dump, or `workload_keys` outright.
        base["workload_keys"] = {(wl["ns"], wl["kind"], wl["name"]) for wl in base["workloads"]}
    if "pod_namespaces" not in overrides:
        # Same rule for the raw pod set `netpol-missing` reads. A test that
        # says the namespace holds a Pod means the cluster has one there; the
        # cases about the gap between the two -- an owned pod the audited set
        # drops, a namespace whose only Pod is in `pod_namespaces` and nowhere
        # else -- pass it outright.
        base["pod_namespaces"] = {wl["ns"] for wl in base["workloads"] if wl["kind"] == "Pod"}
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

    # -- Impact, per arm. §3.1 flags a container missing cpu *or* memory, so
    # the check's own "first evicted under node pressure" describes only the
    # BestEffort arm. The other two carry their own sentence, composed of two
    # independently-varying halves: what the missing requests cost, and what
    # the pod's real QoS class means for eviction order.

    def test_a_besteffort_pod_keeps_the_checks_own_impact(self):
        # Nothing declared anywhere: the pod really is BestEffort and really is
        # evicted first, so the hit must NOT override the Impact.
        self.assertNotIn("impact", self.check(self.wl(resources={})))

    def test_a_burstable_pod_is_not_called_first_evicted(self):
        # The live shape: `kube-proxy` and `antrea-controller` run on every
        # cluster in the fleet with a CPU request and no memory request.
        hit = self.check(self.wl(resources={"requests": {"cpu": "100m"}}))
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertIn("memory goes unreserved", hit["impact"])
        self.assertNotIn("first evicted", hit["impact"])
        # And it must not swap one false eviction claim for another. The
        # kubelet does not rank by QoS class at all -- it sorts on whether
        # usage exceeds requests, then Pod Priority -- so "Burstable is evicted
        # after every BestEffort pod" is as wrong as "first evicted" was.
        self.assertIn("Eviction does not follow the class", hit["impact"])
        self.assertNotIn("after every BestEffort pod", hit["impact"])

    def test_a_limit_with_no_request_is_reserved_at_its_ceiling(self):
        # Kubernetes copies the limit into the request at admission, so this pod
        # is Guaranteed -- the last thing evicted. Still flagged, because §3.1
        # wants the request declared, but for the opposite reason.
        hit = self.check(self.wl(resources={"limits": {"cpu": "1", "memory": "1Gi"}}))
        self.assertIn("copies that limit into the request", hit["impact"])
        self.assertIn("Guaranteed", hit["impact"])
        self.assertIn("last group evicted", hit["impact"])
        self.assertNotIn("costs nothing", hit["impact"])

    # -- The eviction half is the *pod's* QoS class, which a sibling container
    # can decide. Reading it off the reported container's own limits is the
    # mistake these four pin: each has every missing request limit-backed, so
    # the ceiling sentence is right and "Guaranteed" is wrong.

    def test_a_sibling_without_a_limit_makes_a_backed_pod_burstable(self):
        # `app` declares limits and no requests -- backed, ceiling-reserved. But
        # `proxy` declares requests and no ceiling, and Guaranteed needs *every*
        # container to carry both limits. The pod is Burstable.
        hit = self.check(
            self.wl(
                resources={"limits": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[
                    {
                        "name": "proxy",
                        "restartPolicy": "Always",
                        "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}},
                    }
                ],
            )
        )
        self.assertIn("copies that limit into the request", hit["impact"])
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_a_request_below_its_own_limit_is_not_guaranteed(self):
        # `memory` is missing and backed by its limit, so the ceiling sentence
        # holds -- but the declared cpu request is under the cpu limit, which is
        # the textbook Burstable pod.
        hit = self.check(
            self.wl(resources={"requests": {"cpu": "500m"}, "limits": {"cpu": "1", "memory": "1Gi"}})
        )
        self.assertIn("copies that limit into the request", hit["impact"])
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_a_limitrange_default_below_the_limit_is_not_guaranteed(self):
        # The LimitRange covers cpu, so cpu drops out of `missing` and memory is
        # the only finding -- backed by its limit. But the injected cpu request
        # is 50m against a 1-core limit, so the admitted pod is Burstable.
        limitranges = {"default": [{"spec": {"limits": [{"defaultRequest": {"cpu": "50m"}}]}}]}
        hit = self.check(self.wl(resources={"limits": {"cpu": "1", "memory": "1Gi"}}), limitranges)
        self.assertIn("memory", hit["excerpt"])
        self.assertNotIn("cpu", hit["excerpt"])
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_a_sibling_with_matching_requests_and_limits_stays_guaranteed(self):
        # The control for the three above: `proxy` declares both limits with
        # requests that match them, so nothing disqualifies the pod and the
        # Guaranteed sentence is the true one.
        hit = self.check(
            self.wl(
                resources={"limits": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[
                    {
                        "name": "proxy",
                        "restartPolicy": "Always",
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "8Mi"},
                            "limits": {"cpu": "10m", "memory": "8Mi"},
                        },
                    }
                ],
            )
        )
        self.assertIn("Guaranteed", hit["impact"])
        self.assertNotIn("Burstable", hit["impact"])

    # -- Which containers, and which quantities, Kubernetes counts. The QoS
    # computation reads a different container set from §3.1's flag-when and
    # ignores quantities §3.1 does not, so both had to be answered separately.

    def test_a_plain_init_containers_requests_decide_the_class(self):
        # `_effective_containers` drops a plain init container, correctly: it
        # never counts toward the pod's effective request. QoS is the other
        # question -- upstream iterates *all* of `spec.initContainers` with no
        # restartPolicy filter -- so this pod is Burstable, not BestEffort, and
        # must not fall through to the check's own "first evicted".
        hit = self.check(
            self.wl(
                resources={},
                init_containers=[{"name": "setup", "resources": {"requests": {"cpu": "100m"}}}],
            )
        )
        self.assertEqual(hit["excerpt"], "app: missing cpu,memory")
        self.assertIn("Burstable, not BestEffort", hit["impact"])

    def test_a_resourceless_plain_init_container_breaks_guaranteed(self):
        # Same container set, opposite direction: `app` alone would be
        # Guaranteed, but `migrate` carries no limits and every container needs
        # both for the class.
        hit = self.check(
            self.wl(
                resources={"limits": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[{"name": "migrate", "resources": {}}],
            )
        )
        self.assertIn("copies that limit into the request", hit["impact"])
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_an_extended_resource_alone_leaves_the_pod_besteffort(self):
        # Kubernetes counts cpu and memory and nothing else, so a container
        # asking only for a GPU is BestEffort -- the one arm where the check's
        # own "first evicted" is true. Calling it Burstable would state the
        # exact inverse.
        hit = self.check(self.wl(resources={"limits": {"nvidia.com/gpu": "1"}}))
        self.assertNotIn("impact", hit)

    def test_an_explicit_zero_request_leaves_the_pod_besteffort(self):
        # A quantity has to be greater than zero to count.
        self.assertNotIn("impact", self.check(self.wl(resources={"requests": {"cpu": "0"}})))
        self.assertNotIn("impact", self.check(self.wl(resources={"limits": {"memory": "0Mi"}})))

    def test_a_zero_limit_does_not_make_a_pod_guaranteed(self):
        hit = self.check(self.wl(resources={"limits": {"cpu": "1", "memory": "0"}}))
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_the_unreserved_claim_is_scoped_to_a_container_not_the_pod(self):
        # Two containers, each limiting a different resource. Both are missing
        # both requests, so the union is {cpu, memory} -- but the pod is sized
        # with cpu (from `app`) *and* memory (from `proxy`), both defaulted from
        # their limits. A pod-level "sized without cpu or memory" would be
        # flatly false; the container-scoped sentence is true.
        hit = self.check(
            self.wl(
                resources={"limits": {"cpu": "1"}},
                init_containers=[
                    {
                        "name": "proxy",
                        "restartPolicy": "Always",
                        "resources": {"limits": {"memory": "1Gi"}},
                    }
                ],
            )
        )
        self.assertIn("cpu or memory goes unreserved on at least one container", hit["impact"])
        self.assertNotIn("size this cluster without", hit["impact"])

    def test_two_spellings_of_one_quantity_fall_to_burstable(self):
        # `0.1` and `100m` are the same quantity and Kubernetes would call this
        # Guaranteed. `_qos_class` compares strings, so it says Burstable --
        # wrong, but in the direction that claims less. Pinned so a later change
        # to real quantity parsing is a deliberate one.
        self.assertEqual(
            collect._qos_class(
                [{"resources": {"requests": {"cpu": "0.1", "memory": "1Gi"}, "limits": {"cpu": "100m", "memory": "1Gi"}}}],
                {},
                "default",
            ),
            "Burstable",
        )

    def test_a_limit_covering_only_one_resource_leaves_the_other_unreserved(self):
        # A memory limit backs the memory request; CPU is backed by nothing, so
        # the sentence must name CPU and only CPU as unreserved.
        hit = self.check(self.wl(resources={"limits": {"memory": "1Gi"}}))
        self.assertIn("cpu goes unreserved", hit["impact"])
        self.assertNotIn("memory", hit["impact"].split("Burstable")[0])

    def test_the_unreserved_resources_are_named_in_sorted_order(self):
        # Both missing but a sibling container declares one, so the pod is
        # Burstable rather than BestEffort while nothing backs either resource.
        hit = self.check(
            self.wl(
                resources={},
                init_containers=[
                    {"name": "proxy", "restartPolicy": "Always", "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}}}
                ],
            )
        )
        self.assertIn("cpu or memory goes unreserved", hit["impact"])

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


def service(name, ns="default", selector=None, svc_type="ClusterIP", ports=None):
    spec = {"type": svc_type}
    if selector is not None:
        spec["selector"] = selector
    if ports is not None:
        spec["ports"] = ports
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

    def test_a_gke_managed_namespace_hpa_is_not_flagged(self):
        # GKE puts kube-state-metrics in `gke-managed-cim`. S1 keeps its
        # StatefulSet out of `workloads`, so before the HPA carried the same
        # suppression this read as a dangling target on every cluster in the
        # fleet -- 17 minor findings about objects Google owns.
        dump = dump_of(
            deployment("kube-state-metrics", ns="gke-managed-cim"),
            hpa("kube-state-metrics", ns="gke-managed-cim",
                target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "kube-state-metrics"}),
        )
        self.assertEqual(collect.check_hpa_cannot_scale(context_of(dump)), [])

    def test_a_gke_managed_namespace_pinned_hpa_is_not_flagged_either(self):
        # The pinned branch needs the suppression as much as the dangling one:
        # an addon HPA Google pinned is not the operator's to widen.
        dump = dump_of(hpa("otel", ns="gke-managed-otel", min_replicas=2, max_replicas=2))
        self.assertEqual(collect.check_hpa_cannot_scale(context_of(dump)), [])

    def test_an_addonmanager_labelled_hpa_is_not_flagged(self):
        # S2, for the addon that sits in a namespace S1 does not cover.
        h = hpa("addon", min_replicas=2, max_replicas=2)
        h["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
        self.assertEqual(collect.check_hpa_cannot_scale(context_of(dump_of(h))), [])

    def test_an_exempted_target_still_exists_so_the_hpa_is_not_dangling(self):
        # S4 takes the Deployment out of the audited set. "scaleTargetRef
        # Deployment/api not found" would be a false statement about the
        # cluster, and opting a workload out of the audit would create a
        # finding rather than remove one.
        dep = deployment("api")
        dep["metadata"]["labels"] = {collect.OPT_OUT_KEY: "exempt"}
        ctx = context_of(dump_of(dep, hpa("h")))
        self.assertEqual(ctx["workloads"], [])
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_a_scaled_to_zero_target_still_exists(self):
        # S5, the same mistake from the other side.
        ctx = context_of(dump_of(deployment("api", **{"spec.replicas": 0}), hpa("h")))
        self.assertEqual(ctx["workloads"], [])
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_a_genuinely_absent_target_is_still_dangling(self):
        # The whole point of the check survives the two fixes above.
        ctx = context_of(dump_of(deployment("other"),
                                 hpa("h", target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gone"})))
        hits = collect.check_hpa_cannot_scale(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")
        self.assertIn("Deployment/gone not found", hits[0]["excerpt"])


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

    def gateway_shaped(self, sidecar_readiness=True):
        """The live `platform-agent-gateway` shape, which this check got wrong.

        Three containers behind one Service: the app on 8642 with a probe, a
        probe-less log shipper serving nothing, and a native sidecar holding
        8643 -- the port the Service actually targets.
        """
        d = deployment("api")
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        d["spec"]["template"]["spec"]["containers"] = [
            {
                "name": "platform-agent",
                "ports": [{"containerPort": 8642, "name": "api"}],
                "readinessProbe": {"exec": {"command": ["true"]}},
            },
            {"name": "fluent-bit"},
        ]
        sidecar = {
            "name": "envoy-credential-proxy",
            "restartPolicy": "Always",
            "ports": [{"containerPort": 8765, "name": "cred-proxy"}, {"containerPort": 8643, "name": "proxy-api"}],
        }
        if sidecar_readiness:
            sidecar["readinessProbe"] = {"exec": {"command": ["true"]}}
        d["spec"]["template"]["spec"]["initContainers"] = [
            {"name": "sandbox-credential-cleanup"},
            sidecar,
        ]
        ctx = context_of(
            services={
                "default": [service("s", selector={"app": "api"}, ports=[{"port": 8642, "targetPort": 8643}])]
            }
        )
        return collect.normalize_workloads(dump_of(d))[0], ctx

    def test_a_probe_on_the_native_sidecar_holding_the_service_port_counts(self):
        """The container serving `targetPort` may be an `initContainer`.

        `initContainers` with `restartPolicy: Always` are native sidecars: they
        run for the pod's whole life and serve ports like anything else. This
        check read `containers` only, so on the gateway it saw 8643 served by
        nobody, judged the probe-less `fluent-bit` to be the workload's answer
        for readiness, and reported a Service-backed workload with no readiness
        probe -- while both the app container and the container actually behind
        the Service port had one. A false positive stated as fact about a live
        deployment, which is the kind that costs a reader the most to disprove.
        """
        workload, ctx = self.gateway_shaped()
        self.assertIsNone(collect.check_probes_readiness(workload, ctx))

    def test_the_container_behind_the_service_port_is_still_required_to_probe(self):
        """The narrowing must not amount to switching the check off.

        Same three containers, same Service; the only change is that the
        sidecar holding 8643 has no readiness probe. Traffic now reaches a
        container with no readiness signal, which is exactly what this check is
        for -- and the app container's probe two lines up must not excuse it.
        """
        workload, ctx = self.gateway_shaped(sidecar_readiness=False)
        hit = collect.check_probes_readiness(workload, ctx)
        self.assertIsNotNone(hit)
        self.assertIn("envoy-credential-proxy", hit["excerpt"])
        # And only that one: naming `fluent-bit` here is what sent a reader
        # looking at the wrong container in the first place.
        self.assertNotIn("fluent-bit", hit["excerpt"])

    def test_a_pod_that_declares_no_ports_keeps_every_container_in_the_path(self):
        """Declaring `ports` is optional, so absence is not evidence.

        kubelet routes to a `targetPort` no container ever named. With nothing
        to match on there is no routing to infer, and narrowing to the empty
        set would silently retire the check for every workload that omits the
        field -- a far bigger hole than the false positive being fixed.
        """
        d = deployment("api")
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        ctx = context_of(
            services={"default": [service("s", selector={"app": "api"}, ports=[{"port": 80, "targetPort": 8080}])]}
        )
        workload = collect.normalize_workloads(dump_of(d))[0]
        hit = collect.check_probes_readiness(workload, ctx)
        self.assertIsNotNone(hit)
        self.assertIn("app", hit["excerpt"])

    def metrics_ctx(self, ports):
        return context_of(services={"default": [service("s-metrics", selector={"app": "api"}, ports=ports)]})

    def test_a_metrics_only_service_is_named_as_such(self):
        """3.9's Impact line claims production traffic; say when there is none.

        Live case, 2026-09-01: three of the six findings this check published
        were on workloads whose only Service exposes one scrape port --
        `cert-manager` and `cert-manager-cainjector` on `http-metrics/9402`,
        `argocd-notifications-controller` on `metrics/9001`. Each shipped
        "Every rollout sends production traffic to pods that are not yet
        serving", which is not true of any of them.
        """
        hit = collect.check_probes_readiness(self.wl(), self.metrics_ctx([{"name": "http-metrics", "port": 9402}]))
        self.assertIsNotNone(hit)
        self.assertIn("s-metrics[http-metrics]", hit["excerpt"])
        self.assertIn("(metrics scrape only)", hit["excerpt"])

    def test_a_serving_port_alongside_a_metrics_port_is_still_serving(self):
        # `argocd-applicationset-controller`: `webhook/7000` and
        # `metrics/8080`. One real port is enough to make the traffic claim
        # true, so the suppression must not trigger on "contains a metrics
        # port".
        hit = collect.check_probes_readiness(
            self.wl(), self.metrics_ctx([{"name": "webhook", "port": 7000}, {"name": "metrics", "port": 8080}])
        )
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_a_second_service_that_serves_traffic_defeats_the_suppression(self):
        # `argocd-server` has both `argocd-server` (http/https) and
        # `argocd-server-metrics`. Judging the Services one at a time would
        # call the workload metrics-only on the strength of the wrong one.
        ctx = context_of(
            services={
                "default": [
                    service("s-metrics", selector={"app": "api"}, ports=[{"name": "metrics", "port": 8083}]),
                    service("s", selector={"app": "api"}, ports=[{"name": "http", "port": 80}]),
                ]
            }
        )
        hit = collect.check_probes_readiness(self.wl(), ctx)
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_an_unnamed_port_is_not_assumed_to_be_metrics(self):
        # Suppressing the impact claim wrongly is the expensive error, so an
        # unnamed port -- common on a single-port Service -- keeps the finding
        # reading exactly as it did before.
        hit = collect.check_probes_readiness(self.wl(), self.metrics_ctx([{"port": 9402}]))
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_a_service_with_no_ports_at_all_is_neither_scope(self):
        # Not metrics-only -- nothing says these pods are scraped. Not serving
        # either: a Service declaring no ports routes nothing through its own
        # ClusterIP, so "every rollout sends production traffic to pods that
        # are not yet serving" is a claim about traffic that does not exist.
        hit = collect.check_probes_readiness(self.wl(), self.metrics_ctx([]))
        self.assertIn("(no ports declared)", hit["excerpt"])
        self.assertIn("s-metrics[no ports]", hit["excerpt"])

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

    def svc_ctx(self, ports=None):
        return context_of(
            services={"default": [service("s", selector={"app": "api"}, **({"ports": ports} if ports else {}))]}
        )

    def test_a_single_replica_service_backed_deployment_is_flagged(self):
        self.assertIsNotNone(collect.check_single_replica(self.wl(), self.svc_ctx()))

    def test_a_metrics_only_service_is_named_as_such(self):
        """The same claim readiness makes, so the same qualifier.

        Live case, 2026-09-01: this check published `cert-manager` and
        `cert-manager-cainjector` as "single replica, Service-backed" -- 3.8's
        Impact line is that a rollout drops user traffic -- in the same report
        where the readiness check had already said the only Service in front of
        each is `http-metrics/9402`. Giving one check the exposure line and not
        the other is how the report contradicted itself.
        """
        hit = collect.check_single_replica(self.wl(), self.svc_ctx([{"name": "http-metrics", "port": 9402}]))
        self.assertIsNotNone(hit)
        self.assertIn("s[http-metrics]", hit["excerpt"])
        self.assertIn("(metrics scrape only)", hit["excerpt"])

    def test_a_serving_port_makes_the_traffic_claim_true(self):
        hit = collect.check_single_replica(self.wl(), self.svc_ctx([{"name": "http", "port": 80}]))
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_an_unnamed_port_is_not_assumed_to_be_metrics(self):
        # `kube-agents-webhook-service` is an unnamed 443 to an admission
        # webhook -- traffic, and the finding has to keep saying so.
        hit = collect.check_single_replica(self.wl(), self.svc_ctx([{"port": 443, "targetPort": 10250}]))
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_a_service_with_no_ports_at_all_is_neither_scope(self):
        # The readiness sibling's reason, and the same trap: 3.11's Impact line
        # is a full outage for "this service", which a Service exposing no port
        # cannot have.
        hit = collect.check_single_replica(self.wl(), self.svc_ctx())
        self.assertIn("(no ports declared)", hit["excerpt"])

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
        # What a proxy truncation looks like: valid JSON up to a point,
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

    def test_two_same_named_clusters_in_two_projects_write_two_files(self):
        """The design's thread-safety rule is that a worker writes only to
        paths keyed by its own cluster, and named this file as the case no two
        threads can collide on. A cluster name is unique within a project, not
        across the eight this collector runs at once: two clusters called
        `prod` wrote one path, and the loser re-read the winner's dump — one
        cluster's workloads published under the other's name."""

        def run(argv, **kwargs):
            return Run(argv, 0, json.dumps({"items": []}), "", 0.05)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "SCRATCH_DIR", tmp):
                a, _, _ = collect.dump_state(Path(tmp) / "kc.yaml", "prod", project="acme-a", location="us-central1", run=run)
                b, _, _ = collect.dump_state(Path(tmp) / "kc.yaml", "prod", project="acme-b", location="us-central1", run=run)
        self.assertNotEqual(a, b)


class TestCollectCluster(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def collect(self, dump_items, cred_rc=0, dump_rc=0, checks=None, cluster=None):
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
                    cluster or self.CLUSTER, "obtainability-audit",
                    checks or collect.OBTAINABILITY_CHECKS, run=run
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

    def test_every_outcome_publishes_the_mode(self):
        # Six SOPs branch on Autopilot and one keys its cohorts on it, so a
        # manifest that withholds the mode sends the model back to
        # `clusters list` for a fact the collector already computed. It rides
        # on every shape, not just `collected`: a mode is a property of the
        # cluster, not of whether this run managed to read inside it.
        for kwargs, outcome in (
            ({}, "collected"),
            ({"cred_rc": 1}, "unreachable"),
            ({"dump_rc": 1}, "gate-failed"),
        ):
            for mode in (True, False):
                with self.subTest(outcome=outcome, autopilot=mode):
                    result, _ = self.collect(
                        [deployment("api")],
                        cluster={**self.CLUSTER, "autopilot": mode},
                        **kwargs,
                    )
                    self.assertEqual(result["outcome"], outcome)
                    self.assertIs(result["autopilot"], mode)

    def test_a_cluster_that_never_ran_still_publishes_the_mode(self):
        entry = collect.not_running_entry(
            {"name": "dr-west", "location": "us-west1", "status": "DEGRADED",
             "autopilot": {"enabled": True}},
            "acme",
        )
        self.assertEqual(entry["outcome"], "unreachable")
        self.assertIs(entry["autopilot"], True)
        # Absent `autopilot` in the gcloud payload means Standard, not unknown.
        self.assertIs(
            collect.not_running_entry({"name": "c", "status": "STOPPING"}, "acme")["autopilot"],
            False,
        )

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
                result = collect.collect_cluster(cluster, "obtainability-audit", collect.OBTAINABILITY_CHECKS, run=run)
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

    def test_a_non_running_cluster_is_recorded_rather_than_audited(self):
        # No check runs against it -- a STOPPING cluster has no API server to
        # read. But it stays in the manifest as a target the document has to
        # account for: dropped entirely it reads exactly like a cluster that
        # does not exist, and the run publishes a fleet-wide verdict over a
        # fleet quietly one cluster short.
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
        outcomes = {c["name"]: c["outcome"] for c in manifest["clusters"]}
        self.assertEqual(outcomes, {"c1": "collected", "stopping": "unreachable"})
        stopping = next(c for c in manifest["clusters"] if c["name"] == "stopping")
        self.assertIn("STOPPING", stopping["error"])
        self.assertNotIn("commands", stopping)

    def test_one_cluster_crashing_costs_that_cluster_and_no_other(self):
        """`future.result()` re-raises, and every SOP redirects this
        collector's stdout into the manifest — so an unmodelled exception on
        one cluster used to leave a zero-byte file and lose the whole fleet.
        Only `GateFailure` was modelled; a `TypeError` off an unexpected API
        shape was not."""
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "boom", "location": "us-central1", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            if "list" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                if any("boom" in str(v) for v in kwargs.get("env", {}).values()):
                    raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")
                return Run(argv, 0, json.dumps(dump_of()), "", 0.05)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("obtainability-audit", "acme", run=run)

        outcomes = {c["name"]: c["outcome"] for c in manifest["clusters"]}
        self.assertEqual(outcomes, {"c1": "collected", "boom": "gate-failed"})
        boom = next(c for c in manifest["clusters"] if c["name"] == "boom")
        self.assertIn("TypeError", boom["error"])

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

    def test_compliance_audits_full_roster_also_verifies_through_collect_fleet(self):
        # The multi-source builder is the part obtainability's version of
        # this test cannot cover -- five distinct collection commands,
        # cross-referenced, assembled by collect_fleet's outer enumeration
        # and thread pool, not just one cluster's collect_cluster call.
        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"] and argv[2] == collect.COMPLIANCE_DUMP_KINDS:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return Run(argv, 0, json.dumps({"workloadIdentityConfig": {"workloadPool": "x"}}), "", 0.1)
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return Run(argv, 0, "[]", "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("compliance-audit", "acme", run=run)

        checks_run = [{"check": c.slug, "command": "x"} for c in collect.COMPLIANCE_CHECKS]
        doc = {"audit": "compliance-audit", "scope": {"clusters": [{"name": "c1", "checks_run": checks_run}]}}
        audit_report.cross_check_manifest(doc, manifest)  # must not raise

    def test_ai_security_audits_full_roster_also_verifies_through_collect_fleet(self):
        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("ai-security-audit", "acme", run=run)

        checks_run = [{"check": c.slug, "command": "x"} for c in collect.AI_SECURITY_CHECKS]
        doc = {"audit": "ai-security-audit", "scope": {"clusters": [{"name": "c1", "checks_run": checks_run}]}}
        audit_report.cross_check_manifest(doc, manifest)  # must not raise


# --------------------------------------------------------------------------- #
# compliance-audit
# --------------------------------------------------------------------------- #


def compliance_pod(name, ns="default", **meta_overrides):
    """A bare Pod — compliance's dump includes these (unlike obtainability's,
    which reads templates only). `pod_spec_of()` gives a mutable reference
    into the container list for tests that need to set securityContext etc.
    """
    doc = {
        "kind": "Pod",
        "metadata": {"namespace": ns, "name": name, "labels": {}, "annotations": {}},
        "spec": {"containers": [{"name": "app", "securityContext": {}}]},
    }
    doc["metadata"].update(meta_overrides)
    return doc


def compliance_workload(kind, name, ns="default"):
    """A Deployment/StatefulSet/DaemonSet/CronJob wrapping the same pod spec
    shape `compliance_pod` uses, nested at the depth compliance's
    `_pod_spec_of` expects for that kind."""
    pod_spec = {"containers": [{"name": "app", "securityContext": {}}]}
    if kind == "CronJob":
        spec = {"jobTemplate": {"spec": {"template": {"spec": pod_spec}}}}
    else:
        spec = {"template": {"spec": pod_spec}}
    return {"kind": kind, "metadata": {"namespace": ns, "name": name, "labels": {}, "annotations": {}}, "spec": spec}


def pod_spec_of(doc):
    return collect._pod_spec_of(doc)


def crb(name, subjects, role="cluster-admin"):
    return {"kind": "ClusterRoleBinding", "metadata": {"name": name}, "roleRef": {"kind": "ClusterRole", "name": role}, "subjects": subjects}


def subject(kind, name, ns=None):
    d = {"kind": kind, "name": name}
    if ns is not None:
        d["namespace"] = ns
    return d


def cluster_role(name, rules, ns=None, labels=None):
    doc = {"kind": "ClusterRole" if ns is None else "Role", "metadata": {"name": name, "labels": labels or {}}, "rules": rules}
    if ns is not None:
        doc["metadata"]["namespace"] = ns
    return doc


def role_binding(role_kind, role_name, subjects, ns=None):
    doc = {
        "kind": "ClusterRoleBinding" if ns is None else "RoleBinding",
        "metadata": {"name": f"{role_name}-binding"},
        "roleRef": {"kind": role_kind, "name": role_name},
        "subjects": subjects,
    }
    if ns is not None:
        doc["metadata"]["namespace"] = ns
    return doc


def netpol(name, ns="default", pod_selector=None, ingress=None, policy_types=None):
    spec = {"podSelector": pod_selector if pod_selector is not None else {}}
    if ingress is not None:
        spec["ingress"] = ingress
    if policy_types is not None:
        spec["policyTypes"] = policy_types
    return {"kind": "NetworkPolicy", "metadata": {"namespace": ns, "name": name}, "spec": spec}


def namespace(name, labels=None):
    return {"kind": "Namespace", "metadata": {"name": name, "labels": labels or {}}}


def netpol_pod(name, ns="default", labels=None, phase="Running"):
    """A `context["pods"]` entry -- the pod-label view §2.6 needs to ask which
    pods a namespace's policies actually select."""
    return {"ns": ns, "name": name, "labels": labels or {}, "phase": phase}


def ccnp(name, selector=None, ingress=None):
    """A Dataplane V2 ClusterNetworkPolicy. Defaults to the shape that
    suppresses §2.6 everywhere: every endpoint, ingress-isolating."""
    spec = {"endpointSelector": selector if selector is not None else {}, "ingress": ingress if ingress is not None else []}
    if not spec["ingress"]:
        spec["ingress"] = [{"fromEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system"}}]}]
    return {"kind": "ClusterNetworkPolicy", "metadata": {"name": name}, "spec": spec}


def default_sa(ns, automount=None):
    doc = {"kind": "ServiceAccount", "metadata": {"namespace": ns, "name": "default"}}
    if automount is not None:
        doc["automountServiceAccountToken"] = automount
    return doc


class TestComplianceNormalize(unittest.TestCase):
    def test_a_bare_unowned_pod_is_included(self):
        out = collect.normalize_compliance_workloads(dump_of(compliance_pod("standalone")))
        self.assertEqual(len(out), 1)

    def test_an_owned_pod_is_excluded_audit_the_controller_instead(self):
        pod = compliance_pod("api-abc123")
        pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "api"}]
        self.assertEqual(collect.normalize_compliance_workloads(dump_of(pod)), [])

    def test_a_deployment_template_is_included(self):
        out = collect.normalize_compliance_workloads(dump_of(compliance_workload("Deployment", "api")))
        self.assertEqual(len(out), 1)
        self.assertIn("containers", out[0]["spec"])

    def test_a_cronjob_resolves_two_levels_deep(self):
        out = collect.normalize_compliance_workloads(dump_of(compliance_workload("CronJob", "job")))
        self.assertEqual(out[0]["spec"]["containers"][0]["name"], "app")

    def test_system_namespace_is_excluded(self):
        self.assertEqual(
            collect.normalize_compliance_workloads(dump_of(compliance_pod("x", ns="kube-system"))), []
        )

    def test_kubeagents_system_is_not_suppressed(self):
        # The harness audits itself -- unlike obtainability's suppression
        # list, compliance deliberately leaves this one unfiltered.
        out = collect.normalize_compliance_workloads(dump_of(compliance_pod("x", ns="kubeagents-system")))
        self.assertEqual(len(out), 1)

    def test_a_gke_addon_is_excluded(self):
        pod = compliance_pod("x")
        pod["metadata"]["labels"]["addonmanager.kubernetes.io/mode"] = "Reconcile"
        self.assertEqual(collect.normalize_compliance_workloads(dump_of(pod)), [])


class TestPrivilegedContainer(unittest.TestCase):
    def wl(self):
        return collect.normalize_compliance_workloads(dump_of(compliance_pod("x")))[0]

    def test_privileged_true_is_flagged(self):
        d = compliance_pod("x")
        pod_spec_of(d)["containers"][0]["securityContext"] = {"privileged": True}
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNotNone(collect.check_privileged_container(wl, context_of()))

    def test_sys_admin_capability_is_flagged(self):
        d = compliance_pod("x")
        pod_spec_of(d)["containers"][0]["securityContext"] = {"capabilities": {"add": ["SYS_ADMIN"]}}
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNotNone(collect.check_privileged_container(wl, context_of()))

    def test_allow_privilege_escalation_alone_is_never_flagged(self):
        d = compliance_pod("x")
        pod_spec_of(d)["containers"][0]["securityContext"] = {"allowPrivilegeEscalation": True}
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNone(collect.check_privileged_container(wl, context_of()))

    def test_a_plain_container_is_not_flagged(self):
        self.assertIsNone(collect.check_privileged_container(self.wl(), context_of()))


class TestHostNamespace(unittest.TestCase):
    def wl(self, **spec_overrides):
        d = compliance_pod("x")
        d["spec"].update(spec_overrides)
        return collect.normalize_compliance_workloads(dump_of(d))[0]

    def test_host_pid_is_critical(self):
        hit = collect.check_host_namespace(self.wl(hostPID=True), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_host_ipc_is_critical(self):
        hit = collect.check_host_namespace(self.wl(hostIPC=True), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_host_network_alone_is_major(self):
        hit = collect.check_host_namespace(self.wl(hostNetwork=True), context_of())
        self.assertEqual(hit["severity"], "major")

    def test_none_set_is_not_flagged(self):
        self.assertIsNone(collect.check_host_namespace(self.wl(), context_of()))

    def ds(self, host_network, host_port=None):
        doc = compliance_workload("DaemonSet", "cni-agent")
        spec = pod_spec_of(doc)
        spec["hostNetwork"] = host_network
        if host_port is not None:
            spec["containers"][0]["ports"] = [{"hostPort": host_port}]
        return collect.normalize_compliance_workloads(dump_of(doc))[0]

    def test_ingress_daemonset_with_hostnetwork_and_hostport_is_downgraded_to_minor(self):
        hit = collect.check_host_namespace(self.ds(True, host_port=443), context_of())
        self.assertEqual(hit["severity"], "minor")

    def test_daemonset_with_hostnetwork_but_no_hostport_stays_major(self):
        hit = collect.check_host_namespace(self.ds(True), context_of())
        self.assertEqual(hit["severity"], "major")

    def test_non_daemonset_with_hostnetwork_and_hostport_stays_major(self):
        hit = collect.check_host_namespace(self.wl(hostNetwork=True), context_of())
        self.assertEqual(hit["severity"], "major")


class TestHostpathMount(unittest.TestCase):
    def wl(self, path, ro, mount_name="hostvol"):
        d = compliance_pod("x")
        d["spec"]["volumes"] = [{"name": mount_name, "hostPath": {"path": path}}]
        d["spec"]["containers"][0]["volumeMounts"] = [{"name": mount_name, "readOnly": ro}]
        return collect.normalize_compliance_workloads(dump_of(d))[0]

    def test_root_path_is_critical(self):
        hit = collect.check_hostpath_mount(self.wl("/", True), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_docker_socket_is_critical(self):
        hit = collect.check_hostpath_mount(self.wl("/var/run/docker.sock", True), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_a_writable_mount_is_critical_regardless_of_path(self):
        hit = collect.check_hostpath_mount(self.wl("/data", False), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_a_readonly_non_sensitive_path_is_major(self):
        hit = collect.check_hostpath_mount(self.wl("/data", True), context_of())
        self.assertEqual(hit["severity"], "major")

    def test_a_declared_but_unmounted_hostpath_is_never_flagged(self):
        d = compliance_pod("x")
        d["spec"]["volumes"] = [{"name": "v", "hostPath": {"path": "/"}}]
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNone(collect.check_hostpath_mount(wl, context_of()))

    def test_var_lib_kubelet_is_critical(self):
        hit = collect.check_hostpath_mount(self.wl("/var/lib/kubelet/pods", True), context_of())
        self.assertEqual(hit["severity"], "critical")


class TestClusterAdminBinding(unittest.TestCase):
    def test_a_non_system_service_account_is_critical(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("ServiceAccount", "app", "default")])])
        hits = collect.check_cluster_admin_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_a_system_masters_group_is_never_flagged(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("Group", "system:masters")])])
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])

    def test_a_kube_system_service_account_is_never_flagged(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("ServiceAccount", "x", "kube-system")])])
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])

    def test_a_google_managed_service_account_email_is_never_flagged(self):
        ctx = context_of(
            clusterrolebindings=[crb("b", [subject("User", "sa@my-project.iam.gserviceaccount.com")])]
        )
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])

    def test_an_org_email_group_is_downgraded_to_minor(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("Group", "platform-admins@acme.com")])])
        hits = collect.check_cluster_admin_binding(ctx)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_a_binding_to_a_different_role_is_never_flagged(self):
        ctx = context_of(
            clusterrolebindings=[crb("b", [subject("ServiceAccount", "app", "default")], role="edit")]
        )
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])


class TestWildcardRbac(unittest.TestCase):
    WILDCARD_RULE = [{"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}]

    def test_a_bound_clusterrole_wildcard_is_critical(self):
        ctx = context_of(
            roles=[cluster_role("god-mode", self.WILDCARD_RULE)],
            clusterrolebindings=[role_binding("ClusterRole", "god-mode", [subject("ServiceAccount", "app", "default")])],
        )
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_a_bound_namespaced_role_wildcard_is_major(self):
        ctx = context_of(
            roles=[cluster_role("god-mode", self.WILDCARD_RULE, ns="default")],
            rolebindings=[role_binding("Role", "god-mode", [subject("ServiceAccount", "app", "default")], ns="default")],
        )
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(hits[0]["severity"], "major")

    def test_enumerated_verbs_over_a_wildcard_scope_are_still_an_escalation(self):
        """Spelling the verbs out is not a boundary, and the miss was live.

        `ClusterRole/argocd-server` on the reference fleet holds
        `apiGroups: ["*"], resources: ["*"], verbs: ["delete","get","patch"]`,
        bound to `ServiceAccount/argocd/argocd-server`, and graded clean because
        the predicate required a `*` in `verbs`. `get` on every resource in
        every group is every Secret in every namespace; `patch` on every
        resource rewrites a Deployment into a privileged pod.
        """
        ctx = context_of(
            roles=[cluster_role("argocd-server", [
                {"verbs": ["delete", "get", "patch"], "resources": ["*"], "apiGroups": ["*"]}
            ])],
            clusterrolebindings=[role_binding(
                "ClusterRole", "argocd-server",
                [subject("ServiceAccount", "argocd-server", "argocd")],
            )],
        )
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_a_read_only_wildcard_scope_is_left_alone(self):
        """The control, and the reason the new branch names its verbs.

        `apiGroups: ["*"], resources: ["*"], verbs: ["get","list","watch"]` is
        the ordinary cluster-monitoring shape -- a scraper, a backup agent --
        and grading every one of those critical is the false-positive flood this
        audit has already paid for once. Reading every Secret in the fleet is a
        real concern; it needs a check that can tell a scraper from an
        escalation, and this is not that check.
        """
        ctx = context_of(
            roles=[cluster_role("scraper", [
                {"verbs": ["get", "list", "watch"], "resources": ["*"], "apiGroups": ["*"]}
            ])],
            clusterrolebindings=[role_binding(
                "ClusterRole", "scraper",
                [subject("ServiceAccount", "prometheus", "monitoring")],
            )],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_enumerated_verbs_under_one_vendor_group_stay_suppressed(self):
        # The new branch requires `apiGroups == ["*"]`, so the operator-owns-its
        # -own-CRDs pattern keeps the exception it already had.
        ctx = context_of(
            roles=[cluster_role("cnrm-admin", [
                {"verbs": ["create", "patch"], "resources": ["*"], "apiGroups": ["cnrm.cloud.google.com"]}
            ])],
            clusterrolebindings=[role_binding(
                "ClusterRole", "cnrm-admin",
                [subject("ServiceAccount", "cnrm", "cnrm-system")],
            )],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_an_unbound_wildcard_role_is_never_flagged(self):
        ctx = context_of(roles=[cluster_role("god-mode", self.WILDCARD_RULE)])
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_a_bootstrapping_default_role_is_never_flagged(self):
        ctx = context_of(
            roles=[cluster_role("x", self.WILDCARD_RULE, labels={"kubernetes.io/bootstrapping": "rbac-defaults"})],
            clusterrolebindings=[role_binding("ClusterRole", "x", [subject("ServiceAccount", "app", "default")])],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_a_vendor_apigroup_wildcard_is_never_flagged(self):
        rule = [{"verbs": ["*"], "resources": ["*"], "apiGroups": ["kubeagents.io"]}]
        ctx = context_of(
            roles=[cluster_role("operator", rule)],
            clusterrolebindings=[role_binding("ClusterRole", "operator", [subject("ServiceAccount", "app", "default")])],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_a_core_group_wildcard_is_never_suppressed(self):
        rule = [{"verbs": ["*"], "resources": ["*"], "apiGroups": [""]}]
        ctx = context_of(
            roles=[cluster_role("core-god", rule)],
            clusterrolebindings=[role_binding("ClusterRole", "core-god", [subject("ServiceAccount", "app", "default")])],
        )
        self.assertEqual(len(collect.check_wildcard_rbac(ctx)), 1)

    # The live shape: GKE's `kubelet-api-admin`, bound to the API server's own
    # user so it can reach kubelets. Neither of the check's own two
    # suppressions sees it -- the label is not `rbac-defaults` and the name has
    # no `system:` prefix -- so before S2 it was one `critical` per cluster.
    GKE_ADDON = {"addonmanager.kubernetes.io/mode": "Reconcile"}

    def test_a_gke_managed_addon_role_is_never_flagged(self):
        ctx = context_of(
            roles=[cluster_role("kubelet-api-admin", self.WILDCARD_RULE, labels=self.GKE_ADDON)],
            clusterrolebindings=[
                role_binding("ClusterRole", "kubelet-api-admin", [subject("User", "kube-apiserver")])
            ],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_the_addon_suppression_keys_on_the_label_and_not_the_name(self):
        ctx = context_of(
            roles=[cluster_role("kubelet-api-admin", self.WILDCARD_RULE)],
            clusterrolebindings=[
                role_binding("ClusterRole", "kubelet-api-admin", [subject("User", "kube-apiserver")])
            ],
        )
        self.assertEqual(len(collect.check_wildcard_rbac(ctx)), 1)


class TestNetpolMissing(unittest.TestCase):
    def test_zero_policies_with_workloads_is_major(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_the_excerpt_never_claims_the_cluster_has_no_policies(self):
        # kube-agents-host has eleven NetworkPolicies and none in cert-manager.
        # The excerpt is rendered under a cluster-wide `-A` command, so a bare
        # "zero NetworkPolicies" is a false statement beside a true finding.
        ctx = context_of(
            namespaces=[namespace("cert-manager"), namespace("argocd")],
            networkpolicies=[netpol("deny", ns="argocd", policy_types=["Ingress"])],
            workloads=[
                {"kind": "Pod", "ns": "cert-manager", "name": "webhook"},
                {"kind": "Pod", "ns": "argocd", "name": "server"},
            ],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual([h["namespace"] for h in hits], ["cert-manager"])
        self.assertEqual(
            hits[0]["excerpt"],
            "no NetworkPolicy in this namespace; 1 in other namespaces of this cluster",
        )
        # The count carries no verdict: it tallies allow-all and
        # system-namespace policies too, so it cannot support a claim that
        # this namespace is the only gap.
        self.assertNotIn("the gap", hits[0]["excerpt"])

    def test_a_cluster_with_no_policies_at_all_says_only_the_namespace_part(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        self.assertEqual(collect.check_netpol_missing(ctx)[0]["excerpt"], "no NetworkPolicy in this namespace")

    def test_zero_policies_and_zero_workloads_is_not_flagged(self):
        ctx = context_of(namespaces=[namespace("empty")], networkpolicies=[], workloads=[])
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_allow_all_policy_is_minor(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("allow-all", ns="payments", ingress=[{}])],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")
        self.assertIn("NetworkPolicy/allow-all", hits[0]["object"])

    def test_a_real_default_deny_policy_is_never_flagged(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("deny", ns="payments", policy_types=["Ingress"])],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_system_namespace_is_never_flagged(self):
        ctx = context_of(namespaces=[namespace("kube-system")], networkpolicies=[], workloads=[])
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_cluster_network_policy_suppresses_zero_policy_namespaces(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
            cluster_network_policies=[ccnp("fleet-wide")],
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_narrow_cluster_policy_does_not_suppress_every_namespace(self):
        """The suppression was `bool(cluster_network_policies)`: one policy
        anywhere silenced §2.6 across the whole cluster. GKE ships Dataplane V2
        policies of its own, so a cluster could report no default-allow
        namespaces on the strength of a policy selecting one workload's
        labels."""
        ctx = context_of(
            namespaces=[namespace("payments"), namespace("shop")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}, {"kind": "Pod", "ns": "shop", "name": "web"}],
            cluster_network_policies=[ccnp("just-shop", selector={"matchLabels": {"k8s:io.kubernetes.pod.namespace": "shop"}})],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual([h["namespace"] for h in hits], ["payments"])

    def test_a_cluster_policy_selecting_pod_labels_suppresses_nothing(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
            cluster_network_policies=[ccnp("by-app", selector={"matchLabels": {"app": "api"}})],
        )
        self.assertEqual(len(collect.check_netpol_missing(ctx)), 1)

    def test_an_egress_only_cluster_policy_suppresses_nothing(self):
        """§2.6 asks who can reach these pods. Cilium isolates ingress only for
        a policy carrying an `ingress` section, so an egress-only cluster
        policy leaves the namespace exactly as reachable as it was."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
            cluster_network_policies=[{"kind": "ClusterNetworkPolicy", "metadata": {"name": "egress"}, "spec": {"endpointSelector": {}, "egress": [{}]}}],
        )
        self.assertEqual(len(collect.check_netpol_missing(ctx)), 1)

    def test_no_cluster_network_policy_still_flags_zero_policy_namespaces(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
            cluster_network_policies=[],
        )
        self.assertEqual(len(collect.check_netpol_missing(ctx)), 1)

    def test_a_namespace_whose_only_pods_are_controller_owned_is_still_flagged(self):
        """The defect this check spent every run not finding.

        `normalize_compliance_workloads` drops a Pod with `ownerReferences`, so
        a namespace running nothing but a Deployment's pods reaches the check
        with no Pod-kind workload at all. Reading exposure off that set made it
        "zero workloads, pure churn" and skipped the namespace -- which is the
        ordinary namespace, and the one §2.6 exists to report.
        """
        ctx = context_of(
            namespaces=[namespace("cert-manager")],
            networkpolicies=[],
            workloads=[{"kind": "Deployment", "ns": "cert-manager", "name": "cert-manager"}],
            pod_namespaces={"cert-manager"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_a_namespace_with_a_workload_but_no_running_pod_is_not_flagged(self):
        """The other side of it: §2.6's test is `get pods … | wc -l`, so a
        Deployment scaled to zero is not exposure. Counting workloads instead
        of pods would fix the case above by flagging this one."""
        ctx = context_of(
            namespaces=[namespace("dormant")],
            networkpolicies=[],
            workloads=[{"kind": "Deployment", "ns": "dormant", "name": "batch"}],
            pod_namespaces=set(),
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_pod_no_policy_selects_is_flagged_even_where_policies_exist(self):
        """NetworkPolicy is additive and pod-scoped, so "this namespace has a
        policy" and "this pod has a policy" are different facts and only the
        second decides exposure. Deciding coverage per namespace let a pod
        selected by nothing sit behind a namespace that graded clean --
        `kubeagents-system` on the reference fleet, whose four policies name
        four workloads and leave the operator's manager pod reachable.
        """
        ctx = context_of(
            namespaces=[namespace("kubeagents-system")],
            networkpolicies=[netpol("litellm", ns="kubeagents-system", pod_selector={"matchLabels": {"app": "litellm"}}, policy_types=["Ingress"])],
            pods=[
                netpol_pod("litellm-7bcc-9rjvl", ns="kubeagents-system", labels={"app": "litellm"}),
                netpol_pod("kube-agents-controller-manager-764d-46gl4", ns="kubeagents-system", labels={"app.kubernetes.io/name": "kube-agents-operator"}),
            ],
            pod_namespaces={"kubeagents-system"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")
        self.assertEqual(hits[0]["object"], "Namespace/kubeagents-system")
        self.assertIn("1 of 2 pods", hits[0]["excerpt"])

    def test_the_excerpt_names_the_workload_and_the_object_stays_the_namespace(self):
        """A pod name carries a ReplicaSet hash and a random suffix. The ledger
        keys on the object, so a pod-scoped finding would resolve and re-raise
        on every rollout; the volatile name belongs in the excerpt, and even
        there the conventional label is the better identifier."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"])],
            pods=[netpol_pod("web-6f8d9c4b5-xk2mn", ns="payments", labels={"app.kubernetes.io/name": "web"})],
            pod_namespaces={"payments"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(hits[0]["object"], "Namespace/payments")
        self.assertIn("web", hits[0]["excerpt"])
        self.assertNotIn("6f8d9c4b5", hits[0]["excerpt"])

    def test_a_namespace_whose_every_pod_is_selected_is_left_alone(self):
        """argocd on the reference fleet: seven pods, seven policies, each
        naming its own workload. The check has to stay silent there or it
        becomes the false-positive flood instead of the missing finding."""
        ctx = context_of(
            namespaces=[namespace("argocd")],
            networkpolicies=[
                netpol("server", ns="argocd", pod_selector={"matchLabels": {"app.kubernetes.io/name": "argocd-server"}}, policy_types=["Ingress"]),
                netpol("redis", ns="argocd", pod_selector={"matchLabels": {"app.kubernetes.io/name": "argocd-redis"}}, policy_types=["Ingress"]),
            ],
            pods=[
                netpol_pod("argocd-server-687f-z89zg", ns="argocd", labels={"app.kubernetes.io/name": "argocd-server"}),
                netpol_pod("argocd-redis-79db-g8xhl", ns="argocd", labels={"app.kubernetes.io/name": "argocd-redis"}),
            ],
            pod_namespaces={"argocd"},
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_egress_only_policy_is_not_ingress_coverage(self):
        """§2.6 asks who can reach these pods. A policy whose `policyTypes` is
        Egress alone leaves its own pods exactly as reachable as they were."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("egress", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Egress"])],
            pods=[netpol_pod("api-1", ns="payments", labels={"app": "api"})],
            pod_namespaces={"payments"},
        )
        self.assertEqual(len(collect.check_netpol_missing(ctx)), 1)

    def test_an_absent_policy_types_still_counts_as_ingress_coverage(self):
        """Kubernetes derives `policyTypes` from the rule blocks present, and
        a spec with neither derives to `["Ingress"]` -- a deny-all, the
        strongest coverage there is. Absent must not read as egress-only."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("deny", ns="payments", pod_selector={"matchLabels": {"app": "api"}})],
            pods=[netpol_pod("api-1", ns="payments", labels={"app": "api"})],
            pod_namespaces={"payments"},
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_finished_job_pod_is_neither_a_gap_nor_a_denominator(self):
        """`kubeagents-system` carries three Failed CronJob pods. A pod that is
        not running cannot be reached, so it is not exposure -- and its name is
        the churniest of all, one per schedule tick."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"])],
            pods=[
                netpol_pod("api-1", ns="payments", labels={"app": "api"}),
                netpol_pod("batch-29803800-892cg", ns="payments", labels={"job-name": "batch"}, phase="Failed"),
                netpol_pod("batch-29803860-ct6br", ns="payments", labels={"job-name": "batch"}, phase="Succeeded"),
            ],
            pod_namespaces={"payments"},
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_allow_all_alongside_a_real_policy_does_cover_every_pod(self):
        """The two branches have to compose. `podSelector: {}` selects every
        pod in the namespace, so a namespace holding one is never a coverage
        gap -- it is the `minor` allow-all finding, and only when that is all
        it holds."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[
                netpol("allow-all", ns="payments", ingress=[{}]),
                netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"]),
            ],
            pods=[netpol_pod("web-1", ns="payments", labels={"app": "web"})],
            pod_namespaces={"payments"},
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_cluster_network_policy_suppresses_a_partial_gap_too(self):
        """The Do-NOT-flag case does not stop applying because the namespace
        also has a namespaced policy of its own."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"])],
            pods=[netpol_pod("web-1", ns="payments", labels={"app": "web"})],
            pod_namespaces={"payments"},
            cluster_network_policies=[ccnp("fleet-wide")],
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_unlabelled_pod_is_uncovered_and_named_by_its_pod_name(self):
        """Nothing but `podSelector: {}` can select a pod with no labels, so it
        is genuinely uncovered, and there is no workload label to name it by."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"])],
            pods=[netpol_pod("bare", ns="payments", labels={})],
            pod_namespaces={"payments"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("bare", hits[0]["excerpt"])


class TestDefaultSaAutomount(unittest.TestCase):
    def test_default_sa_with_no_override_is_flagged(self):
        ctx = context_of(
            serviceaccounts=[default_sa("default")],
            workloads=[{"kind": "Pod", "ns": "default", "name": "api", "spec": {}}],
        )
        self.assertEqual(len(collect.check_default_sa_automount(ctx)), 1)

    def test_a_dedicated_service_account_is_never_flagged(self):
        ctx = context_of(
            serviceaccounts=[default_sa("default")],
            workloads=[{"kind": "Pod", "ns": "default", "name": "api", "spec": {"serviceAccountName": "api-sa"}}],
        )
        self.assertEqual(collect.check_default_sa_automount(ctx), [])

    def test_the_namespace_default_sa_disabling_automount_suppresses_it(self):
        ctx = context_of(
            serviceaccounts=[default_sa("default", automount=False)],
            workloads=[{"kind": "Pod", "ns": "default", "name": "api", "spec": {}}],
        )
        self.assertEqual(collect.check_default_sa_automount(ctx), [])

    def test_the_pod_level_override_suppresses_it_even_if_the_sa_does_not(self):
        ctx = context_of(
            serviceaccounts=[default_sa("default")],
            workloads=[
                {"kind": "Pod", "ns": "default", "name": "api", "spec": {"automountServiceAccountToken": False}}
            ],
        )
        self.assertEqual(collect.check_default_sa_automount(ctx), [])


class TestWorkloadIdentityOff(unittest.TestCase):
    def test_empty_workload_pool_is_flagged(self):
        ctx = context_of(cluster_describe={"workloadIdentityConfig": {}})
        self.assertEqual(len(collect.check_workload_identity_off(ctx)), 1)

    def test_a_set_workload_pool_is_never_flagged(self):
        ctx = context_of(cluster_describe={"workloadIdentityConfig": {"workloadPool": "acme.svc.id.goog"}})
        self.assertEqual(collect.check_workload_identity_off(ctx), [])


class TestClusterScopedObject(unittest.TestCase):
    """The object of a cluster-scoped finding is `Cluster/<name>`, never `Cluster`.

    Both checks below emitted the bare kind until 2026-08-29. The finding id
    derives from `object`, so the day the collector started supplying it the
    compliance ledger announced four unchanged public control planes as
    resolved and re-opened them as new.
    """

    def test_both_cluster_scoped_checks_name_the_cluster(self):
        cases = (
            (
                collect.check_workload_identity_off,
                {"workloadIdentityConfig": {}},
            ),
            (
                collect.check_public_control_plane,
                {"privateClusterConfig": {}, "masterAuthorizedNetworksConfig": {}},
            ),
        )
        for check, describe in cases:
            with self.subTest(check=check.__name__):
                ctx = context_of(cluster_describe=describe)
                ctx["cluster_name"] = "kube-agents-host"
                (hit,) = check(ctx)
                self.assertEqual(hit["object"], "Cluster/kube-agents-host")

    def test_a_context_with_no_cluster_name_fails_the_cluster_closed(self):
        # Rather than emitting a nameless object that `audit_report` would
        # refuse at publish time, fifty minutes later.
        ctx = context_of(cluster_describe={"workloadIdentityConfig": {}})
        ctx["cluster_name"] = ""
        with self.assertRaises(collect.GateFailure):
            collect.check_workload_identity_off(ctx)

    def test_the_real_context_builder_supplies_it(self):
        # `context_of` is a test double; the assertion above is only worth
        # anything if the production builder sets the same key.
        source = inspect.getsource(collect._collect_compliance)
        self.assertIn('"cluster_name": name', source)


class TestLegacyMetadata(unittest.TestCase):
    def test_gce_metadata_mode_is_flagged(self):
        ctx = context_of(node_pools=[{"name": "pool-1", "config": {"workloadMetadataConfig": {"mode": "GCE_METADATA"}}}])
        self.assertEqual(len(collect.check_legacy_metadata(ctx)), 1)

    def test_empty_mode_is_flagged(self):
        ctx = context_of(node_pools=[{"name": "pool-1", "config": {}}])
        self.assertEqual(len(collect.check_legacy_metadata(ctx)), 1)

    def test_gke_metadata_mode_is_never_flagged(self):
        ctx = context_of(node_pools=[{"name": "pool-1", "config": {"workloadMetadataConfig": {"mode": "GKE_METADATA"}}}])
        self.assertEqual(collect.check_legacy_metadata(ctx), [])


class TestPublicControlPlane(unittest.TestCase):
    def test_public_endpoint_with_no_restriction_is_flagged(self):
        ctx = context_of(cluster_describe={"privateClusterConfig": {}, "masterAuthorizedNetworksConfig": {}})
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_public_endpoint_with_unrestricted_cidr_is_flagged(self):
        """`cidrBlocks` carries CidrBlock objects, so a string fixture proves nothing.

        The GKE discovery document types this array as `CidrBlock`
        (`{displayName, cidrBlock}`) and the API never emits bare strings. A
        membership test for the string therefore could not match, and a cluster
        that turned authorized networks on and then allowed the whole internet
        -- the single configuration this branch exists to catch -- was reported
        as restricted.
        """
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "masterAuthorizedNetworksConfig": {
                    "enabled": True,
                    "cidrBlocks": [{"displayName": "everywhere", "cidrBlock": "0.0.0.0/0"}],
                },
            }
        )
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_the_v6_default_route_is_allow_all_too(self):
        # A dual-stack cluster can write `::/0` where only `0.0.0.0/0` was ever
        # recognised. Matching the v4 string alone reads that as an allowlist,
        # drops the finding, and loses a `critical` on a control plane open to
        # every IPv6 address on the internet.
        for block in ("::/0", "0.0.0.0/0"):
            with self.subTest(block=block):
                ctx = context_of(
                    cluster_describe={
                        "privateClusterConfig": {},
                        "masterAuthorizedNetworksConfig": {
                            "enabled": True,
                            "cidrBlocks": [
                                {"displayName": "office", "cidrBlock": "203.0.113.0/24"},
                                {"displayName": "everywhere", "cidrBlock": block},
                            ],
                        },
                    }
                )
                self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_enabled_with_no_cidr_blocks_stays_restrictive(self):
        # Authorized networks on with an empty list is the strict end of the
        # setting -- nothing outside Google's own access reaches the endpoint.
        # Treating "no blocks" as "nothing was allowlisted, so it is open"
        # would invert it and report the most locked-down clusters.
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "masterAuthorizedNetworksConfig": {"enabled": True, "cidrBlocks": []},
            }
        )
        self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_a_config_present_but_not_enabled_does_not_count_as_restrictive(self):
        # What `kube-agents-host` actually returns: a non-empty
        # `masterAuthorizedNetworksConfig` carrying only
        # `gcpPublicCidrsAccessEnabled`, with `enabled` absent. Testing the
        # object for emptiness rather than for `enabled` would call that
        # cluster restricted and lose the finding.
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "masterAuthorizedNetworksConfig": {"gcpPublicCidrsAccessEnabled": True},
                "controlPlaneEndpointsConfig": {
                    "ipEndpointsConfig": {
                        "enablePublicEndpoint": True,
                        "authorizedNetworksConfig": {"gcpPublicCidrsAccessEnabled": True},
                    }
                },
            }
        )
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_the_excerpt_names_the_field_that_decided_the_verdict(self):
        """Two clusters that both fire must not produce the same evidence.

        This excerpt used to be the constant sentence "public endpoint
        reachable with no restrictive authorized networks", and
        `adopt_collector_evidence` overwrites whatever the model measured with
        it -- so on a live fleet of sixteen clusters all sixteen findings
        carried byte-identical evidence naming no field, no value, and no
        cluster. A reader could not check one against the API, and the two
        shapes below, which are materially different postures reached through
        different fields, were indistinguishable in the ledger.
        """
        current = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "masterAuthorizedNetworksConfig": {"gcpPublicCidrsAccessEnabled": True},
                    "controlPlaneEndpointsConfig": {"ipEndpointsConfig": {"enablePublicEndpoint": True}},
                }
            )
        )
        legacy = collect.check_public_control_plane(
            context_of(cluster_describe={"privateClusterConfig": {}, "masterAuthorizedNetworksConfig": {}})
        )
        self.assertEqual((len(current), len(legacy)), (1, 1))
        self.assertNotEqual(current[0]["excerpt"], legacy[0]["excerpt"])

        # The deciding field, spelled the way the JSON read spelled it.
        self.assertIn(
            "controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint=true",
            current[0]["excerpt"],
        )
        self.assertIn("privateClusterConfig.enablePrivateEndpoint=absent", legacy[0]["excerpt"])

        # Absent is not `false`: a field GKE omitted has to read as omitted, or
        # the excerpt asserts a value nobody observed.
        self.assertIn("masterAuthorizedNetworksConfig.enabled=absent", current[0]["excerpt"])
        self.assertIn("gcpPublicCidrsAccessEnabled=true", current[0]["excerpt"])
        self.assertNotIn("gcpPublicCidrsAccessEnabled", legacy[0]["excerpt"])

        # Both surfaces are named even where GKE returned only one of them, so
        # "not mentioned" cannot be confused with "not read".
        for excerpt in (current[0]["excerpt"], legacy[0]["excerpt"]):
            self.assertIn("ipEndpointsConfig.authorizedNetworksConfig.enabled=", excerpt)

    def test_an_external_dns_endpoint_survives_restrictive_authorized_networks(self):
        """The one shape where silence here was a false negative.

        Authorized networks gates the IP endpoint and nothing else. A cluster
        that allowlists its IP endpoint and serves the DNS endpoint to external
        traffic is still answering the internet, and the operator who enabled
        authorized networks to close it has not.
        """
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"displayName": "office", "cidrBlock": "203.0.113.0/24"}],
                    },
                    "controlPlaneEndpointsConfig": {
                        "ipEndpointsConfig": {"enablePublicEndpoint": True},
                        "dnsEndpointConfig": {"allowExternalTraffic": True},
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        self.assertIn("dnsEndpointConfig.allowExternalTraffic=true", found[0]["excerpt"])
        # The allowlisted IP path is closed, so claiming it is open would send
        # the reader to fix a setting that is already right.
        self.assertNotIn("enablePublicEndpoint", found[0]["excerpt"])

    def test_a_dns_only_cluster_is_not_called_reachable_over_an_ip_it_lacks(self):
        """`ipEndpointsConfig.enabled` is the switch `enablePublicEndpoint` sits under.

        `--no-enable-ip-access` serves no IP endpoint at all, and GKE leaves
        the now-moot `enablePublicEndpoint` behind it.
        """
        self.assertEqual(
            collect.check_public_control_plane(
                context_of(
                    cluster_describe={
                        "privateClusterConfig": {},
                        "masterAuthorizedNetworksConfig": {},
                        "controlPlaneEndpointsConfig": {
                            "ipEndpointsConfig": {"enabled": False, "enablePublicEndpoint": True},
                            "dnsEndpointConfig": {"allowExternalTraffic": False},
                        },
                    }
                )
            ),
            [],
        )

    def test_a_dns_only_cluster_open_externally_is_flagged_for_that_path_alone(self):
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {},
                    "controlPlaneEndpointsConfig": {
                        "ipEndpointsConfig": {"enabled": False, "enablePublicEndpoint": True},
                        "dnsEndpointConfig": {"allowExternalTraffic": True},
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        self.assertIn("dnsEndpointConfig.allowExternalTraffic=true", found[0]["excerpt"])
        self.assertNotIn("enablePublicEndpoint", found[0]["excerpt"])

    def test_both_paths_open_names_both(self):
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {},
                    "controlPlaneEndpointsConfig": {
                        "ipEndpointsConfig": {"enablePublicEndpoint": True},
                        "dnsEndpointConfig": {"allowExternalTraffic": True},
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        self.assertIn("enablePublicEndpoint=true", found[0]["excerpt"])
        self.assertIn("dnsEndpointConfig.allowExternalTraffic=true", found[0]["excerpt"])

    def test_google_cloud_access_is_marked_inert_when_there_is_no_allowlist(self):
        """It grants an exception to an allowlist that is not switched on.

        Unannotated it was the only difference between one cluster's excerpt
        and fifteen identical ones, reading as an aggravating factor on a
        cluster no worse than its neighbours.
        """
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {"gcpPublicCidrsAccessEnabled": True},
                }
            )
        )
        self.assertIn("gcpPublicCidrsAccessEnabled=true (inert:", found[0]["excerpt"])

    def test_google_cloud_access_is_not_marked_inert_beside_a_live_allowlist(self):
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "gcpPublicCidrsAccessEnabled": True,
                        "cidrBlocks": [{"displayName": "everywhere", "cidrBlock": "0.0.0.0/0"}],
                    },
                }
            )
        )
        self.assertIn("gcpPublicCidrsAccessEnabled=true", found[0]["excerpt"])
        self.assertNotIn("inert", found[0]["excerpt"])

    def test_the_excerpt_quotes_the_cidr_that_made_it_unrestricted(self):
        # A cluster caught by the allow-all branch is caught *because of* a
        # specific block. Leaving it out of the excerpt makes the one finding
        # whose evidence is genuinely checkable read like the ones that are not.
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [
                            {"displayName": "office", "cidrBlock": "203.0.113.0/24"},
                            {"displayName": "everywhere", "cidrBlock": "::/0"},
                        ],
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        self.assertIn("masterAuthorizedNetworksConfig.enabled=true", found[0]["excerpt"])
        self.assertIn("cidrBlocks=[203.0.113.0/24,::/0]", found[0]["excerpt"])

    def test_a_private_endpoint_is_never_flagged(self):
        ctx = context_of(cluster_describe={"privateClusterConfig": {"enablePrivateEndpoint": True}})
        self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_the_public_endpoint_turned_off_the_current_way_is_never_flagged(self):
        """`enablePublicEndpoint: false` is the whole answer where GKE returns it.

        The legacy `privateClusterConfig` block keeps coming back on a cluster
        that has none of it set -- GKE fills in the addresses and nothing else
        -- so a cluster that closed its public endpoint on the current surface
        carries no `enablePrivateEndpoint: true` to find. Reading the two as an
        `or` therefore reported it reachable from the internet at `critical`,
        which is the reverse of what it had configured.
        """
        for label, private_cfg in (
            ("legacy block absent", {}),
            ("legacy block present but only addressed", {"privateEndpoint": "10.0.0.2", "publicEndpoint": ""}),
        ):
            with self.subTest(legacy=label):
                ctx = context_of(
                    cluster_describe={
                        "privateClusterConfig": private_cfg,
                        "controlPlaneEndpointsConfig": {"ipEndpointsConfig": {"enablePublicEndpoint": False}},
                    }
                )
                self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_the_current_field_outranks_a_stale_legacy_one(self):
        # The other direction, so the fix is a precedence rule rather than a
        # second way to reach "not flagged": a cluster still serving the public
        # endpoint is flagged whatever the legacy block claims.
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {"enablePrivateEndpoint": True},
                "controlPlaneEndpointsConfig": {"ipEndpointsConfig": {"enablePublicEndpoint": True}},
            }
        )
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_a_narrow_authorized_cidr_is_never_flagged(self):
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "masterAuthorizedNetworksConfig": {
                    "enabled": True,
                    "cidrBlocks": [{"displayName": "corp", "cidrBlock": "10.0.0.0/8"}],
                },
            }
        )
        self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_authorized_networks_on_the_ip_endpoints_surface_are_honoured(self):
        """Setting both surfaces is invalid, so the newer one has to be read too.

        `IPEndpointsConfig.authorizedNetworksConfig` is where a cluster on the
        current API surface keeps this, and the discovery document says
        specifying it alongside `Cluster.masterAuthorizedNetworksConfig` is
        invalid. Reading only the legacy field reported every such cluster as
        having no restriction at all -- a critical raised against a control
        plane that is in fact closed.
        """
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "controlPlaneEndpointsConfig": {
                    "ipEndpointsConfig": {
                        "enablePublicEndpoint": True,
                        "authorizedNetworksConfig": {
                            "enabled": True,
                            "cidrBlocks": [{"displayName": "corp", "cidrBlock": "10.0.0.0/8"}],
                        },
                    }
                },
            }
        )
        self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_an_unrestricted_cidr_on_the_ip_endpoints_surface_is_flagged(self):
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "controlPlaneEndpointsConfig": {
                    "ipEndpointsConfig": {
                        "enablePublicEndpoint": True,
                        "authorizedNetworksConfig": {
                            "enabled": True,
                            "cidrBlocks": [{"displayName": "everywhere", "cidrBlock": "0.0.0.0/0"}],
                        },
                    }
                },
            }
        )
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)


class TestPodSecurityGaps(unittest.TestCase):
    # Every container-level setting the restricted Pod Security Standard
    # requires. A fixture short of one of these is a non-compliant container,
    # so "compliant" has to name them all or the control tests are asserting
    # against the check's blind spot rather than against compliance.
    COMPLIANT = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }

    def wl(self, container_sc=None, pod_sc=None):
        d = compliance_pod("x")
        if container_sc is not None:
            d["spec"]["containers"][0]["securityContext"] = container_sc
        if pod_sc is not None:
            d["spec"]["securityContext"] = pod_sc
        return collect.normalize_compliance_workloads(dump_of(d))[0]

    def test_no_security_context_at_all_is_flagged(self):
        self.assertIsNotNone(collect.check_podsecurity_gaps(self.wl(), context_of()))

    def test_full_compliant_context_is_not_flagged(self):
        self.assertIsNone(collect.check_podsecurity_gaps(self.wl(container_sc=self.COMPLIANT), context_of()))

    def test_explicit_false_over_a_compliant_pod_default_is_still_flagged(self):
        # The has()-vs-// distinction the SOP is emphatic about: a container
        # explicitly setting runAsNonRoot: false must not inherit a
        # compliant pod-level true.
        wl = self.wl(container_sc={**self.COMPLIANT, "runAsNonRoot": False}, pod_sc={"runAsNonRoot": True})
        hit = collect.check_podsecurity_gaps(wl, context_of())
        self.assertEqual(hit["excerpt"], "containers: app (runAsNonRoot=false)")

    def test_runAsUser_zero_is_flagged_even_with_nonroot_true(self):
        hit = collect.check_podsecurity_gaps(self.wl(container_sc={**self.COMPLIANT, "runAsUser": 0}), context_of())
        self.assertEqual(hit["excerpt"], "containers: app (runAsUser=0)")

    def test_the_excerpt_names_which_of_the_five_settings_failed(self):
        """Five independent settings decide this check, and the excerpt is
        published verbatim -- `adopt_collector_evidence` overwrites whatever the
        model wrote with it. A bare container name would tell a reader a
        workload is non-compliant without telling them what to change, and the
        fix for `runAsUser=0` is not the fix for a missing seccomp profile."""
        only_uid = collect.check_podsecurity_gaps(
            self.wl(container_sc={**self.COMPLIANT, "runAsUser": 0}), context_of()
        )
        self.assertEqual(only_uid["excerpt"], "containers: app (runAsUser=0)")

        only_caps = collect.check_podsecurity_gaps(
            self.wl(container_sc={**self.COMPLIANT, "capabilities": {"drop": ["NET_RAW"]}}), context_of()
        )
        self.assertEqual(only_caps["excerpt"], 'containers: app (capabilities.drop=["NET_RAW"])')

        everything = collect.check_podsecurity_gaps(self.wl(), context_of())
        self.assertEqual(
            everything["excerpt"],
            "containers: app (runAsNonRoot=null, seccompProfile.type=absent, "
            "allowPrivilegeEscalation=null, capabilities.drop=[])",
        )

    def test_missing_seccomp_profile_is_flagged(self):
        sc = {k: v for k, v in self.COMPLIANT.items() if k != "seccompProfile"}
        self.assertIsNotNone(collect.check_podsecurity_gaps(self.wl(container_sc=sc), context_of()))

    def test_privilege_escalation_left_enabled_is_flagged(self):
        """A container hardened on the other four still escalates to root the
        moment a setuid binary runs, which is the whole point of the setting."""
        sc = {k: v for k, v in self.COMPLIANT.items() if k != "allowPrivilegeEscalation"}
        hit = collect.check_podsecurity_gaps(self.wl(container_sc=sc), context_of())
        self.assertEqual(hit["excerpt"], "containers: app (allowPrivilegeEscalation=null)")

    def test_retained_capabilities_are_flagged(self):
        """`drop: [ALL]` is what restricted requires; dropping some of them is
        not most of the way there, it is a container that kept CAP_NET_ADMIN."""
        sc = {**self.COMPLIANT, "capabilities": {"drop": ["NET_RAW", "SYS_CHROOT"]}}
        self.assertIsNotNone(collect.check_podsecurity_gaps(self.wl(container_sc=sc), context_of()))

    def test_dropping_all_in_lower_case_still_counts(self):
        sc = {**self.COMPLIANT, "capabilities": {"drop": ["all"]}}
        self.assertIsNone(collect.check_podsecurity_gaps(self.wl(container_sc=sc), context_of()))

    def test_pod_level_inheritance_is_honored_when_container_is_silent(self):
        """Only for the fields that have it. `runAsNonRoot`, `runAsUser` and
        `seccompProfile` exist on `PodSecurityContext` and inherit; the
        container is silent on all three here and still grades clean."""
        inheritable = {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}}
        container_only = {k: v for k, v in self.COMPLIANT.items() if k not in inheritable}
        self.assertIsNone(
            collect.check_podsecurity_gaps(self.wl(container_sc=container_only, pod_sc=inheritable), context_of())
        )

    def test_allow_privilege_escalation_does_not_inherit_from_the_pod(self):
        """`PodSecurityContext` carries neither `allowPrivilegeEscalation` nor
        `capabilities`, so a pod-level value is not a value the kubelet reads.
        Falling back to one would grade a container clean on a setting nothing
        applied to it."""
        container_only = {k: v for k, v in self.COMPLIANT.items() if k != "allowPrivilegeEscalation"}
        hit = collect.check_podsecurity_gaps(
            self.wl(container_sc=container_only, pod_sc={"allowPrivilegeEscalation": False}), context_of()
        )
        self.assertIsNotNone(hit)
        self.assertIn("allowPrivilegeEscalation", hit["excerpt"])

    def test_already_flagged_by_privileged_container_is_suppressed_here(self):
        d = compliance_pod("x")
        d["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNone(collect.check_podsecurity_gaps(wl, context_of()))

    def test_a_restricted_labelled_namespace_is_suppressed(self):
        ctx = context_of(namespaces=[namespace("default", labels={"pod-security.kubernetes.io/enforce": "restricted"})])
        self.assertIsNone(collect.check_podsecurity_gaps(self.wl(), ctx))


class TestComplianceCollectCluster(unittest.TestCase):
    """One end-to-end pass over compliance-audit's real collection plan --
    five distinct kubectl/gcloud commands, gated and cross-referenced --
    proving the multi-source builder actually composes with the shared
    check-iteration loop `collect_cluster` runs regardless of stream shape.
    """

    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    # GKE's own answer when `node-pools list` is aimed at an Autopilot
    # cluster. The fake used to return rc=0 here whatever the cluster was,
    # which is why the gate failure this class is supposed to cover survived
    # a test named for exactly that case: a fake that answers every argv
    # successfully cannot tell a command the API runs from one it refuses.
    AUTOPILOT_NODE_POOLS_ERROR = (
        "ERROR: (gcloud.container.node-pools.list) ResponseError: code=400, "
        "message=Autopilot node pools cannot be accessed or modified."
    )

    def run_with(self, workload_items=(), rbac_items=(), netpol_items=(), sa_items=(), describe=None, node_pools=(), ccnp_run=None, cluster=None):
        describe = describe if describe is not None else {}
        target = cluster or self.CLUSTER
        self.issued = []

        def run(argv, **kwargs):
            self.issued.append(list(argv))
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                kinds = argv[2]
                if kinds == collect.COMPLIANCE_DUMP_KINDS:
                    return Run(argv, 0, json.dumps(dump_of(*workload_items)), "", 0.1)
                if "clusterroles" in kinds:
                    return Run(argv, 0, json.dumps(dump_of(*rbac_items)), "", 0.1)
                if kinds == "netpol,ns":
                    return Run(argv, 0, json.dumps(dump_of(*netpol_items)), "", 0.1)
                if kinds == "sa":
                    return Run(argv, 0, json.dumps(dump_of(*sa_items)), "", 0.1)
                if kinds == "ccnp" and ccnp_run is not None:
                    return ccnp_run
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return Run(argv, 0, json.dumps(describe), "", 0.1)
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                if target.get("autopilot"):
                    return Run(argv, 1, "", self.AUTOPILOT_NODE_POOLS_ERROR, 0.1)
                return Run(argv, 0, json.dumps(list(node_pools)), "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.collect_cluster(target, "compliance-audit", collect.COMPLIANCE_CHECKS, run=run)

    def test_a_clean_cluster_reports_nothing(self):
        result = self.run_with(
            describe={
                "workloadIdentityConfig": {"workloadPool": "acme.svc.id.goog"},
                "privateClusterConfig": {"enablePrivateEndpoint": True},
            }
        )
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["commands"]), 11)

    def test_a_dirty_cluster_reports_across_multiple_sources(self):
        privileged_pod = compliance_pod("bad")
        privileged_pod["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        result = self.run_with(
            workload_items=[privileged_pod],
            rbac_items=[
                crb("admin-binding", [subject("ServiceAccount", "app", "default")]),
            ],
            netpol_items=[namespace("default")],
            describe={"workloadIdentityConfig": {}},
        )
        slugs = {c["check"] for c in result["candidates"]}
        self.assertIn("privileged-container", slugs)
        self.assertIn("cluster-admin-binding", slugs)
        self.assertIn("netpol-missing", slugs)
        self.assertIn("workload-identity-off", slugs)

    def test_a_gate_failure_on_one_source_fails_the_whole_cluster(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"] and argv[2] == collect.COMPLIANCE_DUMP_KINDS:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:2] == ["kubectl", "get"] and "clusterroles" in argv[2]:
                return Run(argv, 1, "", "RBAC forbidden", 0.1)  # this one fails
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(self.CLUSTER, "compliance-audit", collect.COMPLIANCE_CHECKS, run=run)
        self.assertEqual(result["outcome"], "gate-failed")
        self.assertNotIn("candidates", result)

    def test_a_cluster_network_policy_suppresses_netpol_missing(self):
        result = self.run_with(
            workload_items=[compliance_pod("api", ns="payments")],
            netpol_items=[namespace("payments")],
            ccnp_run=Run(["x"], 0, json.dumps(dump_of(ccnp("fleet-wide"))), "", 0.1),
        )
        self.assertNotIn("netpol-missing", {c["check"] for c in result["candidates"]})

    def test_ccnp_read_failure_does_not_gate_the_cluster_closed(self):
        """Unlike RBAC/netpol/workload dumps, a missing `ccnp` CRD is the
        common case (Dataplane V2's ClusterNetworkPolicy is not installed on
        every cluster) -- it must degrade to "no cluster-wide policies seen"
        rather than failing the whole cluster the way a real input gap does."""
        result = self.run_with(
            workload_items=[compliance_pod("api", ns="payments")],
            netpol_items=[namespace("payments")],
            ccnp_run=Run(["x"], 1, "", "the server doesn't have a resource type \"ccnp\"", 0.01),
        )
        self.assertEqual(result["outcome"], "collected")
        self.assertIn("netpol-missing", {c["check"] for c in result["candidates"]})

    def test_autopilot_pre_fills_checks_not_applicable_and_excludes_them_from_commands(self):
        autopilot_cluster = {**self.CLUSTER, "autopilot": True}
        result = self.run_with(cluster=autopilot_cluster)
        not_applicable_slugs = {e["check"] for e in result["checks_not_applicable"]}
        self.assertEqual(
            not_applicable_slugs,
            {"privileged-container", "host-namespace", "hostpath-mount", "legacy-metadata"},
        )
        command_slugs = {c["check"] for c in result["commands"]}
        self.assertFalse(not_applicable_slugs & command_slugs)
        # Every reason is the SOP's own canonical text, not a placeholder.
        for entry in result["checks_not_applicable"]:
            self.assertIn("Autopilot", entry["reason"])

    def test_standard_cluster_carries_no_checks_not_applicable_key(self):
        result = self.run_with()
        self.assertNotIn("checks_not_applicable", result)

    def test_a_check_that_found_something_is_not_also_declared_inapplicable(self):
        """The filter reached `commands` and not `candidates`, so a privileged
        pod on an Autopilot cluster produced a manifest saying both that the
        check cannot apply here and that it fired — with no `commands` entry
        behind the candidate. Each reason asserts the object cannot exist, so a
        candidate falsifies the reason rather than the finding."""
        privileged = compliance_pod("legacy", ns="payments")
        privileged["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        result = self.run_with(workload_items=[privileged], cluster={**self.CLUSTER, "autopilot": True})
        not_applicable = {e["check"] for e in result.get("checks_not_applicable") or []}
        found = {c["check"] for c in result["candidates"]}
        self.assertIn("privileged-container", found)
        self.assertNotIn("privileged-container", not_applicable)
        self.assertIn("privileged-container", {c["check"] for c in result["commands"]})
        # The other three still are: they found nothing, so nothing falsifies
        # the claim that they cannot apply here.
        self.assertEqual(not_applicable, {"host-namespace", "hostpath-mount", "legacy-metadata"})

    def test_autopilot_collects_rather_than_gate_failing_on_a_read_the_api_refuses(self):
        """The live regression: `node-pools list` 400s on Autopilot, and it was
        the last read in the compliance collector, so eleven checks that had
        already succeeded were discarded on the twelfth -- which could not have
        run. Three of the four clusters in the validation fleet are Autopilot,
        so every daily run of this stream collected one and gate-failed the
        rest."""
        result = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        self.assertEqual(result["outcome"], "collected")
        self.assertNotIn("error", result)

    def test_autopilot_never_issues_the_node_pools_read_at_all(self):
        """Not merely tolerating the failure -- not making the call. The check
        it backs is already declared inapplicable here, so the read has nothing
        to inform, and issuing it spends a round trip to be told 400."""
        self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        self.assertEqual([a for a in self.issued if a[:3] == ["gcloud", "container", "node-pools"]], [])

    def test_a_standard_cluster_still_issues_it(self):
        self.run_with()
        self.assertTrue([a for a in self.issued if a[:3] == ["gcloud", "container", "node-pools"]])

    def test_a_standard_cluster_still_gate_fails_when_node_pools_fails(self):
        """The skip is Autopilot-specific. On a Standard cluster the read backs
        a check that genuinely applies, so losing it still fails closed."""

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return Run(argv, 0, json.dumps({}), "", 0.1)
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return Run(argv, 1, "", "permission denied", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(self.CLUSTER, "compliance-audit", collect.COMPLIANCE_CHECKS, run=run)
        self.assertEqual(result["outcome"], "gate-failed")
        self.assertIn("node-pools", result["error"])

    def test_autopilot_records_every_check_it_could_run(self):
        """The point of not gate-failing: the other checks reach the manifest.
        Eleven checks minus the four Autopilot rules out is seven, each with a
        command behind it, and `legacy-metadata` dispositioned rather than
        silently absent -- absent is what §6 reads as a coverage gap."""
        result = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        command_slugs = {c["check"] for c in result["commands"]}
        na_slugs = {e["check"] for e in result["checks_not_applicable"]}
        self.assertEqual(len(command_slugs), 7)
        self.assertIn("legacy-metadata", na_slugs)
        self.assertNotIn("legacy-metadata", command_slugs)
        self.assertTrue(all(c["rc"] == 0 for c in result["commands"]))


# --------------------------------------------------------------------------- #
# ai-security-audit
# --------------------------------------------------------------------------- #


def ai_workload(kind="Deployment", name="vllm-llama", ns="default", image="acme/vllm:v1", container=None, pod_labels=None, volumes=None):
    """A workload whose default image trips the §2 AI discriminator on its
    own — every test that does not care about the discriminator itself can
    ignore that and focus on its own check."""
    c = {"name": "server", "image": image}
    if container:
        c.update(container)
    pod_spec = {"containers": [c]}
    if volumes is not None:
        pod_spec["volumes"] = volumes
    labels = pod_labels if pod_labels is not None else {"app": name}
    if kind == "Pod":
        return {"kind": "Pod", "metadata": {"namespace": ns, "name": name, "labels": labels}, "spec": pod_spec}
    template = {"metadata": {"labels": labels}, "spec": pod_spec}
    spec = {"jobTemplate": {"spec": {"template": template}}} if kind == "CronJob" else {"template": template}
    return {"kind": kind, "metadata": {"namespace": ns, "name": name, "labels": {}}, "spec": spec}


def ai_service(name, ns="default", selector=None, svc_type="LoadBalancer", annotations=None, ingress=None):
    svc = {
        "kind": "Service",
        "metadata": {"namespace": ns, "name": name, "annotations": annotations or {}},
        "spec": {"type": svc_type, "selector": selector if selector is not None else {"app": name}},
    }
    # Omitted entirely rather than left empty when `ingress` is None: a load
    # balancer that has not been assigned an address yet has no `ingress` key,
    # and that is the case the check falls back to annotations for.
    if ingress is not None:
        svc["status"] = {"loadBalancer": {"ingress": [{"ip": addr} for addr in ingress]}}
    return svc


class TestIsAiWorkload(unittest.TestCase):
    def test_a_known_model_server_image_matches(self):
        self.assertTrue(collect._is_ai_workload({"containers": [{"image": "docker.io/vllm/vllm-openai:v0.6"}]}))

    def test_an_unrelated_image_does_not_match(self):
        self.assertFalse(collect._is_ai_workload({"containers": [{"image": "nginx:1.25"}]}))

    def test_a_gpu_request_matches_regardless_of_image(self):
        spec = {"containers": [{"image": "acme/recommender:v4", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}
        self.assertTrue(collect._is_ai_workload(spec))

    def test_a_tpu_request_matches(self):
        spec = {"containers": [{"image": "acme/recommender:v4", "resources": {"limits": {"google.com/tpu": "1"}}}]}
        self.assertTrue(collect._is_ai_workload(spec))

    def test_a_node_label_style_tpu_key_does_not_match_a_resource_limit(self):
        # cloud.google.com/tpu-accelerator is a nodeSelector value, never a
        # resources.limits key -- the SOP is explicit this must not match.
        spec = {"containers": [{"image": "acme/x", "resources": {"limits": {}}}]}
        self.assertFalse(collect._is_ai_workload(spec))


class TestNormalizeAiWorkloads(unittest.TestCase):
    def test_a_deployment_carries_its_pod_template_labels(self):
        d = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm", "tier": "serving"})
        out = collect.normalize_ai_workloads(dump_of(d))
        self.assertEqual(out[0]["lbl"], {"app": "vllm", "tier": "serving"})

    def test_a_cronjob_carries_the_nested_template_labels(self):
        c = ai_workload("CronJob", "batch-embed", pod_labels={"app": "batch-embed"})
        out = collect.normalize_ai_workloads(dump_of(c))
        self.assertEqual(out[0]["lbl"], {"app": "batch-embed"})

    def test_a_bare_pod_carries_its_own_labels(self):
        p = ai_workload("Pod", "one-off", pod_labels={"app": "one-off"})
        out = collect.normalize_ai_workloads(dump_of(p))
        self.assertEqual(out[0]["lbl"], {"app": "one-off"})

    def test_an_owned_pod_is_suppressed(self):
        p = ai_workload("Pod", "one-off")
        p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "x"}]
        self.assertEqual(collect.normalize_ai_workloads(dump_of(p)), [])

    def test_a_system_namespace_is_suppressed(self):
        d = ai_workload("Deployment", "vllm", ns="kube-system")
        self.assertEqual(collect.normalize_ai_workloads(dump_of(d)), [])

    def test_a_non_ai_workload_never_appears(self):
        d = ai_workload("Deployment", "web", image="nginx:1.25")
        self.assertEqual(collect.normalize_ai_workloads(dump_of(d)), [])

    def test_an_addon_managed_object_is_suppressed_even_if_it_would_match(self):
        d = ai_workload("DaemonSet", "nvidia-gpu-device-plugin", container={"resources": {"limits": {"nvidia.com/gpu": "1"}}})
        d["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
        self.assertEqual(collect.normalize_ai_workloads(dump_of(d)), [])


class TestModelRemoteCodeTrusted(unittest.TestCase):
    def hit(self, container):
        w = ai_workload(container=container)
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        return collect.check_model_remote_code_trusted(w, {})

    def test_flags_the_trust_remote_code_arg(self):
        self.assertIsNotNone(self.hit({"args": ["--trust-remote-code", "--model", "x"]}))

    def test_flags_the_underscore_spelling(self):
        self.assertIsNotNone(self.hit({"args": ["--trust_remote_code"]}))

    def test_does_not_flag_the_flag_explicitly_disabled(self):
        self.assertIsNone(self.hit({"args": ["--trust-remote-code=false"]}))

    def test_flags_a_truthy_env_var(self):
        self.assertIsNotNone(self.hit({"env": [{"name": "TRUST_REMOTE_CODE", "value": "true"}]}))

    def test_does_not_flag_a_falsy_env_var(self):
        self.assertIsNone(self.hit({"env": [{"name": "TRUST_REMOTE_CODE", "value": "false"}]}))

    def test_init_containers_count(self):
        w = ai_workload()
        w["spec"]["template"]["spec"]["initContainers"] = [{"name": "fetch", "args": ["--trust-remote-code"]}]
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        hit = collect.check_model_remote_code_trusted(w, {})
        self.assertIn("fetch", hit["excerpt"])


class TestWeightsMountWritable(unittest.TestCase):
    def collected(self, container, volumes):
        w = ai_workload(container=container, volumes=volumes)
        return collect.normalize_ai_workloads(dump_of(w))[0]

    def test_flags_a_readwrite_csi_mount(self):
        w = self.collected(
            {"volumeMounts": [{"name": "weights", "mountPath": "/weights"}]},
            [{"name": "weights", "csi": {"driver": "gcsfuse.csi.storage.gke.io"}}],
        )
        self.assertIsNotNone(collect.check_weights_mount_writable(w, {}))

    def test_flags_a_readwrite_pvc_mount(self):
        w = self.collected(
            {"volumeMounts": [{"name": "weights", "mountPath": "/weights"}]},
            [{"name": "weights", "persistentVolumeClaim": {"claimName": "weights-pvc"}}],
        )
        self.assertIsNotNone(collect.check_weights_mount_writable(w, {}))

    def test_does_not_flag_a_readonly_mount(self):
        w = self.collected(
            {"volumeMounts": [{"name": "weights", "mountPath": "/weights", "readOnly": True}]},
            [{"name": "weights", "csi": {"driver": "x"}}],
        )
        self.assertIsNone(collect.check_weights_mount_writable(w, {}))

    def test_does_not_flag_a_readonly_volume(self):
        w = self.collected(
            {"volumeMounts": [{"name": "weights", "mountPath": "/weights"}]},
            [{"name": "weights", "csi": {"driver": "x", "readOnly": True}}],
        )
        self.assertIsNone(collect.check_weights_mount_writable(w, {}))

    def test_does_not_flag_an_emptydir(self):
        w = self.collected(
            {"volumeMounts": [{"name": "scratch", "mountPath": "/tmp"}]},
            [{"name": "scratch", "emptyDir": {}}],
        )
        self.assertIsNone(collect.check_weights_mount_writable(w, {}))

    def test_the_join_by_name_is_required_not_a_direct_field_test(self):
        # Regression for the exact bug the SOP calls load-bearing: csi/pvc
        # live on the volume, never on the mount, so a mount naming a
        # volume that does not exist must not spuriously match.
        w = self.collected({"volumeMounts": [{"name": "missing", "mountPath": "/x"}]}, [])
        self.assertIsNone(collect.check_weights_mount_writable(w, {}))

    def test_names_env_paths_that_the_flagged_container_writes_into(self):
        # An auto-merged remediation added readOnly: true to a mount whose own
        # container had HOME set inside it, and the pod went to CrashLoopBackOff
        # with "remove /models/.ollama/models/manifests: read-only file system".
        # The evidence has to carry the conflict or the fix cannot see it.
        w = self.collected(
            {
                "volumeMounts": [{"name": "weights", "mountPath": "/models"}],
                "env": [
                    {"name": "HOME", "value": "/models"},
                    {"name": "HF_HOME", "value": "/models/.cache"},
                ],
            },
            [{"name": "weights", "persistentVolumeClaim": {"claimName": "w"}}],
        )
        hit = collect.check_weights_mount_writable(w, {})
        self.assertIn("HOME=/models", hit["excerpt"])
        self.assertIn("HF_HOME=/models/.cache", hit["excerpt"])

    def test_env_outside_the_mount_is_not_reported_as_a_writer(self):
        # /models-cache is a sibling of /models, not a path inside it; a prefix
        # test without the separator would call it a conflict and push the
        # remediation into a manual finding for no reason.
        w = self.collected(
            {
                "volumeMounts": [{"name": "weights", "mountPath": "/models"}],
                "env": [
                    {"name": "HOME", "value": "/state"},
                    {"name": "CACHE", "value": "/models-cache"},
                ],
            },
            [{"name": "weights", "persistentVolumeClaim": {"claimName": "w"}}],
        )
        hit = collect.check_weights_mount_writable(w, {})
        self.assertNotIn("container writes here", hit["excerpt"])


class TestModelArtifactUnpinnedSource(unittest.TestCase):
    def hit(self, container):
        w = ai_workload(container=container)
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        return collect.check_model_artifact_unpinned_source(w, {})

    def test_flags_a_plaintext_http_url(self):
        self.assertIsNotNone(self.hit({"args": ["--weights", "http://example.com/model.bin"]}))

    def test_flags_an_ftp_url(self):
        self.assertIsNotNone(self.hit({"env": [{"name": "MODEL_URL", "value": "ftp://example.com/model.bin"}]}))

    def test_does_not_flag_an_https_url(self):
        self.assertIsNone(self.hit({"args": ["--weights", "https://example.com/model.bin"]}))

    def test_does_not_flag_an_object_store_uri(self):
        self.assertIsNone(self.hit({"args": ["--weights", "gs://bucket/model.bin"]}))

    def test_flags_model_without_revision(self):
        self.assertIsNotNone(self.hit({"args": ["--model", "meta-llama/Llama-3"]}))

    def test_does_not_flag_model_with_revision(self):
        self.assertIsNone(self.hit({"args": ["--model", "meta-llama/Llama-3", "--revision", "abc123"]}))

    def test_accepts_the_equals_spelling_of_both_flags(self):
        self.assertIsNone(self.hit({"args": ["--model=meta-llama/Llama-3", "--revision=abc123"]}))
        self.assertIsNotNone(self.hit({"args": ["--model=meta-llama/Llama-3"]}))

    def test_escalates_to_critical_alongside_a_remote_code_finding_on_the_same_container(self):
        hit = self.hit({"args": ["--model", "x", "--trust-remote-code"]})
        self.assertEqual(hit["severity"], "critical")

    def test_does_not_escalate_without_a_remote_code_finding(self):
        hit = self.hit({"args": ["--model", "x"]})
        self.assertNotIn("severity", hit)

    def test_excerpt_names_the_offending_url(self):
        hit = self.hit({"args": ["--weights", "http://example.com/model.bin"]})
        self.assertIn("http://example.com/model.bin", hit["excerpt"])

    def test_excerpt_names_the_model_a_bare_flag_leaves_in_the_next_argument(self):
        hit = self.hit({"args": ["--model", "meta-llama/Llama-3"]})
        self.assertIn("--model meta-llama/Llama-3", hit["excerpt"])
        self.assertIn("no --revision", hit["excerpt"])

    def test_excerpt_reports_both_conditions_on_one_container(self):
        hit = self.hit({"args": ["--model", "m", "--weights", "http://example.com/w.bin"]})
        self.assertIn("plaintext URL", hit["excerpt"])
        self.assertIn("no --revision", hit["excerpt"])

    def test_excerpt_strips_url_userinfo_and_query_string(self):
        # The ledger is a public GitHub issue; a signed-URL token or a
        # basic-auth password in the manifest must not be republished there.
        hit = self.hit({"args": ["--weights", "http://user:pw@example.com/m.bin?sig=SECRET"]})
        self.assertNotIn("SECRET", hit["excerpt"])
        self.assertNotIn("pw", hit["excerpt"])
        self.assertIn("example.com/m.bin", hit["excerpt"])


class TestModelCredentialPlaintextEnv(unittest.TestCase):
    def hit(self, env):
        w = ai_workload(container={"env": env})
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        return collect.check_model_credential_plaintext_env(w, {})

    def test_flags_a_literal_hf_token(self):
        self.assertIsNotNone(self.hit([{"name": "HF_TOKEN", "value": "hf_xxx"}]))

    def test_does_not_flag_a_secretkeyref(self):
        self.assertIsNone(self.hit([{"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]))

    def test_does_not_flag_an_empty_value(self):
        self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": ""}]))

    def test_flags_openai_and_anthropic_keys(self):
        self.assertIsNotNone(self.hit([{"name": "OPENAI_API_KEY", "value": "sk-x"}]))
        self.assertIsNotNone(self.hit([{"name": "ANTHROPIC_API_KEY", "value": "sk-ant-x"}]))

    def test_never_puts_the_value_in_the_excerpt(self):
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_super_secret_value"}])
        self.assertNotIn("hf_super_secret_value", hit["excerpt"])

    def test_does_not_flag_the_sops_own_named_non_secret_examples(self):
        self.assertIsNone(self.hit([{"name": "HF_TOKEN_PATH", "value": "/var/run/secrets/hf/token"}]))
        self.assertIsNone(self.hit([{"name": "OPENAI_API_KEY_FILE", "value": "/etc/openai/key"}]))
        self.assertIsNone(self.hit([{"name": "MODEL_REGISTRY_KEY_ID", "value": "key-2026-01"}]))

    def test_a_live_looking_token_keeps_the_default_severity(self):
        self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": "hf_qMBpTvKzLdWnXaHrYuEjCiSoPfGb"}]).get("severity"))

    def test_a_placeholder_is_reported_but_downgraded(self):
        # The exact value the ai-inference demo ships in this fleet. Reported
        # so nobody has to trust the heuristic, minor so it does not read as
        # a leaked credential.
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}])
        self.assertEqual(hit["severity"], "minor")
        self.assertIn("placeholder", hit["excerpt"])
        self.assertIn("server:HF_TOKEN", hit["excerpt"])

    def test_an_unexpanded_reference_is_downgraded(self):
        for value in ("$(HF_TOKEN_REF)", "${HF_TOKEN}", "{{ .Values.hfToken }}"):
            self.assertEqual(self.hit([{"name": "HF_TOKEN", "value": value}])["severity"], "minor")

    def test_a_placeholder_beside_a_live_token_stays_major(self):
        # One real credential is not made safe by the placeholders next to it,
        # so the downgrade requires every value to be inert.
        hit = self.hit(
            [
                {"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER"},
                {"name": "OPENAI_API_KEY", "value": "sk-qMBpTvKzLdWnXaHrYuEj"},
            ]
        )
        self.assertIsNone(hit.get("severity"))

    def test_the_placeholder_value_still_never_reaches_the_excerpt(self):
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}])
        self.assertNotIn("NOT_A_REAL_TOKEN", hit["excerpt"])

    def test_a_live_token_that_merely_contains_a_placeholder_word_stays_major(self):
        # A substring test downgrades every one of these: "todo" inside a
        # random run, "sample" inside another, and -- worst -- a DSN whose
        # *hostname* is example.com while its password is live. Suppressing a
        # real credential is the expensive error, so the whole value has to
        # read as inert, not some part of it.
        for value in (
            "sk-proj-Todo7xKqPnVrLmZbHdGw",
            "hf_QsampleWnXaHrYuEjCiSoPfGb",
            "postgres://svc:9fKq2LmZbHdGw@db.example.com:5432/models",
            "AKIAIOSFODNN7EXAMPLE",
        ):
            with self.subTest(value=value):
                self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": value}]).get("severity"))

    def test_a_live_token_carrying_reference_punctuation_stays_major(self):
        # `${`, `$(` and `{{` anywhere in the value used to inert it, so any
        # secret whose alphabet includes them was silently downgraded.
        for value in ("sk-live-qMBpTvKzLdWnX${", "hf_2LmZbHdGw{{PnVrKq", "pw:$(9fKq2LmZbHdGwXa"):
            with self.subTest(value=value):
                self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": value}]).get("severity"))

    def test_a_placeholder_written_without_separators_is_left_at_major(self):
        # Deliberate, and the direction to err in. Splitting on non-alphanumerics
        # is what makes `hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN` readable, and
        # the cost is that a run-together placeholder has no separators to split
        # on, so `CHANGEME` reads as one opaque token and keeps `major`.
        #
        # The obvious repair -- letting a token match a *sequence* of placeholder
        # words -- is what must not be done: `SECRET`+`KEY` tiles `secretkey`,
        # which is a weak password rather than a placeholder, and downgrading a
        # live credential is the error this check cannot afford. Over-reporting
        # a placeholder is the one it can. `ai_security_audit_sop.md` says the
        # same under check 3.5: never dropped, only downgraded.
        for value in ("CHANGEME", "YOURTOKENHERE"):
            with self.subTest(value=value):
                self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": value}]).get("severity"))

    def test_the_excerpt_reads_correctly_beside_a_severity_it_did_not_set(self):
        # `adopt_collector_evidence` forces this excerpt onto the finding but
        # never copies `severity`, so a model that kept `major` gets this
        # sentence under it. It has to describe what was measured rather than
        # assert the conclusion, or the report contradicts itself.
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}])
        self.assertNotIn("not a live secret", hit["excerpt"])
        self.assertIn("matches this check's placeholder or unexpanded-reference patterns", hit["excerpt"])


class TestModelImageFloatingTag(unittest.TestCase):
    def hit(self, image):
        # The discriminator is independent of the image under test here, so
        # pin it via the accelerator prong -- otherwise an image string that
        # does not happen to name a known model server (e.g. a bare registry
        # host) would drop the workload out of scope before this check ever
        # saw it.
        w = ai_workload(container={"image": image, "resources": {"limits": {"nvidia.com/gpu": "1"}}})
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        return collect.check_model_image_floating_tag(w, {})

    def test_flags_latest(self):
        self.assertIsNotNone(self.hit("acme/vllm:latest"))

    def test_flags_no_tag_at_all(self):
        self.assertIsNotNone(self.hit("acme/vllm"))

    def test_does_not_flag_a_version_tag(self):
        self.assertIsNone(self.hit("acme/vllm:v0.6.2"))

    def test_does_not_flag_a_digest_even_with_a_floating_tag(self):
        self.assertIsNone(self.hit("acme/vllm:latest@sha256:" + "a" * 64))

    def test_a_registry_port_is_not_mistaken_for_a_tag(self):
        # gcr.io:5000/i has a colon with a `/` after it before the string
        # ends -- that is not a tag, so this is the untagged case, not a
        # false negative.
        self.assertIsNotNone(self.hit("gcr.io:5000/i"))
        self.assertIsNone(self.hit("gcr.io:5000/i:v1"))


class TestInferenceEndpointPublic(unittest.TestCase):
    def result(self, svc, workloads):
        context = {"services": [svc], "ai_workloads": collect.normalize_ai_workloads(dump_of(*workloads))}
        return collect.check_inference_endpoint_public(context)

    def test_flags_a_public_loadbalancer_selecting_an_ai_workload(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Service/vllm-svc")

    def test_does_not_flag_clusterip(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, svc_type="ClusterIP")
        self.assertEqual(self.result(svc, [w]), [])

    def test_does_not_flag_the_current_internal_lb_annotation(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, annotations={"networking.gke.io/load-balancer-type": "Internal"})
        self.assertEqual(self.result(svc, [w]), [])

    def test_does_not_flag_the_legacy_internal_lb_annotation(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, annotations={"cloud.google.com/load-balancer-type": "Internal"})
        self.assertEqual(self.result(svc, [w]), [])

    def test_does_not_flag_a_selector_less_service(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("headless", selector={})
        self.assertEqual(self.result(svc, [w]), [])

    def test_does_not_flag_a_loadbalancer_selecting_a_non_ai_workload(self):
        svc = ai_service("web-svc", selector={"app": "web"})
        self.assertEqual(self.result(svc, []), [])

    def test_the_selector_must_be_a_subset_not_an_exact_match(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm", "tier": "serving"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        self.assertEqual(len(self.result(svc, [w])), 1)

    def test_an_rfc1918_address_is_not_a_public_endpoint(self):
        # The annotation says what was asked for; the assigned address says
        # what was given. A private address means unreachable, so the finding
        # would be untrue however the annotations read.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["10.150.0.78"])
        self.assertEqual(self.result(svc, [w]), [])

    def test_a_routable_address_is_flagged_without_publishing_the_address(self):
        # `ai_security_audit_sop.md` Red Lines: the address of a reachable
        # model endpoint never reaches `title`, `object`, `evidence.excerpt`
        # or `recommendation`. These findings are filed as issues on a public
        # repository, and `adopt_collector_evidence` forces this excerpt over
        # whatever the model wrote, so the rule has to hold here or nowhere.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["136.70.153.197"])
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        self.assertNotIn("136.70.153.197", hits[0]["excerpt"])
        self.assertNotIn("136.70.153.197", hits[0]["object"])
        self.assertIn("1 assigned address, none of them private", hits[0]["excerpt"])

    def test_one_public_address_among_private_ones_still_counts(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["10.0.0.5", "136.70.153.197"])
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        self.assertNotIn("136.70.153.197", hits[0]["excerpt"])
        self.assertNotIn("10.0.0.5", hits[0]["excerpt"])
        self.assertIn("1 assigned address, none of them private", hits[0]["excerpt"])

    def test_an_ingress_entry_carrying_both_ip_and_hostname_keeps_the_hostname(self):
        # `ip or hostname` short-circuits and would throw the hostname away.
        # The private IP would then be the only address considered, the
        # all-private branch would fire, and a reachable endpoint would go
        # unreported -- the one direction that loses a finding.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        svc["status"] = {"loadBalancer": {"ingress": [{"ip": "10.0.0.5", "hostname": "vllm.example.com"}]}}
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        self.assertNotIn("vllm.example.com", hits[0]["excerpt"])

    def test_a_pending_load_balancer_still_falls_back_to_annotations(self):
        # No address assigned yet, so status says nothing and the annotation
        # is all there is -- the behaviour before this check read status.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        self.assertEqual(len(self.result(ai_service("vllm-svc", selector={"app": "vllm"}, ingress=[]), [w])), 1)

    def test_a_hostname_is_treated_as_public_rather_than_dropped(self):
        # The collector resolves nothing, so an unresolvable address keeps the
        # finding instead of silently clearing it.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        svc["status"] = {"loadBalancer": {"ingress": [{"hostname": "a1b2.elb.amazonaws.com"}]}}
        self.assertEqual(len(self.result(svc, [w])), 1)

    def restricted(self, ranges, ingress=("136.70.153.197",)):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=list(ingress))
        svc["spec"]["loadBalancerSourceRanges"] = ranges
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        return hits[0]

    def test_a_source_range_allowlist_downgrades_rather_than_drops(self):
        # The check's `impact` says anyone who finds the address can use the
        # endpoint. GKE programs `loadBalancerSourceRanges` into the firewall
        # in front of the forwarding rule, so under an allowlist that is not
        # true and `critical` overstates it. Still a finding, though -- an
        # allowlist bounds who reaches the endpoint, it does not make it
        # unreachable, so this downgrades where the private-address branch
        # drops outright.
        hit = self.restricted(["203.0.113.0/24"])
        self.assertEqual(hit["severity"], "major")
        self.assertIn("admits 1 CIDR, not the whole internet", hit["excerpt"])

    def test_the_allowed_ranges_are_counted_never_printed(self):
        # Which networks are trusted is the other half of the target, and
        # these findings are filed as issues on a public repository.
        hit = self.restricted(["203.0.113.0/24", "198.51.100.7/32"])
        self.assertNotIn("203.0.113", hit["excerpt"])
        self.assertNotIn("198.51.100", hit["excerpt"])
        self.assertIn("admits 2 CIDRs", hit["excerpt"])

    def test_a_default_route_is_not_a_restriction(self):
        # `0.0.0.0/0` is the whole internet written as an allowlist. Reading
        # the field's presence rather than its contents would downgrade every
        # one of these to `major`.
        for ranges in (["0.0.0.0/0"], ["::/0"], ["203.0.113.0/24", "0.0.0.0/0"]):
            with self.subTest(ranges=ranges):
                hit = self.restricted(ranges)
                self.assertIsNone(hit.get("severity"))
                self.assertNotIn("admits", hit["excerpt"])

    def test_an_unreadable_range_leaves_the_severity_alone(self):
        # An allowlist the collector cannot parse is one it cannot vouch for,
        # and guessing in the other direction downgrades a live exposure.
        for ranges in (["not-a-cidr"], ["203.0.113.0/24", "10.0.0.0/8/8"]):
            with self.subTest(ranges=ranges):
                self.assertIsNone(self.restricted(ranges).get("severity"))

    def test_blank_entries_do_not_defeat_the_allowlist(self):
        # A blank string is not a CIDR and widens nothing, so the ranges
        # beside it still restrict. Counting it would also misreport the total.
        hit = self.restricted(["203.0.113.0/24", "", "  "])
        self.assertEqual(hit["severity"], "major")
        self.assertIn("admits 1 CIDR,", hit["excerpt"])

    def test_an_absent_field_leaves_the_severity_alone(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["136.70.153.197"])
        self.assertIsNone(self.result(svc, [w])[0].get("severity"))


class TestAiSecurityCollectCluster(unittest.TestCase):
    """One end-to-end pass over ai-security-audit's real collection plan --
    a workload dump and a Service dump, joined for one check and read alone
    by the other five."""

    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def run_with(self, workload_items=(), service_items=()):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                if argv[2] == collect.COMPLIANCE_DUMP_KINDS:
                    return Run(argv, 0, json.dumps(dump_of(*workload_items)), "", 0.1)
                if argv[2] == "svc":
                    return Run(argv, 0, json.dumps(dump_of(*service_items)), "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.collect_cluster(self.CLUSTER, "ai-security-audit", collect.AI_SECURITY_CHECKS, run=run)

    def test_a_cluster_with_no_ai_workloads_still_runs_every_check(self):
        result = self.run_with(workload_items=[deployment("web")], service_items=[])
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["commands"]), 6)

    def test_a_dirty_cluster_reports_across_both_sources(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"}, container={"args": ["--trust-remote-code"], "image": "acme/vllm:latest"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        result = self.run_with(workload_items=[w], service_items=[svc])
        slugs = {c["check"] for c in result["candidates"]}
        self.assertIn("model-remote-code-trusted", slugs)
        self.assertIn("model-image-floating-tag", slugs)
        self.assertIn("inference-endpoint-public", slugs)

    def test_a_gate_failure_on_the_service_dump_fails_the_whole_cluster(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"] and argv[2] == collect.COMPLIANCE_DUMP_KINDS:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:2] == ["kubectl", "get"] and argv[2] == "svc":
                return Run(argv, 1, "", "RBAC forbidden", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(self.CLUSTER, "ai-security-audit", collect.AI_SECURITY_CHECKS, run=run)
        self.assertEqual(result["outcome"], "gate-failed")
        self.assertNotIn("candidates", result)

    def test_the_service_dump_backs_only_inference_endpoint_public(self):
        result = self.run_with(workload_items=[deployment("web")], service_items=[])
        commands_by_slug = {c["check"]: c["command"] for c in result["commands"]}
        self.assertIn(" svc ", commands_by_slug["inference-endpoint-public"])
        for slug in commands_by_slug:
            if slug != "inference-endpoint-public":
                self.assertIn(collect.COMPLIANCE_DUMP_KINDS, commands_by_slug[slug])


class TestEvidenceCommandsArePasteable(unittest.TestCase):
    """Every published command is an offer: run this and see what we saw.

    The ledger says so in as many words — "these are re-runnable so that it
    does not have to be taken on trust". A command that a shell refuses is
    worse than no command at all, because the reader concludes the finding is
    junk rather than the rendering. `compliance-audit` shipped seventeen
    criticals on 2026-08-29 citing

        gcloud ... --format json(workloadIdentityConfig,privateClusterConfig,...)

    which answers `Syntax error: "(" unexpected`, rc=2 — the argv was correct
    and only the space-join that rendered it was not.
    """

    MODULES = ("collect", "fleet_drift", "fleet_stockout", "fleet_waste", "patch_readiness")

    def test_no_collector_renders_an_argv_by_space_joining_it(self):
        # A behavioural test can only reach the argvs some fixture happens to
        # provoke. This reaches all of them, including the ones no test builds
        # yet, and it is the check that fails when the next site is added.
        pattern = re.compile(r"""["'] ["']\.join\((\w*argv|cmd)\)""")
        for name in self.MODULES:
            with self.subTest(module=name):
                source = (Path(__file__).resolve().parent / f"{name}.py").read_text()
                self.assertEqual(
                    pattern.findall(source),
                    [],
                    f"{name}.py renders an argv with a space-join; use shlex.join",
                )

    def test_the_compliance_describe_command_survives_a_shell(self):
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(
                    {"name": "kube-agents-host", "location": "us-east4", "project": "adamparco-kage"},
                    "compliance-audit",
                    collect.COMPLIANCE_CHECKS,
                    run=self._run,
                )
        commands = {c["check"]: c["command"] for c in result["commands"]}
        rendered = commands["public-control-plane"]
        self.assertIn("--format", rendered)
        # The whole `json(...)` selector arrives as one word, parentheses and
        # all, rather than as a subshell the shell then tries to open.
        selector = shlex.split(rendered)[shlex.split(rendered).index("--format") + 1]
        self.assertTrue(selector.startswith("json(") and selector.endswith(")"), selector)
        self.assertEqual(shlex.split(rendered), self.describe_argv)

    def setUp(self):
        self.describe_argv = None

    def _run(self, argv, **kwargs):
        if argv[:4] == ["gcloud", "container", "clusters", "describe"]:
            self.describe_argv = list(argv)
            return Run(argv, 0, json.dumps({"privateClusterConfig": {}}), "", 0.1)
        if argv[:2] == ["kubectl", "get"] and argv[2] == collect.COMPLIANCE_DUMP_KINDS:
            return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
        if argv[:2] == ["gcloud", "container"] and argv[2] == "node-pools":
            return Run(argv, 0, "[]", "", 0.1)
        return Run(argv, 0, json.dumps({"items": []}), "", 0.05)


if __name__ == "__main__":
    unittest.main()
