#!/usr/bin/env python3
"""Hermetic tests for detect-drift (Phase 4 D3, Acc-e).

Dependency-free (stdlib unittest + real local git repo, JSON manifests) so drift detection and the
corrective-PR artifact are provable offline with NO real GitHub and NO cluster. Proves:
  - a real change to a GitOps-declared field is detected as drift;
  - server-defaulted / controller-added fields live adds (that desired never specified) are NOT drift
    (no false-positive PR);
  - the ignore-set (`status`, `resourceVersion`, `managedFields`, …) is stripped before diffing;
  - on drift, --emit-corrective produces the corrective-PR artifact (branch + diff) via
    submit-suggestion --dry-run with NO push / NO PR / NO token broker;
  - detection is read-only: the live JSON file is byte-identical before and after a run.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DEV = REPO_ROOT / "local-dev"
DRIFT_SCRIPTS = REPO_ROOT / "agents/platform/skills/detect-drift/scripts"

sys.path.insert(0, str(DRIFT_SCRIPTS))
sys.path.insert(0, str(LOCAL_DEV))

import detect_drift  # noqa: E402
import submit_suggestion  # noqa: E402  (same module detect_drift imports)


def _write(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)


DESIRED = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "default-deny", "namespace": "team-x"},
    "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
}

# Live as the cluster returns it: same authored fields + server bookkeeping + defaults.
LIVE_CLEAN = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {
        "name": "default-deny",
        "namespace": "team-x",
        "uid": "abc-123",
        "resourceVersion": "998877",
        "creationTimestamp": "2026-07-01T00:00:00Z",
        "generation": 3,
        "managedFields": [{"manager": "kube-apiserver"}],
        "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}"},
    },
    "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    "status": {"conditions": []},
}


class _NoNetwork:
    def __init__(self, tc: unittest.TestCase) -> None:
        self.tc = tc
        self._saved: dict = {}

    def _boom(self, name):
        def _f(*_a, **_k):
            self.tc.fail(f"drift --dry-run must not call {name}()")
        return _f

    def __enter__(self):
        for name in ("refresh_git_credentials", "push_branch", "create_pull_request"):
            self._saved[name] = getattr(submit_suggestion, name)
            setattr(submit_suggestion, name, self._boom(name))
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(submit_suggestion, name, fn)
        return False


class TestDriftDetection(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="drift-")
        self.desired = os.path.join(self.dir, "desired.json")
        self.live = os.path.join(self.dir, "live.json")
        _write(self.desired, DESIRED)

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", self.dir], check=False)

    def test_no_false_positive_on_server_defaults(self) -> None:
        _write(self.live, LIVE_CLEAN)
        rc = detect_drift.run(["--desired", self.desired, "--live", self.live])
        self.assertEqual(rc, 0, "server defaults / bookkeeping must not count as drift")

    def test_detects_real_field_change(self) -> None:
        drifted = json.loads(json.dumps(LIVE_CLEAN))
        drifted["spec"]["policyTypes"] = ["Ingress"]  # someone dropped Egress on the live object
        _write(self.live, drifted)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = detect_drift.run(["--desired", self.desired, "--live", self.live])
        self.assertEqual(rc, 2)
        self.assertIn("DRIFT DETECTED", buf.getvalue())

    def test_emit_corrective_artifact_without_push(self) -> None:
        drifted = json.loads(json.dumps(LIVE_CLEAN))
        drifted["spec"]["policyTypes"] = ["Ingress"]
        _write(self.live, drifted)
        live_before = Path(self.live).read_bytes()

        work = tempfile.mkdtemp(prefix="drift-gitops-")
        artifact = tempfile.mkdtemp(prefix="drift-artifact-")
        obj_path = "clusters/cluster-a/namespaces/team-x/netpol.json"
        try:
            # Seed a GitOps working tree containing the (correct) desired manifest.
            os.makedirs(os.path.join(work, os.path.dirname(obj_path)))
            _write(os.path.join(work, obj_path), DESIRED)
            subprocess.run(["git", "-C", work, "init", "-q", "-b", "main"], check=True)
            subprocess.run(["git", "-C", work, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", work, "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
                check=True,
            )

            with _NoNetwork(self):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = detect_drift.run([
                        "--desired", self.desired, "--live", self.live,
                        "--emit-corrective", "--work-dir", work,
                        "--object-path", obj_path,
                        "--created", "2026-07-24",
                        "--dry-run", "--artifact-dir", artifact,
                    ])
            self.assertEqual(rc, 2)
            branch = (Path(artifact) / "branch.txt").read_text().strip()
            self.assertEqual(branch, "platform-agent/drift-networkpolicy-default-deny")
            diff = (Path(artifact) / "suggestion.diff").read_text()
            self.assertIn("knowledge/observation/drift-networkpolicy-default-deny.md", diff)
            self.assertIn("type: observation", diff)
            self.assertIn("policyTypes", diff)
            # Read-only: the live JSON is untouched by detection.
            self.assertEqual(Path(self.live).read_bytes(), live_before)
        finally:
            subprocess.run(["rm", "-rf", work, artifact], check=False)


if __name__ == "__main__":
    unittest.main()
