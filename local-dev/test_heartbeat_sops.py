#!/usr/bin/env python3
"""Structural tests for the per-tier heartbeat SOPs + cron wiring (Phase 4 D3, P4-T9, Acc-d).

Dependency-free (stdlib unittest over the committed governance/cron files) so the heartbeat
backstop's load-bearing properties are asserted offline:
  - each tier's heartbeat SOP is scoped to that tier's authority (cluster-admin = its one cluster,
    developer-team = its one namespace, with an explicit over-reach guard);
  - the heartbeat is documented as the BACKSTOP after event triggers + cron (04 §4), not the primary
    mechanism, and a clean sweep is silent (NO_REPLY);
  - anything the sweep wants to change is proposed via submit-suggestion — never a direct mutation
    (04 §9; invariant 1) — and cross-tier findings go through raise-escalation, never a direct call
    (invariant 3);
  - each tier's cron/jobs.json wires an enabled `heartbeat` job that reads the SOP and carries the
    read-knowledge / submit-suggestion / raise-escalation skills.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CA_SOP = REPO_ROOT / "agents/cluster-admin/governance/heartbeat_sop.md"
DT_SOP = REPO_ROOT / "agents/developer-team/governance/heartbeat_sop.md"
CA_JOBS = REPO_ROOT / "agents/cluster-admin/cron/jobs.json"
DT_JOBS = REPO_ROOT / "agents/developer-team/cron/jobs.json"

# Every heartbeat SOP, regardless of tier, must carry these load-bearing properties.
_COMMON_SUBSTRINGS = [
    "backstop",  # it is the safety net, not the primary path
    "04 §4",  # cites the push-first / poll-as-backstop model
    "read-only",  # the sweep never mutates
    "NO_REPLY",  # a clean sweep is silent
    "submit-suggestion",  # changes are proposed as reviewed PRs
    "raise-escalation",  # cross-tier findings go up as OKF escalations
    "invariant 3",  # never a direct agent-to-agent call
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _jobs(path: Path) -> dict:
    return json.loads(_read(path))


def _heartbeat_job(path: Path) -> dict:
    for job in _jobs(path)["jobs"]:
        if job.get("id") == "heartbeat":
            return job
    raise AssertionError(f"no 'heartbeat' job in {path}")


class TestHeartbeatSops(unittest.TestCase):
    def test_both_sops_exist(self) -> None:
        self.assertTrue(CA_SOP.is_file(), CA_SOP)
        self.assertTrue(DT_SOP.is_file(), DT_SOP)

    def test_common_backstop_properties(self) -> None:
        for path in (CA_SOP, DT_SOP):
            body = _read(path)
            for needle in _COMMON_SUBSTRINGS:
                self.assertIn(needle, body, f"{path.name} must mention {needle!r}")
            # "last resort" / "last-resort" — either spelling proves the backstop ordering.
            self.assertRegex(body, r"last[ -]resort", f"{path.name} must call it the last resort")

    def test_cluster_admin_is_cluster_scoped(self) -> None:
        body = _read(CA_SOP)
        self.assertIn("one cluster", body)
        # Over-reach guard: it must NOT reach into other clusters / fleet-level resources.
        self.assertRegex(body.lower(), r"other cluster|fleet")
        # Cluster-admin escalates fleet-wide findings UPWARD.
        self.assertIn("fleet-wide", body)

    def test_developer_team_is_namespace_scoped(self) -> None:
        body = _read(DT_SOP)
        self.assertIn("one namespace", body)
        # Over-reach guard: cannot read other namespaces or any cluster-scoped resource.
        self.assertIn("cannot", body.lower())
        self.assertRegex(body.lower(), r"other namespace|cluster-scoped")
        # Dev-team escalates cluster-scoped findings to its parent Cluster Admin.
        self.assertIn("Cluster Admin Agent", body)

    def test_cluster_admin_cron_wires_heartbeat(self) -> None:
        job = _heartbeat_job(CA_JOBS)
        self.assertTrue(job["enabled"])
        self.assertEqual(job["schedule"]["kind"], "cron")
        self.assertTrue(job["schedule"]["expr"])
        self.assertIn("heartbeat_sop.md", job["prompt"])
        self.assertIn("NO_REPLY", job["prompt"])
        self.assertIn("cluster", job["prompt"].lower())
        for skill in ("read-knowledge", "submit-suggestion", "raise-escalation"):
            self.assertIn(skill, job["skills"])

    def test_developer_team_cron_wires_heartbeat(self) -> None:
        job = _heartbeat_job(DT_JOBS)
        self.assertTrue(job["enabled"])
        self.assertEqual(job["schedule"]["kind"], "cron")
        self.assertTrue(job["schedule"]["expr"])
        self.assertIn("heartbeat_sop.md", job["prompt"])
        self.assertIn("NO_REPLY", job["prompt"])
        self.assertIn("namespace", job["prompt"].lower())
        # A namespace-scoped heartbeat must never advertise a cluster-wide sweep.
        self.assertNotIn("cluster-wide", job["prompt"].lower())
        for skill in ("read-knowledge", "submit-suggestion", "raise-escalation"):
            self.assertIn(skill, job["skills"])

    def test_both_cron_files_still_valid_json(self) -> None:
        for path in (CA_JOBS, DT_JOBS):
            data = _jobs(path)
            self.assertIsInstance(data["jobs"], list)
            ids = [j["id"] for j in data["jobs"]]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate job id in {path.name}")


if __name__ == "__main__":
    unittest.main()
