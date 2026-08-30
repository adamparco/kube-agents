#!/usr/bin/env python3
"""Tests for fleet_drift.py, the fleet-consistency-drift collector."""

import copy
import json
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import fleet_drift as fd  # noqa: E402

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def run_of(rc: int, stdout: str = "", stderr: str = "") -> fd.Run:
    return fd.Run(["x"], rc, stdout, stderr, 0.01)


def cluster(name, project="acme", location="us-central1", autopilot=False, status="RUNNING", created="2020-01-01T00:00:00Z", labels=None, **overrides):
    doc = {
        "name": name, "_project": project, "location": location, "status": status, "createTime": created,
        "autopilot": {"enabled": autopilot},
        "resourceLabels": labels if labels is not None else {"environment": "prod"},
        "releaseChannel": {"channel": "REGULAR"},
        "shieldedNodes": {"enabled": True},
        "nodePools": [
            {
                "name": "default-pool",
                "config": {"shieldedInstanceConfig": {"enableSecureBoot": True, "enableIntegrityMonitoring": True}, "imageType": "COS_CONTAINERD"},
                "autoscaling": {"enabled": True},
            }
        ],
        "networkConfig": {"datapathProvider": "ADVANCED_DATAPATH", "enableIntraNodeVisibility": True},
        "networkPolicy": {"enabled": False},
        "privateClusterConfig": {"enablePrivateNodes": True, "enablePrivateEndpoint": True},
        "masterAuthorizedNetworksConfig": {"enabled": True, "cidrBlocks": ["10.0.0.0/8"]},
        "loggingConfig": {"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS", "WORKLOADS"]}},
        "monitoringConfig": {"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS"]}, "managedPrometheusConfig": {"enabled": True}},
        "binaryAuthorization": {"evaluationMode": "PROJECT_SINGLETON_POLICY_ENFORCE"},
        "autoscaling": {"enableNodeAutoprovisioning": True},
        "databaseEncryption": {"state": "ENCRYPTED"},
    }
    for path, value in overrides.items():
        target = doc
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return doc


class DiscoverProjectsTest(unittest.TestCase):
    def test_uses_the_given_project(self):
        self.assertEqual(fd.discover_projects("acme", run=lambda a: run_of(0), read_text=lambda p: None), ["acme"])

    def test_falls_back_to_active_gcloud_project(self):
        result = fd.discover_projects(None, run=lambda a: run_of(0, "acme\n"), read_text=lambda p: None)
        self.assertEqual(result, ["acme"])

    def test_adds_inventory_project_ids(self):
        text = "Discovered projects: acme-prod, acme-staging during onboarding."
        result = fd.discover_projects("acme-prod", run=lambda a: run_of(0), read_text=lambda p: text)
        self.assertIn("acme-staging", result)
        self.assertEqual(result[0], "acme-prod")

    def test_missing_inventory_file_is_not_a_crash(self):
        result = fd.discover_projects("acme", run=lambda a: run_of(0), read_text=lambda p: None)
        self.assertEqual(result, ["acme"])


class EnumerateProjectClustersTest(unittest.TestCase):
    def test_tags_each_cluster_with_its_project(self):
        clusters_json = json.dumps([{"name": "c1"}])
        clusters, record, error = fd.enumerate_project_clusters("acme", run=lambda a: run_of(0, clusters_json))
        self.assertEqual(clusters[0]["_project"], "acme")
        self.assertIsNotNone(record)
        self.assertIsNone(error)

    def test_failed_list_returns_empty_with_no_record_and_says_why(self):
        clusters, record, error = fd.enumerate_project_clusters("acme", run=lambda a: run_of(1, "", "denied"))
        self.assertEqual(clusters, [])
        self.assertIsNone(record)
        # The error travels back so `collect_fleet` can put it in the manifest.
        # A log line alone leaves the failure nowhere a validator can read it.
        self.assertIn("denied", error)
        self.assertIn("rc=1", error)


class ClusterEligibilityTest(unittest.TestCase):
    def test_running_and_old_is_eligible(self):
        self.assertIsNone(fd.cluster_eligibility(cluster("c1"), now=NOW))

    def test_reconciling_is_ineligible(self):
        reason = fd.cluster_eligibility(cluster("c1", status="RECONCILING"), now=NOW)
        self.assertIn("RECONCILING", reason)

    def test_brand_new_is_ineligible(self):
        reason = fd.cluster_eligibility(cluster("c1", created="2026-07-31T23:00:00Z"), now=NOW)
        self.assertIn("under 24h", reason)


class EnvironmentOfTest(unittest.TestCase):
    def test_reads_resource_label(self):
        self.assertEqual(fd.environment_of(cluster("c1", labels={"environment": "staging"})), ("staging", "label"))

    def test_normalizes_synonyms(self):
        self.assertEqual(fd.environment_of(cluster("c1", labels={"environment": "prd"})), ("prod", "label"))

    def test_infers_from_name_when_no_label(self):
        self.assertEqual(fd.environment_of(cluster("prod-usc1", labels={})), ("prod", "inferred"))

    def test_unknown_when_neither(self):
        self.assertEqual(fd.environment_of(cluster("cluster-one", labels={}))[0], "unknown")

    def test_prefers_label_over_name(self):
        self.assertEqual(fd.environment_of(cluster("dev-box", labels={"environment": "prod"})), ("prod", "label"))


class CohortStrategyTest(unittest.TestCase):
    def test_environment_strategy_when_any_cluster_has_one(self):
        clusters = [cluster("a", labels={"environment": "prod"}), cluster("b", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")

    def test_project_strategy_when_multi_project_and_no_environment(self):
        clusters = [cluster("a", project="p1", labels={}), cluster("b", project="p2", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "project")

    def test_mode_only_as_last_resort(self):
        clusters = [cluster("a", project="p1", labels={}), cluster("b", project="p1", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "mode-only")

    def test_a_sparse_name_guess_does_not_select_environment(self):
        """One `test` token in sixteen names is our inference, not the fleet's
        convention, and acting on it costs coverage rather than buying
        precision: the guessed cluster lands alone in a cohort of one and is
        compared against nothing. Cohorting by mode compares all sixteen."""
        clusters = [cluster("deploy-test", labels={})] + [cluster(f"c{i}", labels={}) for i in range(15)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "mode-only")

    def test_a_fleetwide_naming_convention_does_select_environment(self):
        """Inference earns the strategy once it is the fleet's actual naming
        convention rather than a guess about a couple of stragglers."""
        clusters = [cluster(f"prod-{i}", labels={}) for i in range(3)]
        clusters += [cluster(f"dev-{i}", labels={}) for i in range(3)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")

    def test_one_real_label_settles_it_without_a_majority(self):
        """A label is the customer declaring how they organize their fleet, so
        it does not need numbers behind it the way a guess does."""
        clusters = [cluster("a", labels={"environment": "prod"})]
        clusters += [cluster(f"c{i}", labels={}) for i in range(15)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")


class ComputeBaselineTest(unittest.TestCase):
    def test_no_baseline_under_the_floor(self):
        self.assertIsNone(fd.compute_baseline({"a": "X", "b": "X"}))

    def test_no_baseline_below_two_thirds(self):
        self.assertIsNone(fd.compute_baseline({"a": "X", "b": "X", "c": "Y", "d": "Y"}))

    def test_baseline_at_exactly_two_thirds(self):
        result = fd.compute_baseline({"a": "X", "b": "X", "c": "Y"})
        self.assertEqual(result[0], "X")
        self.assertAlmostEqual(result[3], 2 / 3)

    def test_unanimous_baseline(self):
        result = fd.compute_baseline({"a": "X", "b": "X", "c": "X"})
        self.assertEqual(result, ("X", 3, 3, 1.0))


class SeverityLadderTest(unittest.TestCase):
    def test_high_confidence_keeps_base_severity(self):
        sev, downgrades = fd.apply_severity_ladder("critical", 1.0, 1, False)
        self.assertEqual(sev, "critical")
        self.assertEqual(downgrades, [])

    def test_r_under_90_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.85, 1, False)
        self.assertEqual(sev, "major")

    def test_r_under_80_drops_two_levels_cumulative(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.75, 1, False)
        self.assertEqual(sev, "minor")

    def test_three_or_more_outliers_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 1.0, 3, False)
        self.assertEqual(sev, "major")

    def test_inferred_environment_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 1.0, 1, True)
        self.assertEqual(sev, "major")

    def test_a_major_facet_at_weak_confidence_is_dropped_entirely(self):
        sev, _ = fd.apply_severity_ladder("major", 0.75, 1, False)
        self.assertIsNone(sev)

    def test_a_critical_facet_at_weak_confidence_survives_as_minor(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.71, 1, False)
        self.assertEqual(sev, "minor")

    def test_downgrades_stack(self):
        sev, downgrades = fd.apply_severity_ladder("critical", 0.75, 3, True)
        self.assertIsNone(sev)  # 2 (r<0.80) + 1 (k>=3) + 1 (inferred) = 4 steps from critical
        self.assertEqual(len(downgrades), 4)


class FacetNormalizeTest(unittest.TestCase):
    """One flag / no-flag pair per facet -- the roster this stream's SOP
    validator checks against, and the reason a slug exists here at all."""

    def hit(self, slug, base_cluster, outlier_overrides):
        facet = fd.FACETS_BY_SLUG[slug]
        baseline_token = facet.normalize(base_cluster)
        outlier = copy.deepcopy(base_cluster)
        for path, value in outlier_overrides.items():
            target = outlier
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        outlier_token = facet.normalize(outlier)
        return baseline_token, outlier_token, facet.should_flag(outlier_token, baseline_token)

    def test_release_channel(self):
        base, out, flagged = self.hit("release-channel", cluster("c"), {"releaseChannel.channel": "STABLE"})
        self.assertTrue(flagged)

    def test_release_channel_unenrolled_is_excluded_not_flagged(self):
        c = cluster("c", **{"releaseChannel.channel": ""})
        self.assertIsNone(fd.norm_release_channel(c))

    def test_shielded_nodes(self):
        base, out, flagged = self.hit("shielded-nodes", cluster("c"), {"shieldedNodes.enabled": False})
        self.assertTrue(flagged)

    def test_secure_boot_all_vs_none(self):
        c = cluster("c", node_pools=None)
        base, out, flagged = self.hit("secure-boot", cluster("c"), {"nodePools": [{"config": {"shieldedInstanceConfig": {"enableSecureBoot": False}}}]})
        self.assertTrue(flagged)

    def test_secure_boot_excludes_windows_pools(self):
        c = cluster(
            "c",
            nodePools=[
                {"name": "linux", "config": {"shieldedInstanceConfig": {"enableSecureBoot": True}, "imageType": "COS_CONTAINERD"}},
                {"name": "win", "config": {"shieldedInstanceConfig": {"enableSecureBoot": False}, "imageType": "WINDOWS_LTSC_CONTAINERD"}},
            ],
        )
        self.assertEqual(fd.norm_secure_boot(c), "ALL")

    def test_integrity_monitoring(self):
        base, out, flagged = self.hit("integrity-monitoring", cluster("c"), {"nodePools": [{"config": {"shieldedInstanceConfig": {"enableIntegrityMonitoring": False}}}]})
        self.assertTrue(flagged)

    # SOP 4.3 and 4.8 both state a one-directional impact ("nodes boot
    # unverified", "cannot absorb load the way its peers do") and a remediation
    # that turns the feature on. A cluster covering *more* pools than its cohort
    # is the reverse, so flagging it renders an inverted impact and a fix that
    # never converges: enabling the feature everywhere lands on ALL, which still
    # is not a NONE baseline, and the finding recurs on every subsequent run.

    def test_less_only_ranks_none_below_some_below_all(self):
        self.assertTrue(fd._flag_less_only("NONE", "SOME"))
        self.assertTrue(fd._flag_less_only("NONE", "ALL"))
        self.assertTrue(fd._flag_less_only("SOME", "ALL"))
        self.assertFalse(fd._flag_less_only("ALL", "ALL"))
        self.assertFalse(fd._flag_less_only("SOME", "SOME"))

    def test_pool_autoscaling_some_against_none_baseline_is_not_flagged(self):
        # The live case: nine peers autoscale no pool, spot-capacity-test
        # autoscales its spot pool. It absorbs load better, not worse.
        base, out, flagged = self.hit(
            "pool-autoscaling",
            cluster("c", nodePools=[{"name": "default-pool"}, {"name": "spot-pool"}]),
            {"nodePools": [{"name": "default-pool"}, {"name": "spot-pool", "autoscaling": {"enabled": True}}]},
        )
        self.assertEqual(base, "NONE")
        self.assertEqual(out, "SOME")
        self.assertFalse(flagged)

    def test_pool_autoscaling_none_against_all_baseline_is_flagged(self):
        base, out, flagged = self.hit(
            "pool-autoscaling",
            cluster("c", nodePools=[{"name": "default-pool", "autoscaling": {"enabled": True}}]),
            {"nodePools": [{"name": "default-pool"}]},
        )
        self.assertEqual((base, out), ("ALL", "NONE"))
        self.assertTrue(flagged)

    def test_secure_boot_all_against_none_baseline_is_not_flagged(self):
        base, out, flagged = self.hit(
            "secure-boot",
            cluster("c", nodePools=[{"name": "p", "config": {"imageType": "COS_CONTAINERD"}}]),
            {"nodePools": [{"name": "p", "config": {"imageType": "COS_CONTAINERD", "shieldedInstanceConfig": {"enableSecureBoot": True}}}]},
        )
        self.assertEqual((base, out), ("NONE", "ALL"))
        self.assertFalse(flagged)

    def test_node_autoprovisioning_on_against_off_baseline_is_not_flagged(self):
        base, out, flagged = self.hit(
            "node-autoprovisioning",
            cluster("c", autoscaling={"enableNodeAutoprovisioning": False}),
            {"autoscaling": {"enableNodeAutoprovisioning": True}},
        )
        self.assertEqual((base, out), ("OFF", "ON"))
        self.assertFalse(flagged)

    def test_node_autoprovisioning_off_against_on_baseline_is_flagged(self):
        base, out, flagged = self.hit(
            "node-autoprovisioning",
            cluster("c", autoscaling={"enableNodeAutoprovisioning": True}),
            {"autoscaling": {"enableNodeAutoprovisioning": False}},
        )
        self.assertEqual((base, out), ("ON", "OFF"))
        self.assertTrue(flagged)

    def test_shielded_nodes_on_against_off_baseline_is_not_flagged(self):
        base, out, flagged = self.hit("shielded-nodes", cluster("c", shieldedNodes={"enabled": False}), {"shieldedNodes.enabled": True})
        self.assertEqual((base, out), ("OFF", "ON"))
        self.assertFalse(flagged)

    def test_network_policy_dpv2_vs_calico_is_never_flagged(self):
        base = "DPV2"
        outlier = "CALICO"
        self.assertFalse(fd._flag_off_only(outlier, base))

    def test_network_policy_off_against_enforcing_majority_is_flagged(self):
        base, out, flagged = self.hit("network-policy", cluster("c"), {"networkConfig.datapathProvider": "LEGACY_DATAPATH", "networkPolicy.enabled": False})
        self.assertEqual(out, "OFF")
        self.assertTrue(flagged)

    def test_private_nodes(self):
        base, out, flagged = self.hit("private-nodes", cluster("c"), {"privateClusterConfig.enablePrivateNodes": False})
        self.assertTrue(flagged)

    def test_private_endpoint_legacy_field(self):
        base, out, flagged = self.hit("private-endpoint", cluster("c"), {"privateClusterConfig.enablePrivateEndpoint": False})
        self.assertTrue(flagged)

    def test_private_endpoint_falls_back_to_newer_field(self):
        c = cluster("c", privateClusterConfig={"enablePrivateNodes": True}, controlPlaneEndpointsConfig={"ipEndpointsConfig": {"enablePublicEndpoint": False}})
        self.assertEqual(fd.norm_private_endpoint(c), "ON")

    def test_authorized_networks_requires_nonempty_cidrs(self):
        c = cluster("c", masterAuthorizedNetworksConfig={"enabled": True, "cidrBlocks": []})
        self.assertEqual(fd.norm_authorized_networks(c), "OFF")

    def test_authorized_networks_falls_back_to_newer_field(self):
        """The two surfaces are mutually exclusive, so one read is not enough.

        `norm_private_endpoint` above already falls back this way. Authorized
        networks did not, so a cluster configured through
        `ipEndpointsConfig.authorizedNetworksConfig` normalised to OFF and drifted
        as a critical against peers holding the identical setting.
        """
        c = cluster(
            "c",
            masterAuthorizedNetworksConfig={},
            controlPlaneEndpointsConfig={
                "ipEndpointsConfig": {
                    "authorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"displayName": "corp", "cidrBlock": "10.0.0.0/8"}],
                    }
                }
            },
        )
        self.assertEqual(fd.norm_authorized_networks(c), "ON")

    def test_logging_components_superset_not_flagged(self):
        baseline = fd.norm_logging_components(cluster("c"))
        outlier = fd.norm_logging_components(cluster("c", loggingConfig={"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS", "WORKLOADS", "APISERVER"]}}))
        self.assertFalse(fd._flag_not_superset(outlier, baseline))

    def test_logging_components_subset_is_flagged(self):
        baseline = fd.norm_logging_components(cluster("c"))
        outlier = fd.norm_logging_components(cluster("c", loggingConfig={"componentConfig": {"enableComponents": ["WORKLOADS"]}}))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_logging_severity_major_when_missing_system_components(self):
        self.assertEqual(fd._logging_severity("WORKLOADS"), "major")

    def test_logging_severity_minor_when_system_components_present(self):
        self.assertEqual(fd._logging_severity("SYSTEM_COMPONENTS,WORKLOADS"), "minor")

    def test_monitoring_components_disjoint_is_flagged(self):
        baseline = "SYSTEM_COMPONENTS"
        outlier = "WORKLOADS"
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_managed_prometheus(self):
        base, out, flagged = self.hit("managed-prometheus", cluster("c"), {"monitoringConfig.managedPrometheusConfig": {"enabled": False}})
        self.assertTrue(flagged)

    def test_binary_authorization_mode_difference_not_flagged(self):
        self.assertFalse(fd._flag_off_only("SOME_OTHER_ENABLED_MODE", "PROJECT_SINGLETON_POLICY_ENFORCE"))

    def test_binary_authorization_off_is_flagged(self):
        c = cluster("c", binaryAuthorization={"evaluationMode": "DISABLED"})
        self.assertEqual(fd.norm_binary_authorization(c), "OFF")
        self.assertTrue(fd._flag_off_only("OFF", "ON"))

    def test_binary_authorization_legacy_enabled_field(self):
        c = cluster("c", binaryAuthorization={"enabled": True})
        self.assertEqual(fd.norm_binary_authorization(c), "ON")

    def test_node_autoprovisioning(self):
        base, out, flagged = self.hit("node-autoprovisioning", cluster("c"), {"autoscaling.enableNodeAutoprovisioning": False})
        self.assertTrue(flagged)

    def test_pool_autoscaling_excludes_tainted_pools(self):
        c = cluster("c", nodePools=[{"name": "pinned", "autoscaling": {"enabled": False}, "config": {"taints": [{"key": "dedicated"}]}}])
        self.assertIsNone(fd.norm_pool_autoscaling(c))

    def test_intra_node_visibility(self):
        base, out, flagged = self.hit("intra-node-visibility", cluster("c"), {"networkConfig.enableIntraNodeVisibility": False})
        self.assertTrue(flagged)

    def test_datapath_provider(self):
        base, out, flagged = self.hit("datapath-provider", cluster("c"), {"networkConfig.datapathProvider": "LEGACY_DATAPATH"})
        self.assertTrue(flagged)

    def test_label_keys_extra_keys_not_flagged(self):
        baseline = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x"}))
        outlier = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x", "extra": "y"}))
        self.assertFalse(fd._flag_not_superset(outlier, baseline))

    def test_label_keys_drops_goog_prefixed(self):
        c = cluster("c", labels={"environment": "prod", "goog-gke-node-pool-provisioning-model": "x"})
        self.assertEqual(fd.norm_label_keys(c), "environment")

    def test_label_keys_missing_key_is_flagged(self):
        baseline = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x"}))
        outlier = fd.norm_label_keys(cluster("c", labels={"environment": "prod"}))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_image_type_windows_pool_excluded(self):
        c = cluster("c", nodePools=[
            {"config": {"imageType": "COS_CONTAINERD"}},
            {"config": {"imageType": "WINDOWS_LTSC_CONTAINERD"}},
        ])
        self.assertEqual(fd.norm_image_type(c), "COS")

    def test_image_type_containerd_rename_is_not_a_divergence(self):
        cos = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS"}}]))
        cos_containerd = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS_CONTAINERD"}}]))
        self.assertEqual(cos, cos_containerd)

    def test_image_type_real_divergence_is_flagged(self):
        baseline = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS_CONTAINERD"}}]))
        outlier = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "UBUNTU_CONTAINERD"}}]))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_database_encryption(self):
        base, out, flagged = self.hit("database-encryption", cluster("c"), {"databaseEncryption.state": "DECRYPTED"})
        self.assertTrue(flagged)

    def test_database_encryption_absent_block_is_decrypted(self):
        c = cluster("c", databaseEncryption={})
        self.assertEqual(fd.norm_database_encryption(c), "DECRYPTED")


class ComputeDriftTest(unittest.TestCase):
    def cohort(self, n=4, outlier_overrides=None, **base_overrides):
        clusters = [cluster(f"c{i}", labels={"environment": "prod"}, **base_overrides) for i in range(n)]
        if outlier_overrides:
            for path, value in outlier_overrides.items():
                target = clusters[-1]
                keys = path.split(".")
                for key in keys[:-1]:
                    target = target.setdefault(key, {})
                target[keys[-1]] = value
        return clusters

    def test_a_clean_cohort_produces_no_findings(self):
        checks_run, candidates = fd.compute_drift(self.cohort(), now=NOW)
        self.assertTrue(all(v == [] for v in candidates.values()))
        self.assertIn("shielded-nodes", checks_run["c0"])

    def test_a_single_outlier_is_flagged(self):
        # n=20 keeps r=0.95, well clear of the confidence ladder's r<0.90
        # step, so this exercises the plain outlier path without also
        # exercising the downgrade -- that is SeverityLadderTest's job.
        clusters = self.cohort(n=20, outlier_overrides={"shieldedNodes.enabled": False})
        _, candidates = fd.compute_drift(clusters, now=NOW)
        outlier_name = clusters[-1]["name"]
        self.assertEqual(len(candidates[outlier_name]), 1)
        self.assertEqual(candidates[outlier_name][0]["check"], "shielded-nodes")
        self.assertEqual(candidates[outlier_name][0]["severity"], "major")
        self.assertEqual(candidates["c0"], [])

    def test_cohort_under_the_floor_produces_nothing(self):
        clusters = [cluster("a"), cluster("b")]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(candidates["a"], [])
        self.assertEqual(fd.compute_drift(clusters, now=NOW)[0]["a"], [])

    def test_autopilot_and_standard_are_never_compared_together(self):
        clusters = self.cohort(n=3, outlier_overrides={"shieldedNodes.enabled": False})
        clusters.append(cluster("c-auto", autopilot=True, labels={"environment": "prod"}, **{"shieldedNodes.enabled": False}))
        _, candidates = fd.compute_drift(clusters, now=NOW)
        # the autopilot cluster is alone in its mode's cohort -- under the
        # floor, so it gets no findings regardless of its shielded-nodes value
        self.assertEqual(candidates["c-auto"], [])

    def test_standard_only_facets_are_never_computed_for_autopilot(self):
        clusters = [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(4)]
        checks_run, _ = fd.compute_drift(clusters, now=NOW)
        self.assertNotIn("secure-boot", checks_run["a0"])
        self.assertNotIn("image-type", checks_run["a0"])

    def test_datapath_provider_is_computed_but_never_flagged_on_autopilot(self):
        clusters = [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(3)]
        clusters.append(cluster("a-outlier", autopilot=True, labels={"environment": "prod"}, **{"networkConfig.datapathProvider": "LEGACY_DATAPATH"}))
        checks_run, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertIn("datapath-provider", checks_run["a-outlier"])
        self.assertEqual(candidates["a-outlier"], [])

    def test_ineligible_cluster_gets_no_facets_compared(self):
        clusters = self.cohort(n=3)
        clusters.append(cluster("reconciling", status="RECONCILING", labels={"environment": "prod"}))
        checks_run, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(checks_run["reconciling"], [])
        self.assertEqual(candidates["reconciling"], [])

    def test_split_cluster_guard_replaces_many_findings_with_one(self):
        # n=20 keeps r=0.95 for every facet -- comfortably clear of the
        # confidence ladder, so all six overrides below survive as
        # findings and the split-cluster guard is what collapses them,
        # not a severity downgrade dropping two of the six first.
        clusters = self.cohort(n=20)
        outlier = clusters[-1]
        for facet_slug, path, value in [
            ("shielded-nodes", "shieldedNodes.enabled", False),
            ("private-nodes", "privateClusterConfig.enablePrivateNodes", False),
            ("private-endpoint", "privateClusterConfig.enablePrivateEndpoint", False),
            ("intra-node-visibility", "networkConfig.enableIntraNodeVisibility", False),
            ("managed-prometheus", "monitoringConfig.managedPrometheusConfig", {"enabled": False}),
            ("database-encryption", "databaseEncryption.state", "DECRYPTED"),
        ]:
            target = outlier
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(len(candidates[outlier["name"]]), 1)
        self.assertEqual(candidates[outlier["name"]][0]["check"], "uncohorted")

    def test_environment_strategy_separates_cohorts(self):
        prod = self.cohort(n=4)
        staging = [cluster(f"s{i}", labels={"environment": "staging"}, **{"shieldedNodes.enabled": False}) for i in range(4)]
        _, candidates = fd.compute_drift(prod + staging, now=NOW)
        # staging's own majority is shieldedNodes=False, so none of them are outliers there
        self.assertEqual(candidates["s0"], [])

    def test_baseline_at_exactly_two_thirds_still_fires_for_a_critical_facet(self):
        # r = 2/3 = 0.667 triggers both the r<0.90 and r<0.80 downgrade
        # steps -- two steps from critical (index 0) lands on minor (index
        # 2), which is exactly the SOP's own worked example: "a base-
        # critical facet at r=0.71 survives as minor."
        clusters = [
            cluster("c0", labels={"environment": "prod"}),
            cluster("c1", labels={"environment": "prod"}),
            cluster("c2", labels={"environment": "prod"}, **{"privateClusterConfig.enablePrivateNodes": False}),
        ]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(len(candidates["c2"]), 1)
        self.assertEqual(candidates["c2"][0]["severity"], "minor")

    def test_baseline_at_exactly_two_thirds_drops_a_major_facet_entirely(self):
        clusters = [
            cluster("c0", labels={"environment": "prod"}),
            cluster("c1", labels={"environment": "prod"}),
            cluster("c2", labels={"environment": "prod"}, **{"shieldedNodes.enabled": False}),
        ]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(candidates["c2"], [])


class CollectFleetTest(unittest.TestCase):
    def test_manifest_shape(self):
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, read_text=lambda p: None, now=NOW)
        self.assertEqual(manifest["audit"], "fleet-consistency-drift")
        self.assertEqual(len(manifest["clusters"]), 4)
        self.assertTrue(all(c["outcome"] == "collected" for c in manifest["clusters"]))

    def test_a_project_that_fails_to_list_is_recorded_not_dropped(self):
        """Returning nothing made a project whose `clusters list` failed
        indistinguishable from one holding no clusters, so the manifest read
        complete and the document was held to nothing. It matters more here
        than in a per-cluster stream: drift ranks each cluster against its
        cohort, and clusters missing from the comparison silently change what
        counts as an outlier."""

        def run(argv, **kwargs):
            return run_of(1, "", "denied")

        manifest = fd.collect_fleet("acme", run=run, read_text=lambda p: None, now=NOW)
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["project/acme"])
        entry = manifest["clusters"][0]
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("denied", entry["error"])

    def test_a_project_that_lists_cleanly_adds_no_project_entry(self):
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, read_text=lambda p: None, now=NOW)
        self.assertEqual([c for c in manifest["clusters"] if c["name"].startswith("project/")], [])


class AutopilotNotApplicableTest(unittest.TestCase):
    """The five `standard_only` facets have to be declared, not just dropped.

    `compute_drift` skips them for an Autopilot cohort, which is right — each
    reads a `.nodePools[]` field or a node-management setting Google owns. But
    dropping a slug leaves it missing from `commands`, which is also how a check
    nobody ran looks, so §6 counts it as a coverage gap unless the model excuses
    it by hand.
    """

    NA = ("secure-boot", "integrity-monitoring", "node-autoprovisioning", "pool-autoscaling", "image-type")

    def manifest(self, clusters):
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        return fd.collect_fleet("acme", run=run, read_text=lambda p: None, now=NOW)

    def autopilot_cohort(self, n=4):
        return [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(n)]

    def test_the_five_are_declared_with_a_reason(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        declared = {n["check"]: n["reason"] for n in entry["checks_not_applicable"]}
        self.assertEqual(sorted(declared), sorted(self.NA))
        for reason in declared.values():
            self.assertIn("Autopilot", reason)

    def test_none_of_the_five_is_also_claimed_as_a_check_that_ran(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        ran = {c["check"] for c in entry["commands"]}
        self.assertEqual(ran & set(self.NA), set())

    def test_a_standard_cluster_declares_nothing_and_runs_them(self):
        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)]
        entry = self.manifest(clusters)["clusters"][0]
        self.assertNotIn("checks_not_applicable", entry)
        self.assertLessEqual(set(self.NA), {c["check"] for c in entry["commands"]})

    def test_an_undersized_autopilot_cohort_still_declares_them(self):
        """The live fleet's shape: two Autopilot clusters in one cohort against
        a floor of three, so no facet compared and every slug is missing from
        `commands`. The `limitations` sentence covers the ones that could have
        run; these five could not have, cohort or no cohort, and belong out of
        the denominator rather than inside the sentence."""
        entry = self.manifest(self.autopilot_cohort(n=2))["clusters"][0]
        self.assertEqual(entry["commands"], [])
        self.assertIn("no facet compared", entry["limitations"])
        self.assertEqual(sorted(n["check"] for n in entry["checks_not_applicable"]), sorted(self.NA))

    def test_datapath_provider_is_not_declared(self):
        """It carries `autopilot_excluded`, not `standard_only`: the facet is
        computed and recorded in `checks_run`, and only the flagging is
        suppressed. Declaring it too would have the manifest assert both."""
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        self.assertNotIn("datapath-provider", [n["check"] for n in entry["checks_not_applicable"]])
        self.assertIn("datapath-provider", {c["check"] for c in entry["commands"]})

    def test_the_two_together_account_for_the_whole_roster(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        ran = {c["check"] for c in entry["commands"]}
        declared = {n["check"] for n in entry["checks_not_applicable"]}
        self.assertEqual({f.slug for f in fd.FACETS} - ran - declared, set())
        self.assertEqual(ran & declared, set())


class CohortLimitationsTest(unittest.TestCase):
    """A cluster no facet compared has to say so.

    The live four-cluster fleet: two autopilot clusters with no environment
    label, one autopilot labelled `test`, one standard. Cohorts of 2, 1 and 1
    against a floor of 3, so every cohort abstained and not one facet was
    compared — and the manifest called all four `collected` with an empty
    `commands` list, four seconds after it started. `collected` is what tells
    the model the target needs no manual fallback, so nothing downstream had
    any way to know the comparison never happened.
    """

    def _floored_fleet(self):
        return [
            cluster("auto-a", autopilot=True),
            cluster("auto-b", autopilot=True),
            cluster("auto-test", autopilot=True, labels={"environment": "test"}),
            cluster("std-a"),
        ]

    def test_every_member_of_an_undersized_cohort_is_explained(self):
        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        self.assertEqual(len(lim), 4)
        self.assertIn("only 2 comparable clusters", lim["auto-a"])
        self.assertIn("only 2 comparable clusters", lim["auto-b"])
        # Singular for a one-member cohort: the sentence a lone cluster like
        # kube-agents-host gets on every run.
        self.assertIn("only 1 comparable cluster ", lim["auto-test"])
        self.assertIn("only 1 comparable cluster ", lim["std-a"])
        for text in lim.values():
            self.assertIn(f"minimum {fd.COHORT_FLOOR}", text)
            self.assertIn("no facet compared", text)

    def test_the_sentence_names_the_cohort_it_floored_out_of(self):
        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        self.assertIn("cohort autopilot/prod", lim["auto-a"])
        self.assertIn("cohort autopilot/test", lim["auto-test"])
        self.assertIn("cohort standard/prod", lim["std-a"])

    def test_the_lone_unlabelled_cluster_is_told_a_label_is_the_difference(self):
        # The live fleet's shape: fifteen of sixteen carry `environment=test`,
        # kube-agents-host carries none, so it cohorts alone under 2.3 and is
        # the one cluster drift can never compare -- on this run or any later
        # one. The floor sentence alone reads as a fleet-size quirk and gets
        # waited out; the cause is what makes it fixable.
        fleet = [cluster(f"c{i}", labels={"environment": "test"}) for i in range(3)]
        fleet.append(cluster("host", labels={}))
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(list(lim), ["host"])
        self.assertIn("cohort standard/unknown has only 1 comparable cluster",
                      lim["host"])
        self.assertIn("no environment label while 3 of 4 do", lim["host"])

    def test_a_fleet_nobody_labelled_is_not_told_to_add_a_label(self):
        # Every cluster unknown together cohorts by mode alone, so there is no
        # named cohort being kept out of and nothing to point at. Counting the
        # label source rather than the resolved environment is what keeps the
        # inferred strategy out too: those clusters carry no label either, and
        # a count of them would make the sentence false.
        fleet = [cluster(f"c{i}", labels={}) for i in range(2)]
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(len(lim), 2)
        for text in lim.values():
            self.assertIn("no facet compared", text)
            self.assertNotIn("environment label", text)

    def test_a_cohort_that_reaches_the_floor_explains_nothing(self):
        """A compared cluster must not carry a limitation — it would read as a
        coverage gap on a cluster that was in fact fully voted on."""
        fleet = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        self.assertEqual(fd.cohort_limitations(fleet, now=NOW), {})

    def test_an_ineligible_cluster_carries_its_eligibility_reason(self):
        fleet = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        fleet.append(cluster("broken", labels={"environment": "prod"}, status="DEGRADED"))
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(set(lim), {"broken"})
        self.assertIn("status DEGRADED", lim["broken"])

    def test_the_manifest_carries_the_sentence(self):
        clusters_json = json.dumps(self._floored_fleet())

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, read_text=lambda p: None, now=NOW)
        self.assertEqual(len(manifest["clusters"]), 4)
        for entry in manifest["clusters"]:
            self.assertEqual(entry["commands"], [])
            self.assertIn("no facet compared", entry["limitations"])

    def test_a_compared_fleet_gets_no_limitations_key_at_all(self):
        clusters_json = json.dumps(
            [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        )

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, read_text=lambda p: None, now=NOW)
        for entry in manifest["clusters"]:
            self.assertNotIn("limitations", entry)
            self.assertTrue(entry["commands"])

    def test_the_limitation_reaches_the_coverage_arithmetic(self):
        """End to end: the sentence has to become a coverage gap, or the run
        still reports a fleet nobody compared as a clean one."""
        import audit_report

        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        doc = {
            "audit": "fleet-consistency-drift",
            "scope": {
                "clusters": [
                    {
                        "name": name,
                        "location": "us-central1",
                        "project": "acme",
                        "checks_run": [],
                        "limitations": text,
                    }
                    for name, text in sorted(lim.items())
                ],
                "skipped": [],
            },
            "findings": [],
        }
        gaps = audit_report.coverage_gaps(doc)
        self.assertEqual(len(gaps), 4)
        self.assertTrue(all("no facet compared" in g for g in gaps))


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_checks_run_copied_from_a_collected_cluster_survives_cross_check(self):
        import audit_report

        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)]
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, read_text=lambda p: None, now=NOW)
        data = {
            "audit": "fleet-consistency-drift",
            "scope": {
                "clusters": [
                    {"name": e["name"], "checks_run": [{"check": c["check"], "command": c["command"]} for c in e["commands"]]}
                    for e in manifest["clusters"]
                ],
                "skipped": [],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_check_absent_from_the_manifest_is_rejected(self):
        import audit_report

        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(2)]  # under the floor
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, read_text=lambda p: None, now=NOW)
        data = {
            "audit": "fleet-consistency-drift",
            "scope": {"clusters": [{"name": "c0", "checks_run": [{"check": "shielded-nodes", "command": "x"}]}]},
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)


if __name__ == "__main__":
    unittest.main()
