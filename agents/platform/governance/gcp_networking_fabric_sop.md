# SOP: GCP Networking Fabric & VPC IPAM Audit (Daily Governance)

**Purpose:** Sweep all managed VPC networks, subnets, Cloud NAT gateways, Private Service Connect (PSC) endpoints, and Cloud Armor security policies across target GCP projects for subnet IP exhaustion, NAT port allocation saturation, PSC routing deadlocks, MTU fragmentation mismatches, and Cloud Armor policy anomalies. The question this audit answers for a platform admin is: _which subnets are running out of secondary IP ranges for GKE Pods, where are Cloud NAT gateways dropping connections due to port exhaustion, and which VPCs have MTU mismatches causing packet fragmentation?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `gcp-networking-fabric-audit`, schedule `0 8 * * *` (daily 08:00 UTC).

**Data sources:** `gcloud compute networks ...`, `gcloud compute routers ...`, `gcloud compute forwarding-rules ...`, and `gcloud compute security-policies ...` across all managed fleet projects (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`).

---

## Execution Checklist

### 0. Open the audit run

```bash
python3 ./skills/fleet-audit/scripts/audit_report.py start --audit gcp-networking-fabric-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/gcp-networking-fabric-audit/org__repo", "findings_path":"/opt/data/scratch/findings_gcp-networking-fabric-audit.json", "pending_remediation_requests": [<finding_id>, ...]}`.

If `pending_remediation_requests` is non-empty, inspect each requested finding in the open issue and write the updated manifest or Terraform file to `workspace` at `remediation.path` before proceeding to step 3 (`finish`).

### 1. Enumerate the target fleet

```bash
gcloud compute networks subnets list --format=json
```

- Target every VPC subnet across fleet projects. Record `{name, location, project, checks_run}` into `scope.clusters`, formatting `name` as unique `<project>/<region>/<subnet>` (or project-scoped target `project/<project-id>`).
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that target.
- A project or target you cannot reach goes in `scope.skipped` with a reason string. If a target is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

**Run the collector before evaluating any check below by hand.**

```bash
./skills/gcp-networking-fabric-audit/scripts/networking_audit.py > /opt/data/scratch/manifest_gcp-networking-fabric-audit.json
```

This stream's targets are GCP compute resources, not GKE clusters, so its collector is its own script rather than `fleet-audit`'s `collect.py` — see the script's own module docstring for the field contracts it assumes of each `gcloud` command's JSON. It sweeps every project named by `MONITORED_PROJECT_IDS`/`GCP_PROJECT_ID` on its own; pass `--project <id>` only to scope a run to one project. Read the manifest before doing anything else:

- Every entry in `manifest.clusters` is one target — a subnet (`<project>/<region>/<subnet>`, `subnet-ip-exhaustion` only) or a project (`project/<project>`, the other four checks) — carrying one `outcome`. `"collected"` means every check that applies to that target already ran; do not re-run it by hand. `"gate-failed"` means one of that target's `gcloud` reads failed; fall back to this section's commands for that target alone.
- For a `"collected"` target, copy its `commands` list into that target's `checks_run` — minus any entry whose `check` that same target also lists in `checks_not_applicable`. A `commands` entry records that a command ran, not that the check reached a verdict on that target, so one read is routinely recorded against slugs it could not answer for; `finish` rejects a `checks_run` naming a slug the collector declared inapplicable there. Copy that target's `checks_not_applicable` and its `limitations` string verbatim too.
- **On an auto-mode network this empties `checks_run` for most subnets, and that is the correct shape.** A subnet Network Analyzer did not measure owes only `subnet-ip-exhaustion` and has it declared not-applicable, so nothing survives the filter and `checks_run` is `[]` — which `finish` accepts because the collector wrote that target a `limitations` string saying why. Do not reinstate the command to avoid the empty list, and do not reword the `limitations`: leaving both as the collector wrote them is what keeps the run off `partial`.
- Every entry in a `"collected"` target's `candidates` is a verified finding: `check`, `object`, `severity`, and `excerpt` are already computed. What is still yours to write is the `recommendation` and, for a `kind: manifest` remediation, the manifest or Terraform file itself (§3).
- Pass `--manifest-file <path>` to `finish` (§5) so it cross-checks your `checks_run` against what the collector actually ran.

#### 2.1 Subnet primary and secondary IP range exhaustion (`subnet-ip-exhaustion`)

- **Command:** `gcloud compute networks subnets list-usable --project=$PROJECT --format=json` to enumerate the subnets, and the Network Analyzer read below to measure them. Record both, joined with `&&`, as this check's command on every subnet target — the enumeration on its own reproduces nothing (see the next bullet), and a command a reader cannot re-run to reach the same figure is the one thing `checks_run` exists to prevent.
- **Flag when:** the subnet's primary range, or any entry in `secondaryIpRanges`, carries `ipUtilization > 0.85` — under 15% of that range's addresses remain.
- **`list-usable` never carries `ipUtilization`, so it cannot answer this check on its own.** The field is absent from gcloud's `UsableSubnetwork` in v1, `beta` and `alpha` alike — an install can return every subnet and still have nothing to measure. The measurement lives in Network Analyzer, and the collector reads it from there, writing each ratio onto the `ipUtilization` field above so the threshold is unchanged:
  `gcloud recommender insights list --project=$PROJECT --location=global --insight-type=google.networkanalyzer.vpcnetwork.ipAddressInsight --format=json`
  Within `content.ipUtilizationSummaryInfo[].networkStats[].subnetStats[]`, each `subnetRangeStats` entry carries `allocationRatio`; **the entry with no `subnetRangeName` is the primary range** and every named entry is the secondary range of that name. Requires `recommender.googleapis.com` and `recommender.networkAnalyzerIpAddressInsights.list`. Do not use `google.compute.subnetwork.IpUtilizationInsight` — it reads plausibly, but it is not a real insight type and the API rejects it with `INVALID_ARGUMENT`.
- **A subnet the insight does not cover is unmeasured, not healthy.** Network Analyzer omits subnets holding no allocations, so an auto-mode network reports one measured subnet and 41 untouched ones. The collector marks those `subnet-ip-exhaustion` not-applicable per target rather than passing them; report them that way and flag nothing. Never substitute `ipCidrRange` alone, which gives a range's size and says nothing about how much of it is used.
- **Do NOT flag:** a primary or secondary range at or under 85% utilization.
- **Severity:** `critical`.
- **Impact:** "New pods or nodes cannot be scheduled once this range's addresses run out, and GKE has no way to expand a live cluster's Pod CIDR after creation."
- **Remediation:** `kind: manifest`. Expand the subnet's primary CIDR or allocate an additional secondary IP range in the Terraform VPC definition.

#### 2.2 Cloud NAT gateway port allocation saturation (`cloud-nat-exhaustion`)

- **Command:** `gcloud compute routers get-nat-mapping-info $ROUTER --region=$REGION --project=$PROJECT --format=json`, corroborated by `routers list` (each NAT's `natIpAllocateOption`/`maxPortsPerVm`) and `routers get-status` (`result.natStatus[].autoAllocatedNatIps`).
- **Flag when:** a NAT gateway is `AUTO_ONLY` with no auto-allocated external IP at all, or any VM's `interfaceNatMappings[].numTotalNatPorts` is `>= 80%` of that NAT's configured port ceiling (`maxPortsPerVm` when dynamic port allocation is on, `minPortsPerVm` otherwise).
- **Do NOT flag:** a `MANUAL` NAT IP allocation that still has addresses assigned; a VM under 80% of its port ceiling.
- **Severity:** `critical`.
- **Impact:** "VMs that exhaust their NAT port allocation see new outbound connections silently fail, which for a GKE node means pods lose egress with no error at the workload layer."
- **Remediation:** `kind: manifest`. Increase `minPortsPerVm` (or `maxPortsPerVm` under dynamic allocation) or add additional NAT IP addresses to the Cloud Router specification in Terraform.

#### 2.3 Private Service Connect endpoint routing deadlock (`psc-routing-deadlock`)

- **Command:** `gcloud compute forwarding-rules list --filter="target:ServiceAttachment" --project=$PROJECT --format=json`
- **Flag when:** a forwarding rule targeting a Private Service Connect service attachment carries `pscConnectionStatus: REJECTED` or `pscConnectionStatus: CLOSED`.
- **Do NOT flag:** a PSC forwarding rule in `ACCEPTED` status; a forwarding rule whose target is not a service attachment at all.
- **Severity:** `major`.
- **Impact:** "Traffic aimed at this Private Service Connect endpoint cannot reach its target service; consumers see connection failures with no signal at the VPC layer."
- **Remediation:** `kind: manual`. Repair the target service attachment reference or update the forwarding rule's routing in Terraform — the correct target is a fact about the producer service this audit cannot read.

#### 2.4 VPC network MTU packet fragmentation mismatch (`mtu-packet-fragmentation`)

- **Command:** `gcloud compute networks list --project=$PROJECT --format=json`
- **Flag when:** two networks are joined by an `ACTIVE` VPC peering and their `mtu` values differ. This is a mismatch between two peered networks, never an absolute threshold — a single network's own MTU (1460, 1500, or otherwise) is a choice, not a defect, and packets only fragment where two different choices meet at a peering.
- **Do NOT flag:** a peering that is not `ACTIVE`; two peered networks whose `mtu` values agree, whatever the shared value is; a network with no peerings at all.
- **Severity:** `major`.
- **Impact:** "Packets crossing this peering at the larger MTU get fragmented or dropped, which shows up as intermittent, hard-to-diagnose latency and retransmits rather than a clean failure."
- **Remediation:** `kind: manual`. Align both networks' MTU to the smaller of the two, or to 1500 if the larger side can be raised — either changes a network's core configuration, which this audit does not have enough context to propose automatically.

#### 2.5 Cloud Armor security policy evaluation anomalies (`cloud-armor-false-positive`)

- **Command:** `gcloud compute security-policies list --project=$PROJECT --format=json`, cross-referenced against `gcloud compute backend-services list --project=$PROJECT --format=json` to find which policies protect a production-looking backend.
- **Flag when:** a security policy attached to at least one production-looking backend service carries a rule in `preview` mode (excluding GCP's implicit default rule at priority `2147483647`), or the policy has two or more rules sharing one `priority`.
- **Do NOT flag:** a policy attached only to backends whose name contains a non-production token (`test`, `staging`, `stage`, `dev`, `sandbox`, `qa`); the implicit default rule's own priority collision with itself.
- **Severity:** `minor`.
- **Impact:** "A preview-mode rule on a production backend logs matches without enforcing them, so the WAF looks like it is protecting traffic it is only observing; conflicting priorities make the effective policy unpredictable."
- **Remediation:** `kind: manual`. Take the validated rule out of preview mode and resolve the conflicting priorities — which of two colliding rules should win is a policy-intent judgment this audit cannot make.

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/vpc/subnets.tf`).
- Never invent phantom paths or write manifests to directories outside the reconciled GitOps hierarchy.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "gcp-networking-fabric-audit"`, `scope.clusters` listing every target you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that target's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

`command` in `checks_run` is the literal inspection command executed, and anything under eight characters is rejected.

Every finding must conform to the full findings schema:

```json
{
  "audit": "gcp-networking-fabric-audit",
  "scope": {
    "clusters": [
      {
        "name": "proj-1/us-central1/gke-pods-subnet",
        "location": "us-central1",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "subnet-ip-exhaustion",
            "command": "gcloud compute networks subnets list-usable --project=proj-1 --format=json && gcloud recommender insights list --project=proj-1 --location=global --insight-type=google.networkanalyzer.vpcnetwork.ipAddressInsight --format=json"
          }
        ],
        "checks_not_applicable": [
          {
            "check": "cloud-nat-exhaustion",
            "reason": "NAT gateways are configured at the Cloud Router level, not per subnet."
          },
          {
            "check": "psc-routing-deadlock",
            "reason": "Private Service Connect endpoints are project-level resources, not subnet resources."
          },
          {
            "check": "mtu-packet-fragmentation",
            "reason": "VPC network MTU is defined at the VPC level, not per subnet."
          },
          {
            "check": "cloud-armor-false-positive",
            "reason": "Cloud Armor security policies are backend service resources, not subnet resources."
          }
        ]
      },
      {
        "name": "project/proj-1",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "cloud-nat-exhaustion",
            "command": "gcloud compute routers get-nat-mapping-info ROUTER --region=us-central1 --project=proj-1 --format=json"
          },
          {
            "check": "psc-routing-deadlock",
            "command": "gcloud compute forwarding-rules list --filter=\"target:ServiceAttachment\" --project=proj-1 --format=json"
          },
          {
            "check": "mtu-packet-fragmentation",
            "command": "gcloud compute networks list --project=proj-1 --format=json"
          },
          {
            "check": "cloud-armor-false-positive",
            "command": "gcloud compute security-policies list --project=proj-1 --format=json"
          }
        ],
        "checks_not_applicable": [
          {
            "check": "subnet-ip-exhaustion",
            "reason": "Subnet IP capacity is audited per individual subnet scope entry."
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": [
    {
      "check": "subnet-ip-exhaustion",
      "severity": "critical",
      "title": "Subnet gke-pods-subnet has < 10% secondary IP addresses remaining",
      "cluster": "proj-1/us-central1/gke-pods-subnet",
      "namespace": "default",
      "object": "Subnet/gke-pods-subnet",
      "impact": "Pod scheduling will fail when secondary IP allocation is exhausted.",
      "evidence": {
        "command": "gcloud compute networks subnets describe gke-pods-subnet --region=us-central1 --project=proj-1 --format=json",
        "excerpt": "ipCidrRange: 10.0.0.0/20"
      },
      "recommendation": {
        "action": "Add an additional secondary IP range to gke-pods-subnet in Terraform.",
        "rationale": "Prevents pod provisioning stockouts during horizontal scaling.",
        "risk": "Requires cluster pod CIDR expansion."
      },
      "remediation": {
        "kind": "manifest",
        "path": "terraform/modules/vpc/subnets.tf"
      }
    }
  ]
}
```

### 5. Close the audit run

```bash
python3 ./skills/fleet-audit/scripts/audit_report.py finish --audit gcp-networking-fabric-audit \
  --findings-file /opt/data/scratch/findings_gcp-networking-fabric-audit.json \
  --manifest-file /opt/data/scratch/manifest_gcp-networking-fabric-audit.json
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true,"inspect_s":214.0,"publish_s":41.5}
```

`--manifest-file` is required and `finish` refuses to publish without it, because nothing else checks the document against what the collector actually ran. On a run where §2's collector never produced one, pass `--no-collector-manifest '<why>'` instead; it publishes but reports the reason as a coverage gap, so the run is partial. Given a manifest, `finish` rejects a `checks_run` entry on a `"collected"` target that names a check the manifest never recorded at `rc == 0`, and rejects a `"collected"` target the document leaves out of `scope.clusters` altogether.

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never delete VPC subnets, modify live firewall rules, or tear down NAT gateways directly.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git/GitHub write path.
- **Never print raw credentials.** Secret tokens, certificates, private keys, and authorization headers must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`Subnet/<name>`, `Router/<name>`), never an ephemeral execution timestamp.
- **Never emit a manifest that directly deletes a network or subnet.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
