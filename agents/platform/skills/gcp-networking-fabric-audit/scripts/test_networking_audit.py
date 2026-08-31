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

    def test_an_empty_list_usable_with_visible_subnets_surfaces_a_gap(self):
        # The production shape. On the deployed install `list-usable` answers
        # rc=0 with `[]` because the audit identity lacks
        # compute.subnetworks.use, while `subnets list` sees 42. rc=0 and
        # valid JSON satisfy run_and_gate, so before this the scope was
        # reported fully covered with zero subnets in it -- subnet-ip-
        # exhaustion silently measured nothing on every run.
        responses = {
            "subnets list-usable": run_of(0, "[]"),
            "subnets list": run_of(0, '[{"name": "s1"}, {"name": "s2"}]'),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        subnet_entry = next(e for e in entries if e["name"] == "project/proj-1/subnets")
        self.assertEqual(subnet_entry["outcome"], "gate-failed")
        self.assertIn("subnet-ip-exhaustion", subnet_entry["error"])
        self.assertIn("compute.subnetworks.use", subnet_entry["error"])
        self.assertIn("2", subnet_entry["error"])

    def test_subnets_without_the_utilization_field_are_a_gap_not_a_clean_pass(self):
        # What the deployed install returns once compute.subnetworks.use is
        # granted: real subnets, and not one carries ipUtilization -- the
        # field is absent from gcloud's UsableSubnetwork in v1, beta and
        # alpha. check_subnet_ip_exhaustion returns None for each, which
        # reads exactly like "measured, all healthy" unless it is caught here.
        # With the Network Analyzer fallback also unavailable (API off, or the
        # insight read refused), the gap is the only honest outcome.
        responses = {
            "subnets list-usable": run_of(
                0,
                '[{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s1", '
                '"ipCidrRange": "10.0.0.0/20"}, '
                '{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s2", '
                '"ipCidrRange": "10.1.0.0/20", '
                '"secondaryIpRanges": [{"rangeName": "pods", "ipCidrRange": "10.4.0.0/14"}]}]',
            ),
            "recommender insights list": run_of(1, "", "PERMISSION_DENIED"),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        subnet_entries = [e for e in entries if e["name"].endswith("/subnets")]
        self.assertEqual(len(subnet_entries), 1)
        self.assertEqual(subnet_entries[0]["outcome"], "gate-failed")
        self.assertIn("ipUtilization", subnet_entries[0]["error"])
        self.assertIn("2 subnets", subnet_entries[0]["error"])
        # Name the permission an operator has to grant, not just the symptom.
        self.assertIn("networkAnalyzerIpAddressInsights.list", subnet_entries[0]["error"])
        # And no per-subnet target claiming it was collected.
        self.assertEqual([e for e in entries if e["name"].startswith("proj-1/")], [])

    # --- Network Analyzer fallback ------------------------------------- #
    #
    # gcloud's UsableSubnetwork carries no utilization field on any API
    # version, so `list-usable` alone can never run subnet-ip-exhaustion. The
    # measurement lives in google.networkanalyzer.vpcnetwork.ipAddressInsight
    # -- not google.compute.subnetwork.IpUtilizationInsight, which is not a
    # real insight type and which the API rejects with INVALID_ARGUMENT.

    INSIGHT = (
        '[{"content": {"ipUtilizationSummaryInfo": [{'
        '"projectUri": "//cloudresourcemanager.googleapis.com/projects/proj-1", '
        '"networkStats": [{'
        '"networkUri": "//compute.googleapis.com/projects/proj-1/global/networks/default", '
        '"subnetStats": [{'
        '"subnetUri": "//compute.googleapis.com/projects/proj-1/regions/us-east4/subnetworks/s1", '
        '"subnetRangeStats": ['
        '{"allocationRatio": 0.9, "subnetRangePrefix": "10.0.0.0/20"}, '
        '{"allocationRatio": 0.95, "subnetRangeName": "pods", "subnetRangePrefix": "10.4.0.0/14"}'
        ']}]}]}]}}]'
    )

    def _two_subnets(self) -> str:
        return (
            '[{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s1", '
            '"ipCidrRange": "10.0.0.0/20", '
            '"secondaryIpRanges": [{"rangeName": "pods", "ipCidrRange": "10.4.0.0/14"}]}, '
            '{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s2", '
            '"ipCidrRange": "10.1.0.0/20"}]'
        )

    def test_the_insight_backfills_utilization_and_the_check_then_fires(self):
        responses = {
            "subnets list-usable": run_of(0, self._two_subnets()),
            "recommender insights list": run_of(0, self.INSIGHT),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        # No gate-failed target: the fallback supplied the missing field.
        self.assertEqual([e for e in entries if e["name"].endswith("/subnets")], [])
        s1 = next(e for e in entries if e["name"] == "proj-1/us-east4/s1")
        self.assertEqual(s1["outcome"], "collected")
        # 0.9 primary and 0.95 secondary are both over the 0.85 threshold.
        excerpt = s1["candidates"][0]["excerpt"]
        self.assertIn("primary range 10.0.0.0/20", excerpt)
        self.assertIn("secondary range pods", excerpt)

    def test_a_subnet_the_insight_does_not_cover_is_not_applicable_not_clean(self):
        # Network Analyzer omits subnets holding no allocations, which on an
        # auto-mode network means it reports 1 of 42. Running the check
        # against the other 41 would return None for each -- "nothing wrong
        # here" -- so they have to be declared unmeasured instead.
        responses = {
            "subnets list-usable": run_of(0, self._two_subnets()),
            "recommender insights list": run_of(0, self.INSIGHT),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        s2 = next(e for e in entries if e["name"] == "proj-1/us-east4/s2")
        not_applicable = {na_entry["check"] for na_entry in s2["checks_not_applicable"]}
        self.assertIn("subnet-ip-exhaustion", not_applicable)
        self.assertEqual(s2["candidates"], [])
        # The covered one must NOT carry the not-applicable marker.
        s1 = next(e for e in entries if e["name"] == "proj-1/us-east4/s1")
        self.assertNotIn(
            "subnet-ip-exhaustion",
            {na_entry["check"] for na_entry in s1["checks_not_applicable"]},
        )

    def test_an_unmeasured_subnet_carries_the_limitation_finish_will_ask_for(self):
        # An unmeasured subnet owes one check and has it declared
        # not-applicable, so §6 filters its `checks_run` down to []. `finish`
        # rejects an empty `checks_run` unless that target says in
        # `limitations` why nothing ran, and the model writing that sentence is
        # what produced three different restatements across three live runs.
        # Emitting it here makes the wording the collector's, not a model's.
        responses = {
            "subnets list-usable": run_of(0, self._two_subnets()),
            "recommender insights list": run_of(0, self.INSIGHT),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        s2 = next(e for e in entries if e["name"] == "proj-1/us-east4/s2")
        self.assertEqual(s2["limitations"], na.UNMEASURED_SUBNET_LIMITATION)
        self.assertIn("subnet-ip-exhaustion", s2["limitations"])
        # The measured subnet ran its check, so a limitation there would put a
        # target that refused nothing into `coverage_gaps` and make the run
        # partial.
        s1 = next(e for e in entries if e["name"] == "proj-1/us-east4/s1")
        self.assertNotIn("limitations", s1)
        # Provenance is unchanged: the unmeasured subnet still records the pair
        # of reads that established it as unmeasured. Dropping the command is
        # the tempting way to clear the same `finish` rejection, and it would
        # hand the reader a target with no evidence of having been looked at.
        self.assertEqual(
            [c["check"] for c in s2["commands"]], ["subnet-ip-exhaustion"]
        )

    # A subnet whose primary and one secondary are published, and whose other
    # secondary is not. This is `us-east4/default` on the live fleet: Network
    # Analyzer published 14 of its 16 pod ranges, omitting the two belonging to
    # Autopilot clusters parked at zero nodes.
    PARTIAL_INSIGHT = (
        '[{"content": {"ipUtilizationSummaryInfo": [{'
        '"projectUri": "//cloudresourcemanager.googleapis.com/projects/proj-1", '
        '"networkStats": [{'
        '"networkUri": "//compute.googleapis.com/projects/proj-1/global/networks/default", '
        '"subnetStats": [{'
        '"subnetUri": "//compute.googleapis.com/projects/proj-1/regions/us-east4/subnetworks/s1", '
        '"subnetRangeStats": ['
        '{"allocationRatio": 0.1, "subnetRangePrefix": "10.0.0.0/20"}, '
        '{"allocationRatio": 0.2, "subnetRangeName": "pods", "subnetRangePrefix": "10.4.0.0/14"}'
        ']}]}]}]}}]'
    )

    def _subnet_with_two_secondaries(self) -> str:
        return (
            '[{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s1", '
            '"ipCidrRange": "10.0.0.0/20", '
            '"secondaryIpRanges": ['
            '{"rangeName": "pods", "ipCidrRange": "10.4.0.0/14"}, '
            '{"rangeName": "ap-pods", "ipCidrRange": "10.8.0.0/14"}]}]'
        )

    def _partial_responses(self) -> dict:
        return {
            "subnets list-usable": run_of(0, self._subnet_with_two_secondaries()),
            "recommender insights list": run_of(0, self.PARTIAL_INSIGHT),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }

    def test_a_range_the_insight_omits_is_named_rather_than_silently_cleared(self):
        # The subnet-level gate asks `any`, so two measured ranges mark this
        # subnet measured and `check_subnet_ip_exhaustion` returns nothing for
        # the third -- indistinguishable from clearing it. The subnet keeps its
        # verdict for what was measured; the limitation says what it does not
        # cover, which is what §6 turns into a coverage gap.
        entries = na.collect_project("proj-1", run=self.fake_run(self._partial_responses()))
        s1 = next(e for e in entries if e["name"] == "proj-1/us-east4/s1")
        self.assertEqual(s1["outcome"], "collected")
        self.assertIn("ap-pods", s1["limitations"])
        self.assertIn("2 of 3 ranges", s1["limitations"])
        # Named by the check that could not reach it, so `coverage_gaps` sees a
        # slug outside `checks_not_applicable` and keeps the string.
        self.assertIn("subnet-ip-exhaustion", s1["limitations"])
        # It stays measured: declaring the check inapplicable here would take a
        # check that ran on two thirds of the subnet out of the denominator,
        # and `cross_check_manifest` rejects exactly that.
        self.assertNotIn(
            "subnet-ip-exhaustion",
            {entry["check"] for entry in s1["checks_not_applicable"]},
        )
        self.assertEqual([c["check"] for c in s1["commands"]], ["subnet-ip-exhaustion"])

    def test_the_measured_ranges_still_reach_a_verdict(self):
        # The limitation must not cost the subnet the finding it did earn. Same
        # fixture, with the one published secondary over the threshold.
        responses = self._partial_responses()
        responses["recommender insights list"] = run_of(
            0, self.PARTIAL_INSIGHT.replace('"allocationRatio": 0.2', '"allocationRatio": 0.95')
        )
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        s1 = next(e for e in entries if e["name"] == "proj-1/us-east4/s1")
        self.assertIn("secondary range pods", s1["candidates"][0]["excerpt"])
        self.assertIn("ap-pods", s1["limitations"])

    def test_a_fully_covered_subnet_carries_no_limitation(self):
        # The other half: the limitation appears because a range was missed,
        # not because the code now writes one on every subnet. A subnet whose
        # every range was published must stay clean, or the stream is partial
        # in perpetuity for no reason.
        responses = self._partial_responses()
        responses["subnets list-usable"] = run_of(
            0,
            '[{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s1", '
            '"ipCidrRange": "10.0.0.0/20", '
            '"secondaryIpRanges": [{"rangeName": "pods", "ipCidrRange": "10.4.0.0/14"}]}]',
        )
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        s1 = next(e for e in entries if e["name"] == "proj-1/us-east4/s1")
        self.assertNotIn("limitations", s1)

    def test_unmeasured_ranges_names_the_primary_and_reads_presence(self):
        self.assertEqual(na._unmeasured_ranges({"ipUtilization": 0.0}), [])
        self.assertEqual(
            na._unmeasured_ranges({"ipCidrRange": "10.0.0.0/20"}), ["the primary range"]
        )
        # 0.0 is measured-and-empty on a secondary too.
        self.assertEqual(
            na._unmeasured_ranges(
                {"ipUtilization": 0.1, "secondaryIpRanges": [{"rangeName": "a", "ipUtilization": 0.0}]}
            ),
            [],
        )
        self.assertEqual(
            na._unmeasured_ranges(
                {"ipUtilization": 0.1, "secondaryIpRanges": [{"rangeName": "a"}, {"ipUtilization": 0.3}]}
            ),
            ["a"],
        )

    def test_an_insight_that_covers_nothing_is_a_gap(self):
        # Reads cleanly, publishes nothing that matches. Distinct from the
        # read failing, and the message has to say so -- Network Analyzer
        # takes about a day to publish after the API is switched on.
        responses = {
            "subnets list-usable": run_of(0, self._two_subnets()),
            "recommender insights list": run_of(0, "[]"),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        subnet_entry = next(e for e in entries if e["name"] == "project/proj-1/subnets")
        self.assertEqual(subnet_entry["outcome"], "gate-failed")
        self.assertIn("published stats for 0", subnet_entry["error"])
        self.assertEqual([e for e in entries if e["name"].startswith("proj-1/")], [])

    def test_the_insight_is_not_consulted_when_list_usable_already_answers(self):
        # If a future gcloud starts populating the field, the extra API call
        # is waste -- and an unstubbed `recommender` here would raise.
        responses = {
            "subnets list-usable": run_of(
                0,
                '[{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s1", '
                '"ipCidrRange": "10.0.0.0/20", "ipUtilization": 0.1}]',
            ),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        self.assertEqual(
            next(e for e in entries if e["name"] == "proj-1/us-east4/s1")["outcome"],
            "collected",
        )

    def test_the_published_command_names_the_read_that_produced_the_figure(self):
        # `list-usable` enumerated the subnets; the insight measured them. A
        # reader handed only the enumeration re-runs it, finds no
        # `ipUtilization` anywhere in the output, and cannot check the verdict
        # against anything -- so the recorded command has to name both.
        responses = {
            "subnets list-usable": run_of(0, self._two_subnets()),
            "recommender insights list": run_of(0, self.INSIGHT),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        s1 = next(e for e in entries if e["name"] == "proj-1/us-east4/s1")
        command = next(c for c in s1["commands"] if c["check"] == "subnet-ip-exhaustion")["command"]
        self.assertTrue(command.startswith("gcloud compute networks subnets list-usable"))
        self.assertIn(f"--insight-type {na._IP_INSIGHT_TYPE}", command)
        # Both halves run, in order, from one shell -- not two lines a reader
        # has to reassemble. The appendix caps a command at 2000 characters.
        self.assertIn(" && gcloud recommender insights list", command)
        self.assertLess(len(command), 2000)
        # Every subnet in the run carries it, including the unmeasured one:
        # the same pair of reads is what established it as unmeasured.
        s2 = next(e for e in entries if e["name"] == "proj-1/us-east4/s2")
        self.assertEqual(
            next(c for c in s2["commands"] if c["check"] == "subnet-ip-exhaustion")["command"],
            command,
        )

    def test_the_published_command_is_the_enumeration_alone_when_it_answers(self):
        # The mirror of the test above: no backfill ran, so naming the insight
        # would credit a read this run never made.
        responses = {
            "subnets list-usable": run_of(
                0,
                '[{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s1", '
                '"ipCidrRange": "10.0.0.0/20", "ipUtilization": 0.1}]',
            ),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        s1 = next(e for e in entries if e["name"] == "proj-1/us-east4/s1")
        command = next(c for c in s1["commands"] if c["check"] == "subnet-ip-exhaustion")["command"]
        self.assertNotIn("recommender", command)
        self.assertNotIn("&&", command)

    def test_utilization_by_subnet_splits_primary_from_secondary(self):
        # The insight marks the primary range by omitting subnetRangeName.
        # Reading a named entry as the primary would attribute a pod range's
        # utilization to the node range.
        by_subnet = na._utilization_by_subnet(
            "proj-1", run=self.fake_run({"recommender insights list": run_of(0, self.INSIGHT)})
        )
        self.assertEqual(list(by_subnet), ["us-east4/s1"])
        self.assertEqual(by_subnet["us-east4/s1"]["primary"], 0.9)
        self.assertEqual(by_subnet["us-east4/s1"]["secondary"], {"pods": 0.95})

    def test_utilization_key_matches_across_the_two_uri_forms(self):
        # `list-usable` returns an https:// self-link and the insight returns
        # a //compute.googleapis.com/ resource URI. Keying on the bare name
        # would also collide: an auto-mode network calls a subnet `default` in
        # every one of its 42 regions.
        self.assertEqual(
            na._utilization_key("https://www.googleapis.com/compute/v1/projects/p/regions/us-east4/subnetworks/default"),
            na._utilization_key("//compute.googleapis.com/projects/p/regions/us-east4/subnetworks/default"),
        )
        self.assertNotEqual(
            na._utilization_key("//compute.googleapis.com/projects/p/regions/us-east4/subnetworks/default"),
            na._utilization_key("//compute.googleapis.com/projects/p/regions/us-west1/subnetworks/default"),
        )

    def test_backfill_does_not_overwrite_a_first_party_reading(self):
        subnets = [
            {
                "subnetwork": "https://x/projects/p/regions/us-east4/subnetworks/s1",
                "ipUtilization": 0.2,
                "secondaryIpRanges": [{"rangeName": "pods", "ipUtilization": 0.3}],
            }
        ]
        by_subnet = {"us-east4/s1": {"primary": 0.9, "secondary": {"pods": 0.95}}}
        self.assertEqual(na._backfill_utilization(subnets, by_subnet), 0)
        self.assertEqual(subnets[0]["ipUtilization"], 0.2)
        self.assertEqual(subnets[0]["secondaryIpRanges"][0]["ipUtilization"], 0.3)

    def test_backfill_reports_how_many_subnets_it_reached(self):
        subnets = [
            {"subnetwork": "https://x/projects/p/regions/us-east4/subnetworks/s1"},
            {"subnetwork": "https://x/projects/p/regions/us-west1/subnetworks/s2"},
        ]
        by_subnet = {"us-east4/s1": {"primary": 0.4, "secondary": {}}}
        self.assertEqual(na._backfill_utilization(subnets, by_subnet), 1)
        self.assertEqual(subnets[0]["ipUtilization"], 0.4)
        self.assertNotIn("ipUtilization", subnets[1])

    def test_a_subnet_measured_at_zero_utilization_is_not_treated_as_unmeasured(self):
        # 0.0 is a measurement. Testing presence rather than truthiness is the
        # difference between "this subnet is empty" and "nobody looked".
        self.assertTrue(na._carries_utilization({"ipUtilization": 0.0}))
        self.assertTrue(
            na._carries_utilization(
                {"secondaryIpRanges": [{"rangeName": "pods", "ipUtilization": 0.0}]}
            )
        )
        self.assertFalse(na._carries_utilization({"ipCidrRange": "10.0.0.0/20"}))
        self.assertFalse(
            na._carries_utilization({"secondaryIpRanges": [{"rangeName": "pods"}]})
        )
        self.assertFalse(na._carries_utilization({"ipUtilization": None}))

    def test_an_empty_list_usable_with_no_subnets_at_all_is_not_a_gap(self):
        # The other reading of the same `[]`, and it must stay quiet: a
        # project with no subnets is a real empty scope, and reporting a
        # coverage gap for it would cry wolf on every run.
        responses = {
            "subnets list-usable": run_of(0, "[]"),
            "subnets list": run_of(0, "[]"),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        self.assertEqual([e for e in entries if e["name"].endswith("/subnets")], [])

    def test_an_empty_list_usable_gates_closed_when_the_tie_breaker_also_fails(self):
        # Neither reading can be ruled out, so fail closed rather than pick
        # the cheerful one -- the whole point of the gate.
        responses = {
            "subnets list-usable": run_of(0, "[]"),
            "subnets list": run_of(1, "", "permission denied"),
            "routers list": run_of(0, "[]"),
            "forwarding-rules list": run_of(0, "[]"),
            "networks list": run_of(0, "[]"),
            "security-policies list": run_of(0, "[]"),
            "backend-services list": run_of(0, "[]"),
        }
        entries = na.collect_project("proj-1", run=self.fake_run(responses))
        subnet_entry = next(e for e in entries if e["name"] == "project/proj-1/subnets")
        self.assertEqual(subnet_entry["outcome"], "gate-failed")
        self.assertIn("corroborating", subnet_entry["error"])
        self.assertIn("permission denied", subnet_entry["error"])

    def test_one_failed_project_level_read_gates_the_whole_project_target_closed(self):
        responses = {
            "subnets list-usable": run_of(0, "[]"),
            # Must follow the -usable key: first match wins in insertion
            # order and "subnets list" is a prefix of it. Empty here means
            # the project really has no subnets, so no coverage gap.
            "subnets list": run_of(0, "[]"),
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
            # Must follow the -usable key: first match wins in insertion
            # order and "subnets list" is a prefix of it. Empty here means
            # the project really has no subnets, so no coverage gap.
            "subnets list": run_of(0, "[]"),
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
            # Must follow the -usable key: first match wins in insertion
            # order and "subnets list" is a prefix of it. Empty here means
            # the project really has no subnets, so no coverage gap.
            "subnets list": run_of(0, "[]"),
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

    def _fleet_with_one_unmeasured_subnet(self):
        """One subnet Network Analyzer measured, one it did not — the auto-mode
        shape, 1-of-2 here and 1-of-42 on the live fleet. Utilization is well
        under the threshold so the run carries no findings and the assertions
        below are about coverage alone."""
        insight = (
            '[{"content": {"ipUtilizationSummaryInfo": [{'
            '"projectUri": "//cloudresourcemanager.googleapis.com/projects/proj-1", '
            '"networkStats": [{'
            '"networkUri": "//compute.googleapis.com/projects/proj-1/global/networks/default", '
            '"subnetStats": [{'
            '"subnetUri": "//compute.googleapis.com/projects/proj-1/regions/us-east4/subnetworks/s1", '
            '"subnetRangeStats": [{"allocationRatio": 0.1, "subnetRangePrefix": "10.0.0.0/20"}]'
            "}]}]}]}}]"
        )
        subnets = (
            '[{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s1", '
            '"ipCidrRange": "10.0.0.0/20"}, '
            '{"subnetwork": "https://x/projects/proj-1/regions/us-east4/subnetworks/s2", '
            '"ipCidrRange": "10.1.0.0/20"}]'
        )

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "subnets list-usable" in joined:
                return run_of(0, subnets)
            if "recommender insights list" in joined:
                return run_of(0, insight)
            return run_of(0, "[]")

        return na.collect_fleet("proj-1", run=run)

    @staticmethod
    def _scope_entry(entry: dict) -> dict:
        """§2's rule, applied literally: copy `commands` minus any slug this
        same target declares not-applicable, and carry the collector's
        `checks_not_applicable` and `limitations` through untouched."""
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

    def test_the_section_two_recipe_publishes_on_the_first_attempt(self):
        # The daily failure, end to end. Both halves of §2's rule are needed:
        # the not-applicable filter, or `cross_check_manifest` refuses the
        # claim; and the collector's `limitations`, or `validate_findings`
        # refuses the empty `checks_run` the filter leaves behind.
        import audit_report

        manifest = self._fleet_with_one_unmeasured_subnet()
        s1 = next(c for c in manifest["clusters"] if c["name"].endswith("/s1"))
        s2 = next(c for c in manifest["clusters"] if c["name"].endswith("/s2"))
        project = next(c for c in manifest["clusters"] if c["name"] == "project/proj-1")
        self.assertEqual(s2["limitations"], na.UNMEASURED_SUBNET_LIMITATION)

        data = {
            "audit": "gcp-networking-fabric-audit",
            "scope": {
                "clusters": [self._scope_entry(e) for e in (s1, s2, project)],
                "skipped": [],
            },
            "findings": [],
        }
        # The measured subnet still reports its check; the unmeasured one
        # reports none, which is the shape that used to need a second attempt.
        self.assertEqual([c["check"] for c in data["scope"]["clusters"][0]["checks_run"]],
                         ["subnet-ip-exhaustion"])
        self.assertEqual(data["scope"]["clusters"][1]["checks_run"], [])

        audit_report.cross_check_manifest(data, manifest)
        audit_report.validate_findings(data, "gcp-networking-fabric-audit")
        # And the run is not partial. The unmeasured subnet refused no check
        # its roster still owed, so it is not a coverage gap — if this starts
        # returning the limitation, every auto-mode fleet goes permanently
        # partial and the ledger can never close.
        self.assertEqual(audit_report.coverage_gaps(data), [])

    def test_copying_commands_verbatim_is_the_rejection_section_two_now_avoids(self):
        # §2 used to say "copy its `commands` list verbatim". On an unmeasured
        # subnet that claims IP-exhaustion coverage nobody has, and it is the
        # first of the two rejections the live run paid every morning.
        import audit_report

        manifest = self._fleet_with_one_unmeasured_subnet()
        s2 = next(c for c in manifest["clusters"] if c["name"].endswith("/s2"))
        # Every collected target has to appear or a different rule fires
        # first — only s2's `checks_run` is the unfiltered copy under test.
        clusters = [self._scope_entry(e) for e in manifest["clusters"]]
        verbatim = next(c for c in clusters if c["name"].endswith("/s2"))
        verbatim["checks_run"] = [
            {"check": c["check"], "command": c["command"]} for c in s2["commands"]
        ]
        data = {
            "audit": "gcp-networking-fabric-audit",
            "scope": {"clusters": clusters, "skipped": []},
            "findings": [],
        }
        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.cross_check_manifest(data, manifest)
        self.assertIn("subnet-ip-exhaustion", str(caught.exception))

    def test_filtering_without_the_limitation_is_the_second_rejection(self):
        # And the other half. Dropping the collector's `limitations` — by
        # rewording it to nothing, or by a future collector not emitting it —
        # leaves an empty `checks_run` that reads as "read it, checked
        # nothing", which is the rejection the run paid on its second attempt.
        import audit_report

        manifest = self._fleet_with_one_unmeasured_subnet()
        s2 = next(c for c in manifest["clusters"] if c["name"].endswith("/s2"))
        stripped = self._scope_entry(s2)
        stripped.pop("limitations")
        data = {
            "audit": "gcp-networking-fabric-audit",
            "scope": {"clusters": [stripped], "skipped": []},
            "findings": [],
        }
        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.validate_findings(data, "gcp-networking-fabric-audit")
        self.assertIn("checks_run", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
