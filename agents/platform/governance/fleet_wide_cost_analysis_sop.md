# SOP: Fleet Waste Audit (Weekly Governance)

**Purpose:** Weekly sweep of the GKE fleet for resources that cost money and do nothing — orphaned storage, idle network reservations, under-allocated node pools, grossly over-requested workloads, and the scale-down blockers that pin them. The output is one ledger issue of reproducible findings, not a dashboard.

**Cron:** id `fleet-wide-cost-analysis`, schedule `50 7 * * 1` (Mondays, 07:50 UTC).

**Data sources:** `kubectl` read verbs (including `kubectl top`), `gcloud compute` / `gcloud container`, and the `gke` MCP server. **Nothing else.** This environment has no GCP Billing BigQuery export, no GKE Cost Allocation data, no Recommender API, and no Prometheus — so this audit observes _physical_ waste directly and reports it in **resource units** (GiB, vCPU, disk count, node count, object count). **Never state a dollar amount, a monthly saving, or a percentage saving — you have no pricing data.** Where a price would help the reviewer decide, say so in the remediation note and point at the GCP console; never invent the number. (`list_cc_pods` / `get_cc_pod_diagnostics` are scoped to `krmapihosting-system` on the Config Controller management cluster and are not used by this audit.)

---

## Execution Checklist

### 0. Open the audit run

Run `./skills/fleet-audit/scripts/audit_report.py start --audit fleet-wide-cost-analysis`. It prints one JSON line:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "findings_path": "/opt/data/scratch/findings_fleet-wide-cost-analysis.json",
  "pending_remediation_requests": [
    "overrequest--prod-us-east--deployment-payments-api"
  ]
}
```

Keep the returned `findings_path`; write findings there and nowhere else. `issue` is this stream's open ledger issue, or `null` when it has none — either way `finish` opens or rewrites it. `pending_remediation_requests` names findings a repo writer asked for with a `/remediate` comment on the ledger: write the manifest for each of those during inspection, **unless** the finding's remediation is `gcloud` or `manual` — those are not promotable, the helper refuses the request with its own comment, and you never manufacture a manifest (least of all a deletion) to satisfy one.

There is no report branch. Do not create branches, commit, push, or call `gh` yourself, and never hand-write the ledger issue body — the helper renders it.

### 1. Enumerate the target fleet

1. `gcloud config get-value project`, then `gcloud container clusters list --project=<p> --format="json(name,location,status,autopilot.enabled,currentNodeCount)"` for every project the agent can see.
2. `scope.clusters` = every cluster with `status: RUNNING` that you could read. Record every cluster you could **not** read in `scope.skipped` with its status or the stderr excerpt as the reason (`"cluster STOPPING"`, `"no kubeconfig: <stderr excerpt>"`).

   **The one-question scope rule.** A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; if some checks did not run there, name them in that cluster's `limitations`. No → `scope.skipped`. Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator enforces all three.

3. Get credentials per cluster: `gcloud container clusters get-credentials <c> --location=<l> --project=<p>`. On failure, skip that cluster with the stderr excerpt as the reason and continue — one unreachable cluster never aborts the run.
4. **If zero clusters are RUNNING and reachable, do not call `finish`** — the helper hard-fails on an empty `scope.clusters`. A fleet you could not read is not a clean fleet, so this is **not** a `[SILENT]` run: report the enumeration failure as your one-line summary and stop.

### 2. Collect state

Collect once per cluster into `/opt/data/scratch/`, then run all checks against the dumps — never re-query per finding, and quote from these dumps as `evidence.excerpt`.

- Per cluster: `kubectl get nodes,pods,pvc,pv,svc,jobs,cronjobs,pdb,ns,resourcequota -A -o json`, plus `gcloud container node-pools list --cluster=<c> --location=<l> --format=json`.
- Per project: `gcloud compute disks list`, `addresses list`, `forwarding-rules list`, `target-pools list`, `backend-services list` (all `--format=json`).
- **Usage samples:** take three `kubectl top pods -A --no-headers` + `kubectl top nodes --no-headers` samples, ~5 minutes apart (`sleep 300` between). Save each as `top-<cluster>-<n>.txt`.
- **Metrics degradation:** if `kubectl top nodes` exits non-zero or `metrics.k8s.io` is absent from `kubectl api-resources`, the cluster still belongs in `scope.clusters` — you read it. Set `"limitations": "metrics-server unavailable: <stderr excerpt>; usage check 3.1 did not run."` on that cluster's `scope.clusters` entry and skip **only** 3.1 there. Every other check reads requests and object state, not usage, and still runs.
- **Autopilot:** where `autopilot.enabled` is true, there are no user-managed node pools — skip 3.7 and set `"limitations": "Autopilot cluster: no user-managed node pools, so check 3.7 does not apply."` on that cluster's `scope.clusters` entry. An Autopilot cluster is never a skipped cluster; you read it fine. 3.8 still applies (Autopilot scales down and PDBs still block it), and 3.1 matters _more_ (Autopilot bills on requests), so **bump 3.1 severity one level on Autopilot clusters**.

**Shared definitions used by every check below.** `SYSTEM_NS` = `kube-system`, `kube-public`, `kube-node-lease`, `gke-*`, `gmp-system`, `gke-gmp-system`, `cnrm-system`, `config-management-*`, `istio-system`, `krmapihosting-system`, `anthos-*`. `AGE(x)` = now minus `creationTimestamp`. "All samples" = all three usage samples from this run.

### 3. Checks

**Severity discipline.** Waste is not an incident. Default to `minor`; use `major` only for magnitude (roughly a node's worth of capacity, ≥500 GiB of storage, or a systemic leak). **`critical` is reserved for two conditions only:** (a) a condition that blocks node drain, which blocks both scale-down _and_ security patching; (b) a waste finding whose obvious remediation is deletion of an object holding the only copy of data — verified by `gcloud compute snapshots list --filter="sourceDisk~<disk-name>"` returning nothing. Rule (b) is a **warning to the reviewer**, not an urgency claim; say so in the impact line. Nothing else reaches `critical` — in particular the one-level Autopilot bump in 3.1 stops at `major`, because a `critical` finding whose remediation is a manifest opens a merge-ready remediation pull request automatically (§4), and a right-size patch derived from a ten-minute sample is never that urgent.

**Finding ids must be stable across runs.** Format: `<check-id>--<cluster-or-project>--<object>`, lowercased, every non-alphanumeric collapsed to `-`, truncated to 100 chars, then any leading or trailing `-` trimmed off. The result must match `^[a-z0-9][a-z0-9._-]{0,98}[a-z0-9]$`, with no `..` segment and no `.lock` suffix: the id becomes a git branch component (`platform-agent/fix-fleet-wide-cost-analysis-<finding-id>`), so a colon, a space, a `*`, a `..`, or a `.lock` ending is rejected outright. Never embed a timestamp, a counter, a sample value, or an age in an id — the same problem must keep the same id so the helper's delta works.

**Project-scoped GCP objects.** The harness requires a non-empty `cluster` on every finding. For a disk, address, or forwarding rule that belongs to a project rather than to one cluster, set `cluster` to the literal string `project/<project-id>` and leave `namespace` empty. Attribute to a real cluster name only when the labels or the name prefix prove it.

#### 3.1 Gross over-request vs. observed usage (`overrequest`)

- **Command:** `kubectl top pods -A --no-headers` (×3, per Step 2) joined against `kubectl get pods -A -o json | jq '[.items[]|{ns:.metadata.namespace,pod:.metadata.name,owner:(.metadata.ownerReferences[0]//{}),start:.status.startTime,req:[.spec.containers[].resources.requests],lim:[.spec.containers[].resources.limits]}]'`, aggregated to the owning controller.
- **Flag when:** for a controller, **every** sample shows aggregate usage ≤ 20% of aggregate requests (use the per-container **maximum across samples**, never the mean), **and** the reclaimable delta is material: ≥ 2 vCPU **or** ≥ 4 GiB summed across its pods. _Justification:_ sane practice requests at p50–p95 with 1.3–2× headroom; a 5× gap is not headroom, it is a copy-pasted default. The absolute floor stops the audit from flagging a 10m sidecar where the percentage is arithmetically true and operationally meaningless.
- **Sampling honesty — state this in the impact line:** `kubectl top` is an instantaneous sample, not a percentile. Three samples over ~10 minutes on one Monday morning **cannot see a nightly batch peak or a weekday traffic curve**. That is why this check requires all three samples to agree, uses the peak rather than the mean, carries an absolute floor, and — see remediation — never proposes a request below 2× the observed peak. The impact line must name the sample window and date so the reviewer knows the basis.
- **Do NOT flag:** `SYSTEM_NS`; DaemonSets (their requests are per-node overhead, not a sizing choice); pods with `AGE < 1h` (cold caches, JIT warmup); anything owned by a `Job` or `CronJob` (periodic by design, sampled at the wrong moment); workloads with an HPA whose `minReplicas` floor is the real lever; `BestEffort` pods (no requests to shrink); pods in `Pending`/`Terminating`; workloads whose request equals the namespace `LimitRange` default (fix the LimitRange, not the workload — note it once per namespace instead).
- **Severity:** `major` if the reclaimable delta is ≥ 8 vCPU or ≥ 32 GiB across the controller (a node's worth); else `minor`. One level higher on Autopilot.
- **Impact:** "Deployment `ns/name` requests 12 vCPU / 48 GiB; peak observed across 3 samples on 2026-08-03 07:50–08:00 UTC was 0.9 vCPU / 3 GiB — ~11 vCPU and ~45 GiB reserved and unschedulable by anything else."
- **Remediation:** `kind: manifest` — a strategic-merge patch setting `requests` to `ceil(peak × 2)` (floor `50m` / `64Mi`), limits untouched. **Skip the manifest and emit `kind: manual` for `Guaranteed`-QoS pods** (requests == limits): lowering requests there silently changes the QoS class and the eviction ordering. Note: "confirm against a longer observation window before merging; this is a 10-minute sample."

#### 3.2 Orphaned PersistentVolumes (`orphan-pv`)

- **Command:** `kubectl get pv -o json | jq '[.items[]|select(.spec.persistentVolumeReclaimPolicy=="Retain")|{name:.metadata.name,phase:.status.phase,since:.status.lastPhaseTransitionTime,created:.metadata.creationTimestamp,cap:.spec.capacity.storage,sc:.spec.storageClassName,claim:.spec.claimRef,handle:(.spec.csi.volumeHandle//.spec.gcePersistentDisk.pdName)}]'`
- **Flag when:** phase is `Released` or `Failed` and `status.lastPhaseTransitionTime` is ≥ 7 days old; or phase is `Available` with a null `claimRef` and `AGE ≥ 30d` and no `Pending` PVC in the fleet matches its storage class. If `lastPhaseTransitionTime` is absent (pre-1.31 control plane), fall back to `AGE ≥ 30d` and say so in the evidence excerpt. _Justification:_ 7 days outlives any deploy/rollback cycle and any weekly batch job; a `Retain` PV that has sat unclaimed for a week was abandoned, not paused. 30 days for `Available` because statically provisioned PVs are legitimately pre-staged.
- **Do NOT flag:** `Delete`-policy PVs (the disk is already gone); PVs whose `claimRef` names a PVC that still exists; PVs whose claim name matches `^<vct>-<sts>-[0-9]+$` for a StatefulSet that still exists in that namespace — **a StatefulSet scaled to zero is a deliberate state, not waste**; PVs annotated by a backup system (`velero.io/*`, `gke.io/backup-*`) or carrying a retention marker; PVs provisioned for GKE-managed addons; anything younger than 7 days in the terminal phase (deletion churn).
- **Severity:** `major` if capacity ≥ 100 GiB or the storage class is SSD/extreme; else `minor`. `critical` under severity rule (b).
- **Impact:** "PV `pvc-abc123` (500 GiB, `premium-rwo`) has been `Released` with `Retain` since 2026-06-12 — the backing disk still exists and no claim can bind it."
- **Remediation:** `kind: manual`. Steps, in order: identify the backing disk from `spec.csi.volumeHandle`; `gcloud compute disks describe` it; **snapshot it** (`gcloud compute snapshots create <disk>-preaudit --source-disk=<disk> --source-disk-zone=<z>`); confirm the owning team no longer needs the data; only then delete. **Never emit a manifest that deletes a PV or PVC.**

#### 3.3 Bound PVCs with no consuming pod (`unconsumed-pvc`)

- **Command:** `kubectl get pvc -A -o json` cross-referenced against `kubectl get pods -A -o json | jq -r '.items[]|.metadata.namespace+"/"+(.spec.volumes[]?|.persistentVolumeClaim.claimName//empty)'`.
- **Flag when:** the PVC is `Bound`, no pod in **any** phase in that namespace references it across all samples, and `AGE ≥ 14d`. _Justification:_ 14 days spans a full deploy/rollback cycle and two runs of this weekly audit, so a flagged PVC has been observed unconsumed at least twice.
- **Do NOT flag:** `SYSTEM_NS`; PVCs whose name matches a live StatefulSet's `volumeClaimTemplate` pattern (scaled-to-zero StatefulSets are deliberate); PVCs referenced by a suspended CronJob's job template or a not-yet-started Job; PVCs referenced by a `VolumeSnapshot` in progress; PVCs with a `WaitForFirstConsumer` storage class that are still `Pending`; PVCs in a namespace under active GitOps sync where the workload is temporarily scaled down (`configsync.gke.io/*` annotations present).
- **Severity:** `major` if ≥ 100 GiB or an SSD class; else `minor`.
- **Impact:** "PVC `ns/data-old` (200 GiB, `standard-rwo`) is Bound with no pod mounting it since at least 2026-07-18 — 200 GiB provisioned and unread."
- **Remediation:** `kind: manual` — confirm with the namespace owner, snapshot the volume, then delete the PVC (which deletes the PV under `Delete`). Never a manifest.

#### 3.4 Unattached Compute Engine persistent disks (`unattached-disk`)

- **Command:** `gcloud compute disks list --project=<p> --filter="-users:* AND creationTimestamp<-P30D" --format="json(name,sizeGb,type,zone,region,creationTimestamp,labels,description)"`
- **Flag when:** the disk has no `users`, `AGE ≥ 30d`, and it is attributable to an audited cluster via the `goog-k8s-cluster-name` / `goog-k8s-cluster-location` labels, a `gke-<cluster>-` name prefix, or a `kubernetes.io-created-for/pv-name` entry in its description. _Justification:_ PD-CSI detaches and reattaches during node upgrades, pod rescheduling, and maintenance windows; 30 days outlives a full monthly GKE maintenance cycle, so this is not churn.
- **Do NOT flag:** disks whose name or handle matches a live PV's `spec.csi.volumeHandle` (claimed, just not currently attached); node boot disks for a pool that is mid-upgrade or mid-scale (`gcloud container operations list` shows an in-flight operation); disks with labels from a managed service (`goog-composer-*`, `goog-dataproc-*`, `goog-gke-node` on a live pool); disks in a project not in `scope.clusters`; disks not attributable to any audited cluster — **report them as `cluster: "project/<project-id>"` with an object of `Disk/<name>`, and say in the impact line that ownership could not be determined**, so a reviewer never deletes another team's disk on this audit's word.
- **Severity:** `major` if `sizeGb ≥ 500` or the type contains `ssd`/`extreme`; else `minor`. `critical` under severity rule (b).
- **Impact:** "3 unattached `pd-ssd` disks in `us-central1` totalling 1,500 GiB, unattached since 2026-05-30, all labelled for cluster `prod-usc1`."
- **Remediation:** `kind: gcloud`, printed for a human to run, never executed here: `gcloud compute disks describe` → `gcloud compute snapshots list --filter="sourceDisk~<name>"` → **snapshot if none exists** → `gcloud compute disks delete`. Note verbatim: "Verify before deleting — an unattached disk can be a deliberate cold archive. Snapshot first. Price the disk in the console if you need the figure."

#### 3.5 Reserved static IP addresses not in use (`idle-address`)

- **Command:** `gcloud compute addresses list --project=<p> --filter="status!=IN_USE" --format="json(name,address,addressType,purpose,region,status,creationTimestamp,users,description)"`
- **Flag when:** `status == RESERVED` and `AGE ≥ 14d`. _Justification:_ an IP reserved ahead of a cutover is normal for a sprint; two weeks exceeds a typical cutover window and means this audit has already seen it twice. A reserved-but-unused external IP bills continuously.
- **Do NOT flag:** addresses whose `purpose` is `VPC_PEERING`, `PRIVATE_SERVICE_CONNECT`, `NAT_AUTO`, `SHARED_LOADBALANCER_VIP`, or `IPSEC_INTERCONNECT` (legitimately "reserved" while serving); addresses named in any fleet annotation — `kubernetes.io/ingress.global-static-ip-name`, `networking.gke.io/load-balancer-ip`, `cloud.google.com/load-balancer-ip`, `networking.gke.io/addresses` — grep the Step 2 dumps for the address name and value before flagging; addresses whose description mentions DR, failover, or a planned migration.
- **Severity:** `minor` per address. If a single project holds ≥ 10 idle addresses, emit one roll-up finding at `major` instead of ten `minor` ones — that is a leak, not a leftover.
- **Impact:** "4 external addresses in `us-central1` have been `RESERVED` and unattached for 40+ days."
- **Remediation:** `kind: gcloud` — `gcloud compute addresses describe <n> --region=<r>` to confirm nothing references it, then `gcloud compute addresses delete`. Caution: "Releasing an external IP is irreversible — the address returns to the pool and cannot be reclaimed. Confirm no DNS record points at it."

#### 3.6 Orphaned load-balancer resources (`orphan-lb`)

- **Command:** `gcloud compute forwarding-rules list --project=<p> --format="json(name,IPAddress,region,target,description,creationTimestamp)"`, plus `target-pools list` and `backend-services list --format="json(name,backends,creationTimestamp)"`.
- **Flag when:** a forwarding rule's description carries a `kubernetes.io/service-name` whose `namespace/name` no longer exists anywhere in the fleet dumps and `AGE ≥ 7d`; or a target pool has zero instances; or a backend service has zero backends. _Justification:_ the GKE service controller tears LB resources down within minutes of Service deletion; a week is three orders of magnitude past normal reconcile latency and past any controller outage.
- **Do NOT flag — this is the highest-risk cross-check in the audit:** **run it only if every cluster in that project was successfully enumerated in Step 1.** If any cluster in the project sits in `scope.skipped`, skip the check for that whole project — the Service may live in the cluster you could not read — and declare the omission as `"limitations": "3.6 not run: cluster <skipped> in project <p> could not be enumerated."` on **every** cluster of that project that is in `scope.clusters`. Never add a second `scope.skipped` row for a cluster that is already in `scope.skipped`; a duplicate fails validation and publishes nothing. Also exclude: rules with no `kubernetes.io/*` description (Terraform- or human-managed); rules backing a `MultiClusterService`/`MultiClusterIngress` (the Service lives at fleet level); PSC service attachments; internal rules whose target is a live backend service.
- **Severity:** `major` — an orphaned forwarding rule keeps a load balancer and usually an IP alive.
- **Impact:** "Forwarding rule `a1b2c3` still targets deleted Service `staging/checkout-lb`; its backend pool has been empty since 2026-06-20."
- **Remediation:** `kind: gcloud` — `gcloud compute forwarding-rules describe` to confirm the target, then delete the rule, then its target pool / backend service, then release the address. Caution: "Confirm no DNS record or external client still resolves to this IP."

#### 3.7 Under-allocated node pools and non-zero autoscaler floors (`idle-nodepool`)

- **Command:** `gcloud container node-pools list --cluster=<c> --location=<l> --format="json(name,initialNodeCount,config.machineType,config.taints,autoscaling,management)"` joined against per-node request sums from `kubectl get pods -A --field-selector=status.phase=Running -o json` versus `.status.allocatable` in `kubectl get nodes -o json`.
- **Flag when:** every node in the pool shows non-DaemonSet CPU requests ≤ 15% **and** memory requests ≤ 15% of allocatable across all samples, and the pool has ≥ 1 node, and either autoscaling is disabled or `autoscaling.minNodeCount ≥ 1`. _Justification:_ measure **requests**, not usage — requests are what the cluster autoscaler acts on, so they are what determines whether the pool can shrink. 15% is below the point where DaemonSet and system overhead (typically 10–25% of a small node) dominates: a pool at ≤ 15% non-DaemonSet allocation is not carrying a workload. A `minNodeCount ≥ 1` floor on such a pool is capacity nobody asked for.
- **Do NOT flag:** Autopilot clusters (this check does not apply there and is declared in their `limitations` per Step 2); pools created < 7 days ago; pools already at `minNodeCount: 0` with zero nodes (nothing to reclaim); pools whose nodes are `SchedulingDisabled` or mid-upgrade; the cluster's only node pool (system pods need somewhere to land); pools where DaemonSets alone account for the allocation _and_ the pool is a deliberate burst pool with `minNodeCount: 0`. GPU/TPU pools are **not** suppressed — an idle accelerator pool with a non-zero floor is the single largest reclaimable item this audit can find.
- **Severity:** `major` if the pool has ≥ 3 nodes, a machine type of ≥ 8 vCPU, or attached accelerators; else `minor`.
- **Impact:** "Node pool `gpu-burst` (2 × `a2-highgpu-1g`, `minNodeCount: 1`) has held ≤ 4% non-DaemonSet CPU allocation across all samples — 2 accelerator nodes reserved for nothing."
- **Remediation:** `kind: gcloud` — `gcloud container node-pools update <pool> --cluster=<c> --location=<l> --enable-autoscaling --min-nodes=0`, or delete the pool if it has been empty for consecutive audits. Caution: "Setting `min-nodes=0` lets the autoscaler drain these nodes; confirm no workload depends on warm capacity. Deleting a pool evicts everything on it."

#### 3.8 Scale-down blockers pinning under-allocated nodes (`scaledown-blocked`)

- **Command:** `kubectl get pdb -A -o json | jq '[.items[]|{ns:.metadata.namespace,name:.metadata.name,maxUnavailable:.spec.maxUnavailable,minAvailable:.spec.minAvailable,allowed:.status.disruptionsAllowed}]'` plus `kubectl get pods -A -o json` filtered to pods on the nodes 3.7 already found under-allocated.
- **Flag when:** an under-allocated node hosts at least one pod the autoscaler cannot evict — a PDB with `maxUnavailable: 0`, a PDB whose `status.disruptionsAllowed == 0` in **all** samples, the annotation `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`, a bare pod with no `ownerReferences`, or a pod with local storage (`emptyDir`/`hostPath`) and no `safe-to-evict: "true"`. Evaluate only nodes 3.7 flagged — that keeps the check cheap and keeps it a waste finding rather than a policy opinion.
- **Do NOT flag:** `SYSTEM_NS` PDBs that GKE manages; a PDB whose `disruptionsAllowed == 0` in only one sample (transient unhealthy replicas, not a policy blocker); nodes already cordoned or mid-upgrade; pods being deliberately pinned by a documented anti-eviction annotation with an owner label.
- **Severity:** `critical` when a PDB sets `maxUnavailable: 0` **and** reports `disruptionsAllowed: 0` — that combination blocks node drain permanently, which blocks autoscaler scale-down _and_ security patching and node upgrades. `major` otherwise (including the very common single-replica `minAvailable: 1` case).
- **Impact:** "Node `gke-prod-default-a1b2` sits at 8% CPU allocation but cannot be drained: PDB `payments/api-pdb` sets `maxUnavailable: 0`, so the autoscaler will hold this node indefinitely."
- **Remediation:** `kind: manual` — change the PDB to `maxUnavailable: 1` (or `minAvailable: <replicas-1>`) so the workload can be rescheduled, or raise replicas so a disruption is permitted. Caution: "This changes availability guarantees during voluntary disruptions — the workload owner must agree before it lands."

#### 3.9 Terminal pods and TTL-less Jobs (`terminal-pods`)

- **Command:** `kubectl get pods -A --field-selector=status.phase==Succeeded -o json` and `...==Failed -o json`, plus `kubectl get jobs -A -o json | jq '[.items[]|{ns:.metadata.namespace,name:.metadata.name,owner:(.metadata.ownerReferences[0].kind//""),ttl:.spec.ttlSecondsAfterFinished,done:.status.completionTime}]'`.
- **Flag when:** a namespace holds ≥ 50 terminal pods, or any terminal pod is older than 7 days; or a standalone Job (no CronJob owner) has been `Complete`/`Failed` for ≥ 7 days with `ttlSecondsAfterFinished` unset. _Justification:_ 50 is well above the handful a normal rollout leaves behind and well below the point where a single namespace hurts anyone; the 7-day floor keeps a debugging engineer's failed pods intact through a working week.
- **Do NOT flag:** `SYSTEM_NS`; pods younger than 24h (someone may still be reading them); Jobs owned by a CronJob (`successfulJobsHistoryLimit` already bounds them, unless it is set above 10 — then flag the CronJob, not the Jobs); Jobs that already set `ttlSecondsAfterFinished`; resources owned by a controller with its own GC (`workflows.argoproj.io/*`, `tekton.dev/*`, `fluxcd.io/*` labels).
- **Severity:** `minor`. `major` when one namespace exceeds 500 terminal pods or the cluster total exceeds 2,000 — at that point it is etcd object growth and API-server list latency, not just untidiness.
- **Impact:** "Namespace `ci` holds 1,840 `Succeeded` pods, the oldest from 2026-03-11 — 1,840 etcd objects listed on every full API-server sweep."
- **Remediation:** `kind: manifest` — the one safe additive patch in this SOP: add `ttlSecondsAfterFinished: 86400` to the Job spec or the CronJob's `jobTemplate.spec` in the GitOps source. Patch the source manifest, never a live completed Job. Note that the existing backlog still needs a one-off `kubectl delete pod --field-selector=status.phase==Succeeded -n <ns>` run by a human.

#### 3.10 Idle namespaces holding billable objects (`idle-namespace`)

- **Command:** `kubectl get ns -o json` cross-referenced against the pod, PVC, Service, and ResourceQuota dumps from Step 2.
- **Flag when:** a non-`SYSTEM_NS` namespace has zero `Running`/`Pending` pods in **all** samples, `AGE ≥ 30d`, and still holds at least one billable or quota-holding object: a PVC, a `type: LoadBalancer` Service, or a ResourceQuota reserving capacity. _Justification:_ a namespace with no workload but a bound volume or a live load balancer is paying for nothing; 30 days covers a monthly release cycle and a pre-provisioned environment awaiting its first deploy.
- **Do NOT flag:** `SYSTEM_NS`; namespaces holding only Secrets, ConfigMaps, RBAC, or CRs (free); namespaces under an active GitOps sync (`configsync.gke.io/*`, `kustomize.toolkit.fluxcd.io/*` annotations) — the controller owns their lifecycle; namespaces that are the target of a suspended or infrequent CronJob (idle by design); namespaces carrying an explicit ownership or retention annotation; namespaces in `Terminating`.
- **Severity:** `major` if it holds a `LoadBalancer` Service or ≥ 100 GiB of PVCs; else `minor`.
- **Impact:** "Namespace `demo-q1` has run no pods for 30+ days but still holds a `LoadBalancer` Service and 250 GiB of PVCs."
- **Remediation:** `kind: manual` — confirm the owner, then remove the LB Service and snapshot-then-delete the PVCs before deleting the namespace. Caution: "Deleting a namespace deletes everything in it, including PVCs whose PVs use `Delete`. Snapshot first."

### 4. Generate remediation artifacts

Only two manifest kinds are permitted, both purely additive: the `ttlSecondsAfterFinished` patch (3.9) and the request right-size patch (3.1). Write each to `remediations/fleet-wide-cost-analysis/<finding-id>.yaml` in the repo working tree **before** calling `finish` — the helper stages that path and hard-fails if it is missing, absolute, contains `..`, carries a glob metacharacter (`* ? [ ]`), or starts with `:`. Head each file with a comment naming the target object and the apply command. Everything else is `kind: gcloud` (a command printed for a human) or `kind: manual`, and those **must omit `remediation.path` entirely** — the helper rejects a path on a non-manifest remediation. **No manifest may delete a PV, PVC, namespace, disk, snapshot, or address.**

**A manifest is the only kind that can become a pull request.** A `critical` manifest finding opens a narrow remediation PR automatically, branched from `main` and linked to the ledger with `Part of #<issue>` — capped at five per run, with the withheld ones named in the ledger as awaiting `/remediate`. That combination should never arise here: the §3 severity ceiling keeps both manifest checks at `major` or below. Every other manifest finding waits for a repo writer to comment `/remediate <finding-id>`. Findings whose `remediation.path` values overlap are promoted together in one PR, so keep one file per finding unless two findings genuinely patch the same object. `gcloud` and `manual` findings are never promotable: this audit is mostly `gcloud`, so most of what it produces stays prose in the ledger issue and never becomes a PR. That is correct — releasing an IP or deleting a disk is a command a human runs after checking, not a file to merge.

A `kind: gcloud` `note` is rendered into the ledger issue **inside a bash fence**, so write it as a shell-pasteable block: the ordered commands on their own lines, and every caution — verify-before-deleting, snapshot-first, the irreversibility warnings this SOP requires — as a `#` comment line above the command it guards. Prose in a `gcloud` note renders as broken shell. A `kind: manual` note is rendered as prose and should be written as prose.

### 5. Emit findings.json

Write the file at the `findings_path` returned by `start`, matching the harness schema exactly.

- `evidence.command` is **mandatory and must be the literal command you ran** — the same string, with the real cluster, project, and object names substituted, so a reviewer can paste it and see what you saw. **A finding you cannot reproduce is dropped, not softened.** Never reconstruct a plausible command after the fact.
- `evidence.excerpt` is trimmed real output from the Step 2 dumps — never a paraphrase, never invented JSON. The helper clips at 40 lines / 2,000 chars, so trim to the lines that prove the finding yourself rather than pasting a whole dump and letting it be cut mid-object.
- **Credential hygiene:** never paste a Secret's `data:` block, a token, a kubeconfig, or a private key into `evidence.excerpt`. If the output that proves the finding carries one, re-run the read with a field selector or an `-o jsonpath` that omits it and quote that instead. The harness redacts high-confidence credential shapes as a backstop, not as the primary control — the control is not reading the value in the first place.
- Every dollar figure, savings estimate, and percentage-saved claim is forbidden. Resource units only.
- **Noise control:** if one check produces more than 20 findings of the same kind in one cluster or project, collapse them into a single roll-up finding (`id` suffix `--rollup`) listing the objects in `evidence.excerpt`, and raise its severity one level. This bounds the fleet-wide noise and it keeps the ledger readable: the harness caps the rendered issue body at 60,000 characters and says so in the body when it has to drop findings, but the counts in the title stay the true totals either way.
- Re-verify the scope before publishing: `scope.clusters` is non-empty; every cluster you could not read is in `scope.skipped` with a specific reason — "unavailable" is not a reason, the stderr excerpt is; every check that did not run on a cluster you _could_ read is named in that cluster's `limitations`; no cluster appears in both lists; no finding names a cluster from `scope.skipped`. The validator rejects the document on any of those.

**Every finding carries a `recommendation`.** Three sub-fields, all required, all non-empty, on **every** finding — not only the two checks that can produce a manifest. Write it while the evidence is in front of you; deferring it until someone asks for the fix means writing it from memory.

- `action` — what to do, imperative, one or two sentences.
- `rationale` — why this fix and not the obvious alternative. Name the alternative you considered and say why you rejected it.
- `risk` — what breaks on apply, and the read-only check to run first. **On this audit the `risk` field is what stops a "waste" finding from becoming an outage.** Every claim here rests on a ten-minute Monday-morning window; the risk line is where you say what that window cannot see.

Worked example, for a 3.7 finding on an idle batch pool — a `gcloud` remediation that will never become a pull request, and still carries the full recommendation:

```json
"recommendation": {
  "action": "Scale the batch node pool to zero, or delete it if the batch workload has moved.",
  "rationale": "Scaling to zero is reversible and keeps the pool's config; deleting is cheaper to reason about but discards the taint/label set the batch jobs depend on, which nobody has confirmed is obsolete.",
  "risk": "If a CronJob targets this pool with a nodeSelector it will go Pending on the next fire. Check for selectors referencing the pool before scaling."
}
```

### 6. Close the audit run

Run `./skills/fleet-audit/scripts/audit_report.py finish --audit fleet-wide-cost-analysis --findings-file <findings_path>`. Exit 0 means published; exit 2 means the validator rejected the document and nothing was published — fix the document and re-run, never delete the finding that tripped it; exit 1 is fatal. The helper prints one JSON line.

- `CLEAN` → the ledger issue was closed as completed and every open remediation PR for this stream closed with it. The final response is **exactly** `[SILENT]`. Nothing else, no preamble.
- `UPDATED` with `new: 0` **and** `resolved: 0` → nothing moved since last week. Also **exactly** `[SILENT]`.
- `OPENED`, or `UPDATED` with a non-zero `new` or `resolved` → one line: counts of new and resolved findings by severity, the ledger issue URL, and anything in `prs_opened` / `prs_closed`. The helper wrote the issue body; do not restate it, do not edit it.

## Red Lines

- **Read-only, always.** Never delete, cordon, drain, patch, scale, or apply anything. Every command in this SOP is a read verb. This is the most dangerous of the five audits to get wrong: **a false positive here can lead a human to delete a disk holding real data.**
- Never emit a manifest that deletes a PV, PVC, namespace, disk, snapshot, or address. Deletion remediations are `kind: manual` or `kind: gcloud` only, always with an explicit verify-before-deleting caution and a snapshot-first step wherever data is involved. A manifest is now one merge away from `main` — a `critical` one opens its own pull request without a human asking — so this line matters more than it did, not less.
- Never state a dollar amount, a monthly saving, a percentage saving, or a price. You have no pricing data, and a fabricated figure is worse than no figure.
- Never emit a finding without a literal, reproducible `evidence.command`. Drop it instead.
- Never infer "unused" from a single observation. Every usage-based claim rests on all three samples.
- Never flag an object in a cluster recorded in `scope.skipped` — the validator rejects the whole document for it — and never flag an object for a check you declared as not run in that cluster's `limitations`. An incomplete read is not evidence of absence.
- Never query BigQuery, the Billing export, the Recommender API, or Prometheus; never delegate any part of this audit to a Cluster Agent or a kanban card. Collection is done inline, here.
- Never write outside `remediations/fleet-wide-cost-analysis/` and the helper's `findings_path`. Never hand-write or edit the ledger issue body or a remediation PR body, and never call `gh issue create` or `gh pr create` — this stream has exactly one ledger issue and `finish` owns it.
