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

- `subnet-ip-exhaustion` enumerates with `networks subnets list-usable` and
  measures with Network Analyzer. `list-usable` does **not** carry
  `ipUtilization` — the field is absent from gcloud's `UsableSubnetwork` in
  v1, beta and alpha alike, so the enumeration alone reaches no verdict. The
  ratio comes from the `google.networkanalyzer.vpcnetwork.ipAddressInsight`
  insight, whose `subnetRangeStats[].allocationRatio` `_backfill_utilization`
  writes onto each range's `ipUtilization` so the threshold in
  `check_subnet_ip_exhaustion` reads one field whatever supplied it. The
  insight names a range only when something is allocated in it, so two
  silences are readings of zero and get zero-filled: a subnet it omits
  entirely (`_zero_fill_unallocated`) and a secondary range it never names
  inside a subnet it does cover (`_zero_fill_skipped_ranges`). Every other
  gap is unmeasured, never healthy — a primary the insight did not publish, a
  range it named but whose `allocationRatio` will not parse, and a Shared VPC
  host project's subnets, which `list-usable` reaches across and the insight
  cannot see. Those `_collect_subnet_targets` turns into a not-applicable
  declaration plus a `limitations` string rather than a pass.
- `cloud-nat-exhaustion` combines three real GCP surfaces: `routers list` for
  each NAT gateway's `natIpAllocateOption` and `maxPortsPerVm`; `routers
  get-status` for `result.natStatus[].autoAllocatedNatIps` (empty under
  `AUTO_ONLY` means Google could not allocate an external IP at all); `routers
  get-nat-mapping-info --nat-name` for each VM's
  `interfaceNatMappings[].numTotalNatPorts` against that ceiling. The port half
  runs only where dynamic port allocation is on — a static gateway hands every
  VM exactly `minPortsPerVm`, so its ratio is the constant 1.0 and measuring it
  reports exhaustion for every VM behind every stock gateway. `--nat-name`
  scopes the read for the same reason: unfiltered, it returns every VM on the
  router, and comparing that against each gateway's own ceiling in turn
  attributes one gateway's VMs to another's limit.
- `psc-routing-deadlock` reads `forwarding-rules list`, unfiltered, and keeps
  the Private Service Connect ones in Python. Each item's `target` names the
  service attachment it points at, and `pscConnectionStatus` carries the
  connection's live state — `REJECTED` or `CLOSED` means traffic aimed at it
  cannot reach the target service.
- `mtu-packet-fragmentation` reads `networks list`, whose `peerings[]` on
  each network names the peer network. A mismatch is an ACTIVE peering
  between two networks with different MTUs, not an absolute MTU threshold —
  a single network's MTU is a choice, but two peered networks at different
  MTUs is where packets actually fragment. A network with no `mtu` key is on
  GCP's default 1460, which is a value to compare and not a gap.
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
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1

# A digest of this file, published in the manifest. `audit_report.py` compares
# it against the previous run's to tell a finding that stopped reproducing from
# a check that stopped looking; see `render_delta_comment`.
# Long enough that two collector sources will not collide, short enough to
# read in a log line. It has to agree across every collector: the comparison
# is between one run's revision and the last one's, so a file that truncated
# differently would report a moved collector on the run that changed it.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[
    :REVISION_DIGEST_CHARS
]

DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8
# `audit_report.validate_check_command`'s ceiling, restated rather than
# imported because this script ships standalone (see the module docstring).
# An over-length `command` is not a clipped field: `finish` refuses the whole
# document, so a project with enough Cloud Routers to overflow the joined NAT
# provenance below would publish nothing at all.
MAX_COMMAND_CHARS = 2000

# `AuditSpec.scopes` already partitions this stream's roster by target kind: a
# `<project>/<region>/<subnet>` target owes `subnet-ip-exhaustion` and nothing
# else, a `project/<id>` target owes the other four. `audit_target_checks` is
# what the coverage denominator reads, so declaring a check inapplicable to a
# target kind that was never asked for it subtracts nothing from anything --
# it is a row in the ledger's "Not applicable" table and no more. This module
# used to declare all four cross-kind slugs on every subnet, which on a fleet
# whose auto-mode network has one `default` subnet per region is 4 x 42 = 168
# identical rows on every run, plus one more on the project target.

# Opens the `reason` of a check that *applies* to its target and was attempted
# without reaching a verdict, as against one the target's shape rules out.
#
# The distinction has to be carried because `checks_not_applicable` is doing
# two jobs at once here. The disposition is what stops the model claiming, in
# §6, that a check ran against a target where nothing could have run it --
# `cross_check_manifest` keys that guard on the slug alone, so it is unaffected
# by this marker. But the same key is what leaves the coverage denominator, and
# a check that was owed and went unanswered must stay in it. Conflating the two
# inverted the accounting: a subnet Network Analyzer measured on 14 of its 16
# ranges reported a coverage gap, while one it measured on 0 of 16 reported
# none, so the less a target was measured the cleaner it read.
#
# `audit_report.checks_unevaluated` is the reader.
UNEVALUATED_MARKER = "UNEVALUATED: "
# An unmeasured subnet owes exactly one check and could not run it, so §6's
# `checks_run` comes out empty for it -- and `finish` rejects an empty
# `checks_run` unless the target says in `limitations` why nothing ran. The
# collector writes that sentence rather than leaving it to the model, because
# the model does not converge on one: every stored run of this stream over the
# same 41 subnets wrote a different sentence -- 11 runs, 11 wordings, measured
# on 2026-08-30. Two of them are the shapes that defeated audit_report's
# `_limitation_restates_na` and forced the structural rule above it.
UNMEASURED_SUBNET_LIMITATION = (
    "subnet-ip-exhaustion, the only check this target owes, could not be "
    "evaluated: neither `list-usable` nor Network Analyzer published an "
    "IP-utilization figure for this subnet. It was enumerated, not measured."
)
# `_carries_utilization` asks `any`, so one range with a figure marks the whole
# subnet measured -- and `check_subnet_ip_exhaustion` then passes over every
# sibling that has none, returning nothing for it, which is indistinguishable
# from clearing it. That is the same silent-clean failure the gate in
# `_collect_subnet_targets` refuses one layer up, surviving inside a subnet
# because the gate is satisfied by a single reading.
#
# One shape of that is not a failure to look: a secondary range Network
# Analyzer never names inside a subnet it covers is an empty range, and
# `_zero_fill_skipped_ranges` reads it as such. Everything else still reaches
# this limitation -- a primary the insight did not publish, which no alias-IP
# evidence speaks to; a range it named but whose `allocationRatio` will not
# parse, which is a read that failed rather than a zero; a subnet `list-usable`
# measures on some ranges and not others, which no released gcloud does today
# but which `_backfill_utilization` already defers to; and a partially measured
# subnet in a Shared VPC host project, which the insight cannot reach at all.
#
# `cross_check_manifest` states where this belongs: a check that applies but
# could not be evaluated is the target's `limitations`, which §6 turns into a
# coverage gap. The subnet stays measured and keeps its verdict for the ranges
# that had figures; the gap says which ranges the verdict does not cover.
PARTIAL_SUBNET_LIMITATION = (
    "subnet-ip-exhaustion reached {measured} of {total} ranges on this subnet. "
    "Neither `list-usable` nor Network Analyzer published an IP-utilization "
    "figure for the rest, which the check passed over rather than cleared: "
    "{names}."
)

# Cloud NAT's documented per-VM dynamic port ceiling, applied when `routers
# list` omits the field — which it does for any NAT that never overrode it.
# There is deliberately no static counterpart: `check_router_nat` does not
# measure a static gateway, whose allocation equals its own reservation.
DEFAULT_MAX_PORTS_PER_VM = 65536

# A VPC's MTU when nobody set one, applied for the same reason as the NAT
# defaults above: `networks list` omits `mtu` for every network still on it.
# Skipping those pairs made this check unable to fire on the only mismatch
# that occurs in practice, a default network peered with one raised to 8896 --
# both sides would have to have been overridden, and to different values, for
# the old reading to see anything at all.
DEFAULT_NETWORK_MTU = 1460

# Where a network reference starts carrying meaning. A `selfLink` is always a
# full `https://.../compute/v1/projects/<p>/global/networks/<n>` URL, and
# everything before this marker is API-version boilerplate two equal networks
# can still disagree about — so the key is the URL truncated here.
#
# A `peerings[].network` is *not* always full: the API documents a partial
# reference as meaning the current network's own project. Those carry no
# `projects/` segment at all, so `_network_key` uses the same marker the other
# way round, as the prefix it synthesises the missing qualification onto.
NETWORK_URL_PROJECT_MARKER = "projects/"

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


def _joined_record(reads: list[tuple[str, Run]]) -> dict:
    """One `commands` entry covering every read that backed one check.

    Three of the five checks take more than one read — `cloud-nat-exhaustion`
    a `routers list` plus a `get-status` and a `get-nat-mapping-info` per
    router, `cloud-armor-false-positive` a `security-policies list` plus a
    `backend-services list` — and a `dict[slug] = record` assignment per read
    published only the last. Cloud Armor's entry therefore named
    `backend-services list`, which carries no rule and so reproduces no
    verdict, and the NAT entry named whichever router happened to be read
    last, which reads as one router inspected on a target where every router
    was.

    Joined with ` && ` so the field stays a line a reader can paste, the same
    form the subnet path already publishes. `rc` is 0 because every read here
    passed its gate — a failure raises `GateFailure` before reaching this.
    """
    parts = [command for command, _ in reads]
    joined = " && ".join(parts)
    if len(joined) > MAX_COMMAND_CHARS:
        # Clipped at a join boundary, and the tail is counted rather than
        # dropped: a `commands` entry silently listing three of a project's
        # seventeen NAT reads claims narrower coverage than the run had.
        kept = [parts[0]]
        for part in parts[1:]:
            if len(" && ".join(kept + [part])) > MAX_COMMAND_CHARS - 64:
                break
            kept.append(part)
        joined = " && ".join(kept) + f"  # and {len(parts) - len(kept)} more read(s) of the same shape"
    return {
        "command": joined[:MAX_COMMAND_CHARS],
        "rc": 0,
        "duration_s": round(sum(result.duration_s for _, result in reads), 2),
        "output_sha256": output_digest("".join(result.stdout for _, result in reads)),
    }


def _last_segment(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1]


def _region_of_subnet_link(self_link: str) -> str:
    """`.../regions/<region>/subnetworks/<name>` -> `<region>`."""
    head = (self_link or "").split("/subnetworks/", 1)[0]
    return _last_segment(head)


def _project_of_subnet_link(self_link: str) -> str:
    """`.../projects/<project>/regions/...` -> `<project>`, or `""`.

    `list-usable` reaches across a Shared VPC, so a subnet it returns is not
    necessarily in the project being audited; the Network Analyzer insight is
    scoped to one project and never mentions the host project's subnets.
    `_zero_fill_unallocated` needs to tell those two absences apart.
    """
    head, sep, _ = (self_link or "").partition("/regions/")
    if not sep:
        return ""
    return _last_segment(head) if "/projects/" in head else ""


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


def check_router_nat(router: dict, status: dict | None, mappings: dict | None) -> dict | None:
    """`router` is one item from `routers list`; `status` is
    `get-status`'s response for it; `mappings` maps a NAT gateway's name to
    `get-nat-mapping-info --nat-name`'s response for that gateway. One finding
    per router, aggregating every NAT gateway on it that is either lacking an
    auto-allocated external IP or has a VM near its port ceiling.

    Keyed by gateway, not one list per router, because `get-nat-mapping-info`
    without `--nat-name` returns every VM behind *any* gateway on the router.
    Read once per router and compared against each gateway's ceiling in turn,
    that cross-attributes: a VM drawing 4096 ports from a dynamic gateway is
    measured a second time against a static gateway's 64 and reported at
    6400%. The mapping has to be fetched per gateway to mean anything.

    The object names the region as well as the router: a router name is
    unique inside a region, not inside a project, so `Router/<name>` collides
    for two NAT routers of the same name in two regions of one project. Both
    would land in the project-scoped target under the same
    `(check, cluster, namespace, object)` identity, and `finish` refuses a
    document holding two findings that agree on all four rather than
    collapsing them.
    """
    router_name = router.get("name", "")
    region = _last_segment(router.get("region", ""))
    problems = []
    for nat in router.get("nats") or []:
        nat_name = nat.get("name", "")
        if nat.get("natIpAllocateOption") == "AUTO_ONLY":
            entry = _nat_status_entry(status, nat_name)
            if entry is not None and not entry.get("autoAllocatedNatIps"):
                problems.append(f"{nat_name}: AUTO_ONLY with no auto-allocated external IP")
                continue
        # The port ratio only means something under dynamic port allocation.
        # With DPA off, Cloud NAT reserves each VM exactly `minPortsPerVm`
        # ports, so `numTotalNatPorts` *is* the ceiling and `total / ceiling`
        # is the constant 1.0 -- every VM behind every stock gateway clears
        # the 80% bar, and the check reported `critical` port exhaustion for
        # each of them daily on a fleet with no exhaustion anywhere. Under DPA
        # the allocation floats between min and max, which is the only
        # configuration where approaching the ceiling is a fact about load.
        # Falling short of ports under static allocation is real, but it shows
        # up as a VM with no mapping at all rather than as a ratio, and
        # nothing read here can see it.
        if not nat.get("enableDynamicPortAllocation"):
            continue
        # Fall back to GCP's documented default rather than skipping. `routers
        # list` omits `maxPortsPerVm` whenever the NAT was left on it, so
        # `ceiling = None` silently dropped §2.2's port half.
        ceiling = nat.get("maxPortsPerVm") or DEFAULT_MAX_PORTS_PER_VM
        for vm in (mappings or {}).get(nat_name) or []:
            for iface in vm.get("interfaceNatMappings") or []:
                total = iface.get("numTotalNatPorts")
                if isinstance(total, (int, float)) and total / ceiling >= 0.8:
                    problems.append(
                        f"{nat_name}: {vm.get('instanceName', '?')} using {total}/{ceiling} "
                        f"ports ({total / ceiling * 100:.0f}%)"
                    )
    if not problems:
        return None
    return {"object": f"Router/{region or 'global'}/{router_name}", "excerpt": "; ".join(problems)}


def check_psc_routing(forwarding_rules: list[dict]) -> list[dict]:
    """`forwarding_rules` is every forwarding rule in the project. One finding
    per rule whose PSC connection has been rejected or closed at the target's
    end; the rest are dropped here rather than by a `--filter` on the list call,
    so a change in gcloud's filter semantics cannot turn the check silent.

    Scoped by region for the same reason `check_router_nat` is: a forwarding
    rule's name is unique per region (or once globally), so the bare name
    collides across regions inside one project and `finish` refuses the whole
    document rather than merging the two findings.
    """
    hits = []
    for fr in forwarding_rules or []:
        name = fr.get("name", "")
        scope = _last_segment(fr.get("region", "")) or "global"
        target = fr.get("target", "")
        status = fr.get("pscConnectionStatus", "")
        if target and "serviceAttachments" in target and status in ("REJECTED", "CLOSED"):
            hits.append({"object": f"ForwardingRule/{scope}/{name}", "excerpt": f"pscConnectionStatus: {status}"})
    return hits


def _network_key(url_or_name: str, project: str) -> str:
    """A network reference reduced to `projects/<project>/global/networks/<name>`.

    The project has to stay in the key. `networks list` returns one project's
    networks, but a `peerings[].network` URL can point at another project's
    VPC, and `default` is the most common network name in GCP — so matching a
    peer on its last segment resolves a cross-project peering to the
    same-named *local* network and compares the wrong two MTUs. That was
    harmless while an absent `mtu` meant "skip this pair"; once it reads as
    1460 the mis-resolved pair becomes a `major` finding naming a peering that
    does not exist.

    A reference carrying no `projects/` segment is *relative*, which in this
    API means the project being audited — a `peerings[].network` given as a
    partial URL, or a listing entry that arrived without a `selfLink`. Both are
    qualified with `project` rather than left as bare names, because a bare
    name matching anything is how the cross-project collision above gets back
    in, and a bare name matching nothing turns a real mismatch into a silent
    clean read.

    A peer in another project still misses, deliberately: its MTU is genuinely
    unread rather than defaulted. One shape misses that arguably should not — a
    peering URL naming the project by number where `selfLink` names it by ID —
    and that costs a missed finding, never a false one.
    """
    text = (url_or_name or "").rstrip("/")
    index = text.find(NETWORK_URL_PROJECT_MARKER)
    if index != -1:
        return text[index:].lower()
    return (
        f"{NETWORK_URL_PROJECT_MARKER}{project}/global/networks/"
        f"{_last_segment(text)}"
    ).lower()


def _network_mtu(network: dict | None) -> int | None:
    """A network's MTU, reading an absent key as GCP's default rather than unknown.

    Only a network the caller could not find at all is unknown; a network that
    is present and silent about `mtu` is on 1460, and that is a number this
    check can compare.
    """
    if network is None:
        return None
    mtu = network.get("mtu")
    return DEFAULT_NETWORK_MTU if mtu is None else mtu


def check_mtu_mismatch(networks: list[dict], project: str) -> list[dict]:
    """One finding per unordered pair of ACTIVE-peered networks whose `mtu`
    values differ, named by both networks sorted so the pair reads the same
    regardless of which side's listing surfaced the peering.

    `networks` is one project's listing, and `project` is whose — needed to
    qualify a reference that arrived relative, on either side of the join.
    """
    by_key = {
        _network_key(n.get("selfLink") or n.get("name", ""), project): n
        for n in networks or []
    }
    seen_pairs = set()
    hits = []
    for net in networks or []:
        name, mtu = net.get("name"), _network_mtu(net)
        for peering in net.get("peerings") or []:
            if peering.get("state") != "ACTIVE":
                continue
            peer = by_key.get(_network_key(peering.get("network", ""), project))
            peer_mtu = _network_mtu(peer)
            # A peer outside this listing -- another project's VPC -- is the one
            # shape still skipped: its MTU is genuinely unread, not defaulted.
            # `_network_key` is what keeps that true now that an absent `mtu`
            # has a value: keyed on the bare name, another project's `default`
            # would resolve to this project's and be compared against it.
            if peer_mtu is None or mtu == peer_mtu:
                continue
            peer_name = peer.get("name", "")
            pair = tuple(sorted((name, peer_name)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            mtus = {name: mtu, peer_name: peer_mtu}
            hits.append(
                {
                    "object": f"NetworkPeering/{pair[0]}--{pair[1]}",
                    "excerpt": f"{pair[0]} mtu={mtus[pair[0]]} peered with "
                    f"{pair[1]} mtu={mtus[pair[1]]}",
                }
            )
    return hits


def _looks_non_production(name: str) -> bool:
    lname = (name or "").lower()
    return any(token in lname for token in ("test", "staging", "stage", "dev", "sandbox", "qa"))


def check_cloud_armor(policies: list[dict], backend_services: list[dict]) -> list[dict]:
    """Flags a policy attached to a production-looking backend that still
    carries a `preview` rule (excluding GCP's implicit default rule at
    priority 2147483647), or that has two rules sharing one `priority`.

    The production gate governs both limbs. §2.5's Do-NOT-flag rule is written
    about the check, not about its first condition, and the impact this stream
    publishes is about production traffic — but the gate sat on the preview
    branch alone, so a duplicate priority was reported on every policy in the
    project, including ones attached to a `dev` backend and ones attached to
    no backend at all, where the effective policy governs nothing.
    """
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
        if not production:
            continue
        problems = []
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


def _carries_utilization(subnet: dict) -> bool:
    """Does this subnet expose the one field `check_subnet_ip_exhaustion`
    reads, on its primary range or any secondary one?

    Presence, not truthiness: a subnet sitting at `ipUtilization: 0.0` is
    measured and empty, which is the opposite of unmeasured.
    """
    if isinstance(subnet.get("ipUtilization"), (int, float)):
        return True
    return any(
        isinstance(sec.get("ipUtilization"), (int, float))
        for sec in subnet.get("secondaryIpRanges") or []
    )


def _unmeasured_ranges(subnet: dict) -> list[str]:
    """The ranges on this subnet that `_carries_utilization` did not account
    for, named the way a reader can go and look them up.

    Presence again, not truthiness, for the same reason: a range at 0.0 was
    measured. The primary is described rather than named because it has no
    `rangeName` to give.
    """
    missing = []
    if not isinstance(subnet.get("ipUtilization"), (int, float)):
        missing.append("the primary range")
    for sec in subnet.get("secondaryIpRanges") or []:
        if not isinstance(sec.get("ipUtilization"), (int, float)):
            missing.append(str(sec.get("rangeName") or "(unnamed secondary range)"))
    return missing


# Network Analyzer's IP-utilization insight, which is where the measurement
# `check_subnet_ip_exhaustion` needs actually lives. Not
# `google.compute.subnetwork.IpUtilizationInsight` -- that name appears in
# plenty of prose but is not a real insight type, and the API rejects it with
# INVALID_ARGUMENT.
_IP_INSIGHT_TYPE = "google.networkanalyzer.vpcnetwork.ipAddressInsight"


def _utilization_key(link: str) -> str:
    """`region/name` for a subnet, from either surface's URI form.

    `list-usable` gives `https://www.googleapis.com/compute/v1/projects/P/
    regions/R/subnetworks/N` and the insight gives
    `//compute.googleapis.com/projects/P/regions/R/subnetworks/N`; the tails
    agree. Region is part of the key because auto-mode networks name a subnet
    `default` in every region, so the bare name collides 42 ways.
    """
    return f"{_region_of_subnet_link(link)}/{_last_segment(link)}"


def _ip_insight_argv(project: str) -> list[str]:
    """The Network Analyzer read, built in one place.

    `_collect_subnet_targets` publishes this command alongside the enumeration
    whenever the backfill supplied the reading, so the two must not drift.
    """
    return [
        "gcloud", "recommender", "insights", "list",
        "--project", project, "--location", "global",
        "--insight-type", _IP_INSIGHT_TYPE, "--format", "json",
    ]


def _utilization_by_subnet(project: str, *, run: RunFn) -> dict[str, dict] | None:
    """Per-subnet IP utilization from Network Analyzer, keyed by `region/name`.

    Each value is `{"primary": ratio|None, "secondary": {range_name: ratio},
    "listed": {range_name}}`. The insight marks the primary range by *omitting*
    `subnetRangeName`; every named entry is a secondary range.

    `listed` holds every secondary range the insight *mentioned*, before asking
    whether its ratio could be read, and it is the set `_zero_fill_skipped_ranges`
    reads. Keeping it separate from `secondary` is the whole point: that helper
    turns a range's absence into 0%, so a range the insight named but gave a
    figure this parser rejects must not land in the same bucket as one the
    insight never mentioned at all. The first is unreadable; only the second is
    empty.

    Returns None when the read gates closed, so the caller can tell "could not
    read" from "read fine, covers nothing".
    """
    parsed, result = run_and_gate(_ip_insight_argv(project), run=run)
    if parsed is None:
        log(f"{project}: ipAddressInsight gate failed (rc={result.rc}); subnet utilization unavailable")
        return None
    by_subnet: dict[str, dict] = {}
    for insight in parsed:
        for summary in (insight.get("content") or {}).get("ipUtilizationSummaryInfo") or []:
            for network in summary.get("networkStats") or []:
                for subnet in network.get("subnetStats") or []:
                    uri = subnet.get("subnetUri") or ""
                    if not uri:
                        continue
                    slot = by_subnet.setdefault(
                        _utilization_key(uri), {"primary": None, "secondary": {}, "listed": set()}
                    )
                    for rng in subnet.get("subnetRangeStats") or []:
                        name = rng.get("subnetRangeName")
                        if name:
                            slot["listed"].add(name)
                        ratio = rng.get("allocationRatio")
                        if not isinstance(ratio, (int, float)):
                            continue
                        if name:
                            slot["secondary"][name] = ratio
                        else:
                            slot["primary"] = ratio
    return by_subnet


def _backfill_utilization(parsed: list[dict], by_subnet: dict[str, dict], project: str) -> int:
    """Write insight ratios onto the `ipUtilization` field the check already
    reads, so `check_subnet_ip_exhaustion` itself needs no change.

    Only fills where the field is absent -- if a future gcloud starts
    populating it, the first-party value wins. Returns the number of subnets
    that gained at least one reading.

    Scoped to `project` for the reason `_utilization_key` gives and the two
    zero-fill helpers already act on: the key is `region/name` with no project
    segment, because the two surfaces spell the project the same way but the
    URI prefixes differ. `list-usable` reaches across a Shared VPC and the
    insight does not, so a host-project `us-east4/default` collides with the
    audited project's own `us-east4/default` and would be written that
    subnet's ratio -- publishing another project's subnet as 97% utilized on
    the strength of a reading that was never about it.
    """
    filled = 0
    for item in parsed:
        link = item.get("subnetwork", "")
        if _project_of_subnet_link(link) != project:
            continue
        slot = by_subnet.get(_utilization_key(link))
        if not slot:
            continue
        touched = False
        if isinstance(slot["primary"], (int, float)) and not isinstance(item.get("ipUtilization"), (int, float)):
            item["ipUtilization"] = slot["primary"]
            touched = True
        for sec in item.get("secondaryIpRanges") or []:
            ratio = slot["secondary"].get(sec.get("rangeName"))
            if isinstance(ratio, (int, float)) and not isinstance(sec.get("ipUtilization"), (int, float)):
                sec["ipUtilization"] = ratio
                touched = True
        if touched:
            filled += 1
    return filled


def _zero_fill_unallocated(parsed: list[dict], by_subnet: dict[str, dict], project: str) -> int:
    """Record a subnet the insight omits entirely at 0% rather than unmeasured.

    Network Analyzer publishes one project-scoped insight -- "Summary of IP
    utilization for all subnet ranges" -- and omits a subnet that has no
    allocation. `_collect_subnet_targets` already says so in prose; this acts on
    it. Once the insight has published for this project at all, which a non-zero
    `_backfill_utilization` establishes, a subnet missing from it is missing
    because nothing is allocated in it, and 0% is a reading rather than a blank.

    Declaring those UNEVALUATED instead turns every empty auto-mode regional
    `default` into a coverage gap. On a stock project that is 41 of 42 targets,
    and a ledger carrying 41 limitations and one measurement reads as an audit
    that could not run -- which is how issue #122 came out.

    One absence is deliberately left alone: a subnet in another project.
    `list-usable` reaches across a Shared VPC and the insight does not, so the
    host project's subnets are absent for a reason that says nothing about their
    allocations. A secondary range the insight never names inside a subnet it
    *does* cover is the same signal one level down; `_zero_fill_skipped_ranges`
    reads that one.

    Returns the number of subnets recorded at 0%.
    """
    zeroed = 0
    for item in parsed:
        link = item.get("subnetwork", "")
        if _utilization_key(link) in by_subnet or _carries_utilization(item):
            continue
        if _project_of_subnet_link(link) != project:
            continue
        item["ipUtilization"] = 0.0
        for sec in item.get("secondaryIpRanges") or []:
            if not isinstance(sec.get("ipUtilization"), (int, float)):
                sec["ipUtilization"] = 0.0
        zeroed += 1
    return zeroed


def _zero_fill_skipped_ranges(parsed: list[dict], by_subnet: dict[str, dict], project: str) -> int:
    """Record a secondary range the insight never named, inside a subnet it
    covers, at 0%.

    `_zero_fill_unallocated`'s rule, one level down, and resting on the same
    measurement. The insight is one project-scoped summary of "IP utilization
    for all subnet ranges", and it names a range only when something is
    allocated in it: on this fleet it published 15 range records for the whole
    project and not one carried `allocationRatio: 0`, the smallest being
    0.0009765625 -- a single node's /24 out of a /14. The 14 secondary ranges it
    named are exactly the 14 that GCE instances hold alias IP ranges in, and the
    two it skipped, the Pod ranges of the `drift-peer-ap-1` and `drift-peer-ap-2`
    Autopilot clusters, hold no alias ranges and run no nodes. Absence is the
    report of zero rather than a failure to look, measured 2026-09-01.

    Reading those two as unmeasured instead cost `us-east4/default` a standing
    `PARTIAL_SUBNET_LIMITATION`, which made the one subnet on the fleet with
    anything in it the only one §6 called a coverage gap. Issue #122 then said
    "this run did not see the whole fleet" about a run that saw all of it, and
    could never close.

    Three things it deliberately will not touch, because for each one absence
    means something other than empty:

    - **The primary range.** The evidence above is entirely alias-IP: it says
      where Pod ranges are used, and nothing about the addresses a primary
      carries. ILB VIPs, PSC endpoints and reserved static internal addresses
      all consume a primary without an alias range in sight, and a primary the
      insight did not publish is exactly the shape a partial read takes. It
      stays unmeasured and `PARTIAL_SUBNET_LIMITATION` still fires for it.
    - **A range the insight named but whose figure this parser could not
      read** -- `allocationRatio` null, a string, absent. `_utilization_by_subnet`
      records every name it sees in `listed` before testing the ratio, so a
      named-but-unreadable range is distinguishable from one never mentioned.
      The first is a read that failed and must stay a limitation; only the
      second is a measured zero.
    - **A subnet the insight omitted entirely**, which stays
      `_zero_fill_unallocated`'s to judge, and a Shared VPC host project's
      subnets, which stay nobody's.

    One residual it accepts, the same one the subnet-level zero-fill already
    accepts: a range created after the insight last refreshed is absent because
    the insight has not looked yet, and reads 0% here. `list-usable` carries no
    per-range creation time to gate on, so there is nothing to compare the
    refresh against. A new range is empty at creation and the insight refreshes
    daily, so the window where this is wrong is the window where 0% is also
    nearly true.

    Returns the number of subnets that gained a 0% range.
    """
    zeroed = 0
    for item in parsed:
        link = item.get("subnetwork", "")
        slot = by_subnet.get(_utilization_key(link))
        if slot is None:
            continue
        if _project_of_subnet_link(link) != project:
            continue
        touched = False
        for sec in item.get("secondaryIpRanges") or []:
            name = sec.get("rangeName")
            if not name or name in slot["listed"]:
                continue
            if isinstance(sec.get("ipUtilization"), (int, float)):
                continue
            sec["ipUtilization"] = 0.0
            touched = True
        if touched:
            zeroed += 1
    return zeroed


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
    if not parsed:
        # rc=0 and a valid `[]`, so the gate above is satisfied -- and the
        # scope would be reported fully covered with zero subnets in it. That
        # is the exact outcome the comment above says must not happen; the
        # gate simply never saw this shape, because `list-usable` answers
        # "none you may use" with success rather than a permission error.
        #
        # The two readings are "this project has no subnets" and "this
        # identity lacks compute.subnetworks.use", and `subnets list` tells
        # them apart: it needs only compute.subnetworks.list, which the
        # read-only role set already grants. On the deployed install it
        # returns 42 while `list-usable` returns 0 -- so every run reported
        # subnet-ip-exhaustion as covered and measured nothing.
        listing, list_result = run_and_gate(
            ["gcloud", "compute", "networks", "subnets", "list",
             "--project", project, "--format", "json"],
            run=run,
        )
        if listing is None:
            # The tie-breaker itself gated closed, so the ambiguity stands.
            # Fail closed rather than pick the cheerful reading.
            why = (
                f"and the corroborating `subnets list` also failed "
                f"(rc={list_result.rc}): {list_result.stderr.strip()[:200]}"
            )
        elif listing:
            why = (
                f"but `subnets list` sees {len(listing)} in this project. "
                f"list-usable returns only subnets the caller holds "
                f"compute.subnetworks.use on, and that enumeration is what "
                f"scopes this check, so it has nothing to measure under the "
                f"current identity. Grant compute.subnetworks.use to the audit "
                f"service account or accept the gap."
            )
        else:
            # Both reads agree the project has no subnets. A real empty scope,
            # not a hidden one -- nothing to audit and nothing to surface.
            log(f"{project}: no subnets in this project (list-usable and list both empty)")
            return []
        return [
            {
                "name": f"project/{project}/subnets",
                "project": project,
                "location": "global",
                "outcome": "gate-failed",
                "error": (
                    f"subnet-ip-exhaustion: {' '.join(argv)} returned 0 usable "
                    f"subnets (rc=0, valid empty JSON) {why}"
                ),
            }
        ]
    measured_by_insight = False
    if not any(_carries_utilization(item) for item in parsed):
        # Subnets came back, but not one carries the only field this check
        # reads. `check_subnet_ip_exhaustion` returns None for every one of
        # them, which is indistinguishable from "measured, all healthy" --
        # the same silent-clean failure as the empty list above, one layer in.
        #
        # This is what the deployed install actually does once
        # compute.subnetworks.use is granted: `list-usable` returns 42 subnets
        # and none has ipUtilization, in v1, beta and alpha alike. The field is
        # simply not part of gcloud's UsableSubnetwork. Network Analyzer
        # publishes the same measurement as a Recommender insight, so read it
        # from there and write it onto the field the check already reads.
        log(f"{project}: {len(parsed)} usable subnets, none carrying ipUtilization; trying Network Analyzer")
        by_subnet = _utilization_by_subnet(project, run=run)
        if by_subnet is None:
            return [
                {
                    "name": f"project/{project}/subnets",
                    "project": project,
                    "location": "global",
                    "outcome": "gate-failed",
                    "error": (
                        f"subnet-ip-exhaustion: {' '.join(argv)} returned "
                        f"{len(parsed)} subnets but none carries `ipUtilization` "
                        f"-- gcloud's UsableSubnetwork does not expose it in v1, "
                        f"beta or alpha -- and the fallback read of "
                        f"{_IP_INSIGHT_TYPE} also failed. Enable "
                        f"recommender.googleapis.com and grant "
                        f"recommender.networkAnalyzerIpAddressInsights.list to "
                        f"the audit service account."
                    ),
                }
            ]
        covered = _backfill_utilization(parsed, by_subnet, project)
        log(f"{project}: Network Analyzer covered {covered}/{len(parsed)} subnets")
        if not covered:
            return [
                {
                    "name": f"project/{project}/subnets",
                    "project": project,
                    "location": "global",
                    "outcome": "gate-failed",
                    "error": (
                        f"subnet-ip-exhaustion: neither surface yields "
                        f"utilization for any of the {len(parsed)} subnets. "
                        f"`list-usable` never carries `ipUtilization`, and "
                        f"{_IP_INSIGHT_TYPE} read cleanly but published stats "
                        f"for {len(by_subnet)} subnet(s), none of them matching. "
                        f"Network Analyzer needs a day or so after the API is "
                        f"enabled before it publishes."
                    ),
                }
            ]
        empty = _zero_fill_unallocated(parsed, by_subnet, project)
        if empty:
            log(f"{project}: {empty} subnet(s) absent from the insight recorded at 0% (nothing allocated)")
        skipped = _zero_fill_skipped_ranges(parsed, by_subnet, project)
        if skipped:
            log(f"{project}: {skipped} covered subnet(s) had a range the insight skipped, recorded at 0%")
        measured_by_insight = True
    # Publish the commands that produced the reading, in the order they ran.
    # On the backfill path `list-usable` only enumerated the subnets -- the
    # figure every verdict below turns on came from the insight -- so naming
    # the enumeration alone hands the reader a command that cannot reproduce
    # the finding, which is the one thing this field exists to allow.
    published = " ".join(argv)
    if measured_by_insight:
        published = f"{published} && {' '.join(_ip_insight_argv(project))}"
    record = _record(published, result)
    out = []
    for item in parsed:
        name = _last_segment(item.get("subnetwork", ""))
        region = _region_of_subnet_link(item.get("subnetwork", ""))
        # Name the target after the project that owns the subnet, not the one
        # being audited. `list-usable` reaches across a Shared VPC, so a host
        # project's `us-east4/default` and this project's own `us-east4/default`
        # both arrive here; labelling both with `project` gives two targets one
        # name. That is not a cosmetic collision -- `validate_scope` refuses a
        # document whose targets share a name, `finish` exits 2, and the run
        # publishes nothing at all rather than losing the one duplicate. The
        # three backfill helpers already discriminate on exactly this value;
        # this is the fourth site that needed it and the only one without it.
        owner = _project_of_subnet_link(item.get("subnetwork", "")) or project
        # A subnet the backfill did not reach has no utilization figure from
        # either surface. Running the check against it would return None --
        # "nothing wrong here" -- so declare it not-applicable instead, which
        # §6's manifest surfaces rather than silently counting as clean.
        # Network Analyzer omits subnets with no allocations, which is why an
        # auto-mode network shows 1 measured subnet and 41 untouched ones.
        measured = _carries_utilization(item)
        not_applicable: list[dict] = []
        if not measured:
            not_applicable.append(
                {
                    "check": "subnet-ip-exhaustion",
                    "reason": (
                        UNEVALUATED_MARKER
                        + "No IP-utilization figure for this subnet on either "
                        "surface: gcloud's UsableSubnetwork omits the field, and "
                        f"{_IP_INSIGHT_TYPE} published no stats for it, which "
                        "Network Analyzer does for subnets holding no allocations."
                    ),
                }
            )
        hit = check_subnet_ip_exhaustion(item) if measured else None
        entry = {
            "name": f"{owner}/{region}/{name}",
            "project": owner,
            "location": region,
            "outcome": "collected",
            # Recorded on every subnet, measured or not. A `commands` entry
            # says the *command* ran, not that the check reached a verdict --
            # and this pair of reads is exactly what established the unmeasured
            # ones as unmeasured. §6 is where that distinction gets applied:
            # the slug declared not-applicable above is dropped from
            # `checks_run` there, not hidden from provenance here.
            "commands": [{"check": "subnet-ip-exhaustion", **record}],
            "candidates": [_emit("subnet-ip-exhaustion", hit)] if hit else [],
            "checks_not_applicable": not_applicable,
        }
        if not measured:
            entry["limitations"] = UNMEASURED_SUBNET_LIMITATION
        else:
            unmeasured = _unmeasured_ranges(item)
            if unmeasured:
                total = 1 + len(item.get("secondaryIpRanges") or [])
                entry["limitations"] = PARTIAL_SUBNET_LIMITATION.format(
                    measured=total - len(unmeasured),
                    total=total,
                    names=", ".join(unmeasured),
                )
        out.append(entry)
    return out


def _collect_project_target(project: str, *, run: RunFn) -> dict:
    name = f"project/{project}"
    # Every read that backed each slug, in the order it ran — see
    # `_joined_record` for why this accumulates rather than assigns.
    reads: dict[str, list[tuple[str, Run]]] = {}
    candidates: list[dict] = []

    def gated(argv: list[str], slug: str):
        parsed, result = run_and_gate(argv, run=run)
        if parsed is None:
            raise GateFailure(f"{slug}: {' '.join(argv)} failed (rc={result.rc}): {result.stderr.strip()[:300]}")
        reads.setdefault(slug, []).append((" ".join(argv), result))
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
            # One read per dynamic gateway rather than one per router: an
            # unfiltered `get-nat-mapping-info` returns every VM behind every
            # gateway on the router, and `check_router_nat` says what comparing
            # that against each gateway's own ceiling in turn produces. Static
            # gateways are not read at all -- the check no longer evaluates
            # them, so the call would buy an answer that is thrown away.
            mappings = {}
            for nat in router["nats"]:
                if not nat.get("enableDynamicPortAllocation"):
                    continue
                nat_name = nat.get("name", "")
                mappings[nat_name] = gated(
                    [
                        "gcloud", "compute", "routers", "get-nat-mapping-info", router_name,
                        "--nat-name", nat_name,
                        "--region", region, "--project", project, "--format", "json",
                    ],
                    "cloud-nat-exhaustion",
                )
            hit = check_router_nat(router, status, mappings)
            if hit:
                candidates.append(_emit("cloud-nat-exhaustion", hit))

        # Listed unfiltered on purpose. `check_psc_routing` already re-tests
        # `"serviceAttachments" in target` on every rule, so a server-side
        # `--filter target:ServiceAttachment` bought nothing but a dependency on
        # gcloud's `:` operator matching a plural, differently-cased substring
        # inside a URL. If that ever stops matching, the filter returns nothing
        # and the check reads CLEAN -- a silent blind spot, not an error.
        forwarding_rules = gated(
            [
                "gcloud", "compute", "forwarding-rules", "list",
                "--project", project, "--format", "json",
            ],
            "psc-routing-deadlock",
        )
        candidates += [_emit("psc-routing-deadlock", hit) for hit in check_psc_routing(forwarding_rules)]

        networks = gated(
            ["gcloud", "compute", "networks", "list", "--project", project, "--format", "json"],
            "mtu-packet-fragmentation",
        )
        candidates += [_emit("mtu-packet-fragmentation", hit) for hit in check_mtu_mismatch(networks, project)]

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
        "commands": [{"check": slug, **_joined_record(slug_reads)} for slug, slug_reads in reads.items()],
        "candidates": candidates,
        # Empty rather than absent: three tests subscript this key directly.
        "checks_not_applicable": [],
    }


def crashed_entries(project: str, exc: BaseException) -> list[dict]:
    """The `clusters[]` entries for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one project
    aborts `collect_fleet` — and the SOP invokes this collector as
    `networking_audit.py … > manifest_gcp-networking-fabric-audit.json`, so by
    then the shell has already truncated the file. The run loses every project
    to one bad object instead of one. The shape is the `gate-failed`
    project-scoped entry `_collect_project_target` already returns, for the
    same reason: a project missing from the manifest reads as a project with
    nothing to report.
    """
    print(f"[networking_audit] {project}: collector raised {type(exc).__name__}: {exc}", file=sys.stderr)
    return [
        {
            "name": f"project/{project}",
            "project": project,
            "location": "global",
            "outcome": "gate-failed",
            "error": f"collector raised {type(exc).__name__}: {exc}"[:300],
        }
    ]


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
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entries
                results[index] = crashed_entries(projects[index], exc)

    return {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
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
