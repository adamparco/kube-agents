#!/usr/bin/env python3
"""P4's dataplane allow-list, exercised against a fake cluster (L0).

WHY A NEGATIVE CONTROL, AND WHY HERMETIC. P4 decides whether a NetworkPolicy result means anything.
Get it wrong in the permissive direction and the three network suites report PROVEN on a dataplane
that stored the policy and ignored it -- LSN-006, which is where "V-CTN-020 is a known liability"
came from. The only way to know the allow-list rejects what it should is to feed it a dataplane it
must reject, and no real cluster is going to volunteer one on demand: the inner loop runs on GKE
Dataplane V2, which is an ACCEPT case, so a live-cluster test of this function can only ever
exercise the arm that says yes.

So the cluster is a shell script. `p4_assert_enforcing_dataplane` takes its kubectl invocation as a
string argument -- it was already written that way so one script could probe two clusters -- and
that seam is the whole test harness: a stub that exits 0 for the DaemonSet names a scenario is
supposed to have and 1 for everything else. No cluster, no network, no credentials, so this runs in
CI on every PR and keeps running long after the substrate changes again.

The kindnet row is the point of the file. It is the dataplane that taught the lesson, it is no
longer in the loop, and it must still be REJECTED -- a check that stops being able to fail the case
it was written for has stopped being evidence (V-MET-014).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRECONDITIONS = REPO / "dev/lib/preconditions.sh"

# name -> the DaemonSets `kubectl -n kube-system get ds` would find, and the verdict P4 must reach.
# rc 0 with a name, or rc 3 with "unknown". There is deliberately no third outcome: P4 never
# reports rc 1, because a non-enforcing dataplane is an experiment that cannot run and not a
# security property that failed.
SCENARIOS = {
    "calico": (["calico-node", "kube-proxy"], 0, "calico"),
    "dataplane-v2": (["anetd", "kube-dns", "metadata-proxy-v0.1"], 0, "dataplane-v2"),
    "cilium": (["cilium", "cilium-operator"], 0, "cilium"),
    "kindnet": (["kindnet", "kube-proxy"], 3, "unknown"),
    "flannel": (["kube-flannel-ds"], 3, "unknown"),
    "nothing": ([], 3, "unknown"),
    # A cluster that answers nothing at all -- credentials expired, context deleted, API server
    # down. It must land on the deferral and not on a pass; "I could not ask" and "I asked and the
    # answer was no" have the same consequence for a network claim.
    "unreachable": (None, 3, "unknown"),
}


def run_p4(daemonsets: list[str] | None) -> tuple[int, str, str]:
    """Source preconditions.sh, call P4 with a stub kubectl, return (rc, P4_DATAPLANE, output)."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "kubectl"
        if daemonsets is None:
            body = 'exit 1\n'
        else:
            # Two shapes to answer, matching the two things P4 asks for: `get daemonset <name>`,
            # whose EXIT STATUS is the probe, and `get ds -o jsonpath=...`, whose stdout is only
            # used to tell the reader what was found instead.
            names = " ".join(daemonsets)
            body = (
                'for a in "$@"; do\n'
                '  case "$a" in\n'
                f'    -o) echo "{names} "; exit 0 ;;\n'
                "  esac\n"
                "done\n"
                'last="${!#}"\n'
                'case " ' + names + ' " in\n'
                '  *" $last "*) exit 0 ;;\n'
                "esac\n"
                "exit 1\n"
            )
        stub.write_text("#!/usr/bin/env bash\n" + body)
        stub.chmod(0o755)
        script = (
            f'. "{PRECONDITIONS}"\n'
            f'p4_assert_enforcing_dataplane "{stub} --context fake-ctx"\n'
            'rc=$?\n'
            'echo "P4_RC=$rc P4_DATAPLANE=$P4_DATAPLANE"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )
        out = proc.stdout + proc.stderr
        marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("P4_RC=")]
        assert marker, f"P4 emitted no verdict line:\n{out}"
        rc_s, dp_s = marker[-1].split()
        return int(rc_s.split("=", 1)[1]), dp_s.split("=", 1)[1], out


class TestDataplaneAllowlist(unittest.TestCase):
    def test_only_known_enforcing_dataplanes_are_accepted(self):
        for name, (daemonsets, want_rc, want_dp) in SCENARIOS.items():
            with self.subTest(dataplane=name):
                rc, dp, out = run_p4(daemonsets)
                self.assertEqual(
                    rc,
                    want_rc,
                    f"P4 returned {rc} for a '{name}' cluster, expected {want_rc}.\n{out}",
                )
                self.assertEqual(dp, want_dp, f"P4 named the '{name}' dataplane '{dp}'.\n{out}")

    def test_rejection_says_what_it_found(self):
        """A deferral that does not name the dataplane sends the reader to look at the policy."""
        _, _, out = run_p4(["kindnet", "kube-proxy"])
        self.assertIn("DEFERRED (P4)", out)
        self.assertIn("kindnet", out, "the deferral did not report what kube-system actually has")

    def test_p4_never_returns_one(self):
        """rc 1 is 'the security property failed' and P4 is never entitled to say that."""
        for name, (daemonsets, _, _) in SCENARIOS.items():
            with self.subTest(dataplane=name):
                rc, _, out = run_p4(daemonsets)
                self.assertNotEqual(rc, 1, f"P4 reported a FAILURE for '{name}'.\n{out}")


if __name__ == "__main__":
    unittest.main()
