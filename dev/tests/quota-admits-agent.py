#!/usr/bin/env python3
"""Every ResourceQuota this installer applies must admit the pods it then creates.

WHAT WENT WRONG, AND WHY NOTHING CAUGHT IT
------------------------------------------
On 2026-07-27 a ResourceQuota was applied to `kubeagents-system` at 8 CPU / 16Gi
limits — half the tenant default, and plausible-looking for a namespace nobody
had measured. The namespace actually needs 10.4 CPU / 18.4Gi: the control plane
plus one agent gateway per co-located tier. Pods already running were
grandfathered, so every existing pod stayed green and the install looked
healthy. `platform-agent-gateway` happened to be mid-rollout with no surviving
old pod, so it was locked out completely and sat at 0/1 with

    FailedCreate: exceeded quota: kubeagents-system-quota,
      requested: limits.cpu=3700m, limits.memory=6528Mi,
      used: limits.cpu=6700m, limits.memory=12Gi,
      limited: limits.cpu=8, limits.memory=16Gi

`3700m / 6528Mi` is not a number anybody typed. It is the sum of the four
containers the controller stamps onto every agent gateway — agent, dashboard,
fluent-bit, event-watcher. It is decided in `agent_manifests.go`. The quota that
has to hold it is decided in `common.sh`. Those two numbers are coupled, they
live in different languages in different directories, and until this check
existed nothing compared them.

WHY THIS READS THE GOLDEN RENDER AND NOT THE GO SOURCE
------------------------------------------------------
The gateway footprint is taken from
`k8s-operator/internal/testing/testdata/platform/expected/agent.yaml`, which is
golden-tested against what the controller actually produces. Parsing
`agent_manifests.go` would mean re-implementing Go struct literals in regex and
would break on any refactor that kept behaviour identical. Going through the
golden file means the coupling is enforced end-to-end: change a container's
limits in Go, the golden test forces the golden file to change, and this check
immediately re-does the arithmetic against the quota defaults. The quota can no
longer silently stop fitting the pod.

WHAT IS CHECKED
---------------
  1. **The tenant quota admits one gateway.** `provision_12` applies it BEFORE
     creating the developer-team pod, precisely so a mis-size fails loudly on
     that step. A tenant quota smaller than the pod turns that deliberate
     ordering into a guaranteed install failure.
  2. **The control quota admits the declared baseline plus
     CONTROL_QUOTA_GATEWAYS gateways.** This is the arithmetic in common.sh,
     re-done here from the real footprint rather than trusted.
  3. **CONTROL_QUOTA_GATEWAYS leaves rolling-update headroom.** Two resident
     gateways (platform + cluster-admin) with no spare cannot roll: the new pod
     must be admitted before the old one is released. That is the configuration
     that produced the lockout above, so a value below 3 is rejected.

This check is pure arithmetic on committed files: no cluster, no network. It is
an L0 line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "k8s-operator/internal/testing/testdata/platform/expected/agent.yaml"
COMMON = REPO / "k8s-operator/scripts/common.sh"

# The gateway must be able to roll: new pod admitted before the old is released.
MIN_GATEWAYS_FOR_ROLLING_UPDATE = 3


# ── quantity arithmetic ───────────────────────────────────────────────────────
def cpu_millis(q: str) -> int:
    """Kubernetes CPU quantity -> millicores. '2' -> 2000, '500m' -> 500."""
    q = q.strip().strip('"')
    if not q:
        return 0
    if q.endswith("m"):
        return int(float(q[:-1]))
    return int(float(q) * 1000)


def mem_mib(q: str) -> int:
    """Kubernetes memory quantity -> MiB. '4Gi' -> 4096, '256Mi' -> 256."""
    q = q.strip().strip('"')
    if not q:
        return 0
    for suffix, mult in (("Ki", 1 / 1024), ("Mi", 1), ("Gi", 1024), ("Ti", 1024 * 1024)):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * mult)
    return int(float(q) / (1024 * 1024))


# ── the gateway footprint, read out of the golden render ──────────────────────
def _resource_blocks(text: str) -> list[str]:
    """Every `resources:` block in the document, as raw text."""
    blocks, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)resources:\s*$", line)
        if not m:
            continue
        indent, body = len(m.group(1)), []
        for nxt in lines[i + 1 :]:
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        blocks.append("\n".join(body))
    return blocks


def _scalars(block: str, key: str) -> dict[str, str]:
    """The `cpu:`/`memory:` scalars under `limits:` or `requests:` in one block."""
    m = re.search(rf"^(\s*){key}:\s*$", block, re.M)
    if not m:
        return {}
    indent, out = len(m.group(1)), {}
    for line in block[m.end() :].splitlines():
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        kv = re.match(r'\s*([a-zA-Z.\-/]+):\s*"?([^"\s]+)"?\s*$', line)
        if kv:
            out[kv.group(1)] = kv.group(2)
    return out


def gateway_footprint(golden: str) -> dict[str, int]:
    """Sum the agent gateway's container resources.

    Only blocks carrying `cpu:` are counted — the same document holds PVC
    `resources: requests: storage:` blocks, which are not containers and whose
    storage figures are governed by a different quota key entirely.
    """
    total = {"limits_cpu": 0, "limits_mem": 0, "requests_cpu": 0, "requests_mem": 0}
    containers = 0
    for block in _resource_blocks(golden):
        limits, requests = _scalars(block, "limits"), _scalars(block, "requests")
        if "cpu" not in limits and "cpu" not in requests:
            continue  # a PVC, not a container
        containers += 1
        total["limits_cpu"] += cpu_millis(limits.get("cpu", ""))
        total["limits_mem"] += mem_mib(limits.get("memory", ""))
        total["requests_cpu"] += cpu_millis(requests.get("cpu", ""))
        total["requests_mem"] += mem_mib(requests.get("memory", ""))
    total["containers"] = containers
    return total


# ── the quota defaults, read out of common.sh ─────────────────────────────────
def shell_default(common: str, var: str) -> str | None:
    """Read `VAR="${VAR:-<default>}"` — the repo's one way of declaring a knob."""
    m = re.search(rf'{re.escape(var)}="\$\{{{re.escape(var)}:-([^}}]*)\}}"', common)
    return m.group(1) if m else None


def check_gateway_footprint_readable(fp: dict[str, int]) -> list[str]:
    """A footprint of zero would make every downstream comparison vacuously pass.

    The parser walks indentation in a file this check does not own. If the golden
    render is restructured, the honest outcome is a loud failure here, not four
    green comparisons against nothing.
    """
    if fp.get("containers", 0) < 4:
        return [
            f"gateway footprint: parsed {fp.get('containers', 0)} containers from {GOLDEN.name}, "
            f"expected at least 4 (agent, dashboard, fluent-bit, event-watcher) — the parser has "
            f"lost the file, and every quota comparison below would pass against a zero footprint"
        ]
    if fp["limits_cpu"] <= 0 or fp["limits_mem"] <= 0:
        return [
            f"gateway footprint: parsed {fp['limits_cpu']}m CPU / {fp['limits_mem']}Mi of limits "
            f"— a zero footprint makes every quota look sufficient"
        ]
    return []


def check_tenant_quota_admits_gateway(common: str, fp: dict[str, int]) -> list[str]:
    """provision_12 applies this quota before creating the pod. It must fit."""
    findings = []
    pairs = (
        ("TENANT_QUOTA_LIMITS_CPU", cpu_millis, "limits_cpu", "m CPU"),
        ("TENANT_QUOTA_LIMITS_MEMORY", mem_mib, "limits_mem", "Mi"),
        ("TENANT_QUOTA_REQUESTS_CPU", cpu_millis, "requests_cpu", "m CPU"),
        ("TENANT_QUOTA_REQUESTS_MEMORY", mem_mib, "requests_mem", "Mi"),
    )
    for var, conv, key, unit in pairs:
        raw = shell_default(common, var)
        if raw is None:
            findings.append(f"tenant quota: {var} has no default in common.sh")
            continue
        have, need = conv(raw), fp[key]
        if have < need:
            findings.append(
                f"tenant quota: {var}={raw} allows {have}{unit} but one agent gateway needs "
                f"{need}{unit} — provision_12 applies this quota BEFORE creating that pod, so "
                f"the install fails on the step that creates it"
            )
    return findings


def check_control_quota_admits_control_plane(common: str, fp: dict[str, int]) -> list[str]:
    """baseline + N gateways must fit, with the gateway term taken from the render."""
    findings = []
    gw_raw = shell_default(common, "CONTROL_QUOTA_GATEWAYS")
    if gw_raw is None:
        return ["control quota: CONTROL_QUOTA_GATEWAYS has no default in common.sh"]
    gateways = int(gw_raw)

    if gateways < MIN_GATEWAYS_FOR_ROLLING_UPDATE:
        findings.append(
            f"control quota: CONTROL_QUOTA_GATEWAYS={gateways} leaves no rolling-update headroom. "
            f"platform + cluster-admin are both resident, and a rollout must admit the new pod "
            f"before releasing the old one — so the floor is {MIN_GATEWAYS_FOR_ROLLING_UPDATE}"
        )

    quads = (
        ("CONTROL_QUOTA_LIMITS_CPU", "CONTROL_QUOTA_BASELINE_LIMITS_CPU_MILLIS",
         cpu_millis, "limits_cpu", "m CPU"),
        ("CONTROL_QUOTA_LIMITS_MEMORY", "CONTROL_QUOTA_BASELINE_LIMITS_MEMORY_MIB",
         mem_mib, "limits_mem", "Mi"),
        ("CONTROL_QUOTA_REQUESTS_CPU", "CONTROL_QUOTA_BASELINE_REQUESTS_CPU_MILLIS",
         cpu_millis, "requests_cpu", "m CPU"),
        ("CONTROL_QUOTA_REQUESTS_MEMORY", "CONTROL_QUOTA_BASELINE_REQUESTS_MEMORY_MIB",
         mem_mib, "requests_mem", "Mi"),
    )
    for quota_var, base_var, conv, key, unit in quads:
        quota_raw, base_raw = shell_default(common, quota_var), shell_default(common, base_var)
        if quota_raw is None or base_raw is None:
            findings.append(
                f"control quota: {quota_var if quota_raw is None else base_var} "
                f"has no default in common.sh"
            )
            continue
        have = conv(quota_raw)
        need = int(base_raw) + gateways * fp[key]
        if have < need:
            findings.append(
                f"control quota: {quota_var}={quota_raw} allows {have}{unit}, but the control "
                f"namespace needs {need}{unit} = {base_raw}{unit} baseline + {gateways} gateways "
                f"x {fp[key]}{unit}. A quota under the workload applies cleanly, grandfathers what "
                f"is already running, and then blocks the next rollout"
            )
    return findings


def run(golden: str, common: str) -> list[str]:
    fp = gateway_footprint(golden)
    blocking = check_gateway_footprint_readable(fp)
    if blocking:
        return blocking
    return (
        check_tenant_quota_admits_gateway(common, fp)
        + check_control_quota_admits_control_plane(common, fp)
    )


def self_test() -> int:
    """Each check must fail on the defect it guards."""
    real_golden = GOLDEN.read_text()
    fp = gateway_footprint(real_golden)
    controls = [
        (
            "unparseable golden render rejected",
            lambda: check_gateway_footprint_readable({"containers": 0, "limits_cpu": 0, "limits_mem": 0}),
        ),
        (
            "tenant quota smaller than one gateway rejected",
            lambda: check_tenant_quota_admits_gateway(
                'TENANT_QUOTA_LIMITS_CPU="${TENANT_QUOTA_LIMITS_CPU:-1}"\n'
                'TENANT_QUOTA_LIMITS_MEMORY="${TENANT_QUOTA_LIMITS_MEMORY:-32Gi}"\n'
                'TENANT_QUOTA_REQUESTS_CPU="${TENANT_QUOTA_REQUESTS_CPU:-8}"\n'
                'TENANT_QUOTA_REQUESTS_MEMORY="${TENANT_QUOTA_REQUESTS_MEMORY:-16Gi}"\n',
                fp,
            ),
        ),
        (
            "the exact 2026-07-27 control quota (8 CPU / 16Gi) rejected",
            lambda: check_control_quota_admits_control_plane(
                'CONTROL_QUOTA_GATEWAYS="${CONTROL_QUOTA_GATEWAYS:-3}"\n'
                'CONTROL_QUOTA_LIMITS_CPU="${CONTROL_QUOTA_LIMITS_CPU:-8}"\n'
                'CONTROL_QUOTA_LIMITS_MEMORY="${CONTROL_QUOTA_LIMITS_MEMORY:-16Gi}"\n'
                'CONTROL_QUOTA_REQUESTS_CPU="${CONTROL_QUOTA_REQUESTS_CPU:-4}"\n'
                'CONTROL_QUOTA_REQUESTS_MEMORY="${CONTROL_QUOTA_REQUESTS_MEMORY:-8Gi}"\n'
                'CONTROL_QUOTA_BASELINE_LIMITS_CPU_MILLIS="${CONTROL_QUOTA_BASELINE_LIMITS_CPU_MILLIS:-3000}"\n'
                'CONTROL_QUOTA_BASELINE_LIMITS_MEMORY_MIB="${CONTROL_QUOTA_BASELINE_LIMITS_MEMORY_MIB:-5760}"\n'
                'CONTROL_QUOTA_BASELINE_REQUESTS_CPU_MILLIS="${CONTROL_QUOTA_BASELINE_REQUESTS_CPU_MILLIS:-510}"\n'
                'CONTROL_QUOTA_BASELINE_REQUESTS_MEMORY_MIB="${CONTROL_QUOTA_BASELINE_REQUESTS_MEMORY_MIB:-1600}"\n',
                fp,
            ),
        ),
        (
            "two resident gateways with no rolling headroom rejected",
            lambda: check_control_quota_admits_control_plane(
                'CONTROL_QUOTA_GATEWAYS="${CONTROL_QUOTA_GATEWAYS:-2}"\n'
                'CONTROL_QUOTA_LIMITS_CPU="${CONTROL_QUOTA_LIMITS_CPU:-16}"\n'
                'CONTROL_QUOTA_LIMITS_MEMORY="${CONTROL_QUOTA_LIMITS_MEMORY:-32Gi}"\n'
                'CONTROL_QUOTA_REQUESTS_CPU="${CONTROL_QUOTA_REQUESTS_CPU:-8}"\n'
                'CONTROL_QUOTA_REQUESTS_MEMORY="${CONTROL_QUOTA_REQUESTS_MEMORY:-16Gi}"\n'
                'CONTROL_QUOTA_BASELINE_LIMITS_CPU_MILLIS="${CONTROL_QUOTA_BASELINE_LIMITS_CPU_MILLIS:-3000}"\n'
                'CONTROL_QUOTA_BASELINE_LIMITS_MEMORY_MIB="${CONTROL_QUOTA_BASELINE_LIMITS_MEMORY_MIB:-5760}"\n'
                'CONTROL_QUOTA_BASELINE_REQUESTS_CPU_MILLIS="${CONTROL_QUOTA_BASELINE_REQUESTS_CPU_MILLIS:-510}"\n'
                'CONTROL_QUOTA_BASELINE_REQUESTS_MEMORY_MIB="${CONTROL_QUOTA_BASELINE_REQUESTS_MEMORY_MIB:-1600}"\n',
                fp,
            ),
        ),
        (
            "a grown agent container that outgrows the quota rejected",
            lambda: check_control_quota_admits_control_plane(
                COMMON.read_text(),
                {**fp, "limits_mem": fp["limits_mem"] * 3},
            ),
        ),
        (
            "missing knob rejected",
            lambda: check_control_quota_admits_control_plane("# nothing declared here\n", fp),
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

    for path in (GOLDEN, COMMON):
        if not path.exists():
            print(f"quota-admits-agent: missing {path}", file=sys.stderr)
            return 1

    golden, common = GOLDEN.read_text(), COMMON.read_text()
    fp = gateway_footprint(golden)
    findings = run(golden, common)

    if findings:
        print("quota-admits-agent: FAIL")
        for f in findings:
            print(f"  - {f}")
        return 1

    print(
        f"quota-admits-agent: OK — gateway footprint {fp['limits_cpu']}m/{fp['limits_mem']}Mi "
        f"limits, {fp['requests_cpu']}m/{fp['requests_mem']}Mi requests "
        f"({fp['containers']} containers); tenant and control quotas both admit it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
