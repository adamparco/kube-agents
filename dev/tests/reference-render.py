#!/usr/bin/env python3
"""Reference-tree render golden (kube-agents Phase 8, P8-T2, extended P8-T3).

Every security manifest in this repo exists twice: once as the template the
installer renders (`k8s-operator/scripts/*.template`) and once as a committed
exemplar in the reference GitOps tree. Two copies of a security manifest drift,
and the direction they drift in is not symmetric — the exemplar is what a human
reads and copies, the template is what actually lands on the cluster. Phase 5
shipped both copies of the egress policies; by Phase 8 the exemplars allowed
`REPLACE_WITH_HUB_INFERENCE_CIDR` (un-appliable) and the platform/cluster-admin
copies had **no rule at all** for the in-cluster LiteLLM/minter hop, which
default-deny egress also governs. Nothing noticed, because nothing compared them.

This check makes the exemplars a *derived artifact*: each must be byte-identical
to the corresponding `render_*` helper in `k8s-operator/scripts/common.sh`,
called the way the install path calls it. There is no tolerance and no
normalisation — a whitespace-only difference is still a diff, because the thing
being protected is "these were regenerated", not "these look similar".

Covered (P8-T2): the three per-tier egress NetworkPolicies.
Covered (P8-T3): the tenant ResourceQuota and the tenant default-deny floor —
added when those two were wired into the install path, because the moment an
installer renders a manifest, the committed copy stops being the source of truth
and becomes a thing that can silently disagree with what ships.
Covered (P8-T4): the tenant ExternalName aliases, on the same terms and for the
same reason — until Phase 8 nothing applied them, so a dev-team agent's model
endpoint resolved to NXDOMAIN on every real multi-tier install.

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

  6. **The tenant quota and default-deny exemplars equal their renders**, and the
     quota actually bounds something — a ResourceQuota with no `requests.*` /
     `limits.*` entries caps nothing and, worse, stops forcing pods to declare
     them, which is the property provision_12 orders itself around.

Covered (P9-T7d-4): the kube-apiserver rule, egress rule 9. It is the only
destination in this allowlist with no published, stable range — the endpoint is
per-cluster, and on a public GKE control plane it is a bare IP — so unlike every
other rule it cannot be pinned in the committed exemplars without stating a
fiction about somebody's cluster. Enforcement therefore lives in `provision_13`
as resolve-or-refuse, and these four properties are what make that claim
checkable at L0 rather than asserted in a comment.

  7. **Rule 9 is absent from the base render**, and that absence is a decision
     rather than an accident. Same shape as check 4: if a default API-server
     address ever appears in the base render, it is a fiction shipped to every
     reader of the exemplars.
  8. **Given the address, rule 9 renders every CIDR it was handed, bound to 443
     and nothing else.** The failure guarded is the one check 5 guards: an allow
     that names the host but omits `ports:` is a whole-host allow, and in a diff
     it reads exactly like the rule that was asked for.
  9. **`resolve_apiserver_cidrs` fails closed.** With an override it returns it
     verbatim; with no override and no reachable cluster it must exit non-zero
     and print nothing. An empty answer that looked like success would render a
     policy with no rule 9 — precisely the hole this unit closes — and nothing
     downstream would notice until a broker hung on TokenReview.
 10. **`provision_13` still refuses.** The resolver can fail closed and the step
     can ignore it; property 9 cannot see that. This one reads the step's source
     for the arm that exits non-zero, and is labelled a source property because
     that is what it is.

Usage:
    python3 dev/tests/reference-render.py [REPO_ROOT]
    python3 dev/tests/reference-render.py --self-test

Exit 0 = the exemplars are the render and the render is sound; 1 = violations
(prints a unified diff for a drifted exemplar). Stdlib only, no cluster.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# TemporaryDirectory objects clean up when they are collected, and the shims below
# have to outlive the call that makes them, so the handles are parked here.
_TMP: list[tempfile.TemporaryDirectory] = []


def _shim_dir(name: str, script: str) -> Path:
    """A throwaway directory holding one executable, for prepending to PATH."""
    tmp = tempfile.TemporaryDirectory()
    _TMP.append(tmp)
    exe = Path(tmp.name) / name
    exe.write_text(script)
    exe.chmod(0o755)
    return Path(tmp.name)


def _stub_repo(func_source: str) -> Path:
    """A fake repo whose common.sh defines only the function under control.

    `check_apiserver_resolution` runs the real shell, so its negative control has
    to be a different shell rather than a mutated string — this builds the
    smallest tree that check knows how to source.
    """
    tmp = tempfile.TemporaryDirectory()
    _TMP.append(tmp)
    scripts = Path(tmp.name) / "k8s-operator" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "common.sh").write_text(func_source + "\n")
    return Path(tmp.name)

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

# The tenant-isolation pair, added in P8-T3 when they were wired into the install
# path. Keyed by the common.sh helper that renders each one; the namespace is the
# helper's only argument, and it is the reference bundle's tenant.
TENANT_NAMESPACE = "team-x"
TENANT = {
    "render_tenant_quota": (
        "examples/gitops-repo/clusters/cluster-a/namespaces/team-x/10-resourcequota.yaml"
    ),
    "render_tenant_default_deny": (
        "examples/gitops-repo/clusters/cluster-a/namespaces/team-x/20-netpol-default-deny.yaml"
    ),
    "render_tenant_service_aliases": (
        "examples/gitops-repo/clusters/cluster-a/namespaces/team-x/40-service-aliases.yaml"
    ),
}

CONTROL_NAMESPACE = "kubeagents-system"

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


def render_tenant(repo: Path, helper: str) -> str:
    """Call a tenant render helper exactly as provision_12/13 do."""
    scripts = repo / "k8s-operator" / "scripts"
    script = (
        f'SCRIPT_DIR="{scripts}"; source "{scripts}/common.sh" --dry-run >/dev/null 2>&1; '
        f'{helper} "{TENANT_NAMESPACE}"'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(scripts),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{helper}({TENANT_NAMESPACE}) failed: {proc.stderr.strip()}")
    return proc.stdout


def check_tenant_exemplars(repo: Path, rendered: dict[str, str]) -> list[str]:
    bad = []
    for helper, rel in TENANT.items():
        path = repo / rel
        if not path.is_file():
            bad.append(f"{rel}: exemplar missing — the reference tree lost a tenant manifest")
            continue
        actual = path.read_text()
        if actual != rendered[helper]:
            diff = "\n".join(
                list(
                    difflib.unified_diff(
                        rendered[helper].splitlines(),
                        actual.splitlines(),
                        fromfile=f"{helper}({TENANT_NAMESPACE})",
                        tofile=rel,
                        lineterm="",
                    )
                )[:40]
            )
            bad.append(
                f"{rel}: DRIFTED from the template render. Regenerate it; do not hand-edit.\n{diff}"
            )
    return bad


def check_quota_bounds_compute(quota: str) -> list[str]:
    """A quota with no compute bounds is not a blast-radius control.

    This is not pedantry about completeness. `provision_12` applies the quota
    *before* the agent pod specifically because a quota carrying `requests.*` /
    `limits.*` forces every pod in the namespace to declare them — that coupling
    is the reason for the ordering. Drop those four entries and the quota still
    applies, still looks like a quota, and silently stops doing either job.
    """
    required = ("requests.cpu", "requests.memory", "limits.cpu", "limits.memory")
    missing = [k for k in required if not re.search(rf"^\s*{re.escape(k)}:", quota, re.M)]
    if missing:
        return [
            f"tenant quota: no {', '.join(missing)} — this quota bounds no compute, and pods in "
            f"the namespace are no longer forced to declare requests+limits"
        ]
    return []


def check_aliases_point_at_the_control_namespace(aliases: str) -> list[str]:
    """The aliases must be ExternalName, and must point somewhere else.

    Two ways this manifest can be wrong while still applying cleanly. It can name
    the right services with the wrong `type` — a ClusterIP Service with no
    selector resolves to a black hole rather than an error, so the agent's model
    calls hang instead of failing. Or an alias can CNAME a name to itself
    (`litellm.team-x` -> `litellm.team-x`), which is what a copy-paste of the
    tenant namespace into the target produces; the resolver loops and the failure
    surfaces as a timeout somewhere else entirely.
    """
    bad = []
    body_text = body(aliases)
    for svc in ("litellm", "github-token-minter"):
        target = f"{svc}.{CONTROL_NAMESPACE}.svc.cluster.local"
        if f"externalName: {target}" not in body_text:
            bad.append(
                f"service aliases: no ExternalName for {svc} pointing at {target} — the "
                f"dev-team agent's rendered config resolves to nothing"
            )
    kinds = re.findall(r"^\s*type: (\S+)", body_text, re.M)
    for k in kinds:
        if k != "ExternalName":
            bad.append(f"service aliases: a Service of type {k} — only ExternalName is a DNS alias")
    for m in re.finditer(r"^\s*externalName: (\S+)", body_text, re.M):
        if f".{CONTROL_NAMESPACE}." not in m.group(1):
            bad.append(
                f"service aliases: {m.group(1)} does not resolve into {CONTROL_NAMESPACE} — an "
                f"alias to its own namespace is a CNAME loop"
            )
    return bad


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


def check_apiserver_absent_by_default(rendered: dict[str, str]) -> list[str]:
    """No API-server rule may appear unless the installer resolved an address.

    The exemplars are a derived artifact that humans read and copy. Any CIDR on
    :443 that is not one of the two published destinations the base allowlist
    already argues for (Google's restricted VIP, GitHub's four blocks) is an
    address somebody's install invented, and it would be copied forward as if it
    were a fact about every cluster.
    """
    known = {
        "199.36.153.8/30",
        "192.30.252.0/22",
        "185.199.108.0/22",
        "140.82.112.0/20",
        "143.55.64.0/20",
    }
    bad = []
    for tier, text in rendered.items():
        for cidr in set(re.findall(r"cidr: (\S+)", body(text))) - known:
            bad.append(
                f"{tier}: the base render allows {cidr}, which is not one of the published ranges "
                f"this allowlist argues for. If that is the API server, it is per-cluster and must "
                f"come from provision_13, not from a default baked into the exemplars."
            )
    return bad


def check_apiserver_block(apiserver_render: str) -> list[str]:
    """Rule 9 must carry every CIDR it was given, on 443 and nothing else."""
    bad = []
    given = ("10.96.0.1/32", "34.86.1.2/32")
    for cidr in given:
        m = re.search(rf"cidr: {re.escape(cidr)}\n(.*?)(?=\n    - to:|\Z)", apiserver_render, re.S)
        if not m:
            bad.append(
                f"apiserver render: no rule for {cidr} — KUBE_APISERVER_CIDRS was a list and the "
                f"render dropped an entry, so pods reach the address the installer did not pick"
            )
            continue
        if "ports:" not in m.group(1):
            bad.append(f"apiserver render: {cidr} has no ports: list — that is a whole-host allow")
            continue
        found = set(re.findall(r"port: (\d+)", m.group(1)))
        if found != {"443"}:
            bad.append(
                f"apiserver render: {cidr} allows {sorted(found)} — the API server is reached on "
                f"443 and the rule may not widen past it"
            )
    return bad


def check_apiserver_resolution(repo: Path) -> list[str]:
    """`resolve_apiserver_cidrs` returns the override, and otherwise fails closed.

    The second half is the load-bearing one. `provision_13` branches on this
    function's exit status, so a resolver that returned 0 with an empty string
    would take the success arm, render a policy with no rule 9, and print a
    success line saying so. Nothing downstream looks again — the first symptom is
    a broker hanging on TokenReview and reporting an authentication error.
    """
    scripts = repo / "k8s-operator" / "scripts"
    prelude = f'SCRIPT_DIR="{scripts}"; source "{scripts}/common.sh" --dry-run >/dev/null 2>&1; '
    base_path = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"

    def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", prelude + "resolve_apiserver_cidrs"],
            capture_output=True,
            text=True,
            cwd=str(scripts),
            env={"PATH": base_path, **env},
        )

    bad = []
    override = run({"KUBE_APISERVER_CIDR": "10.96.0.1/32,34.86.1.2/32"})
    if override.returncode != 0 or override.stdout.strip() != "10.96.0.1/32,34.86.1.2/32":
        bad.append(
            f"resolve_apiserver_cidrs: KUBE_APISERVER_CIDR was not returned verbatim "
            f"(rc={override.returncode}, out={override.stdout.strip()!r}) — the operator's explicit "
            f"answer must win over anything auto-detected"
        )

    # No override, and a kubectl that cannot reach anything. Shimmed rather than
    # removed from PATH: an unrunnable kubectl and an unreachable cluster are
    # different failures, and the one the installer actually meets is the second.
    shim = _shim_dir(
        "kubectl", "#!/bin/sh\necho 'The connection to the server was refused' >&2\nexit 1\n"
    )
    closed = run({"PATH": f"{shim}:{base_path}"})
    if closed.returncode == 0:
        bad.append(
            f"resolve_apiserver_cidrs: exited 0 with nothing to resolve (out={closed.stdout.strip()!r}) "
            f"— provision_13 would take the success arm and apply a policy with no rule 9"
        )
    elif closed.stdout.strip():
        bad.append(
            f"resolve_apiserver_cidrs: failed but still printed {closed.stdout.strip()!r} — a caller "
            f"that captures stdout before checking rc would render that as a CIDR"
        )
    return bad


def check_provision_refuses(source: str) -> list[str]:
    """provision_13 must end the run when the address cannot be resolved.

    A source property, and named as one: it reads the shell rather than running
    it, because running it needs a cluster. What it protects is the coupling — a
    later edit that turns the `else` arm into a warning would leave properties 7-9
    green and reopen the hole, because each of those is true of a resolver nobody
    obeys.
    """
    m = re.search(
        r"elif\s+KUBE_APISERVER_CIDRS=\"\$\(resolve_apiserver_cidrs\)\";\s*then(.*?)\nfi\b",
        source,
        re.S,
    )
    if not m:
        return [
            "provision_13: no `elif KUBE_APISERVER_CIDRS=\"$(resolve_apiserver_cidrs)\"; then` arm — "
            "the step no longer branches on whether the address resolved (or this check's subject "
            "was refactored away; see LSN-035)"
        ]
    else_arm = m.group(1).split("\nelse\n")
    if len(else_arm) != 2:
        return ["provision_13: the resolve branch has no else arm — an unresolved address is unhandled"]
    if not re.search(r"^\s*exit 1\s*$", else_arm[1], re.M):
        return [
            "provision_13: the unresolved-address arm does not `exit 1`. Warning and continuing "
            "applies an egress policy with no rule 9, and the broker's every write then fails as an "
            "authentication error that never mentions the network."
        ]
    return []


def run_all(repo: Path) -> list[str]:
    rendered = {t: render(repo, t) for t in TIERS}
    wi = render(repo, "platform", {"WORKLOAD_IDENTITY_ENABLED": "true", "GKE_DATAPLANE": "auto"})
    tenant = {h: render_tenant(repo, h) for h in TENANT}
    apiserver = render(repo, "platform", {"KUBE_APISERVER_CIDRS": "10.96.0.1/32,34.86.1.2/32"})
    provision = (
        repo / "k8s-operator" / "scripts" / "provision_13_apply_network_policies.sh"
    ).read_text()
    return (
        check_exemplars_match(repo, rendered)
        + check_no_placeholder(rendered)
        + check_no_open_egress(rendered)
        + check_metadata_absent_by_default(rendered)
        + check_wi_pairs(wi)
        + check_tenant_exemplars(repo, tenant)
        + check_quota_bounds_compute(tenant["render_tenant_quota"])
        + check_aliases_point_at_the_control_namespace(tenant["render_tenant_service_aliases"])
        + check_apiserver_absent_by_default(rendered)
        + check_apiserver_block(apiserver)
        + check_apiserver_resolution(repo)
        + check_provision_refuses(provision)
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
        (
            "drifted tenant exemplar rejected",
            lambda: check_tenant_exemplars(Path("/nonexistent"), {h: "x" for h in TENANT}),
        ),
        (
            "quota with no compute bounds rejected",
            lambda: check_quota_bounds_compute('spec:\n  hard:\n    pods: "50"\n'),
        ),
        (
            "alias of the wrong Service type rejected",
            lambda: check_aliases_point_at_the_control_namespace(
                "spec:\n  type: ClusterIP\n  externalName: litellm.kubeagents-system.svc.cluster.local\n"
                "---\nspec:\n  type: ExternalName\n"
                "  externalName: github-token-minter.kubeagents-system.svc.cluster.local\n"
            ),
        ),
        (
            "alias CNAMEing to its own namespace rejected",
            lambda: check_aliases_point_at_the_control_namespace(
                "spec:\n  type: ExternalName\n  externalName: litellm.team-x.svc.cluster.local\n"
                "---\nspec:\n  type: ExternalName\n"
                "  externalName: github-token-minter.kubeagents-system.svc.cluster.local\n"
            ),
        ),
        (
            "quota missing only limits.memory rejected",
            lambda: check_quota_bounds_compute(
                'spec:\n  hard:\n    requests.cpu: "8"\n    requests.memory: 16Gi\n'
                '    limits.cpu: "16"\n'
            ),
        ),
        (
            "an API-server address baked into the base render rejected",
            lambda: check_apiserver_absent_by_default(
                {"platform": "        - ipBlock:\n            cidr: 34.86.1.2/32"}
            ),
        ),
        (
            "rule 9 dropping one of the CIDRs it was given rejected",
            lambda: check_apiserver_block(
                "    - to:\n        - ipBlock:\n            cidr: 10.96.0.1/32\n"
                "      ports:\n        - protocol: TCP\n          port: 443\n"
            ),
        ),
        (
            "rule 9 with no ports: list rejected",
            lambda: check_apiserver_block(
                "    - to:\n        - ipBlock:\n            cidr: 10.96.0.1/32\n"
                "        - ipBlock:\n            cidr: 34.86.1.2/32\n"
            ),
        ),
        (
            "rule 9 widened past 443 rejected",
            lambda: check_apiserver_block(
                "    - to:\n        - ipBlock:\n            cidr: 10.96.0.1/32\n"
                "      ports:\n        - protocol: TCP\n          port: 443\n"
                "        - protocol: TCP\n          port: 10250\n"
                "    - to:\n        - ipBlock:\n            cidr: 34.86.1.2/32\n"
                "      ports:\n        - protocol: TCP\n          port: 443\n"
            ),
        ),
        (
            "a resolver that succeeds with nothing rejected",
            lambda: check_apiserver_resolution(_stub_repo("resolve_apiserver_cidrs() { return 0; }")),
        ),
        (
            "a resolver that prints on failure rejected",
            lambda: check_apiserver_resolution(
                _stub_repo(
                    "resolve_apiserver_cidrs() {\n"
                    '  [ -n "${KUBE_APISERVER_CIDR:-}" ] && { printf %s\\\\n "${KUBE_APISERVER_CIDR}"; return 0; }\n'
                    "  echo 'error: nothing resolved'\n"
                    "  return 1\n"
                    "}"
                )
            ),
        ),
        (
            "provision_13 warning instead of exiting rejected",
            lambda: check_provision_refuses(
                'elif KUBE_APISERVER_CIDRS="$(resolve_apiserver_cidrs)"; then\n'
                '  print_success "ok"\n'
                "else\n"
                '  print_warning "could not resolve — continuing without rule 9"\n'
                "fi\n"
            ),
        ),
        (
            "provision_13 losing the resolve branch entirely rejected",
            lambda: check_provision_refuses(
                'export KUBE_APISERVER_CIDRS="${KUBE_APISERVER_CIDR:-}"\n'
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
        print("Reference-tree render violations:\n")
        for v in violations:
            print(f"  - {v}")
        return 1
    n = len(TIERS) + len(TENANT)
    print(f"Reference render: OK — all {n} exemplars are the template render, the base allowlist is")
    print("  placeholder-free, metadata-free and API-server-free, the WI rules are correctly paired,")
    print("  rule 9 renders on 443 only when an address is supplied, resolve_apiserver_cidrs fails")
    print("  closed and provision_13 refuses, the tenant quota bounds compute, and the service")
    print("  aliases resolve into the control namespace.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
