#!/opt/hermes/.venv/bin/python3
"""compute_fleet_audit.py — Procedural collector for the GCE Compute Engine
and MIG fleet audit.

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §6, and
governance/gce_compute_fleet_sop.md.

Ships alongside its SOP as this stream's own collector, in the shape §4.2
calls "a per-stream collector of their own shape" rather than folded into
`fleet-audit/scripts/collect.py`: that driver enumerates GKE clusters and
fetches per-cluster kubeconfigs, neither of which this stream needs — its
targets are GCP projects, read directly with `gcloud compute`. It emits the
run manifest §6 specifies, the same one `collect.py` and `networking_audit.py`
emit, so `audit_report.py finish --manifest-file` cross-checks the model's
`checks_run` against what actually ran. It used to print a whole findings
document instead, which no reader joins on: `adopt_collector_evidence` never
saw it and the model's retyped excerpts shipped in place of the observed ones.

All four of the SOP's checks are implemented here:

- `gce-startup-script-status` enumerates with `compute instances list` and
  measures with `compute instances get-serial-port-output` per RUNNING
  instance, matching §2.1's two literal markers in the console text.
- `mig-convergence-stalled` reads `compute instance-groups managed list` and
  holds §2.2's two limbs against each group's `currentActions` counters.
- `sole-tenant-headroom` reads `compute sole-tenancy node-groups list`, then
  `list-nodes` per group, and ratios consumed against total vCPU and memory.
- `orphaned-snapshots` cross-references `compute snapshots list` against
  `compute disks list` — a snapshot whose `sourceDisk` names neither a live
  disk's name nor its `selfLink`, carries no `resourcePolicies`, and is older
  than ninety days.

The roster was five checks and three of them were declared `UNEVALUATED:` on
every target with a reason saying no code performed them. That declaration was
wrong in both directions at once, which is why it is gone. `UNEVALUATED:`
leaves the coverage denominator, so a two-of-five run published
`coverage_gaps: []` and `partial: false` and closed the ledger claiming
coverage it did not have; and `unevaluated_targets` unions the target into
`blocked`, where `unverifiable_findings` is scoped to the *target* rather than
the (target, check) pair — so no finding on the two checks that did work could
ever be announced resolved, and its remediation pull request could never close.
The marker's own contract, in `audit_report.checks_unevaluated`, is "this check
applies, the collector read the surface, and no figure came back"; a check
nothing had implemented read no surface at all.

`ops-agent-guest-health` left the roster with them rather than being
implemented. §2.3's condition is whether the guest's Ops Agent is reporting,
which lives in Cloud Monitoring or in OS Config inventory, and neither is
reachable: `gcloud monitoring` exposes no metric read the credential proxy's
allowlist could carry, and `osconfig.googleapis.com` is not enabled on the
reference install. Keeping it as a permanent `UNEVALUATED:` would have kept the
whole stream's ledger open forever over one unimplementable check, so the
stream now audits the four things it can actually establish. §2.3 of the SOP
records the surface it would need.

Field contracts assumed of `gcloud ... --format=json` output:

- `compute instances list` items carry `name`, `status` and a `zone`
  selfLink whose last segment is the zone. Only `RUNNING` instances have
  serial console output to read.
- `compute instances get-serial-port-output` returns **text, not JSON**, so it
  runs outside `run_and_gate`. A failure on one instance does not fail the
  project closed — a single stopped-mid-run or IAM-refused VM would otherwise
  cost the project its other check — but it does subtract from coverage: the
  instances that could not be read are named in the target's `limitations`,
  and a project where *every* RUNNING instance refused the read declares the
  slug `UNEVALUATED:` rather than reporting a clean fleet.
- `compute disks list` items carry `name` and `selfLink`; a snapshot's
  `sourceDisk` appears in the wild in both forms, so both are indexed.
- `compute snapshots list` items carry `sourceDisk`, `creationTimestamp`
  (RFC-3339, `Z`-suffixed) and optionally `resourcePolicies`.
- `compute instance-groups managed list` items carry `name`, `size`,
  `targetSize`, a `status` object, a `currentActions` object of thirteen
  integer counters, and a `zone` *or* `region` selfLink — regional MIGs carry
  the latter, so `_scope_of` reads whichever is present. Every counter is read
  through `_count`, which defaults a missing or non-integer one to zero: a
  changed contract must read as "no churn observed" rather than cost the
  project its whole MIG check.
- `compute sole-tenancy node-groups list-nodes` items carry `totalResources`
  and `consumedResources`, each `{guestCpus, memoryMb, localSsdGb}`. This is
  the one shape here not verified against live output — the reference install
  reserves no sole-tenant node groups, so the read returns `[]` and the check
  declares a structural non-applicability instead. `check_sole_tenant_headroom`
  is therefore written to skip any node missing either object and to report
  `measured=False` when none of them yields figures, which is the `UNEVALUATED:`
  case the marker actually exists for.

What this collector deliberately does not do, because it would change what
gets flagged: it applies neither §2.1's GKE-node exclusion, nor §2.2's
pod-driven-scale exclusion, nor §2.5's legal-hold exclusion, nor §2.4's
maintenance-window one. Each is the model's call on the candidate, which is why
those candidates carry a `needs_triage` slug naming the judgment rather than a
`null`. The one exclusion applied here is §2.4's autoscaling limb, because a
node group's `autoscalingPolicy.mode` is a field on the group rather than a
judgment about it.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, NamedTuple

AUDIT_ID = "gce-compute-fleet-audit"

MANIFEST_VERSION = 1

# A digest of this file, published in the manifest. `audit_report.py` compares
# it against the previous run's to tell a finding that stopped reproducing from
# a check that stopped looking. It has to agree across every collector: the
# comparison is between one run's revision and the last one's, so a file that
# truncated differently would report a moved collector on the run that changed
# it.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[
    :REVISION_DIGEST_CHARS
]

DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8
# `audit_report.validate_check_command`'s ceiling, restated rather than
# imported because this script ships standalone. An over-length `command` is
# not a clipped field: `finish` refuses the whole document, so a project with
# enough RUNNING instances to overflow the joined serial-read provenance below
# would publish nothing at all.
MAX_COMMAND_CHARS = 2000
# `audit_report.MAX_EXCERPT_CHARS`, restated for the same reason.
MAX_EXCERPT_CHARS = 2000
# `cross_check_manifest` quotes a target's `error` back clipped to 150 and 200
# characters, so the sentence has to name the check, the command and the rc
# inside this budget.
ERROR_CLIP_CHARS = 300
# How much of a joined command the clip keeps for the counted tail.
JOIN_TAIL_BUDGET_CHARS = 64

GLOBAL_LOCATION = "global"
UNRESOLVED_PROJECT = "unknown"
RUNNING_STATUS = "RUNNING"

STARTUP_SLUG = "gce-startup-script-status"
MIG_SLUG = "mig-convergence-stalled"
SOLE_TENANT_SLUG = "sole-tenant-headroom"
SNAPSHOT_SLUG = "orphaned-snapshots"

# §2.1's condition, verbatim: the two literal substrings that mean the guest's
# startup script exited non-zero. Case-sensitive substring match, no regex —
# the same test the previous revision of this file ran, kept unchanged so the
# conversion moves the output shape and not what gets flagged.
STARTUP_FAILURE_MARKERS = (
    "startup-script exit status 1",
    "Finished running startup scripts with error",
)

# §2.5's threshold. Measured against the snapshot's own `creationTimestamp`,
# which is what this collector can see; §2.5 words the condition as the source
# disk having been deleted more than ninety days ago, and no read here recovers
# a deleted disk's deletion time. The two coincide whenever the disk outlived
# the snapshot's first ninety days, and the snapshot age is the conservative
# side of the difference — it flags later, never earlier.
ORPHAN_AGE_DAYS = 90

# §2.4's threshold, as a percentage of the node group's aggregate capacity.
SOLE_TENANT_UTILISATION_PCT = 90

SEVERITY = {
    STARTUP_SLUG: "critical",
    MIG_SLUG: "major",
    SOLE_TENANT_SLUG: "minor",
    SNAPSHOT_SLUG: "minor",
}

# Opens the `reason` of a check that *applies* to its target and was attempted
# or owed without reaching a verdict, as against one the target's shape rules
# out. `audit_report.checks_unevaluated` is the reader: the slug still leaves
# the coverage denominator, so a stream missing three of five checks does not
# read as permanently partial, but a finding on that slug can never be
# announced resolved on the strength of this run's silence.
UNEVALUATED_MARKER = "UNEVALUATED: "

# Structural non-applicability: the enumeration ran, came back empty, and the
# check has no object to hold its condition against. Deliberately *not* carrying
# `UNEVALUATED_MARKER`. The marker's contract, set out in
# `audit_report.checks_unevaluated`, is "this check applies, the collector read
# the surface, and no figure came back" — a target carrying one is unioned into
# `blocked` and no finding on *any* of its checks can be announced resolved,
# because `unverifiable_findings` is scoped to the target rather than to the
# (target, check) pair. A project that simply runs no MIGs is not a project this
# run cannot vouch for, and marking it so would pin the whole stream's ledger
# open over an absence the read positively established.
NO_MIGS_REASON = (
    "This project runs no Managed Instance Groups: `compute instance-groups "
    "managed list` returned an empty array, so §2.2's convergence condition "
    "has nothing to hold against. Structural, not a missed read."
)
NO_NODE_GROUPS_REASON = (
    "This project reserves no sole-tenant node groups: `compute sole-tenancy "
    "node-groups list` returned an empty array, so §2.4's headroom condition "
    "has no reservation to measure. Structural, not a missed read."
)
# The genuine `UNEVALUATED:` case for §2.4, and the shape the marker was written
# for: the groups exist and were enumerated, the per-node read ran, and not one
# node carried the resource figures the condition needs. Nobody looked, so a
# stale headroom finding must not be called fixed on the strength of this run.
UNMEASURED_NODE_GROUPS_REASON = (
    UNEVALUATED_MARKER
    + "§2.4's headroom condition needs each node's `totalResources` and "
    "`consumedResources`, and `compute sole-tenancy node-groups list-nodes` "
    "returned neither for any node of the {groups} node group(s) on this "
    "project. They were enumerated, not measured."
)

# One or more RUNNING instances refused `get-serial-port-output` while others
# answered. The check keeps its verdict for the instances it read; this names
# the ones it passed over rather than cleared, which is what turns the
# shortfall into a coverage gap instead of a silent clean pass.
PARTIAL_SERIAL_LIMITATION = (
    "gce-startup-script-status read serial console output from {read} of "
    "{total} RUNNING instance(s) on this project. "
    "`compute instances get-serial-port-output` failed for the rest, which "
    "the check passed over rather than cleared: {names}."
)
# Every RUNNING instance refused the read, so the check reached no verdict at
# all on this project and the slug is declared `UNEVALUATED:` alongside this.
UNREAD_SERIAL_LIMITATION = (
    "gce-startup-script-status could not be evaluated on this project: "
    "`compute instances get-serial-port-output` failed for all {total} "
    "RUNNING instance(s), so no serial console output was read. They were "
    "enumerated, not examined."
)

# §2.1's Do-NOT-flag limb is a GKE-node exclusion this collector does not
# apply, and §2.5's is a legal-hold exclusion it cannot see. `needs_triage` is
# how a candidate says which judgment it is handing back — it is not read by
# `audit_report.py`, it is an instruction to the model.
TRIAGE_GKE_NODE = "gke-managed-node"
TRIAGE_GKE_MIG = "gke-managed-mig"
TRIAGE_MAINTENANCE = "maintenance-window"
TRIAGE_RETENTION_HOLD = "retention-hold"

# Both GKE node-pool MIG spellings: `gke-` for Standard, `gk3-` for Autopilot.
# Every one of the 79 groups on the reference install carries one of these.
GKE_MIG_PREFIXES = ("gke-", "gk3-")

REDACTED = "[REDACTED]"
# Serial console output is untrusted text from inside the guest, and §2.1's
# excerpt is a line lifted straight out of it. The SOP's red line — "credentials
# in serial port output must never reach an excerpt" — used to be the model's to
# honour, because the model retyped the excerpt; `adopt_collector_evidence`
# overwrites the model's text with this one, so it is now this file's. Shapes
# that a startup script is known to echo, matched before the line is published.
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN[^-]{0,64}PRIVATE KEY-----"),
    re.compile(r"ya29\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)\b(?:bearer|token|password|passwd|secret|api[_-]?key)\b[\"'\s:=]+\S+"),
    # A long unbroken run of encoded material — a key or a JWT segment. The
    # uppercase-or-`+` lookahead is what keeps this from swallowing the
    # excerpt's diagnostic content: the previous form,
    # `[A-Za-z0-9+/_\-]{40,}`, matched any 40-character run of lowercase,
    # digits and separators, which is precisely the shape of a GKE node name
    # (`gke-prod-cluster-default-pool-9f8a7b6c-abcd`, 43 chars) and of a deep
    # filesystem path. Both were being redacted out of real serial lines — and
    # since `adopt_collector_evidence` overwrites the model's excerpt with this
    # string, the degraded line is what shipped. The node name is the very
    # string this collector's `needs_triage: gke-managed-node` handoff asks the
    # model to judge, and the failing script's path is the only part of a
    # startup-script excerpt that says *what* failed.
    re.compile(r"\b(?=[A-Za-z0-9+/_\-]*[A-Z+])[A-Za-z0-9+/_\-]{40,}={0,2}\b"),
    # Lowercase hex digests, which the lookahead above deliberately excludes.
    # Precise enough not to reach a hostname: `[a-f0-9]` admits none of the
    # letters a DNS label needs, and a hyphen ends the run.
    re.compile(r"\b[a-f0-9]{32,}\b"),
)


def log(msg: str) -> None:
    """Every log line goes to stderr: the SOP redirects stdout to the manifest
    file, so one stray `print` corrupts the JSON."""
    print(f"[compute_fleet_audit] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    """One subprocess's outcome, in the shape the manifest records it — the
    same shape `collect.py`'s and `networking_audit.py`'s `Run` use, kept as a
    separate definition because this script ships standalone."""

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
    output, or non-JSON output all gate closed (§4.1: a truncated result must
    never read as "nothing here")."""
    result = run(argv)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result


class GateFailure(Exception):
    """Raised when one of a project target's several independent `gcloud`
    reads fails its gate. Fails that whole target closed — one `outcome` per
    manifest entry, not one per check. A shorter `candidates` list from the
    checks that happened to run first is indistinguishable from a clean
    project."""


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _joined_record(reads: list[tuple[str, Run]]) -> dict:
    """One `commands` entry covering every read that backed one check.

    Both implemented checks take more than one read — an enumeration plus a
    serial read per instance, an enumeration of disks plus one of snapshots —
    and publishing only the last names a command that cannot reproduce the
    verdict, which is the one thing this field exists to allow.

    Joined with ` && ` so the field stays a line a reader can paste. `rc` is 0
    because every read here passed its gate: a gcloud failure raises
    `GateFailure` before reaching this, and a per-instance serial read that
    failed is never appended.
    """
    parts = [command for command, _ in reads]
    joined = " && ".join(parts)
    if len(joined) > MAX_COMMAND_CHARS:
        # Clipped at a join boundary, and the tail is counted rather than
        # dropped: a `commands` entry silently listing three of a project's
        # ninety serial reads claims narrower coverage than the run had.
        kept = [parts[0]]
        for part in parts[1:]:
            if len(" && ".join(kept + [part])) > MAX_COMMAND_CHARS - JOIN_TAIL_BUDGET_CHARS:
                break
            kept.append(part)
        joined = (
            " && ".join(kept)
            + f"  # and {len(parts) - len(kept)} more read(s) of the same shape"
        )
    return {
        "command": joined[:MAX_COMMAND_CHARS],
        "rc": 0,
        "duration_s": round(sum(result.duration_s for _, result in reads), 2),
        "output_sha256": output_digest("".join(result.stdout for _, result in reads)),
    }


def _last_segment(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1]


def redact(text: str) -> str:
    """Blank out anything in a serial console line that looks like a secret.

    Conservative in the direction that matters: a redaction that swallows a
    harmless token costs a reader some context, while a miss publishes a
    credential into a GitHub issue.
    """
    out = text
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out[:MAX_EXCERPT_CHARS]


def get_target_projects(cli_project: str | None = None, *, run: RunFn = default_run) -> list[str]:
    """Resolves all target GCP projects to audit.

    Unchanged from the pre-manifest revision of this file, including the order:
    `gcloud projects list` is consulted before the single-project environment
    variables, so an identity that can list the organisation sweeps every
    accessible project rather than the one it happens to be configured for.
    """
    if cli_project:
        return [cli_project]

    projects: set[str] = set()
    monitored = os.environ.get("MONITORED_PROJECT_IDS", "")
    if monitored:
        for candidate in monitored.replace(",", " ").split():
            candidate = candidate.strip()
            if candidate:
                projects.add(candidate)

    if not projects:
        result = run(["gcloud", "projects", "list", "--format=value(projectId)"])
        if result.rc == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                if line.strip():
                    projects.add(line.strip())

    if not projects:
        for env_var in ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID"):
            val = os.environ.get(env_var, "").strip()
            if val:
                projects.add(val)

    if not projects:
        result = run(["gcloud", "config", "get-value", "project"])
        if result.rc == 0 and result.stdout.strip():
            projects.add(result.stdout.strip())

    return sorted(projects)


# --------------------------------------------------------------------------- #
# Check bodies: pure functions over already-read `gcloud` output. Each returns
# either one hit or `None`, or a list of hits, and knows nothing about the
# manifest.
# --------------------------------------------------------------------------- #


def check_startup_script(instance_name: str, zone: str, serial_text: str) -> dict | None:
    """`serial_text` is one instance's `get-serial-port-output` body. Flags the
    first line carrying either of §2.1's two fatal markers.

    First match wins and the scan stops, the same as the pre-manifest revision:
    a boot that failed twice is one degraded instance, not two findings.

    The `object` carries the zone because a GCE instance name is unique per
    zone, not per project: `web-1` in `us-central1-a` and `web-1` in
    `us-central1-b` are two different VMs one project can hold at once.
    Unqualified they derive the same finding id, and `validate_findings`
    refuses the *whole* document over the collision — so the run publishes
    nothing at all rather than one merged finding. `networking_audit.py` scopes
    `Router/<region>/<name>` and `ForwardingRule/<scope>/<name>` for the same
    reason.
    """
    for line in serial_text.splitlines():
        if any(marker in line for marker in STARTUP_FAILURE_MARKERS):
            return {
                "object": f"ComputeInstance/{zone}/{instance_name}",
                "excerpt": redact(line.strip()),
                "impact": (
                    f"Instance {instance_name} failed initialization and may be in a "
                    "degraded or unbootstrapped state."
                ),
                "needs_triage": TRIAGE_GKE_NODE,
            }
    return None


def running_instances(instances: list) -> list[tuple[str, str]]:
    """The `(name, zone)` pairs §2.1 has console output to read.

    A non-RUNNING instance has no live serial console, and an item missing
    either field cannot be addressed by a `get-serial-port-output` call.
    """
    out = []
    for inst in instances or []:
        if not isinstance(inst, dict):
            continue
        name = inst.get("name", "")
        zone = _last_segment(inst.get("zone", ""))
        if inst.get("status", "") != RUNNING_STATUS or not name or not zone:
            continue
        out.append((name, zone))
    return out


def active_disk_index(disks: list) -> set[str]:
    """Every spelling a live disk answers to.

    A snapshot's `sourceDisk` arrives as a bare name in some payloads and as a
    full `selfLink` in others, so both go in and the membership test matches
    whichever form the snapshot used.
    """
    index: set[str] = set()
    for disk in disks or []:
        if not isinstance(disk, dict):
            continue
        for key in ("name", "selfLink"):
            value = disk.get(key, "")
            if value:
                index.add(value)
    return index


def check_orphaned_snapshot(snapshot: dict, active_disks: set[str], now: datetime.datetime) -> dict | None:
    """One item from `compute snapshots list`. Flags a snapshot whose source
    disk is gone, that no resource policy retains, and that is older than
    ninety days.

    A snapshot with no `sourceDisk` at all is never flagged: it is an import or
    a hand-made image, and there is no deleted disk to attribute it to. An
    unparseable `creationTimestamp` is likewise not flagged — an age nobody
    could compute is not an age over the threshold.
    """
    name = snapshot.get("name", "")
    source_disk = snapshot.get("sourceDisk", "")
    source_disk_name = _last_segment(source_disk) if source_disk else ""
    created = snapshot.get("creationTimestamp", "")
    if not source_disk_name:
        return None
    if source_disk_name in active_disks or source_disk in active_disks:
        return None
    if snapshot.get("resourcePolicies", []):
        return None
    if not created:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        age_days = (now - stamp).days
    except (ValueError, TypeError):
        # A timestamp that will not parse, or one with no UTC offset to
        # subtract `now` from. An age nobody could compute is not an age over
        # the threshold — the same silence the pre-manifest revision kept.
        return None
    if age_days <= ORPHAN_AGE_DAYS:
        return None
    return {
        "object": f"Snapshot/{name}",
        "excerpt": (
            f'{{"name": "{name}", "sourceDisk": "{source_disk_name}", '
            f'"creationTimestamp": "{created}"}}'
        ),
        "impact": (
            f"Snapshot {name} incurs ongoing storage charges without active source disk."
        ),
        "needs_triage": TRIAGE_RETENTION_HOLD,
    }


def _scope_of(resource: dict) -> str:
    """The zone or region a scoped Compute resource lives in.

    Carried into every `object` this file derives for a MIG or a node group,
    for the reason `check_startup_script` spells out at length: a Compute name
    is unique per zone, not per project, so two same-named groups in different
    zones derive one finding id and `validate_findings` refuses the whole
    document rather than merging them.
    """
    for key in ("zone", "region"):
        value = resource.get(key)
        if value:
            return _last_segment(str(value))
    return "unknown"


def _count(actions: dict, key: str) -> int:
    """One `currentActions` counter as an int, defaulting to 0.

    The API publishes all thirteen counters on every group, but a changed
    contract or a partial response must read as "no churn observed" rather than
    raise — the alternative is one malformed group costing the project its
    whole MIG check.
    """
    try:
        return int(actions.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def check_mig_convergence(mig: dict) -> dict | None:
    """§2.2's condition against one `instance-groups managed list` entry.

    Two limbs, neither of which a healthy group satisfies and both readable
    from a single point-in-time list:

    `creating` and `deleting` both non-zero — the group is adding and removing
    instances at the same moment. A scale-up only creates and a scale-down only
    deletes, so both at once is the resize loop §2.2 was written about, caught
    in the act.

    `creatingWithoutRetries` non-zero — creation failed and the group will not
    try again, so it sits below target indefinitely. A zonal stockout for the
    machine type and an instance template that no longer resolves are the two
    usual causes.

    What is deliberately *not* the condition is `status.isStable == false` on
    its own. Instability is the normal state of any group mid-scale, so flagging
    it would report every healthy autoscaler under load — on the reference
    install that is all 79 groups the moment a workload arrives. §2.2's original
    wording was a rate (repeated resizes inside fifteen minutes) and no `gcloud`
    read carries a MIG's resize history to count one; these two limbs are the
    part of that intent a single read can actually establish, which is why the
    slug says `convergence-stalled` rather than `autoscaler-flapping`.
    """
    name = str(mig.get("name", "")).strip()
    actions = mig.get("currentActions")
    if not name or not isinstance(actions, dict):
        return None

    creating = _count(actions, "creating")
    deleting = _count(actions, "deleting")
    without_retries = _count(actions, "creatingWithoutRetries")

    if creating > 0 and deleting > 0:
        detail = f"creating={creating}, deleting={deleting}"
        impact = (
            "The group is adding and removing instances at the same time, "
            "which is a resize loop rather than a scale event. Every cycle "
            "pays a full instance boot and the capacity actually serving "
            "traffic oscillates underneath it."
        )
    elif without_retries > 0:
        detail = f"creatingWithoutRetries={without_retries}"
        impact = (
            "Instance creation failed and the group will not retry, so it "
            "stays below its target size until someone intervenes. Capacity "
            "planning that assumes the target is met is wrong by that margin."
        )
    else:
        return None

    scope = _scope_of(mig)
    status = mig.get("status") if isinstance(mig.get("status"), dict) else {}
    return {
        "object": f"ManagedInstanceGroup/{scope}/{name}",
        "excerpt": redact(
            f"{name} ({scope}): size={mig.get('size', '?')} "
            f"targetSize={mig.get('targetSize', '?')} "
            f"isStable={status.get('isStable')} currentActions {detail}"
        )[:MAX_EXCERPT_CHARS],
        "impact": impact,
        # §2.2's Do-NOT-flag limb excuses GKE node pools undergoing pod-driven
        # scale events. The prefix is mechanical, but whether a given churn is
        # pod-driven is not, so the judgment goes back to the model.
        "needs_triage": TRIAGE_GKE_MIG if name.startswith(GKE_MIG_PREFIXES) else None,
    }


def check_sole_tenant_headroom(group: dict, nodes: list) -> tuple[dict | None, bool]:
    """§2.4's condition against one node group and its `list-nodes` result.

    Returns `(candidate, measured)`. `measured` is False when not one node
    carried both `totalResources` and `consumedResources` — the read ran and no
    figure came back, which is the one disposition `UNEVALUATED:` exists for. A
    group whose nodes read cleanly and sit under the threshold returns
    `(None, True)`: a verdict, not a missing one. Keeping the two apart is the
    whole point — collapsing them is how a stream reports a clean fleet on the
    strength of reads that never happened.

    The condition is §2.4's conjunction, both halves off the same read:
    utilisation at or above `SOLE_TENANT_UTILISATION_PCT` of aggregate capacity,
    *and* less than one node's worth of vCPU still free. The second half is what
    "without failover host headroom" means — a group at 90% across ten nodes
    still has a whole node spare and survives losing one.
    """
    name = str(group.get("name", "")).strip()
    if not name:
        return None, False

    total_cpus = consumed_cpus = 0
    total_mem = consumed_mem = 0
    measured_nodes = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        total = node.get("totalResources")
        consumed = node.get("consumedResources")
        if not isinstance(total, dict) or not isinstance(consumed, dict):
            continue
        try:
            node_cpus = int(total.get("guestCpus") or 0)
            node_mem = int(total.get("memoryMb") or 0)
            used_cpus = int(consumed.get("guestCpus") or 0)
            used_mem = int(consumed.get("memoryMb") or 0)
        except (TypeError, ValueError):
            continue
        if node_cpus <= 0:
            # A node reporting no capacity cannot contribute a ratio, and
            # counting it would drag the denominator down and manufacture a
            # utilisation figure out of a malformed record.
            continue
        total_cpus += node_cpus
        total_mem += node_mem
        consumed_cpus += used_cpus
        consumed_mem += used_mem
        measured_nodes += 1

    if not measured_nodes or total_cpus <= 0:
        return None, False

    # §2.4's Do-NOT-flag limb: a node group that grows itself is not short of
    # headroom, it is between sizes. Applied here rather than handed back as
    # triage because the mode is a field on the group, not a judgment.
    policy = group.get("autoscalingPolicy")
    mode = str((policy or {}).get("mode", "")).strip().upper()
    if isinstance(policy, dict) and mode not in ("", "OFF"):
        return None, True

    cpu_pct = 100.0 * consumed_cpus / total_cpus
    mem_pct = 100.0 * consumed_mem / total_mem if total_mem > 0 else 0.0
    one_node_cpus = total_cpus / measured_nodes
    free_cpus = total_cpus - consumed_cpus

    if max(cpu_pct, mem_pct) < SOLE_TENANT_UTILISATION_PCT:
        return None, True
    if free_cpus >= one_node_cpus:
        return None, True

    scope = _scope_of(group)
    return (
        {
            "object": f"NodeGroup/{scope}/{name}",
            "excerpt": redact(
                f"{name} ({scope}): {measured_nodes} node(s), "
                f"vCPU {consumed_cpus}/{total_cpus} ({cpu_pct:.0f}%), "
                f"memory {consumed_mem}/{total_mem} MB ({mem_pct:.0f}%), "
                f"{free_cpus} vCPU free against {one_node_cpus:.0f} per node"
            )[:MAX_EXCERPT_CHARS],
            "impact": (
                "The reservation is at capacity with less than one node's "
                "worth of vCPU spare, so losing a single host leaves nowhere "
                "for its VMs to land. Sole-tenant VMs do not spill onto shared "
                "hardware — they stay down until capacity is added."
            ),
            "needs_triage": TRIAGE_MAINTENANCE,
        },
        True,
    )


def _emit(slug: str, hit: dict) -> dict:
    return {
        "check": slug,
        "namespace": "",
        "object": hit["object"],
        "severity": hit.get("severity") or SEVERITY[slug],
        "excerpt": hit["excerpt"],
        "impact": hit["impact"],
        "needs_triage": hit.get("needs_triage"),
    }


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


def _serial_argv(instance: str, zone: str, project: str) -> list[str]:
    # `--port=1` is gcloud's own default, so naming it changes nothing that is
    # read; it is spelled out because §2.1's command and its evidence example
    # both spell it, and the model copies this string into `checks_run`.
    return [
        "gcloud", "compute", "instances", "get-serial-port-output", instance,
        f"--zone={zone}", "--port=1", f"--project={project}",
    ]


def collect_project(project: str, *, run: RunFn = default_run) -> dict:
    """The single manifest entry for one project (§6's `clusters[]` shape,
    reused for a target that is a project rather than a GKE cluster).

    The name is `project/<id>`, the spelling `networking_audit.py` uses.
    `audit_report.target_kind` reads the `project/` prefix and answers
    "project"; the hyphenated `project-<id>` this file used to emit falls
    through to the bare-name branch and is classified as a *cluster*, so the
    published scope line called a sweep of one GCP project "1 cluster". There
    is no ledger to orphan by re-deriving the ids: this stream has never
    published a GitHub issue — the `audit:gce-compute-fleet-audit` label does
    not exist on the repository and no report directory for it exists on the
    install — because until this conversion the collector emitted a findings
    document nothing consumed.

    No `autopilot` key: the target stands for a project, and a `false` there
    would read as a fleet of Standard clusters.
    """
    name = f"project/{project}"
    # Every read that backed each slug, in the order it ran.
    reads: dict[str, list[tuple[str, Run]]] = {}
    candidates: list[dict] = []
    not_applicable: list[dict] = []
    limitations: list[str] = []

    def gated(argv: list[str], slug: str) -> list:
        parsed, result = run_and_gate(argv, run=run)
        if parsed is None:
            raise GateFailure(
                f"{slug}: {' '.join(argv)} failed (rc={result.rc}): "
                f"{result.stderr.strip()[:ERROR_CLIP_CHARS]}"
            )
        if not isinstance(parsed, list):
            # `--format=json` on a `list` sub-command returns an array. A dict
            # here is an error envelope or a changed contract, and reading zero
            # items off it would report an empty, healthy project.
            raise GateFailure(
                f"{slug}: {' '.join(argv)} returned "
                f"{type(parsed).__name__}, not a JSON array"
            )
        reads.setdefault(slug, []).append((" ".join(argv), result))
        return parsed

    try:
        # --- 2.1 startup script failures ---------------------------------- #
        instances = gated(
            ["gcloud", "compute", "instances", "list", "--project", project, "--format=json"],
            STARTUP_SLUG,
        )
        targets = running_instances(instances)
        unread: list[str] = []
        for instance_name, zone in targets:
            argv = _serial_argv(instance_name, zone, project)
            result = run(argv)
            if result.rc != 0 or not result.stdout.strip():
                # Not a project-level gate failure. One VM that stopped
                # mid-sweep, or one the identity cannot read the console of,
                # must not cost the project its snapshot check — but it is not
                # a pass either, so it is named in `limitations` below.
                unread.append(f"{instance_name} ({zone})")
                continue
            hit = check_startup_script(instance_name, zone, result.stdout)
            # A read that produced a candidate goes to the front of the slug's
            # provenance list. `_joined_record` clips the join at
            # MAX_COMMAND_CHARS, and `adopt_collector_evidence` writes that one
            # string onto every finding of this (target, check) — so on a
            # project with more RUNNING instances than the budget holds, a
            # tail-ordered list ships an excerpt from instance #30 under a
            # command naming only instances #1-#16. Hitting reads first means
            # the published command always contains the read behind the
            # published excerpt.
            if hit:
                reads.setdefault(STARTUP_SLUG, []).insert(1, (" ".join(argv), result))
                candidates.append(_emit(STARTUP_SLUG, hit))
            else:
                reads.setdefault(STARTUP_SLUG, []).append((" ".join(argv), result))

        if targets and len(unread) == len(targets):
            # Nothing was examined. The enumeration ran, so the slug has a
            # `commands` entry pending — dropped at emit time, because a
            # recorded command corroborates exactly the `checks_run` claim
            # this declaration exists to refuse.
            not_applicable.append(
                {
                    "check": STARTUP_SLUG,
                    "reason": UNEVALUATED_MARKER
                    + "Every RUNNING instance on this project refused "
                    "`compute instances get-serial-port-output`, so no serial "
                    "console text was read and §2.1's markers were matched "
                    "against nothing.",
                }
            )
            limitations.append(UNREAD_SERIAL_LIMITATION.format(total=len(targets)))
        elif unread:
            limitations.append(
                PARTIAL_SERIAL_LIMITATION.format(
                    read=len(targets) - len(unread),
                    total=len(targets),
                    names=", ".join(sorted(unread)),
                )
            )

        # --- 2.2 MIG convergence -------------------------------------------- #
        migs = gated(
            [
                "gcloud", "compute", "instance-groups", "managed", "list",
                "--project", project, "--format=json",
            ],
            MIG_SLUG,
        )
        if not migs:
            not_applicable.append({"check": MIG_SLUG, "reason": NO_MIGS_REASON})
        else:
            for mig in migs:
                if not isinstance(mig, dict):
                    continue
                hit = check_mig_convergence(mig)
                if hit:
                    candidates.append(_emit(MIG_SLUG, hit))

        # --- 2.4 sole-tenant headroom --------------------------------------- #
        groups = gated(
            [
                "gcloud", "compute", "sole-tenancy", "node-groups", "list",
                "--project", project, "--format=json",
            ],
            SOLE_TENANT_SLUG,
        )
        if not groups:
            not_applicable.append(
                {"check": SOLE_TENANT_SLUG, "reason": NO_NODE_GROUPS_REASON}
            )
        else:
            measured_any = False
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_name = str(group.get("name", "")).strip()
                if not group_name:
                    continue
                nodes_argv = [
                    "gcloud", "compute", "sole-tenancy", "node-groups",
                    "list-nodes", group_name, f"--zone={_scope_of(group)}",
                    f"--project={project}", "--format=json",
                ]
                parsed, result = run_and_gate(nodes_argv, run=run)
                # Not gated to the project: one node group whose nodes refuse
                # to list must not cost the project its snapshot check, the way
                # one unreadable serial console does not. It costs this check
                # its verdict only if *every* group comes back unmeasurable.
                if not isinstance(parsed, list):
                    reads.setdefault(SOLE_TENANT_SLUG, []).append(
                        (" ".join(nodes_argv), result)
                    )
                    continue
                hit, measured = check_sole_tenant_headroom(group, parsed)
                measured_any = measured_any or measured
                if hit:
                    reads.setdefault(SOLE_TENANT_SLUG, []).insert(
                        1, (" ".join(nodes_argv), result)
                    )
                    candidates.append(_emit(SOLE_TENANT_SLUG, hit))
                else:
                    reads.setdefault(SOLE_TENANT_SLUG, []).append(
                        (" ".join(nodes_argv), result)
                    )
            if not measured_any:
                not_applicable.append(
                    {
                        "check": SOLE_TENANT_SLUG,
                        "reason": UNMEASURED_NODE_GROUPS_REASON.format(
                            groups=len(groups)
                        ),
                    }
                )

        # --- 2.5 orphaned snapshots ---------------------------------------- #
        # Both reads are gated: a `disks list` that failed used to skip the
        # snapshot check silently, which left the project in scope carrying one
        # check and no sign that the other had been abandoned. It is now what
        # it is — a read that failed, and a project the run could not cover.
        disks = gated(
            ["gcloud", "compute", "disks", "list", "--project", project, "--format=json"],
            SNAPSHOT_SLUG,
        )
        snapshots = gated(
            ["gcloud", "compute", "snapshots", "list", "--project", project, "--format=json"],
            SNAPSHOT_SLUG,
        )
        active = active_disk_index(disks)
        now = datetime.datetime.now(datetime.timezone.utc)
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            hit = check_orphaned_snapshot(snapshot, active, now)
            if hit:
                candidates.append(_emit(SNAPSHOT_SLUG, hit))
    except GateFailure as exc:
        return {
            "name": name,
            "project": project,
            "location": GLOBAL_LOCATION,
            "outcome": "gate-failed",
            "error": str(exc)[:ERROR_CLIP_CHARS],
        }

    not_applicable_slugs = {entry["check"] for entry in not_applicable}
    entry = {
        "name": name,
        "project": project,
        "location": GLOBAL_LOCATION,
        "outcome": "collected",
        "commands": [
            {"check": slug, **_joined_record(slug_reads)}
            for slug, slug_reads in reads.items()
            if slug not in not_applicable_slugs
        ],
        "candidates": candidates,
        # Empty rather than absent where nothing applies: the reader tolerates
        # either, and tests subscript this key directly.
        "checks_not_applicable": not_applicable,
    }
    if limitations:
        entry["limitations"] = "; ".join(limitations)
    return entry


def crashed_entry(project: str, exc: BaseException) -> dict:
    """The `clusters[]` entry for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one project
    would abort `collect_fleet` — and the SOP invokes this collector as
    `compute_fleet_audit.py > manifest_gce-compute-fleet-audit.json`, so by
    then the shell has already truncated the file. The run would lose every
    project to one bad object instead of one.
    """
    log(f"{project}: collector raised {type(exc).__name__}: {exc}")
    return {
        "name": f"project/{project}",
        "project": project,
        "location": GLOBAL_LOCATION,
        "outcome": "gate-failed",
        "error": f"collector raised {type(exc).__name__}: {exc}"[:ERROR_CLIP_CHARS],
    }


def unresolved_entry() -> dict:
    """The stand-in for a run that enumerated no project at all.

    An empty `clusters` list reads as a fleet with nothing in it, which is a
    clean, fully covered scope. The SOP routes this name to `scope.skipped`.
    """
    return {
        "name": f"project/{UNRESOLVED_PROJECT}",
        "project": UNRESOLVED_PROJECT,
        "location": GLOBAL_LOCATION,
        "outcome": "gate-failed",
        "error": (
            "No GCP project resolved from --project-id, MONITORED_PROJECT_IDS, "
            "`gcloud projects list`, GCP_PROJECT_ID/GKE_PROJECT_ID/PROJECT_ID "
            "or `gcloud config get-value project`."
        ),
    }


def collect_fleet(
    project: str | None = None,
    *,
    run: RunFn = default_run,
    max_workers: int = MAX_WORKERS,
) -> dict:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    projects = get_target_projects(project, run=run)
    log(f"auditing {len(projects)} project(s)")

    entries: list[dict] = [{} for _ in projects]
    if projects:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(collect_project, p, run=run): i for i, p in enumerate(projects)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    entries[index] = future.result()
                except Exception as exc:  # noqa: BLE001 — see crashed_entry
                    entries[index] = crashed_entry(projects[index], exc)
    else:
        entries = [unresolved_entry()]

    return {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": AUDIT_ID,
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-id",
        help="single project to audit; omit to sweep MONITORED_PROJECT_IDS/`gcloud projects list`/GCP_PROJECT_ID",
    )
    parser.add_argument(
        "--output",
        help="also write the manifest here; it goes to stdout either way",
    )
    args = parser.parse_args(argv)
    manifest = collect_fleet(args.project_id)
    text = json.dumps(manifest, indent=2)
    if args.output:
        try:
            directory = os.path.dirname(os.path.abspath(args.output))
            os.makedirs(directory, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            log(f"failed to write {args.output}: {exc}")
            return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
