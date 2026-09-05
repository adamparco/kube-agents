# SOP: GCP Networking Fabric & VPC IPAM Audit (Daily Governance)

**Purpose:** Sweep all managed VPC networks, subnets, Cloud NAT gateways, Private Service Connect (PSC) endpoints, and Cloud Armor security policies across target GCP projects for subnet IP exhaustion, NAT port allocation saturation, PSC routing deadlocks, MTU fragmentation mismatches, and Cloud Armor policy anomalies. The question this audit answers for a platform admin is: _which subnets are running out of secondary IP ranges for GKE Pods, where are Cloud NAT gateways dropping connections due to port exhaustion, and which VPCs have MTU mismatches causing packet fragmentation?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `gcp-networking-fabric-audit`, schedule `0 8 * * *` (daily 08:00 UTC).

**Data sources:** `gcloud compute networks ...`, `gcloud compute routers ...`, `gcloud compute forwarding-rules ...`, and `gcloud compute security-policies ...` across all managed fleet projects (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`).

---

## Execution Checklist

### 0. Open the audit run

```bash
python3 ./skills/fleet-audit/scripts/audit_report.py start --audit gcp-networking-fabric-audit [--repo "<owner>/<repo>"]
```

If multiple repositories are registered in `$GITOPS_STATE_CONFIGMAP` (`managed_repos`), pass `--repo "<owner>/<repo>"` explicitly:

- **Interactive session:** If no `--repo` was specified, prompt the user to choose which repository to target before proceeding.
- **Scheduled / unattended cron:** Iterate over all repositories in `managed_repos` in sequence, executing the audit and running `audit_report.py start` and `audit_report.py finish` for each repository with `--repo "<owner>/<repo>"`.

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
python3 ./skills/gcp-networking-fabric-audit/scripts/networking_audit.py > /opt/data/scratch/manifest_gcp-networking-fabric-audit.json
```

This stream's targets are GCP compute resources, not GKE clusters, so its collector is its own script rather than `fleet-audit`'s `collect.py` — see the script's own module docstring for the field contracts it assumes of each `gcloud` command's JSON. It sweeps every project named by `MONITORED_PROJECT_IDS`/`GCP_PROJECT_ID` on its own; pass `--project <id>` only to scope a run to one project. Read the manifest before doing anything else:

- Every entry in `manifest.clusters` is one target — a subnet (`<project>/<region>/<subnet>`, `subnet-ip-exhaustion` only) or a project (`project/<project>`, the other four checks) — carrying one `outcome`. `"collected"` means every check that applies to that target already ran; do not re-run it by hand. `"gate-failed"` means one of that target's `gcloud` reads failed; fall back to this section's commands for that target alone.
- **A third target shape appears only on failure: `project/<project>/subnets`, always `"gate-failed"`.** It stands in for that project's whole subnet scope when the enumeration itself could not be read, so there are no per-subnet entries to carry the failure. Put it in `scope.skipped` with its `error` as the reason. Leaving it out reports the project as holding no subnets, which is the one reading the collector emits this entry to prevent — and its name is not a real subnet, so it does not belong in `scope.clusters`.
- For a `"collected"` target, copy its `commands` list into that target's `checks_run` — minus any entry whose `check` that same target also lists in `checks_not_applicable`. A `commands` entry records that a command ran, not that the check reached a verdict on that target, so one read is routinely recorded against slugs it could not answer for; `finish` rejects a `checks_run` naming a slug the collector declared inapplicable there. Copy that target's `checks_not_applicable` and its `limitations` string verbatim too.
- **A target owes only the checks its own shape carries, and the collector has already declared the rest.** A subnet target owes `subnet-ip-exhaustion` and nothing else; the other four are the project target's. Do not add `checks_not_applicable` entries of your own saying so — a row per subnet per project-scoped check is 168 constant rows on this fleet, all of them restating the target shape §1 just gave you, and every one renders under _Not applicable_ where it buries the handful of exclusions a reader needs to see. The list you publish is the collector's, unchanged: copy it, and where a target has none, publish none.
- **Where the collector did leave a subnet unmeasured, `checks_run` comes out `[]` and that is the correct shape.** Nothing survives the filter, and `finish` accepts it because the collector wrote that target a `limitations` string saying why. Do not reinstate the command to avoid the empty list, and do not reword the `limitations`: leaving both as the collector wrote them is what keeps the gap honest.
- Every entry in a `"collected"` target's `candidates` is a verified finding: `check`, `object`, `severity`, and `excerpt` are already computed. What is still yours to write is the `recommendation` and, for a `kind: manifest` remediation, the manifest or Terraform file itself (§3).
- Pass `--manifest-file <path>` to `finish` (§5) so it cross-checks your `checks_run` against what the collector actually ran.

#### 2.1 Subnet primary and secondary IP range exhaustion (`subnet-ip-exhaustion`)

- **Command:** `gcloud compute networks subnets list-usable --project=$PROJECT --format=json` to enumerate the subnets, and the Network Analyzer read below to measure them. Record both, joined with `&&`, as this check's command on every subnet target — the enumeration on its own reproduces nothing (see the next bullet), and a command a reader cannot re-run to reach the same figure is the one thing `checks_run` exists to prevent.
- **Flag when:** the subnet's primary range, or any entry in `secondaryIpRanges`, carries `ipUtilization > 0.85` — under 15% of that range's addresses remain.
- **`list-usable` never carries `ipUtilization`, so it cannot answer this check on its own.** The field is absent from gcloud's `UsableSubnetwork` in v1, `beta` and `alpha` alike — an install can return every subnet and still have nothing to measure. The measurement lives in Network Analyzer, and the collector reads it from there, writing each ratio onto the `ipUtilization` field above so the threshold is unchanged:
  `gcloud recommender insights list --project=$PROJECT --location=global --insight-type=google.networkanalyzer.vpcnetwork.ipAddressInsight --format=json`
  Within `content.ipUtilizationSummaryInfo[].networkStats[].subnetStats[]`, each `subnetRangeStats` entry carries `allocationRatio`; **the entry with no `subnetRangeName` is the primary range** and every named entry is the secondary range of that name. Requires `recommender.googleapis.com` and `recommender.networkAnalyzerIpAddressInsights.list`. Do not use `google.compute.subnetwork.IpUtilizationInsight` — it reads plausibly, but it is not a real insight type and the API rejects it with `INVALID_ARGUMENT`.
- **A subnet the insight omits entirely is a subnet holding nothing, and the collector records it at 0%.** Network Analyzer omits a subnet with no allocation, so on an auto-mode network 41 of 42 regional `default` subnets are absent from it and every one of them is empty. Absence is the reading, not the lack of one — the collector zero-fills them once the insight has published for that project at all, and they carry an ordinary `checks_run`. The same holds one level down: a **secondary** range the insight never names inside a subnet it did cover is an empty range, and the collector zero-fills that too. Three absences it will not zero-fill, and reports unmeasured instead: a subnet in a Shared VPC host project, which `list-usable` reaches across and the insight never mentions; a **primary** range the insight did not publish, which nothing in that evidence speaks to; and a range the insight did name but whose `allocationRatio` will not parse, which is a read that failed rather than a zero. Report those as the collector wrote them and flag nothing. Never substitute `ipCidrRange` alone, which gives a range's size and says nothing about how much of it is used.
- **Do NOT flag:** a primary or secondary range at or under 85% utilization.
- **Severity:** `critical`.
- **Impact:** "New pods or nodes cannot be scheduled once this range's addresses run out, and GKE has no way to expand a live cluster's Pod CIDR after creation."
- **Remediation:** `kind: manifest`. Expand the subnet's primary CIDR or allocate an additional secondary IP range in the Terraform VPC definition.

#### 2.2 Cloud NAT gateway port allocation saturation (`cloud-nat-exhaustion`)

- **Command:** `gcloud compute routers get-nat-mapping-info $ROUTER --nat-name=$NAT --region=$REGION --project=$PROJECT --format=json`, corroborated by `routers list` (each NAT's `natIpAllocateOption`/`maxPortsPerVm`) and `routers get-status` (`result.natStatus[].autoAllocatedNatIps`).
- **`--nat-name` is not optional.** Unfiltered, `get-nat-mapping-info` returns every VM behind every gateway on the router. Compared against each gateway's own ceiling in turn, that measures one gateway's VMs against another's limit: a VM drawing 4096 ports from a dynamic gateway reads as 6400% of a static gateway's 64. Read once per gateway.
- **Flag when:** a NAT gateway is `AUTO_ONLY` with no auto-allocated external IP at all, or — **only where `enableDynamicPortAllocation` is on** — any VM's `interfaceNatMappings[].numTotalNatPorts` is `>= 80%` of that NAT's `maxPortsPerVm`. **A NAT that never overrode the field has no field:** `routers list` omits `maxPortsPerVm`, and the ceiling is then GCP's default of 65536. Use the default rather than passing over the gateway, which reads as clearing it.
- **Never measure a static gateway's ports.** With dynamic port allocation off, Cloud NAT reserves each VM exactly `minPortsPerVm`, so `numTotalNatPorts` _is_ the ceiling, the ratio is the constant 1.0, and every VM behind every stock gateway clears the 80% bar. Flagging on it reports `critical` port exhaustion fleet-wide, daily, on an install with no exhaustion anywhere. A static gateway that has genuinely run out shows up as a VM with no mapping at all — indistinguishable here from a VM that is simply idle, so report nothing rather than inventing a ratio.
- **Do NOT flag:** a `MANUAL` NAT IP allocation that still has addresses assigned; a VM under 80% of its port ceiling; any VM behind a gateway with dynamic port allocation off.
- **Severity:** `critical`.
- **Impact:** "VMs that exhaust their NAT port allocation see new outbound connections silently fail, which for a GKE node means pods lose egress with no error at the workload layer."
- **Remediation:** `kind: manifest`. Raise `maxPortsPerVm`, or add NAT IP addresses to the Cloud Router specification in Terraform. A port finding only ever names a dynamic-allocation gateway, so `minPortsPerVm` is never the ceiling that was breached.

#### 2.3 Private Service Connect endpoint routing deadlock (`psc-routing-deadlock`)

- **Command:** `gcloud compute forwarding-rules list --project=$PROJECT --format=json`
- **List unfiltered and select in the check.** `--filter="target:ServiceAttachment"` asks gcloud's `:` operator to match a plural, differently-cased substring inside a URL, and the check re-tests `serviceAttachments` in the target anyway. The filter therefore buys nothing, and if its semantics ever shift it returns an empty list, which reads as `CLEAN` rather than as an error — a blind check that reports the same word as a healthy one.
- **Flag when:** a forwarding rule targeting a Private Service Connect service attachment carries `pscConnectionStatus: REJECTED` or `pscConnectionStatus: CLOSED`.
- **Do NOT flag:** a PSC forwarding rule in `ACCEPTED` status; a forwarding rule whose target is not a service attachment at all.
- **Severity:** `major`.
- **Impact:** "Traffic aimed at this Private Service Connect endpoint cannot reach its target service; consumers see connection failures with no signal at the VPC layer."
- **Remediation:** `kind: manual`. Repair the target service attachment reference or update the forwarding rule's routing in Terraform — the correct target is a fact about the producer service this audit cannot read.

#### 2.4 VPC network MTU packet fragmentation mismatch (`mtu-packet-fragmentation`)

- **Command:** `gcloud compute networks list --project=$PROJECT --format=json`
- **Flag when:** two networks are joined by an `ACTIVE` VPC peering and their MTUs differ. This is a mismatch between two peered networks, never an absolute threshold — a single network's own MTU (1460, 1500, or otherwise) is a choice, not a defect, and packets only fragment where two different choices meet at a peering.
- **A missing `mtu` key means 1460, not unknown.** `networks list` omits the field on every network still at the default, so treating absence as unreadable skips the pair and leaves the check unable to fire on the mismatch that actually happens: a default network peered with one raised to 8896. Both sides would have to have been overridden, to different values, before that reading saw anything at all.
- **Do NOT flag:** a peering that is not `ACTIVE`; two peered networks at the same MTU, whatever the shared value is; a network with no peerings at all; a peering whose other end is not in this listing — a VPC in another project is genuinely unread, and defaulting it to 1460 would invent a mismatch.
- **Severity:** `major`.
- **Impact:** "Packets crossing this peering at the larger MTU get fragmented or dropped, which shows up as intermittent, hard-to-diagnose latency and retransmits rather than a clean failure."
- **Remediation:** `kind: manual`. Align both networks' MTU to the smaller of the two, or to 1500 if the larger side can be raised — either changes a network's core configuration, which this audit does not have enough context to propose automatically.

#### 2.5 Cloud Armor security policy evaluation anomalies (`cloud-armor-false-positive`)

- **Command:** `gcloud compute security-policies list --project=$PROJECT --format=json`, cross-referenced against `gcloud compute backend-services list --project=$PROJECT --format=json` to find which policies protect a production-looking backend.
- **Flag when:** a security policy attached to at least one production-looking backend service carries a rule in `preview` mode (excluding GCP's implicit default rule at priority `2147483647`), or the policy has two or more rules sharing one `priority`.
- **Do NOT flag:** a policy attached only to backends whose name contains a non-production token (`test`, `staging`, `stage`, `dev`, `sandbox`, `qa`); a policy attached to no backend service at all, which governs no traffic; the implicit default rule's own priority collision with itself. The production-backend condition governs both limbs above, the priority collision as much as the preview rule — an unenforced policy is a housekeeping note, not a finding this stream publishes.
- **Severity:** `minor`.
- **Impact:** "A preview-mode rule on a production backend logs matches without enforcing them, so the WAF looks like it is protecting traffic it is only observing; conflicting priorities make the effective policy unpredictable."
- **Remediation:** `kind: manual`. Take the validated rule out of preview mode and resolve the conflicting priorities — which of two colliding rules should win is a policy-intent judgment this audit cannot make.

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/vpc/subnets.tf`).
- **The Declaration Rule in the fleet-audit skill decides where the file goes** — for an object the repo already declares and for one it does not yet. Follow it rather than a rule of your own. This audit's targets are projects and subnets, so the sibling that proves a directory is reconciled is another declaration governing that same project: a Config Connector `Compute*` resource, or the Terraform file that already describes the network, subnets, or gateways you are fixing. Find no sibling for the project and the finding is `kind: manual`.
- Never write to a directory outside the reconciled GitOps hierarchy. Creating an object the repo does not yet declare is permitted and is what makes a finding resolvable by a pull request; inventing the _directory_ to put it in is not, because the repository is reconciled over a fixed set of paths and a file outside them is applied by nothing.

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
            "command": "gcloud compute forwarding-rules list --project=proj-1 --format=json"
          },
          {
            "check": "mtu-packet-fragmentation",
            "command": "gcloud compute networks list --project=proj-1 --format=json"
          },
          {
            "check": "cloud-armor-false-positive",
            "command": "gcloud compute security-policies list --project=proj-1 --format=json"
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
      "namespace": "",
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
  --manifest-file /opt/data/scratch/manifest_gcp-networking-fabric-audit.json \
  [--repo "<owner>/<repo>"]
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true,"chat_summary":"...","inspect_s":214.0,"publish_s":41.5}
```

`--manifest-file` is required and `finish` refuses to publish without it, because nothing else checks the document against what the collector actually ran. On a run where §2's collector never produced one, pass `--no-collector-manifest '<why>'` instead; it publishes but reports the reason as a coverage gap, so the run is partial. Given a manifest, `finish` rejects a `checks_run` entry on a `"collected"` target that names a check the manifest never recorded at `rc == 0`, and rejects a `"collected"` target the document leaves out of `scope.clusters` altogether.

- On a **scheduled** run, your entire final response is `chat_summary`, copied verbatim from the JSON with nothing before it and nothing after it. On `silent_ok: true` that string is exactly `[SILENT]`, so obeying the flag and copying the field are the same act; on anything else it is the one line, already carrying the counts, the delta, and `issue_url`. Silence is a message not sent, never a message about silence: do not preface the marker, quote it inside a sentence, restate `silent_ok`, or explain that the run is staying quiet — a response that describes its own silence has already spoken. Nor announce the copying: a run that opens `Per the skill's instructions my entire final response must be chat_summary copied verbatim` and then prints the line has put two sentences in the channel ahead of the one that was wanted, and quoting the rule is not following it.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never delete VPC subnets, modify live firewall rules, or tear down NAT gateways directly.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git/GitHub write path.
- **Never print raw credentials.** Secret tokens, certificates, private keys, and authorization headers must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`Subnet/<name>`, `Router/<region>/<name>`, `ForwardingRule/<region|global>/<name>`), never an ephemeral execution timestamp. Regional resources carry their region because the four project-scoped checks all report against one target, `project/<project>`, where two same-named routers in two regions would otherwise be one finding identity and `finish` would refuse the document.
- **Never emit a manifest that directly deletes a network or subnet.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
