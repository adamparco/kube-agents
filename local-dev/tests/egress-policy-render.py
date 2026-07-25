#!/usr/bin/env python3
"""Egress-policy render golden (kube-agents Phase 8, P8-T2).

The three per-tier egress NetworkPolicies exist twice: once as the template the
installer renders (`k8s-operator/scripts/netpol-agent-egress.yaml.template`) and
once as committed exemplars in the reference GitOps tree. Two copies of a
security policy drift, and the direction they drift in is not symmetric — the
exemplar is what a human reads and copies, the template is what actually lands
on the cluster. Phase 5 shipped both; by Phase 8 the exemplars allowed
`REPLACE_WITH_HUB_INFERENCE_CIDR` (un-appliable) and the platform/cluster-admin
copies had **no rule at all** for the in-cluster LiteLLM/minter hop, which
default-deny egress also governs. Nothing noticed, because nothing compared them.

This check makes the exemplars a *derived artifact*: they must be byte-identical
to `render_egress_policy` from `k8s-operator/scripts/common.sh`, called with each
tier's name/namespace/tier and no optional blocks. There is no tolerance and no
normalisation — a whitespace-only difference is still a diff, because the thing
being protected is "these were regenerated", not "these look similar".

Checks (all must pass for exit 0):

  1. **Each exemplar equals its reference render**, byte for byte.
  2. **The reference render carries no placeholder token.** `REPLACE_WITH_*` in a
     `cidr:` field is not a fillable template — the API server rejects it, so the
     whole bundle fails to apply (V-CMP-003). The optional remote-hub and
     Workload-Identity rules are absent-unless-configured for exactly this reason.
  3. **No `0.0.0.0/0` rule.** The policy is a pure allowlist; a broad
     allow-with-exceptions would make "arbitrary hosts are unreachable" false.
  4. **The metadata server is absent from the base render.** Raw node credentials
     are unreachable *by omission*, not by a deny rule a later edit could reorder
     away. `WORKLOAD_IDENTITY_ENABLED=true` is what adds it back, narrowly.
  5. **The WI render pairs each metadata IP with the right ports.**
     `169.254.169.252/32` → 988, 987 (Dataplane V1 / Calico, GKE >= 1.21.0-gke.1000);
     `169.254.169.254/32` → 80, 8080 (Dataplane V2). These are not interchangeable
     and the wrong pairing fails as a timeout inside the auth client library — an
     authentication error that never mentions the network.

Negative control (`--self-test`): each check is re-run against a fixture that
reintroduces the defect it guards, and must fail. A check that cannot fail is not
evidence (09 §6, V-MET-014).

Usage:
    python3 local-dev/tests/egress-policy-render.py [REPO_ROOT]
    python3 local-dev/tests/egress-policy-render.py --self-test

Exit 0 = the exemplars are the render and the render is sound; 1 = violations
(prints a unified diff for a drifted exemplar). Stdlib only, no cluster.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
from pathlib import Path

# tier -> (netpol name, namespace, committed exemplar path)
TIERS = {
    "platform": (
        "platform-egress",
        "kubeagents-system",
        "examples/gitops-repo/fleet/netpol-platform-egress.yaml",
    ),
    "cluster-admin": (
        "cluster-admin-egress",
        "kubeagents-system",
        "examples/gitops-repo/clusters/cluster-a/agents/netpol-cluster-admin-egress.yaml",
    ),
    "developer-team": (
        "developer-team-egress",
        "team-x",
        "examples/gitops-repo/clusters/cluster-a/namespaces/team-x/30-netpol-developer-team-egress.yaml",
    ),
}

PLACEHOLDER = re.compile(r"REPLACE_WITH_|PLACEHOLDER")

# The dataplane-specific metadata pairings, from
# cloud.google.com/kubernetes-engine/docs/how-to/network-policy.
WI_PAIRS = {
    "169.254.169.252/32": ("988", "987"),  # Dataplane V1 / Calico
    "169.254.169.254/32": ("80", "8080"),  # Dataplane V2
}


def render(repo: Path, tier: str, env: dict[str, str] | None = None) -> str:
    """Call render_egress_policy exactly as provision_13 does."""
    name, ns, _ = TIERS[tier]
    scripts = repo / "k8s-operator" / "scripts"
    script = (
        f'SCRIPT_DIR="{scripts}"; source "{scripts}/common.sh" --dry-run >/dev/null 2>&1; '
        f'render_egress_policy "{name}" "{ns}" "{tier}"'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(scripts),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **(env or {})},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"render_egress_policy({tier}) failed: {proc.stderr.strip()}")
    return proc.stdout


def check_exemplars_match(repo: Path, rendered: dict[str, str]) -> list[str]:
    bad = []
    for tier, (_, _, rel) in TIERS.items():
        path = repo / rel
        if not path.is_file():
            bad.append(f"{rel}: exemplar missing — the reference tree lost a policy")
            continue
        actual = path.read_text()
        if actual != rendered[tier]:
            diff = "\n".join(
                list(
                    difflib.unified_diff(
                        rendered[tier].splitlines(),
                        actual.splitlines(),
                        fromfile=f"render_egress_policy({tier})",
                        tofile=rel,
                        lineterm="",
                    )
                )[:40]
            )
            bad.append(
                f"{rel}: DRIFTED from the template render. Regenerate it; do not hand-edit.\n{diff}"
            )
    return bad


def body(text: str) -> str:
    """The YAML the API server sees, with commentary dropped.

    These two checks are about what the policy *allows*, and a comment allows
    nothing. The template's own header says "there is deliberately NO 0.0.0.0/0
    rule" — matching that sentence as a violation would make the check fire on
    the documentation of its own invariant, which trains people to ignore it.
    """
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def check_no_placeholder(rendered: dict[str, str]) -> list[str]:
    return [
        f"{tier}: rendered policy contains a placeholder token — un-appliable (V-CMP-003)"
        for tier, text in rendered.items()
        if PLACEHOLDER.search(body(text))
    ]


def check_no_open_egress(rendered: dict[str, str]) -> list[str]:
    return [
        f"{tier}: rendered policy contains 0.0.0.0/0 — that is not an allowlist"
        for tier, text in rendered.items()
        if "0.0.0.0/0" in body(text)
    ]


def check_metadata_absent_by_default(rendered: dict[str, str]) -> list[str]:
    return [
        f"{tier}: the base render reaches a metadata address ({m.group(0)}) with Workload Identity "
        f"OFF — the raw node service account is reachable"
        for tier, text in rendered.items()
        for m in [re.search(r"cidr: (169\.254\.169\.\d+/32)", body(text))]
        if m
    ]


def check_wi_pairs(wi_render: str) -> list[str]:
    bad = []
    for cidr, ports in WI_PAIRS.items():
        # The rule's own ports: list — from this cidr up to the next `- to:`.
        m = re.search(
            rf"cidr: {re.escape(cidr)}\n(.*?)(?=\n    - to:|\Z)", wi_render, re.S
        )
        if not m:
            bad.append(f"WI render: no rule for {cidr}")
            continue
        body = m.group(1)
        if "ports:" not in body:
            bad.append(f"WI render: {cidr} has no ports: list — that is a whole-host allow")
            continue
        found = set(re.findall(r"port: (\d+)", body))
        for p in ports:
            if p not in found:
                bad.append(
                    f"WI render: {cidr} is missing port {p}. The IP<->port pairings are "
                    f"dataplane-specific; the wrong one fails as an auth timeout."
                )
        for extra in found - set(ports):
            bad.append(f"WI render: {cidr} allows unexpected port {extra} — widen deliberately or not at all")
    return bad


def run_all(repo: Path) -> list[str]:
    rendered = {t: render(repo, t) for t in TIERS}
    wi = render(repo, "platform", {"WORKLOAD_IDENTITY_ENABLED": "true", "GKE_DATAPLANE": "auto"})
    return (
        check_exemplars_match(repo, rendered)
        + check_no_placeholder(rendered)
        + check_no_open_egress(rendered)
        + check_metadata_absent_by_default(rendered)
        + check_wi_pairs(wi)
    )


def self_test() -> int:
    """Each check must fail on the defect it guards."""
    controls = [
        (
            "drifted exemplar rejected",
            lambda: check_exemplars_match(
                Path("/nonexistent"), {t: "x" for t in TIERS}
            ),
        ),
        (
            "placeholder in a rendered policy rejected",
            lambda: check_no_placeholder({"platform": "cidr: REPLACE_WITH_HUB_INFERENCE_CIDR"}),
        ),
        (
            "0.0.0.0/0 rejected",
            lambda: check_no_open_egress({"platform": "        - ipBlock:\n            cidr: 0.0.0.0/0"}),
        ),
        (
            "metadata address with WI off rejected",
            lambda: check_metadata_absent_by_default({"platform": "cidr: 169.254.169.254/32"}),
        ),
        (
            "metadata rule with no ports rejected",
            lambda: check_wi_pairs("    - to:\n        - ipBlock:\n            cidr: 169.254.169.252/32\n"),
        ),
        (
            "wrong IP<->port pairing rejected",
            lambda: check_wi_pairs(
                "    - to:\n        - ipBlock:\n            cidr: 169.254.169.252/32\n"
                "      ports:\n        - protocol: TCP\n          port: 80\n"
                "    - to:\n        - ipBlock:\n            cidr: 169.254.169.254/32\n"
                "      ports:\n        - protocol: TCP\n          port: 988\n"
            ),
        ),
    ]
    failures = 0
    for name, fn in controls:
        if fn():
            print(f"  control OK   (fires): {name}")
        else:
            print(f"  control DEAD (silent): {name}")
            failures += 1
    print(f"\n{len(controls) - failures}/{len(controls)} negative controls fire.")
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    try:
        violations = run_all(repo)
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1
    if violations:
        print("Egress-policy render violations:\n")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("Egress policy render: OK — the three exemplars are the template render, the base")
    print("  allowlist is placeholder-free and metadata-free, and the WI rules are correctly paired.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
