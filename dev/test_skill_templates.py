#!/usr/bin/env python3
"""The third copy of every isolation manifest (kube-agents Phase 8, P8-T4).

P8-T2 found that a security manifest existing twice — as an installer template and
as a committed exemplar — drifts, and drifts asymmetrically: the copy a human reads
stays right while the copy that lands on the cluster goes wrong. It bound those two
together (dev/tests/reference-render.py) and nobody looked for a third copy.

There is a third copy. The cluster-admin agent's `provision-developer-team` skill
carries its own `assets/*.tmpl` of the tenant quota, the default-deny floor, the
egress allowlist and the service aliases, and that is the copy a REAL TENANT gets:
the skill renders it into a GitOps PR, a human approves it, and CI/CD applies it.
By the time P8-T4 looked, the skill's quota and default-deny assets were the Phase 3
originals and its egress asset still stubbed `REPLACE_WITH_HUB_INFERENCE_CIDR` into
a `cidr:` field — a bundle the API server rejects outright (V-CMP-003). Both gates
were green the whole time, because neither of them could see this file.

So the property here is not "the assets look similar to the templates". It is:

    rendering the skill bundle produces THE SAME BYTES the installer applies.

Compared for two configurations, because the interesting divergence is in the
optional egress widenings and a check that only ever exercises the default would
not see it:

  1. the default bundle (no widenings)
  2. Workload Identity + all three remote-hub CIDRs

Also checked, because it is the specific way these files broke before:

  3. **No corrupted placeholder.** `@@CLUSTER@@dmin` appeared 8 times across 5
     assets — the residue of deriving a template from an instance with a naive
     `cluster-a` -> `@@CLUSTER@@` replace, which over-matched inside the word
     "cluster-admin". It renders correctly for exactly one cluster name and
     silently wrong for every other, which is worse than a syntax error.
  4. **Every token the assets use is one the renderer substitutes**, and every
     token the renderer substitutes is one some asset uses. A typo'd token
     survives into the applied manifest as literal `@@NAMSPACE@@`; a token the
     renderer still knows but no asset mentions is a CLI flag that silently does
     nothing, which is how `--github-cidrs` outlived its rule.

Discovered by `python3 -m unittest discover dev` — the mechanization is the
file being discovered, not a line in a document promising someone will run it
(LSN-019).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "k8s-operator" / "scripts"
SKILL = REPO / "agents/cluster-admin/skills/provision-developer-team"
ASSETS = SKILL / "assets"
RENDERER = SKILL / "scripts" / "render_developer_team.py"

CLUSTER = "cluster-a"
TENANT = "team-x"

# asset basename -> the common.sh invocation that renders the installer's copy.
BOUND = {
    "10-resourcequota.yaml": f'render_tenant_quota "{TENANT}"',
    "20-netpol-default-deny.yaml": f'render_tenant_default_deny "{TENANT}"',
    "30-netpol-developer-team-egress.yaml": (
        f'render_egress_policy "developer-team-egress" "{TENANT}" "developer-team"'
    ),
    "40-service-aliases.yaml": f'render_tenant_service_aliases "{TENANT}"',
}

# The widenings, as the two halves of the same configuration: env for the installer,
# flags for the skill. Kept adjacent so a new widening cannot be added to one side only.
WIDE_ENV = {
    "WORKLOAD_IDENTITY_ENABLED": "true",
    "GKE_DATAPLANE": "auto",
    "HUB_INFERENCE_CIDR": "10.10.0.0/28",
    "HUB_MINTY_CIDR": "10.10.0.16/28",
    "MCP_GROUNDING_CIDRS": "10.10.0.32/28,10.10.0.48/28",
    # Rule 9. The installer resolves this from the cluster; the skill has to be told. Both halves
    # are exercised here because the skill bundle is a SECOND install path for this tier — the
    # installer applying rule 9 says nothing about what a tenant provisioned through the F4 cascade
    # actually gets, and that gap is what P9-T7d-4 was closing.
    "KUBE_APISERVER_CIDRS": "10.96.0.1/32,34.86.1.2/32",
}
WIDE_FLAGS = [
    "--workload-identity",
    "--gke-dataplane", "auto",
    "--hub-inference-cidr", "10.10.0.0/28",
    "--hub-minty-cidr", "10.10.0.16/28",
    "--mcp-cidrs", "10.10.0.32/28,10.10.0.48/28",
    "--kube-apiserver-cidrs", "10.96.0.1/32,34.86.1.2/32",
]

PLACEHOLDER = re.compile(r"@@[A-Z_]+@@")
# A placeholder swallowed by the word it was carved out of. `-` is allowed on purpose:
# `@@NAMESPACE@@-quota` is a legitimate name, `@@CLUSTER@@dmin` is a corruption.
CORRUPTED = re.compile(r"@@[A-Z_]+@@[A-Za-z0-9]")


def install_render(call: str, env: dict[str, str] | None = None) -> str:
    """Render one manifest exactly as the install path renders it."""
    script = (
        f'SCRIPT_DIR="{SCRIPTS}"; source "{SCRIPTS}/common.sh" --dry-run >/dev/null 2>&1; {call}'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(SCRIPTS),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **(env or {})},
    )
    if proc.returncode != 0:
        raise AssertionError(f"install render failed for `{call}`: {proc.stderr.strip()}")
    return proc.stdout


def skill_render(extra: list[str] | None = None) -> Path:
    """Render the whole skill bundle into a temp repo; return the namespace dir."""
    out = Path(tempfile.mkdtemp(prefix="dt-bundle-"))
    proc = subprocess.run(
        [
            sys.executable, str(RENDERER),
            "--cluster", CLUSTER,
            "--namespace", TENANT,
            "--project-id", "your-gcp-project-id",
            "--location", "us-central1",
            "--repo-root", str(out),
            *(extra or []),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"skill render failed: {proc.stderr.strip()}")
    return out / "clusters" / CLUSTER / "namespaces" / TENANT


class TestTheSkillProposesWhatTheInstallerApplies(unittest.TestCase):
    def assert_same_bytes(self, produced: Path, expected: str, name: str, how: str) -> None:
        self.assertTrue(produced.is_file(), f"{name}: the skill bundle no longer contains it")
        actual = produced.read_text()
        if actual == expected:
            return
        import difflib

        diff = "\n".join(
            list(
                difflib.unified_diff(
                    expected.splitlines(),
                    actual.splitlines(),
                    fromfile=f"installer ({how})",
                    tofile=f"skill asset ({how})",
                    lineterm="",
                )
            )[:40]
        )
        self.fail(
            f"{name}: the manifest the skill PROPOSES is not the manifest the installer APPLIES.\n"
            f"Edit k8s-operator/scripts/*.template and regenerate the asset; do not hand-edit it.\n{diff}"
        )

    def test_default_bundle_matches_the_install_path(self):
        bundle = skill_render()
        for name, call in BOUND.items():
            with self.subTest(manifest=name):
                self.assert_same_bytes(bundle / name, install_render(call), name, "default")

    def test_widened_bundle_matches_the_install_path(self):
        """Workload Identity and the remote-hub CIDRs, on both sides.

        The default bundle omits every optional rule, so a check that only rendered
        the default would pass on a skill that could not emit them at all — which
        is the state this test was written to end.
        """
        bundle = skill_render(WIDE_FLAGS)
        egress = "30-netpol-developer-team-egress.yaml"
        self.assert_same_bytes(
            bundle / egress, install_render(BOUND[egress], WIDE_ENV), egress, "widened"
        )

    def test_the_widened_render_actually_widens(self):
        """A check that cannot fail is not evidence (09 §6, V-MET-014).

        If both sides silently dropped the optional blocks, the comparison above
        would still pass — two identical empties. Assert the rules are there.
        """
        egress = (skill_render(WIDE_FLAGS) / "30-netpol-developer-team-egress.yaml").read_text()
        for expected in ("169.254.169.252/32", "169.254.169.254/32", "10.10.0.0/28",
                         "10.10.0.16/28", "10.10.0.32/28", "10.10.0.48/28"):
            self.assertIn(expected, egress, f"the widened render is missing {expected}")
        plain = (skill_render() / "30-netpol-developer-team-egress.yaml").read_text()
        self.assertNotIn("169.254.169", plain, "the DEFAULT bundle reaches the metadata server")
        self.assertNotIn("10.10.0.", plain, "the default bundle carries a hub CIDR nobody asked for")


class TestNoPlaceholderSurvivesIntoAManifest(unittest.TestCase):
    def assets(self) -> list[Path]:
        found = sorted(p for p in ASSETS.rglob("*.tmpl") if p.is_file())
        self.assertTrue(found, "no skill assets found — this check covers nothing")
        return found

    def test_no_corrupted_placeholder(self):
        offenders = []
        for path in self.assets():
            for n, line in enumerate(path.read_text().splitlines(), 1):
                for m in CORRUPTED.finditer(line):
                    offenders.append(f"{path.relative_to(REPO)}:{n}: {m.group(0)}")
        self.assertEqual(
            offenders,
            [],
            "a placeholder has swallowed the text after it — this renders correctly for exactly\n"
            "one value and silently wrong for every other:\n" + "\n".join(offenders),
        )

    def test_the_corruption_detector_fires(self):
        self.assertTrue(CORRUPTED.search("the @@CLUSTER@@dmin tier owns tenancy"))
        self.assertTrue(CORRUPTED.search("name: @@NAMESPACE@@x"))
        self.assertIsNone(CORRUPTED.search("name: @@NAMESPACE@@-quota"))
        self.assertIsNone(CORRUPTED.search("namespace: @@NAMESPACE@@"))
        self.assertIsNone(CORRUPTED.search("clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@"))

    def test_asset_tokens_and_renderer_tokens_are_the_same_set(self):
        used = set()
        for path in self.assets():
            used |= set(PLACEHOLDER.findall(path.read_text()))
        known = set(PLACEHOLDER.findall(RENDERER.read_text()))

        unknown = sorted(used - known)
        self.assertEqual(
            unknown,
            [],
            "an asset uses a token the renderer does not substitute — it would survive verbatim "
            f"into the applied manifest: {unknown}",
        )
        dead = sorted(known - used)
        self.assertEqual(
            dead,
            [],
            "the renderer substitutes a token no asset uses — its CLI flag silently does nothing, "
            f"which is how --github-cidrs outlived its rule: {dead}",
        )

    def test_rendered_bundle_has_no_unsubstituted_token(self):
        bundle = skill_render()
        for path in sorted(bundle.iterdir()):
            with self.subTest(manifest=path.name):
                self.assertIsNone(
                    PLACEHOLDER.search(path.read_text()),
                    f"{path.name} still carries a @@TOKEN@@ after rendering",
                )

    def test_no_placeholder_cidr_in_a_rendered_manifest(self):
        """V-CMP-003, on the copy a real tenant gets.

        `REPLACE_WITH_HUB_INFERENCE_CIDR` is not a CIDR. The API server rejects the
        object, `kubectl apply -f .` fails on that file, and the whole bundle stops
        applying — so the reviewer's choice was to invent a number or delete a
        security rule. The team-lead chat ID is deliberately exempt: it is a plain
        string that applies and matches nobody.
        """
        bundle = skill_render()
        for path in sorted(bundle.glob("*.yaml")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if "cidr:" in line or "ipBlock" in line:
                    self.assertNotIn(
                        "REPLACE_WITH_",
                        line,
                        f"{path.name}:{n} renders an un-appliable placeholder CIDR",
                    )


if __name__ == "__main__":
    unittest.main()
