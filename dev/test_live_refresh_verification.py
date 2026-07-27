#!/usr/bin/env python3
"""The live-refresh convergence checks must be able to fail, and to succeed (L0).

Two defects found on 2026-07-26 by running `make live-refresh` against the live install. Both had
the same shape: a check or a step that looked like it was doing its job and was structurally
incapable of it.

1. **The digest verification inspected nothing.** `check_namespace` read one tab-separated record
   per pod: `name TAB deletionTimestamp TAB (image TAB imageID)...`. Tab is IFS *whitespace*, so
   bash collapsed the run of separators around the empty `deletionTimestamp` a healthy pod has.
   `deleted` then held the image name, the `[ -z "$deleted" ] || continue` guard read that as
   "pod is being torn down", and every healthy pod was skipped. Every namespace verified zero
   containers. Only the `MATCHED=0` refusal (LSN-008/LSN-024) kept this from printing a green.

2. **A deployed agent tier could never be updated.** `verify_cluster_admin` asked whether the Agent
   CR existed; `run_step` treats a true verify as "already completed" and skips execute. So the
   first apply won and every later refresh was a no-op. The live install ran the operator and the
   platform tier at src-49cd0e3 while cluster-admin and developer-team sat at src-e8e6423 — and
   the tag was only the visible part, since the template also carries chat enablement, secret refs
   and harness config.

3. **A local deploy blinded a release check.** `kustomize edit set image` writes TRACKED files
   (config/{manager,router}/kustomization.yaml). Repointing them at the project's Artifact Registry
   makes them stop matching `ghcr.io/gke-labs/kube-agents/...`, which is the only thing
   test_image_provenance scans for, so the released tag it guards vanished from the scan.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "k8s-operator/scripts"
LIVE_REFRESH = SCRIPTS / "live_refresh.sh"
PROVISION_12 = SCRIPTS / "provision_12_deploy_agent_tiers.sh"
PROVISION_03 = SCRIPTS / "provision_03_gcp_gke_operator.sh"

# The `while IFS=<sep> read -r -a line` that drives the digest comparison.
IFS_LINE = re.compile(r"while\s+IFS=(\$'[^']*'|'[^']*'|\"[^\"]*\"|\S+)\s+read\b")


def _unquote(tok: str) -> str:
    """Resolve the shell spelling of a separator to the character it denotes."""
    return subprocess.run(
        ["bash", "-c", f'printf %s {tok}'], capture_output=True, text=True, check=True
    ).stdout


class DigestVerificationCanSeeHealthyPods(unittest.TestCase):
    def setUp(self):
        self.text = LIVE_REFRESH.read_text()

    def test_the_record_separator_is_not_ifs_whitespace(self):
        """Space, tab and newline get run-collapsed by `read`; nothing else does.

        This is the property, stated directly. A separator that is IFS whitespace cannot carry an
        empty field, and `deletionTimestamp` is empty for exactly the pods this check exists to
        look at.
        """
        seps = IFS_LINE.findall(self.text)
        self.assertTrue(seps, "live_refresh.sh no longer has a `while IFS=... read` loop to check")
        for tok in seps:
            sep = _unquote(tok)
            self.assertNotIn(
                sep,
                (" ", "\t", "\n"),
                f"IFS={tok} is IFS whitespace: bash collapses runs of it and drops empty fields, so"
                " a healthy pod's empty deletionTimestamp vanishes and every field after it shifts"
                " left by one. Use a non-whitespace separator such as '|'.",
            )

    def test_the_jsonpath_emits_the_same_separator_the_loop_splits_on(self):
        """Two definition sites, one contract. Changing one and not the other silently mis-parses."""
        seps = {_unquote(t) for t in IFS_LINE.findall(self.text)}
        # Anchored on deletionTimestamp: live_refresh.sh has a second, simpler per-pod jsonpath
        # (the deployment-name listing) that starts identically and carries no field separator.
        jsonpath = re.search(r"-o jsonpath='([^']*deletionTimestamp[^']*)'", self.text)
        self.assertIsNotNone(jsonpath, "the per-pod jsonpath in check_namespace was not found")
        # Every {"..."} literal, escapes resolved. `\t` is TWO characters in the source, so a
        # single-character pattern here would not see a reintroduced tab at all — the mutation that
        # skews the jsonpath back to tabs sailed through this test until the escape was decoded.
        emitted = {
            lit.encode().decode("unicode_escape") for lit in re.findall(r'\{"([^"]+)"\}', jsonpath.group(1))
        }
        emitted.discard("\n")  # the record terminator, not a field separator
        self.assertTrue(emitted, "the jsonpath emits no field separator")
        self.assertTrue(
            emitted <= seps,
            f"the jsonpath emits {emitted!r} as a field separator but the read loop splits on"
            f" {seps!r}; the fields will not line up with the positions check_namespace indexes",
        )

    def test_a_healthy_pod_record_actually_parses(self):
        """Behavioural, not structural: run the real separator against a real-shaped record.

        A pod that is NOT being deleted emits an empty second field. Parsed correctly the array is
        [name, "", image, imageID]; under the tab bug it was [name, image, imageID] and the guard
        skipped the pod. Asserting the arity and the empty slot pins the actual failure.
        """
        seps = {_unquote(t) for t in IFS_LINE.findall(self.text)}
        self.assertEqual(1, len(seps), f"expected one separator across the read loops, got {seps!r}")
        sep = seps.pop()
        record = sep.join(["pod-abc", "", "reg/platform-agent:t", "reg/platform-agent@sha256:dead"])
        out = subprocess.run(
            [
                "bash",
                "-c",
                'printf "%s\\n" "$1" | while IFS="$2" read -r -a a; do '
                'echo "${#a[@]}"; echo "[${a[1]}]"; done',
                "_",
                record,
                sep,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        self.assertEqual(
            ["4", "[]"],
            out,
            "a healthy pod's record did not parse as [name, <empty>, image, imageID]; the"
            " deletionTimestamp slot must survive splitting or every healthy pod is skipped",
        )


class AgentTiersAreReapplied(unittest.TestCase):
    def test_tier_verifies_are_not_existence_checks(self):
        """`run_step` skips execute when verify succeeds, so verify must mean "converged".

        `kubectl get agent <name>` answers "was this ever applied", which is true forever after the
        first refresh. provision_08 already settled this for the platform tier with an unconditional
        `return 1`; the two child tiers did not follow, and went stale for it.
        """
        text = PROVISION_12.read_text()
        for func in ("verify_cluster_admin", "verify_developer_team"):
            body = re.search(rf"^{func}\(\)\s*\{{(.*?)^\}}", text, re.MULTILINE | re.DOTALL)
            self.assertIsNotNone(body, f"{func} is gone from provision_12")
            src = body.group(1)
            self.assertNotRegex(
                src,
                r"kubectl\s+get\s+agent",
                f"{func} decides convergence by whether the Agent CR exists. That is true from the"
                " first apply onward, so run_step prints 'Already completed' and the tier never"
                " picks up a new image, chat setting or secret ref. Return 1 and let the"
                " idempotent `kubectl apply` in execute_* do the work.",
            )
            self.assertRegex(
                src,
                r"(?m)^\s*return 1\s*$",
                f"{func} must unconditionally request a re-apply (after its disabled/absent"
                " short-circuit), the way provision_08's verify_custom_resource does",
            )


class DeployDoesNotDirtyTrackedPins(unittest.TestCase):
    def test_provision_03_restores_the_kustomizations_it_rewrites(self):
        text = PROVISION_03.read_text()
        for f in ("config/manager/kustomization.yaml", "config/router/kustomization.yaml"):
            self.assertIn(
                f,
                text,
                f"provision_03 no longer accounts for {f}, which `kustomize edit set image` writes",
            )
        self.assertRegex(
            text,
            r"git -C \"\$OPERATOR_DIR\" checkout -- \"\$_f\"",
            "provision_03 no longer restores the kustomizations it rewrites; a local deploy leaves"
            " the release pins repointed at a private registry, where test_image_provenance's"
            " ghcr.io scan cannot see them",
        )
        self.assertRegex(
            text,
            r"git -C \"\$OPERATOR_DIR\" diff --quiet",
            "the restore must be conditional on the file being clean beforehand, or it discards an"
            " edit somebody is working on",
        )


if __name__ == "__main__":
    unittest.main()
