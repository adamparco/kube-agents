#!/usr/bin/env python3
"""Structural tests for the per-tier heartbeat SOPs + cron wiring (Phase 4 D3, P4-T9, Acc-d).

Dependency-free (stdlib unittest over the committed governance/cron files) so the heartbeat
backstop's load-bearing properties are asserted offline:
  - each tier's heartbeat SOP is scoped to that tier's authority (cluster-admin = its one cluster,
    developer-team = its one namespace, with an explicit over-reach guard);
  - the heartbeat is documented as the BACKSTOP after event triggers + cron (04 §4), not the primary
    mechanism, and a clean sweep is silent (NO_REPLY);
  - anything the sweep wants to change goes out as an Action Envelope via apply-change, so the
    broker is the only thing that mutates (04 §9; invariant 3), and out-of-scope findings leave the
    tier through the mesh (02 §2.3), never by widening the sweep's own scope;
  - each tier's cron/jobs.json wires an enabled `heartbeat` job that reads the SOP and carries the
    read-knowledge / apply-change skills plus exactly the mesh verbs 02 §2.1 grants that tier —
    `delegate` and `escalate` for cluster-admin, `escalate` alone for the leaf.
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
#
# Repointed by P13-T5. Two of these entries used to name skills that no longer exist -- and the
# more interesting damage was that the list kept the SOPs GREEN while they described a workflow the
# system had stopped having. `submit-suggestion` was the GitOps proposal path and `raise-escalation`
# wrote a ticket into the knowledge store; the replacements are `apply-change`, which submits an
# Action Envelope the broker executes on this run, and `escalate`, which is a synchronous call to
# another tier. A heartbeat that proposes is now a defect (02 §2.5.1), so requiring the old names
# was requiring the old behaviour.
#
# The `invariant 3` comment was also simply wrong, and had been since it was written: it read
# "never a direct agent-to-agent call". Invariant 3 is *every mutation is brokered* -- see
# `docs/design/README.md` line 38 and `docs/design/03-security-model.md` line 323. The needle was
# right and the reason attached to it was not, which is the worse of the two failure modes: the
# assertion could never go red to correct the comment. The mesh (02 §2.3) now makes direct
# agent-to-agent calls the DESIGNED path, so had the comment been load-bearing it would have argued
# for deleting exactly the wrong needle.
_COMMON_SUBSTRINGS = [
    "backstop",  # it is the safety net, not the primary path
    "04 §4",  # cites the push-first / poll-as-backstop model
    "read-only",  # DETECTION never mutates -- the fix that follows it goes through the broker
    "NO_REPLY",  # a clean sweep is silent
    "apply-change",  # in-scope findings are fixed on this run, as an Action Envelope
    "escalate",  # out-of-scope findings go up as a synchronous cross-tier call
    "invariant 3",  # every mutation is brokered, journaled and reversible
]

# The mesh skills each tier may name, by 02 §2.1. Asserted in BOTH directions per tier, because the
# interesting failure is a leaf tier acquiring a downward verb: `delegate` in a developer-team SOP
# would be an instruction to reach into something it has no child to reach.
_MESH_SKILLS = {"delegate", "escalate"}
_TIER_MESH = {
    "cluster-admin": {"delegate", "escalate"},
    "developer-team": {"escalate"},
}


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
    def _assert_mesh_scope(self, tier: str, job: dict) -> None:
        """The heartbeat may name exactly the mesh verbs 02 §2.1 gives this tier -- no more.

        Stated as an equality over the mesh subset rather than a pair of `assertIn`s, so that a
        verb the tier does not hold cannot be added without failing. `developer-team` is the leaf:
        it has no child, so `delegate` there is not a redundant grant, it is an instruction to reach
        somewhere that does not exist.
        """
        granted = set(job["skills"]) & _MESH_SKILLS
        self.assertEqual(
            granted,
            _TIER_MESH[tier],
            f"{tier}'s heartbeat job wires mesh skills {sorted(granted)}; 02 §2.1 gives it "
            f"{sorted(_TIER_MESH[tier])}",
        )

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
        for skill in ("read-knowledge", "apply-change", "escalate"):
            self.assertIn(skill, job["skills"])
        self._assert_mesh_scope("cluster-admin", job)

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
        for skill in ("read-knowledge", "apply-change", "escalate"):
            self.assertIn(skill, job["skills"])
        self._assert_mesh_scope("developer-team", job)

    def test_both_cron_files_still_valid_json(self) -> None:
        for path in (CA_JOBS, DT_JOBS):
            data = _jobs(path)
            self.assertIsInstance(data["jobs"], list)
            ids = [j["id"] for j in data["jobs"]]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate job id in {path.name}")


if __name__ == "__main__":
    unittest.main()
