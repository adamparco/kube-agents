#!/opt/hermes/.venv/bin/python3
"""networking_audit.py — Procedural collector for the GCP networking fabric
and VPC IPAM audit.

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §10 phase 4, and
governance/gcp_networking_fabric_sop.md.

Ships alongside its SOP as this stream's own collector, in the shape §4.2
calls "a per-stream collector of their own shape" rather than folded into
`fleet-audit/scripts/collect.py`: that driver enumerates GKE clusters and
fetches per-cluster kubeconfigs, neither of which this stream needs — its
targets are GCP compute resources (subnets, routers, forwarding rules,
networks, security policies) read directly with `gcloud compute`, scoped by
project rather than by cluster. This file supersedes the PSC-only helper
that shipped here before (`psc-routing-deadlock` was the only check it ran);
it now covers the stream's full five-check roster and emits the same run
manifest shape `collect.py` does (§6), so `audit_report.py finish
--manifest-file` cross-checks `checks_run` against it exactly as it does for
`collect.py`.

Field contracts assumed of `gcloud ... --format=json` output, spelled out
because none of these five checks has one authoritative field name for "how
exhausted is this":

- `subnet-ip-exhaustion` reads `networks subnets list-usable`. Each item's
  primary range and each entry in `secondaryIpRanges` carries `ipUtilization`
  as a fraction (0-1) of that range's addresses currently allocated.
- `cloud-nat-exhaustion` combines three real GCP surfaces: `routers list` for
  each NAT gateway's `natIpAllocateOption` and (when dynamic port allocation
  is on) `maxPortsPerVm`; `routers get-status` for
  `result.natStatus[].autoAllocatedNatIps` (empty under `AUTO_ONLY` means
  Google could not allocate an external IP at all); `routers
  get-nat-mapping-info` for each VM's `interfaceNatMappings[].numTotalNatPorts`
  against that ceiling.
- `psc-routing-deadlock` reads `forwarding-rules list --filter
  target:ServiceAttachment`. Each item's `target` names the Private Service
  Connect service attachment it points at, and `pscConnectionStatus` carries
  the connection's live state — `REJECTED` or `CLOSED` means traffic aimed at
  it cannot reach the target service.
- `mtu-packet-fragmentation` reads `networks list`, whose `peerings[]` on
  each network names the peer network. A mismatch is an ACTIVE peering
  between two networks with different `mtu` values, not an absolute MTU
  threshold — a single network's MTU is a choice, but two peered networks at
  different MTUs is where packets actually fragment.
- `cloud-armor-false-positive` cross-references `security-policies list`
  against `backend-services list` to find which policies protect a
  production backend (heuristic: the backend's name does not look like a
  test/staging/dev environment), then flags a rule left in `preview` mode or
  two rules sharing one `priority`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1
DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8

SUBNET_SCOPE_NOT_APPLICABLE = (
    ("cloud-nat-exhaustion", "NAT gateways are configured at the Cloud Router level, not per subnet."),
    ("psc-routing-deadlock", "Private Service Connect endpoints are project-level resources, not subnet resources."),
    ("mtu-packet-fragmentation", "VPC network MTU is defined at the VPC level, not per subnet."),
    ("cloud-armor-false-positive", "Cloud Armor security policies are backend service resources, not subnet resources."),
)
PROJECT_SCOPE_NOT_APPLICABLE = (
    ("subnet-ip-exhaustion", "Subnet IP capacity is audited per individual subnet scope entry."),
)

SEVERITY = {
    "subnet-ip-exhaustion": "critical",
    "cloud-nat-exhaustion": "critical",
    "psc-routing-deadlock": "major",
    "mtu-packet-fragmentation": "major",
    "cloud-armor-false-positive": "minor",
}
IMPACT = {
    "subnet-ip-exhaustion": (
        "New pods or nodes cannot be scheduled once this range's addresses run out, and GKE has "
        "no way to expand a live cluster's Pod CIDR after creation."
    ),
    "cloud-nat-exhaustion": (
        "VMs that exhaust their NAT port allocation see new outbound connections silently fail, "
        "which for a GKE node means pods lose egress with no error at the workload layer."
    ),
    "psc-routing-deadlock": (
        "Traffic aimed at this Private Service Connect endpoint cannot reach its target service; "
        "consumers see connection failures with no signal at the VPC layer."
    ),
    "mtu-packet-fragmentation": (
        "Packets crossing this peering at the larger MTU get fragmented or dropped, which shows up "
        "as intermittent, hard-to-diagnose latency and retransmits rather than a clean failure."
    ),
    "cloud-armor-false-positive": (
        "A preview-mode rule on a production backend logs matches without enforcing them, so the "
        "WAF looks like it is protecting traffic it is only observing; conflicting priorities make "
        "the effective policy unpredictable."
    ),
}


def log(msg: str) -> None:
    print(f"[networking_audit] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    """One subprocess's outcome, in the shape the manifest records it —
    the same shape `collect.py`'s `Run` uses, kept as a separate definition
    because this script ships standalone (see the module docstring)."""

    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def default_run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, 124, exc.stdout or "", exc.stderr or "", time.monotonic() - t0)
    except Exception as exc:  # gcloud missing, permission denied on exec, etc.
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def run_and_gate(argv: list[str], *, run: RunFn = default_run) -> tuple[object | None, Run]:
    """One `gcloud` call behind a fail-closed gate — non-zero exit, empty
    output, or non-JSON output all gate closed, mirroring `collect.py`'s
    `run_and_gate` (§4.1: a truncated result must never read as "nothing
    here")."""
    result = run(argv)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result


class GateFailure(Exception):
    """Raised when one of a project-level target's several independent
    `gcloud` reads fails its gate. Fails that whole target closed, the same
    trade-off `collect.py`'s compliance-audit collector accepts for its own
    several independent reads — one `outcome` per manifest entry, not one
    per check."""


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(argv_str: str, result: Run) -> dict:
    return {
        "command": argv_str,
        "rc": result.rc,
        "duration_s": round(result.duration_s, 2),
        "output_sha256": output_digest(result.stdout),
    }


def _last_segment(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1]


def _region_of_subnet_link(self_link: str) -> str:
    """`.../regions/<region>/subnetworks/<name>` -> `<region>`."""
    head = (self_link or "").split("/subnetworks/", 1)[0]
    return _last_segment(head)


def get_target_projects(cli_project: str | None = None) -> list[str]:
    """Resolves all target GCP projects to audit."""
    if cli_project:
        return [cli_project]

    projects = set()
    monitored = os.environ.get("MONITORED_PROJECT_IDS", "")
    if monitored:
        for p in monitored.split(","):
            p = p.strip()
            if p:
                projects.add(p)

    for env_var in ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID"):
        val = os.environ.get(env_var, "").strip()
        if val:
            projects.add(val)

    if not projects:
        result = default_run(["gcloud", "config", "get-value", "project"])
        if result.rc == 0 and result.stdout.strip():
            projects.add(result.stdout.strip())

    return sorted(projects)


# --------------------------------------------------------------------------- #
# Check bodies: pure functions over already-parsed `gcloud` JSON. Each
# returns either one hit (`workload`-shaped checks: one call per item) or a
# list of hits (`cluster`-shaped checks: one call over the whole collection),
# matching `collect.py`'s two `CheckSpec` kinds without importing that module.
# --------------------------------------------------------------------------- #


def check_subnet_ip_exhaustion(subnet: dict) -> dict | None:
    """`subnet` is one item from `networks subnets list-usable`. Flags when
    the primary range or any secondary range has < 15% headroom left."""
    bad = []
    util = subnet.get("ipUtilization")
    if isinstance(util, (int, float)) and util > 0.85:
        bad.append(f"primary range {subnet.get('ipCidrRange')}: {util * 100:.1f}% utilized")
    for sec in subnet.get("secondaryIpRanges") or []:
        sec_util = sec.get("ipUtilization")
        if isinstance(sec_util, (int, float)) and sec_util > 0.85:
            bad.append(
                f"secondary range {sec.get('rangeName')} ({sec.get('ipCidrRange')}): "
                f"{sec_util * 100:.1f}% utilized"
            )
    if not bad:
        return None
    return {"object": f"Subnet/{_last_segment(subnet.get('subnetwork', ''))}", "excerpt": "; ".join(bad)}


def _nat_status_entry(status: dict | None, nat_name: str) -> dict | None:
    for entry in ((status or {}).get("result") or {}).get("natStatus") or []:
        if entry.get("name") == nat_name:
            return entry
    return None


def check_router_nat(router: dict, status: dict | None, mapping: list | None) -> dict | None:
    """`router` is one item from `routers list`; `status` is
    `get-status`'s response for it; `mapping` is
    `get-nat-mapping-info`'s response for it. One finding per router,
    aggregating every NAT gateway on it that is either lacking an
    auto-allocated external IP or has a VM near its port ceiling."""
    router_name = router.get("name", "")
    problems = []
    for nat in router.get("nats") or []:
        nat_name = nat.get("name", "")
        if nat.get("natIpAllocateOption") == "AUTO_ONLY":
            entry = _nat_status_entry(status, nat_name)
            if entry is not None and not entry.get("autoAllocatedNatIps"):
                problems.append(f"{nat_name}: AUTO_ONLY with no auto-allocated external IP")
                continue
        ceiling = nat.get("maxPortsPerVm") if nat.get("enableDynamicPortAllocation") else nat.get("minPortsPerVm")
        if not ceiling or mapping is None:
            continue
        for vm in mapping:
            for iface in vm.get("interfaceNatMappings") or []:
                total = iface.get("numTotalNatPorts")
                if isinstance(total, (int, float)) and total / ceiling >= 0.8:
                    problems.append(
                        f"{nat_name}: {vm.get('instanceName', '?')} using {total}/{ceiling} "
                        f"ports ({total / ceiling * 100:.0f}%)"
                    )
    if not problems:
        return None
    return {"object": f"Router/{router_name}", "excerpt": "; ".join(problems)}


def check_psc_routing(forwarding_rules: list[dict]) -> list[dict]:
    """`forwarding_rules` is `forwarding-rules list --filter
    target:ServiceAttachment`'s response. One finding per rule whose PSC
    connection has been rejected or closed at the target's end."""
    hits = []
    for fr in forwarding_rules or []:
        name = fr.get("name", "")
        target = fr.get("target", "")
        status = fr.get("pscConnectionStatus", "")
        if target and "serviceAttachments" in target and status in ("REJECTED", "CLOSED"):
            hits.append({"object": f"ForwardingRule/{name}", "excerpt": f"pscConnectionStatus: {status}"})
    return hits


def check_mtu_mismatch(networks: list[dict]) -> list[dict]:
    """One finding per unordered pair of ACTIVE-peered networks whose `mtu`
    values differ, named by both networks sorted so the pair reads the same
    regardless of which side's listing surfaced the peering."""
    by_name = {n.get("name"): n for n in networks or []}
    seen_pairs = set()
    hits = []
    for net in networks or []:
        name, mtu = net.get("name"), net.get("mtu")
        for peering in net.get("peerings") or []:
            if peering.get("state") != "ACTIVE":
                continue
            peer_name = _last_segment(peering.get("network", ""))
            peer = by_name.get(peer_name)
            if peer is None or mtu is None or peer.get("mtu") is None or mtu == peer.get("mtu"):
                continue
            pair = tuple(sorted((name, peer_name)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            hits.append(
                {
                    "object": f"NetworkPeering/{pair[0]}--{pair[1]}",
                    "excerpt": f"{pair[0]} mtu={by_name[pair[0]].get('mtu')} peered with "
                    f"{pair[1]} mtu={by_name[pair[1]].get('mtu')}",
                }
            )
    return hits


def _looks_non_production(name: str) -> bool:
    lname = (name or "").lower()
    return any(token in lname for token in ("test", "staging", "stage", "dev", "sandbox", "qa"))


def check_cloud_armor(policies: list[dict], backend_services: list[dict]) -> list[dict]:
    """Flags a policy attached to a production-looking backend that still
    carries a `preview` rule (excluding GCP's implicit default rule at
    priority 2147483647), or that has two rules sharing one `priority`."""
    attached_by_policy: dict[str, list[str]] = {}
    for svc in backend_services or []:
        policy_ref = svc.get("securityPolicy") or ""
        if not policy_ref:
            continue
        attached_by_policy.setdefault(_last_segment(policy_ref), []).append(svc.get("name", ""))

    hits = []
    for policy in policies or []:
        name = policy.get("name", "")
        rules = policy.get("rules") or []
        production = [svc for svc in attached_by_policy.get(name, []) if not _looks_non_production(svc)]
        problems = []
        if production:
            preview_priorities = [r.get("priority") for r in rules if r.get("preview") and r.get("priority") != 2147483647]
            if preview_priorities:
                problems.append(
                    f"attached to production backend(s) {', '.join(production)} with rule(s) in "
                    f"preview: {', '.join(str(p) for p in preview_priorities)}"
                )
        priorities = [r.get("priority") for r in rules if r.get("priority") is not None]
        dupes = sorted({p for p in priorities if priorities.count(p) > 1})
        if dupes:
            problems.append(f"conflicting rule priorities: {dupes}")
        if not problems:
            continue
        hits.append({"object": f"SecurityPolicy/{name}", "excerpt": "; ".join(problems)})
    return hits


def _emit(slug: str, hit: dict) -> dict:
    return {
        "check": slug,
        "namespace": "",
        "object": hit["object"],
        "severity": hit.get("severity") or SEVERITY[slug],
        "excerpt": hit["excerpt"],
        "impact": IMPACT[slug],
        "needs_triage": None,
    }


def _collect_subnet_targets(project: str, *, run: RunFn) -> list[dict]:
    argv = ["gcloud", "compute", "networks", "subnets", "list-usable", "--project", project, "--format", "json"]
    parsed, result = run_and_gate(argv, run=run)
    if parsed is None:
        log(f"{project}: subnets list-usable gate failed (rc={result.rc}); no subnet-scoped targets this run")
        # A silent `[]` here would read as "this project has no subnets" --
        # a clean, fully-covered scope -- rather than "this scope could not
        # be read this run". §6's manifest contract requires a surfaced
        # coverage gap instead, the same as any other failed gate.
        return [
            {
                "name": f"project/{project}/subnets",
                "project": project,
                "location": "global",
                "outcome": "gate-failed",
                "error": f"subnet-ip-exhaustion: {' '.join(argv)} failed (rc={result.rc}): {result.stderr.strip()[:300]}",
            }
        ]
    record = _record(" ".join(argv), result)
    out = []
    for item in parsed:
        name = _last_segment(item.get("subnetwork", ""))
        region = _region_of_subnet_link(item.get("subnetwork", ""))
        hit = check_subnet_ip_exhaustion(item)
        out.append(
            {
                "name": f"{project}/{region}/{name}",
                "project": project,
                "location": region,
                "outcome": "collected",
                "commands": [{"check": "subnet-ip-exhaustion", **record}],
                "candidates": [_emit("subnet-ip-exhaustion", hit)] if hit else [],
                "checks_not_applicable": [{"check": slug, "reason": reason} for slug, reason in SUBNET_SCOPE_NOT_APPLICABLE],
            }
        )
    return out


def _collect_project_target(project: str, *, run: RunFn) -> dict:
    name = f"project/{project}"
    commands: dict[str, dict] = {}
    candidates: list[dict] = []

    def gated(argv: list[str], slug: str):
        parsed, result = run_and_gate(argv, run=run)
        if parsed is None:
            raise GateFailure(f"{slug}: {' '.join(argv)} failed (rc={result.rc}): {result.stderr.strip()[:300]}")
        commands[slug] = _record(" ".join(argv), result)
        return parsed

    try:
        routers = gated(
            ["gcloud", "compute", "routers", "list", "--project", project, "--format", "json"],
            "cloud-nat-exhaustion",
        )
        for router in routers:
            if not router.get("nats"):
                continue
            router_name = router.get("name", "")
            region = _last_segment(router.get("region", ""))
            status = gated(
                ["gcloud", "compute", "routers", "get-status", router_name, "--region", region, "--project", project, "--format", "json"],
                "cloud-nat-exhaustion",
            )
            mapping = gated(
                [
                    "gcloud", "compute", "routers", "get-nat-mapping-info", router_name,
                    "--region", region, "--project", project, "--format", "json",
                ],
                "cloud-nat-exhaustion",
            )
            hit = check_router_nat(router, status, mapping)
            if hit:
                candidates.append(_emit("cloud-nat-exhaustion", hit))

        forwarding_rules = gated(
            [
                "gcloud", "compute", "forwarding-rules", "list", "--filter", "target:ServiceAttachment",
                "--project", project, "--format", "json",
            ],
            "psc-routing-deadlock",
        )
        candidates += [_emit("psc-routing-deadlock", hit) for hit in check_psc_routing(forwarding_rules)]

        networks = gated(
            ["gcloud", "compute", "networks", "list", "--project", project, "--format", "json"],
            "mtu-packet-fragmentation",
        )
        candidates += [_emit("mtu-packet-fragmentation", hit) for hit in check_mtu_mismatch(networks)]

        policies = gated(
            ["gcloud", "compute", "security-policies", "list", "--project", project, "--format", "json"],
            "cloud-armor-false-positive",
        )
        backends = gated(
            ["gcloud", "compute", "backend-services", "list", "--project", project, "--format", "json"],
            "cloud-armor-false-positive",
        )
        candidates += [_emit("cloud-armor-false-positive", hit) for hit in check_cloud_armor(policies, backends)]
    except GateFailure as exc:
        return {"name": name, "project": project, "location": "global", "outcome": "gate-failed", "error": str(exc)}

    return {
        "name": name,
        "project": project,
        "location": "global",
        "outcome": "collected",
        "commands": [{"check": slug, **record} for slug, record in commands.items()],
        "candidates": candidates,
        "checks_not_applicable": [{"check": slug, "reason": reason} for slug, reason in PROJECT_SCOPE_NOT_APPLICABLE],
    }


def collect_project(project: str, *, run: RunFn = default_run) -> list[dict]:
    """Every manifest entry for one project: one per subnet plus one
    project-scoped entry for the other four checks (§6's `clusters[]` shape,
    reused for a target that is a subnet or a project rather than a GKE
    cluster)."""
    return _collect_subnet_targets(project, run=run) + [_collect_project_target(project, run=run)]


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, max_workers: int = MAX_WORKERS) -> dict:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    projects = get_target_projects(project)

    results: list[list[dict]] = [[] for _ in projects]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_project, p, run=run): i for i, p in enumerate(projects)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    return {
        "version": MANIFEST_VERSION,
        "audit": "gcp-networking-fabric-audit",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": [entry for group in results for entry in group],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="single project to audit; omit to sweep MONITORED_PROJECT_IDS/GCP_PROJECT_ID")
    args = parser.parse_args(argv)
    manifest = collect_fleet(args.project)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
