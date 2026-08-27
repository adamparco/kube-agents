#!/usr/bin/env python3
"""Unit tests for networking_audit.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import networking_audit as na  # noqa: E402

FLEET_AUDIT_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "..", "fleet-audit", "scripts")
sys.path.insert(0, os.path.abspath(FLEET_AUDIT_SCRIPTS))


def run_of(rc: int, stdout: str = "", stderr: str = "") -> na.Run:
    return na.Run(["gcloud"], rc, stdout, stderr, 0.01)


class RunAndGateTest(unittest.TestCase):
    def test_gate_closes_on_nonzero_rc(self):
        parsed, result = na.run_and_gate(["x"], run=lambda argv: run_of(1, "[]", "denied"))
        self.assertIsNone(parsed)
        self.assertEqual(result.rc, 1)

    def test_gate_closes_on_empty_stdout(self):
        parsed, _ = na.run_and_gate(["x"], run=lambda argv: run_of(0, "   "))
        self.assertIsNone(parsed)

    def test_gate_closes_on_non_json(self):
        parsed, _ = na.run_and_gate(["x"], run=lambda argv: run_of(0, "not json"))
        self.assertIsNone(parsed)

    def test_gate_opens_on_clean_json(self):
        parsed, _ = na.run_and_gate(["x"], run=lambda argv: run_of(0, "[1, 2]"))
        self.assertEqual(parsed, [1, 2])


class UrlHelpersTest(unittest.TestCase):
    def test_last_segment(self):
        self.assertEqual(na._last_segment("https://x/y/z"), "z")
        self.assertEqual(na._last_segment(""), "")

    def test_region_of_subnet_link(self):
        link = "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1/subnetworks/gke-pods-subnet"
        self.assertEqual(na._region_of_subnet_link(link), "us-central1")

    def test_region_of_subnet_link_empty(self):
        self.assertEqual(na._region_of_subnet_link(""), "")


class SubnetIpExhaustionTest(unittest.TestCase):
    def test_flags_primary_range_over_85_percent(self):
        hit = na.check_subnet_ip_exhaustion(
            {"subnetwork": ".../subnetworks/gke-pods-subnet", "ipCidrRange": "10.0.0.0/24", "ipUtilization": 0.9}
        )
        self.assertEqual(hit["object"], "Subnet/gke-pods-subnet")
        self.assertIn("90.0%", hit["excerpt"])

    def test_flags_secondary_range_over_85_percent(self):
        hit = na.check_subnet_ip_exhaustion(
            {
                "subnetwork": ".../subnetworks/gke-pods-subnet",
                "ipCidrRange": "10.0.0.0/24",
                "ipUtilization": 0.2,
                "secondaryIpRanges": [{"rangeName": "pods", "ipCidrRange": "10.4.0.0/20", "ipUtilization": 0.95}],
            }
        )
        self.assertIn("secondary range pods", hit["excerpt"])

    def test_does_not_flag_healthy_subnet(self):
        hit = na.check_subnet_ip_exhaustion(
            {"subnetwork": ".../subnetworks/gke-pods-subnet", "ipCidrRange": "10.0.0.0/24", "ipUtilization": 0.5}
        )
        self.assertIsNone(hit)

    def test_missing_utilization_field_is_not_a_crash(self):
        hit = na.check_subnet_ip_exhaustion({"subnetwork": ".../subnetworks/x", "ipCidrRange": "10.0.0.0/24"})
        self.assertIsNone(hit)


class RouterNatTest(unittest.TestCase):
    def router(self, **overrides):
        base = {
            "name": "nat-router",
            "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1",
            "nats": [{"name": "nat-gw", "natIpAllocateOption": "AUTO_ONLY", "enableDynamicPortAllocation": True, "maxPortsPerVm": 4096}],
        }
        base.update(overrides)
        return base

    def test_flags_missing_auto_allocated_ip(self):
        status = {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": []}]}}
        hit = na.check_router_nat(self.router(), status, [])
        self.assertEqual(hit["object"], "Router/nat-router")
        self.assertIn("no auto-allocated external IP", hit["excerpt"])

    def test_does_not_flag_healthy_auto_allocation(self):
        status = {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": ["34.1.2.3"]}]}}
        mapping = [{"instanceName": "vm-1", "interfaceNatMappings": [{"numTotalNatPorts": 512}]}]
        hit = na.check_router_nat(self.router(), status, mapping)
        self.assertIsNone(hit)

    def test_flags_port_ceiling_at_80_percent(self):
        status = {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": ["34.1.2.3"]}]}}
        mapping = [{"instanceName": "vm-1", "interfaceNatMappings": [{"numTotalNatPorts": 3277}]}]  # 80.0% of 4096
        hit = na.check_router_nat(self.router(), status, mapping)
        self.assertIn("vm-1", hit["excerpt"])
        self.assertIn("3277/4096", hit["excerpt"])

    def test_fixed_allocation_uses_min_ports_per_vm_as_ceiling(self):
        router = self.router(nats=[{"name": "nat-gw", "natIpAllocateOption": "MANUAL_ONLY", "minPortsPerVm": 64, "natIps": ["34.1.2.3"]}])
        mapping = [{"instanceName": "vm-1", "interfaceNatMappings": [{"numTotalNatPorts": 64}]}]
        hit = na.check_router_nat(router, None, mapping)
        self.assertIn("64/64", hit["excerpt"])

    def test_no_mapping_data_is_not_a_crash(self):
        hit = na.check_router_nat(self.router(), {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": ["1.2.3.4"]}]}}, None)
        self.assertIsNone(hit)


class PscRoutingTest(unittest.TestCase):
    def test_flags_rejected_service_attachment(self):
        hits = na.check_psc_routing(
            [{"name": "psc-ep-1", "target": "projects/p/regions/us-central1/serviceAttachments/sa-1", "pscConnectionStatus": "REJECTED"}]
        )
        self.assertEqual(hits, [{"object": "ForwardingRule/psc-ep-1", "excerpt": "pscConnectionStatus: REJECTED"}])

    def test_does_not_flag_accepted(self):
        hits = na.check_psc_routing(
            [{"name": "psc-ep-2", "target": "projects/p/regions/us-central1/serviceAttachments/sa-2", "pscConnectionStatus": "ACCEPTED"}]
        )
        self.assertEqual(hits, [])

    def test_ignores_non_psc_forwarding_rules(self):
        hits = na.check_psc_routing([{"name": "lb-rule", "target": "projects/p/global/targetHttpProxies/lb", "pscConnectionStatus": ""}])
        self.assertEqual(hits, [])

    def test_empty_list(self):
        self.assertEqual(na.check_psc_routing([]), [])


class MtuMismatchTest(unittest.TestCase):
    def test_flags_active_peering_with_differing_mtu(self):
        networks = [
            {"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-b", "state": "ACTIVE"}]},
            {"name": "vpc-b", "mtu": 1500, "peerings": [{"network": ".../networks/vpc-a", "state": "ACTIVE"}]},
        ]
        hits = na.check_mtu_mismatch(networks)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "NetworkPeering/vpc-a--vpc-b")

    def test_does_not_double_count_the_pair_from_both_sides(self):
        networks = [
            {"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-b", "state": "ACTIVE"}]},
            {"name": "vpc-b", "mtu": 1500, "peerings": [{"network": ".../networks/vpc-a", "state": "ACTIVE"}]},
        ]
        hits = na.check_mtu_mismatch(networks)
        self.assertEqual(len(hits), 1)

    def test_does_not_flag_matching_mtu(self):
        networks = [
            {"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-b", "state": "ACTIVE"}]},
            {"name": "vpc-b", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-a", "state": "ACTIVE"}]},
        ]
        self.assertEqual(na.check_mtu_mismatch(networks), [])

    def test_does_not_flag_inactive_peering(self):
        networks = [
            {"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-b", "state": "INACTIVE"}]},
            {"name": "vpc-b", "mtu": 1500, "peerings": []},
        ]
        self.assertEqual(na.check_mtu_mismatch(networks), [])

    def test_peer_outside_this_project_is_not_a_crash(self):
        networks = [{"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/other-project-vpc", "state": "ACTIVE"}]}]
        self.assertEqual(na.check_mtu_mismatch(networks), [])


class CloudArmorTest(unittest.TestCase):
    def test_flags_preview_rule_on_production_backend(self):
        policies = [{"name": "waf-1", "rules": [{"priority": 1000, "preview": True}, {"priority": 2147483647, "preview": False}]}]
        backends = [{"name": "checkout-api", "securityPolicy": ".../securityPolicies/waf-1"}]
        hits = na.check_cloud_armor(policies, backends)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "SecurityPolicy/waf-1")
        self.assertIn("checkout-api", hits[0]["excerpt"])

    def test_does_not_flag_preview_on_staging_backend(self):
        policies = [{"name": "waf-1", "rules": [{"priority": 1000, "preview": True}]}]
        backends = [{"name": "checkout-staging", "securityPolicy": ".../securityPolicies/waf-1"}]
        self.assertEqual(na.check_cloud_armor(policies, backends), [])

    def test_ignores_the_implicit_default_rule_in_preview(self):
        policies = [{"name": "waf-1", "rules": [{"priority": 2147483647, "preview": True}]}]
        backends = [{"name": "checkout-api", "securityPolicy": ".../securityPolicies/waf-1"}]
        self.assertEqual(na.check_cloud_armor(policies, backends), [])

    def test_flags_conflicting_priorities_regardless_of_attachment(self):
        policies = [{"name": "waf-2", "rules": [{"priority": 1000}, {"priority": 1000}]}]
        hits = na.check_cloud_armor(policies, [])
        self.assertIn("conflicting rule priorities: [1000]", hits[0]["excerpt"])

    def test_unattached_policy_is_never_flagged_for_preview(self):
        policies = [{"name": "waf-3", "rules": [{"priority": 1000, "preview": True}]}]
        self.assertEqual(na.check_cloud_armor(policies, []), [])


class CollectProjectTest(unittest.TestCase):
    def fake_run(self, responses: dict) -> na.RunFn:
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

    def test_subnet_targets_and_project_target_both_collected(self):
        responses = {
            "subnets list-usable": run_of(
                0,
                '[{"subnetwork": "https://x/projects/proj-1/regions/us-central1/subnetworks/s1", '
                '"ipCidrRange": "10.0.0.0/24", "ipUtilization": 0.1}]',
            ),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        names = {e["name"] for e in entries}
        self.assertIn("proj-1/us-central1/s1", names)
        self.assertIn("project/proj-1", names)
        self.assertEqual(len(entries), 2)
        project_entry = next(e for e in entries if e["name"] == "project/proj-1")
        self.assertEqual(project_entry["outcome"], "collected")
        self.assertEqual({c["check"] for c in project_entry["commands"]}, {
            "cloud-nat-exhaustion", "psc-routing-deadlock", "mtu-packet-fragmentation", "cloud-armor-false-positive",
        })

    def test_subnets_list_usable_failure_surfaces_a_gate_failed_entry_and_the_project_target_survives(self):
        responses = {
            "subnets list-usable": run_of(1, "", "denied"),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        self.assertEqual(len(entries), 2)
        subnet_entry = next(e for e in entries if e["name"] == "project/proj-1/subnets")
        self.assertEqual(subnet_entry["outcome"], "gate-failed")
        self.assertIn("subnet-ip-exhaustion", subnet_entry["error"])
        project_entry = next(e for e in entries if e["name"] == "project/proj-1")
        self.assertEqual(project_entry["outcome"], "collected")

    def test_one_failed_project_level_read_gates_the_whole_project_target_closed(self):
        responses = {
            "subnets list-usable": run_of(0, "[]"),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(1, "", "permission denied"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        project_entry = next(e for e in entries if e["name"] == "project/proj-1")
        self.assertEqual(project_entry["outcome"], "gate-failed")
        self.assertIn("mtu-packet-fragmentation", project_entry["error"])

    def test_router_with_nat_pulls_status_and_mapping(self):
        responses = {
            "subnets list-usable": run_of(0, "[]"),
            "routers list": run_of(
                0,
                '[{"name": "r1", "region": ".../regions/us-central1", '
                '"nats": [{"name": "n1", "natIpAllocateOption": "AUTO_ONLY", "enableDynamicPortAllocation": true, "maxPortsPerVm": 4096}]}]',
            ),
            "get-status": run_of(0, '{"result": {"natStatus": [{"name": "n1", "autoAllocatedNatIps": []}]}}'),
            "get-nat-mapping-info": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        project_entry = next(e for e in entries if e["name"] == "project/proj-1")
        self.assertEqual(len(project_entry["candidates"]), 1)
        self.assertEqual(project_entry["candidates"][0]["check"], "cloud-nat-exhaustion")

    def test_router_without_nats_skips_status_and_mapping_calls(self):
        responses = {
            "subnets list-usable": run_of(0, "[]"),
            "routers list": run_of(0, '[{"name": "r1", "region": ".../regions/us-central1", "nats": []}]'),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        project_entry = next(e for e in entries if e["name"] == "project/proj-1")
        self.assertEqual(project_entry["outcome"], "collected")
        self.assertEqual(project_entry["candidates"], [])


class CollectFleetTest(unittest.TestCase):
    def test_sweeps_every_monitored_project(self):
        os.environ["MONITORED_PROJECT_IDS"] = "proj-1,proj-2"
        os.environ.pop("GCP_PROJECT_ID", None)
        try:
            def run(argv, **kwargs):
                return run_of(0, "[]")

            manifest = na.collect_fleet(run=run)
            projects = {c["project"] for c in manifest["clusters"]}
            self.assertEqual(projects, {"proj-1", "proj-2"})
            self.assertEqual(manifest["audit"], "gcp-networking-fabric-audit")
            self.assertIn("version", manifest)
        finally:
            os.environ.pop("MONITORED_PROJECT_IDS", None)

    def test_single_project_override(self):
        def run(argv, **kwargs):
            return run_of(0, "[]")

        manifest = na.collect_fleet("proj-only", run=run)
        self.assertEqual({c["project"] for c in manifest["clusters"]}, {"proj-only"})


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    """`collect_fleet`'s real output, run through `audit_report`'s own
    `cross_check_manifest` — the same integration shape
    `test_collect.py`'s `TestManifestComposesWithAuditReport` uses for the
    other two streams."""

    def test_checks_run_copied_from_a_collected_target_survives_cross_check(self):
        import audit_report

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "subnets list-usable" in joined:
                return run_of(
                    0,
                    '[{"subnetwork": "https://x/projects/proj-1/regions/us-central1/subnetworks/s1", '
                    '"ipCidrRange": "10.0.0.0/24", "ipUtilization": 0.9}]',
                )
            return run_of(0, "[]")

        manifest = na.collect_fleet("proj-1", run=run)
        subnet_entry = next(c for c in manifest["clusters"] if c["name"] == "proj-1/us-central1/s1")
        project_entry = next(c for c in manifest["clusters"] if c["name"] == "project/proj-1")

        data = {
            "audit": "gcp-networking-fabric-audit",
            "scope": {
                "clusters": [
                    {"name": subnet_entry["name"], "checks_run": [{"check": "subnet-ip-exhaustion", "command": subnet_entry["commands"][0]["command"]}]},
                    {
                        "name": project_entry["name"],
                        "checks_run": [{"check": c["check"], "command": c["command"]} for c in project_entry["commands"]],
                    },
                ],
                "skipped": [],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_check_claimed_but_absent_from_a_collected_target_is_rejected(self):
        import audit_report

        def run(argv, **kwargs):
            return run_of(0, "[]")

        manifest = na.collect_fleet("proj-1", run=run)
        project_entry = next(c for c in manifest["clusters"] if c["name"] == "project/proj-1")
        data = {
            "audit": "gcp-networking-fabric-audit",
            "scope": {
                "clusters": [
                    {"name": project_entry["name"], "checks_run": [{"check": "subnet-ip-exhaustion", "command": "gcloud compute networks subnets list-usable --project proj-1 --format json"}]}
                ],
                "skipped": [],
            },
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)


if __name__ == "__main__":
    unittest.main()
