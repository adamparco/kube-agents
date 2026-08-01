# SOP: Workload Reliability Audit (Daily Governance)

**Purpose:** Sweep every managed GKE cluster for workloads configured in a way that will hurt during a node drain, a control-plane or node-pool upgrade, a scale event, or a traffic spike. The question this audit answers for a platform admin is: _which workloads on my fleet break when I upgrade a node pool, and which ones cannot scale?_ Output is a single GitHub PR carrying generated remediation manifests and a rendered finding list.

**Cron:** id `obtainability-audit`, schedule `50 6 * * *` (daily 06:50 UTC). The id is a stable observability identifier and does not change even though the audit is named "Workload Reliability".

**Data sources:** `kubectl` read verbs, `gcloud container ...`, the `gke` MCP server, and the Config Controller MCP tools (`list_cc_pods`, `get_cc_pod_diagnostics`, `list_cc_healthchecks`, `get_cc_operator_status`). **Nothing else** — no BigQuery, no Prometheus/GMP, no VPA recommendations, no Policy Controller, no external blueprint, no delegation to Cluster Agents via kanban. Every conclusion is derived from live cluster reads you performed in this run.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_pr.py start --audit obtainability-audit
```

Returns `{"branch":…, "existing_pr": <int|null>, "repo":"org/repo", "findings_path":"/opt/data/scratch/findings_obtainability-audit.json"}`. Keep `findings_path` and the repo working tree from this call; you write both. The helper owns every `git`/`gh` operation and renders the PR body — **never hand-write a PR body, never run `git commit`, `git push`, or `gh pr create` yourself.**

### 1. Enumerate the target fleet

```bash
gcloud container clusters list --format=json
```

- Target every cluster with `status == "RUNNING"`. Record `{name, location, project}` into `scope.clusters`. Note each cluster's `autopilot.enabled` — Step 3 changes behaviour on Autopilot. The `scope` schema has no field for it, so carry it in your own working state and surface it in `evidence.excerpt` when it changes a verdict.
- Any cluster you cannot audit goes in `scope.skipped` with a literal reason: `"status=STOPPING"`, `"get-credentials failed: <stderr first line>"`, `"RBAC: cannot list deployments"`, `"timeout after 30s"`. A skipped cluster is never silently dropped.
- Obtain per-cluster credentials into an isolated kubeconfig so clusters cannot bleed into each other:
  ```bash
  export KC=/opt/data/.kubeconfigs/wra_<project>_<cluster>_<location>.yaml
  KUBECONFIG=$KC gcloud container clusters get-credentials <cluster> --location=<location> --project=<project>
  ```
- If **zero** clusters land in `scope.clusters`, do **not** call `finish` — the helper hard-fails on an empty scope. Report the enumeration failure as your one-line summary and stop.

### 2. Collect workload state

One JSON dump per cluster answers every check in Step 3. **Do not run a separate full-fleet query per check.**

```bash
KUBECONFIG=$KC kubectl get deployments,statefulsets,daemonsets,poddisruptionbudgets,\
horizontalpodautoscalers,services,limitranges -A -o json > /opt/data/scratch/wra_state_<cluster>.json
```

- Because multiple kinds are requested, every element of `.items[]` carries its own `kind` — filter with `select(.kind=="…")`. (A single-kind `kubectl get` omits per-item `kind`; do not build the checks on that shape.)
- Read workload **templates** (`spec.template.spec`), not live Pods. Templates are what an admin edits, and they are unaffected by admission-time defaulting.
- Pods, Jobs, CronJobs, and Events are deliberately excluded: Events expire in roughly an hour, so a fixed 06:50 run samples an arbitrary window, and pod-level data doubles the payload without changing any verdict.

**Autopilot adjustments.** Autopilot injects resource requests (and, absent explicit limits, mirrors limits from requests) at Pod admission, so a missing-request or missing-memory-limit template is a cost-attribution and predictability problem there, not a scheduling failure. On an Autopilot cluster: downgrade checks 3.1 and 3.2 by one severity level and say so in `impact`. Hostname pinning (3.7) stays `critical` on Autopilot — nodes are ephemeral and are replaced on every upgrade, so a hostname-pinned pod has a guaranteed outage. All other checks are mode-independent.

### 3. Checks

**Standard exclusions — apply to every check below.** Skip an object if any holds:

- **S1 — system namespace:** `kube-system`, `kube-public`, `kube-node-lease`, `gmp-system`, `gmp-public`, `cnrm-system`, `configconnector-operator-system`, `istio-system`, `asm-system`, `gatekeeper-system`, `krmapihosting-system`, `anthos-identity-service`, or any namespace matching `gke-*` or `config-management-*`.
- **S2 — GKE-managed object:** carries the label `addonmanager.kubernetes.io/mode` (any value). GKE reverts edits to these; a finding is unactionable.
- **S3 — operator-owned:** the workload has a non-empty `metadata.ownerReferences` (its replica count, PDB, and probes belong to its controller, not to a human).
- **S4 — explicit opt-out:** the workload carries `kubeagents.x-k8s.io/reliability-audit: exempt` as a label or annotation.
- **S5 — not running:** `spec.replicas == 0`, or the workload is a Job/CronJob or is owned by one.

**Evidence discipline.** The dump is the _detector_; a live single-object read is the _confirmer_. For every candidate finding, run the object-scoped command below, capture a trimmed excerpt, and store that exact string in `evidence.command`. If the confirm command fails or the condition no longer holds, **drop the finding — do not soften it.**

```bash
KUBECONFIG=$KC kubectl get <kind> -n <ns> <name> -o yaml
```

If one check yields more than 25 findings in a single cluster, roll the surplus into one namespace-level finding per namespace: same severity, `object: "Namespace/<ns> (<n> workloads)"`, and a namespace-scoped confirm command (`kubectl get <kind> -n <ns> -o yaml`). This keeps a fleet that has never set requests from producing a thousand-line PR.

**Finding ids must be stable across runs.** Build them deterministically as `wra-<check-slug>-<cluster>-<namespace>-<kind>-<name>`, lowercased, every run of non-alphanumerics collapsed to `-`. **Never** put a timestamp, replica count, image tag, pod name, or resource value in an id — the delta between runs depends on the same problem keeping the same id.

#### 3.1 No CPU or memory request

- **Command:** derived from `$STATE`; confirmed with the object-scoped read above.
- **Flag when:** any container in `spec.template.spec.containers[]`, or any native sidecar (`initContainers[]` with `restartPolicy: Always`, which counts toward the pod's effective request), is missing `resources.requests.cpu` or `resources.requests.memory`.
- **Do NOT flag:** standard exclusions; plain init containers; any container whose namespace has a `LimitRange` with a matching `spec.limits[].defaultRequest` (the request is injected at admission, so there is nothing to fix).
- **Severity:** `major` (`minor` on Autopilot). The scheduler treats an unrequested container as free, so this corrupts bin-packing for every other workload on the node, not just this one.
- **Impact:** "The scheduler and cluster autoscaler size this cluster as if this workload costs nothing; its pods are the first evicted under node pressure and its cost cannot be attributed."
- **Remediation:** if the same container declares `resources.limits`, emit `kind: manifest` — a patch setting `requests` equal to the declared limits (an exact value the owner already chose). Otherwise `kind: manual`, note: "Size from observed usage (`kubectl top pod -n <ns>`) over a representative window and set requests explicitly." **Never invent a request value.**

#### 3.2 No memory limit

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a container has no `resources.limits.memory`.
- **Do NOT flag:** standard exclusions; namespaces whose `LimitRange` sets a `default.memory`; **missing CPU limits, ever.** Omitting a CPU limit is a deliberate and widely recommended choice — it avoids CFS throttling — and flagging it would make this audit noise.
- **Severity:** `major`. An unbounded container's blast radius is the whole node: a leak drives the kubelet to evict neighbouring pods before the offender is killed.
- **Impact:** "A memory leak here is absorbed by the node, not by this pod — the kubelet evicts co-located workloads first."
- **Remediation:** if `resources.requests.memory` is set, emit `kind: manifest` setting `limits.memory` to that same declared request (Guaranteed QoS, no invented number), and state in `note` that this is the conservative reading of the owner's own request and needs owner sign-off. If no memory request exists either, the finding is already covered by 3.1 — emit `kind: manual` and cross-reference it.

#### 3.3 Multi-replica workload with no PodDisruptionBudget

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a Deployment or StatefulSet has `spec.replicas >= 2` and no PDB in the same namespace whose `spec.selector` matches `spec.template.metadata.labels`. Evaluate the selector properly (`matchLabels` **and** `matchExpressions`); never match on names.
- **Do NOT flag:** standard exclusions; **DaemonSets** (a drain deletes DaemonSet pods rather than rescheduling them — a PDB finding on a DaemonSet is nonsense and would be the fastest way to get this audit switched off); workloads with `spec.replicas <= 1`.
- **Severity:** `major`. The upgrade still completes; the workload is simply taken fully offline while it does.
- **Impact:** "Nothing constrains the eviction API, so a single node drain during an upgrade can terminate every replica at once."
- **Remediation:** `kind: manifest`. Generate a PDB with `maxUnavailable: 1` and `spec.selector` copied verbatim from the workload's own `spec.selector.matchLabels`. Always `maxUnavailable`, **never** `minAvailable` — `minAvailable` is the shape that deadlocks drains (see 3.4), and `maxUnavailable: 1` is structurally safe at any replica count ≥ 2.

#### 3.4 Drain-blocking PodDisruptionBudget

- **Command:** derived from `$STATE`; confirmed with `kubectl get pdb -n <ns> <name> -o yaml`, whose `status` block is the corroborating excerpt.
- **Flag when:** a PDB has `maxUnavailable: 0` or `maxUnavailable: "0%"`; or `minAvailable` as an integer `>=` the matched workload's `spec.replicas`; or `minAvailable: "100%"`. Corroborate with `status.expectedPods > 0 && status.disruptionsAllowed == 0`, but decide on the spec — status alone is transient when a pod is briefly unready.
- **Do NOT flag:** standard exclusions (S1 applies to the PDB's namespace); a PDB whose `minAvailable` is genuinely below the target's replica count; a PDB matching a workload scaled to zero (`status.expectedPods == 0` — there is nothing to evict, so no drain is blocked); orphan PDBs matching no workload at all (harmless, and reported at most as a `minor` config-rot finding).
- **Severity:** `critical`. This is the highest-value finding in the audit and the one most often missed. It does not degrade a workload — it stops the cluster's entire lifecycle.
- **Impact:** "Blocks every node drain in this cluster indefinitely: node-pool upgrades, node auto-repair, and autoscaler scale-down all stall until a human deletes or edits this PDB."
- **Remediation:** if the matched workload has `spec.replicas >= 2`, emit `kind: manifest` rewriting the PDB to `maxUnavailable: 1`. If `replicas == 1` with `minAvailable: 1`, emit `kind: manual` — the PDB is doing exactly what it says, and the real fix (run more than one replica) is the owner's call, not a config patch.

#### 3.5 Deployment with `replicas >= 3` and no HorizontalPodAutoscaler

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a Deployment has `spec.replicas >= 3` and no HPA in the namespace whose `spec.scaleTargetRef` resolves to `{apiVersion: apps/v1, kind: Deployment, name: <name>}`.
- **Do NOT flag:** standard exclusions; **StatefulSets** (horizontal autoscaling of stateful members is rarely safe and is an owner decision); DaemonSets; workloads already fronted by a KEDA-generated HPA — KEDA creates a real `HorizontalPodAutoscaler`, so the selector match above already covers them.
- **Severity:** `minor`. A fixed replica count is a capacity decision, not a fault; nothing breaks during an upgrade. Ranking it below 3.3 and 3.4 is the point.
- **Impact:** "Capacity is pinned at a hand-chosen number: the workload cannot absorb a traffic spike and cannot give capacity back when idle."
- **Remediation:** `kind: manual`. `minReplicas` can be taken from the observed count, but `maxReplicas` and the utilisation target cannot be derived from anything this audit can read — emit guidance ("set `minReplicas` to the current <n>; choose `maxReplicas` and the CPU target from the workload's own headroom requirements; a CPU request is a prerequisite for utilisation-based scaling") rather than a manifest full of invented numbers.

#### 3.6 HPA that cannot scale

- **Command:** derived from `$STATE`; confirmed with `kubectl get hpa -n <ns> <name> -o yaml`.
- **Flag when:** (a) `spec.minReplicas == spec.maxReplicas`; or (b) `spec.scaleTargetRef` names an object absent from the dump, corroborated by `status.conditions[type=AbleToScale].status == "False"`.
- **Do NOT flag:** standard exclusions; HPAs owned by a KEDA `ScaledObject` (S3 covers them — the real configuration lives in a CRD this audit does not read); HPAs whose target exists but is a kind outside the dump (record as skipped, not as a finding).
- **Severity:** (a) `major` — the workload is pinned _and_ the admin believes it is autoscaled, so the HPA silently overrides the Deployment's own `replicas`. (b) `minor` — dangling config rot; nothing is currently degraded.
- **Impact:** (a) "An HPA is attached but `min == max`, so this workload cannot scale in either direction — the autoscaling is cosmetic." (b) "This HPA targets an object that no longer exists and autoscales nothing."
- **Remediation:** `kind: manual` for both. Widening a range and deleting a stale object are owner decisions, and this repo's manifest path cannot express a deletion.

#### 3.7 Rigid scheduling constraints

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** `spec.template.spec.nodeSelector` contains `kubernetes.io/hostname`; or `nodeSelector` pins `topology.kubernetes.io/zone` to one zone; or a `nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` term restricts `kubernetes.io/hostname` or `topology.kubernetes.io/zone` to exactly one value.
- **Do NOT flag:** standard exclusions; zone terms listing two or more `values` (deliberate and healthy); `preferredDuringScheduling…` (soft, never blocks scheduling); hardware selectors — `cloud.google.com/gke-accelerator`, `cloud.google.com/machine-family`, `cloud.google.com/compute-class`, `cloud.google.com/gke-spot` — which are legitimate requirements, not rigidity; StatefulSets with `spec.volumeClaimTemplates` bound to zonal storage, which are _correctly_ zone-pinned by their disks.
- **Severity:** hostname pin → `critical` (with node auto-upgrade the node is guaranteed to be replaced, so this is a scheduled outage). Single-zone pin → `major`.
- **Impact:** hostname — "This pod cannot be rescheduled; the next node upgrade or repair takes it down and it does not come back." Zone — "Pinned to one zone: a zonal stockout or zonal outage takes this workload down while capacity sits unused in the other two."
- **Remediation:** `kind: manual` for both. A pin usually encodes an assumption (node-local state, a zonal disk, a licence) that this audit cannot see, and blindly stripping a scheduling constraint is how an audit causes an incident. Guidance: replace the pin with a compute class plus topology spread (see the `gke-compute-classes` skill).

#### 3.8 Multi-replica workload with no spreading

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** `spec.replicas >= 2` and `spec.template.spec.topologySpreadConstraints` is absent or empty **and** there is no `podAntiAffinity` (required or preferred) keyed on `kubernetes.io/hostname` or `topology.kubernetes.io/zone`.
- **Do NOT flag:** standard exclusions; DaemonSets (one pod per node by construction); workloads that already have either mechanism.
- **Severity:** `minor`. kube-scheduler applies best-effort default spreading, so co-location is possible rather than certain — but the default skew tolerance is wide enough that a small Deployment can still land entirely on one node.
- **Impact:** "Nothing guarantees these replicas are on different nodes; losing one node can take the whole workload out despite the replica count."
- **Remediation:** `kind: manifest`. Add a single `topologySpreadConstraints` entry: `maxSkew: 1`, `topologyKey: kubernetes.io/hostname`, `whenUnsatisfiable: ScheduleAnyway`, `labelSelector` copied from the workload's own `spec.selector.matchLabels`. `ScheduleAnyway` is mandatory here — `DoNotSchedule` can make a workload unschedulable, which is a worse outcome than the finding.

#### 3.9 Missing probes

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** **readiness** — a container has no `readinessProbe` and the workload's pod labels are selected by a `Service` in the same namespace. **Liveness** — a container has no `livenessProbe`. Emit these as two separate findings with distinct check slugs; never merge them.
- **Do NOT flag:** standard exclusions; readiness on workloads no Service selects (nothing routes to them); Services of `type: ExternalName` or with no `selector`; injected sidecars that manage their own readiness (`istio-proxy`, `cloud-sql-proxy`, `gke-metadata-server`).
- **Severity:** readiness → `major`; liveness → `minor`. They are not equivalent: a missing readiness probe means every rolling update declares success the instant a container starts and immediately routes live traffic to a process that is not serving. A missing liveness probe is frequently the _correct_ choice — a badly tuned one causes restart storms — so it is reported as information, not as a defect.
- **Impact:** readiness — "Every rollout sends production traffic to pods that are not yet serving, and a broken new version is never detected as broken." Liveness — "A wedged process is never restarted automatically; recovery requires a human."
- **Remediation:** `kind: manual`, always. A probe's path, port, and timings are application knowledge; a generated `/healthz` probe would break the workload the moment it was applied. **Do not generate probe YAML.**

#### 3.10 Single-replica Service-backed Deployment

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a Deployment has `spec.replicas == 1` and a `Service` in the namespace selects its pods.
- **Do NOT flag:** standard exclusions; StatefulSets (a single-member StatefulSet is usually intentional and often disk-bound); Deployments with `strategy.type: Recreate`, which explicitly declares that two copies must never run at once; workloads carrying the S4 opt-out label, which is the sanctioned way for an owner to say "this is meant to be one replica".
- **Severity:** `minor`. This is a known-cost design decision, not a misconfiguration.
- **Impact:** "Zero-downtime is impossible: every rollout, node drain, and node repair is a full outage for this service."
- **Remediation:** `kind: manual`. Going HA touches leader election, session handling, and storage — guidance only.

**Dropped deliberately.** Right-sizing from VPA recommendations, "HPA pegged at max", CPU-throttling ratios, and OOMKill history all require Prometheus/GMP, VPA, or an event history this audit is forbidden from or cannot sample reliably at a fixed daily time. Node-pool surge and maintenance-window settings are real reliability risks but belong to the upgrade/security-patch audit, not here.

### 4. Generate remediation artifacts

- Every `remediation.kind == "manifest"` finding writes exactly one file to `remediations/obtainability-audit/<finding-id>.yaml` in the repo working tree **before** `finish` runs. Naming the file after the finding id guarantees uniqueness and makes the PR diff self-describing.
- Each file is a complete, appliable object (new PDBs and HPAs) or a minimal strategic-merge patch (spec edits), with a leading comment naming the cluster, the check, and the finding id.
- Copy selectors and labels verbatim from the live object. **Never invent a resource quantity, replica count, utilisation target, or probe endpoint** — if the value cannot be read off the object or is not structurally safe (`maxUnavailable: 1`, `maxSkew: 1`), the finding is `kind: manual`. These files are proposals in a PR for human review; do not `kubectl apply` anything, ever.

### 5. Emit findings.json

Write the schema exactly as the helper validates it to the `findings_path` returned in Step 0: `audit` set to `obtainability-audit`; `scope.clusters` non-empty; `scope.skipped` complete; and, for each finding, `id`, `severity`, `title`, `cluster`, `namespace`, `object` (as `Kind/name`), `evidence.command` (the literal confirm command you ran) and `evidence.excerpt` (trimmed to the few lines that prove the finding), `impact`, and `remediation` — with `remediation.path` present and the file on disk whenever `kind == "manifest"`. Sort findings by severity (`critical`, `major`, `minor`), then cluster, then namespace, so the diff between runs stays readable. A schema violation hard-fails the run; validate your own JSON before calling `finish`.

### 6. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_pr.py finish --audit obtainability-audit \
  --findings-file /opt/data/scratch/findings_obtainability-audit.json
```

- `status: "CLEAN"` — your entire final response is exactly `[SILENT]`. Nothing else, no preamble.
- `status: "OPENED"` or `"UPDATED"` — reply with **one line**: counts by severity, new vs. resolved, skipped-cluster count if any, and the `pr_url`. Example: `Workload Reliability Audit: 2 critical, 6 major, 11 minor across 4 clusters (3 new, 1 resolved, 1 skipped) — <pr_url>`. A partial audit that reads as a complete one is worse than no audit.

## Red Lines

- **Read-only against every cluster.** No `apply`, `patch`, `edit`, `delete`, `scale`, `drain`, `cordon`, or eviction — including dry-runs that reach the eviction API.
- **No hand-written PR bodies and no direct git/gh calls.** `audit_pr.py` owns the branch, the commit, and the PR body.
- **A finding you cannot reproduce is dropped, not softened.** `evidence.command` is the literal command you executed; if the confirm read fails or the condition has cleared, the finding does not ship.
- **No fabricated numbers.** Resource quantities, replica counts, autoscaling targets, and probe endpoints are either read off the live object or left to a human.
- **No forbidden sources.** BigQuery, Prometheus/GMP, VPA recommendations, Policy Controller, and external blueprints are out of scope; so is delegating any part of this audit to a Cluster Agent.
- **Stable ids or the delta lies.** An id that varies between runs turns one persistent problem into an infinite stream of "new" findings.
