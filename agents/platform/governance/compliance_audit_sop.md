# SOP: Security & RBAC Posture Audit (Daily Governance)

**Purpose:** A read-only, fleet-wide security sweep of every managed GKE cluster. Detects privilege-escalation surfaces, over-broad RBAC, missing network isolation, and cluster-level identity misconfiguration, then emits reproducible findings and remediation artifacts as one Pull Request. Cron id `compliance-audit`, schedule `20 6 * * *` (daily 06:20 UTC).

**Data sources:** `kubectl` read verbs, `gcloud container clusters|node-pools list|describe`, and the `gke` MCP server. Nothing else. There are **no external inputs** — no blueprint, no CMDB, no BigQuery, no Prometheus, no Policy Controller / Gatekeeper, no Security Command Center, no kanban delegation to Cluster Agents. Every finding comes from a command this SOP runs itself, in this run.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_pr.py start --audit compliance-audit
# -> {"branch":..., "existing_pr": <int|null>, "repo":"org/repo", "findings_path":"/opt/data/scratch/findings_compliance-audit.json"}
```

`findings_path` is the only file you write findings to. `start` leaves you in the GitOps repo working tree on `branch`; every path in §3 is relative to it. Do not run `git`, `gh`, or `submit-suggestion` anywhere in this SOP — `audit_pr.py` owns the write path and renders the PR body. **Never hand-write a PR body.**

### 1. Enumerate the target fleet

```bash
PROJECT=$(gcloud config get-value project)
gcloud container clusters list --project="$PROJECT" \
  --format='json(name,location,status,autopilot.enabled,currentMasterVersion)'
```

For each cluster with `status == RUNNING`, pin a per-cluster kubeconfig (local-only, mutates nothing) the way `platform_mcp_server.switch_kube_context` does, then confirm read access:

```bash
export KUBECONFIG="$HERMES_HOME/.kubeconfigs/kubeconfig_${PROJECT}_${C}_${L}.yaml"
gcloud container clusters get-credentials "$C" --location="$L" --project="$PROJECT"
kubectl auth can-i list pods --all-namespaces
```

Every cluster you actually query goes in `scope.clusters` as `{name, location, project}`. `scope.clusters` must be non-empty — if enumeration returns nothing or every cluster fails, do **not** emit an empty-scope file; stop and report the enumeration failure.

Record what you could not audit in `scope.skipped` as `{cluster, reason}`:

- `status != RUNNING` → `"cluster status <STATUS>, not queried"`.
- `get-credentials` / `auth can-i` fails → `"no read access: <trimmed stderr>"`. Never infer a finding from a cluster you could not reach.
- **Autopilot** (`autopilot.enabled == true`): checks 2.1–2.3 are rejected by admission and 2.9 has no user-managed node pools, so all four are inapplicable. Skip them and record one aggregate entry: `{"cluster":"<name>","reason":"autopilot: 2.1-2.3 admission-enforced, 2.9 no user node pools"}`. Checks 2.4–2.8, 2.10, 2.11 still run there. A privileged-container finding on Autopilot is a false positive by construction.

### 2. Checks

Shared setup, evaluated once per cluster. `$PRE` normalises every auditable workload to `{kind, ns, name, spec}` and applies the universal suppressions, so each workload check below is `$WL | jq -r --arg sys "$SYS" "$PRE"'| <filter>'`.

```bash
SYS='^(kube-system|kube-public|kube-node-lease|gke-.*|gmp-system|gke-gmp-system|gke-managed-.*|cnrm-system|configconnector-operator-system|krmapihosting-system|istio-system|asm-system|anthos-identity-service|config-management-.*|gatekeeper-system|composer-system)$'
WL='kubectl get deploy,sts,ds,cronjob,pod -A -o json'
PRE='.items[]
 | select((.metadata.namespace|test($sys)|not)
      and (.kind!="Pod" or ((.metadata.ownerReferences//[])|length)==0)
      and (((.metadata.labels//{})["addonmanager.kubernetes.io/mode"] // (.metadata.annotations//{})["components.gke.io/component-name"])==null))
 | {kind, ns:.metadata.namespace, name:.metadata.name,
    spec:(.spec.template.spec // .spec.jobTemplate.spec.template.spec // .spec)}'
```

**Universal suppressions — every check in this section:** namespaces matching `$SYS`; objects carrying `addonmanager.kubernetes.io/mode` or `components.gke.io/component-name` (the GKE-managed add-ons — `fluentbit-gke`, `gke-metrics-agent`, `pdcsi-node`, `netd`, `anetd`, `ip-masq-agent`, `konnectivity-agent`, `gke-metadata-server`, `nvidia-gpu-device-plugin`; flagging these is the fastest way to get this audit switched off); pods with a non-empty `ownerReferences` — audit the **owning controller**, never the pod, because pod name suffixes are random. `kubeagents-system` is deliberately **not** suppressed: the harness audits itself.

**Finding identity.** Derive `id` deterministically — never from a timestamp, counter, pod suffix, or ReplicaSet hash:

```
id = "<check-slug>.<cluster>.<namespace-or-_>.<kind>-<name>"    lowercased, [^a-z0-9.-] -> "-"
```

e.g. `privileged-container.prod-usc1.payments.deployment-api`. One finding per (check, workload): three privileged containers in one Deployment is **one** finding listing all three in `evidence.excerpt`.

**Evidence.** `evidence.command` is mandatory and must be the literal command run, with `$WL`/`$SYS`/`$PRE` expanded so a human can paste it unchanged. **A finding you cannot reproduce is dropped, not softened** — there is no "possible" severity.

#### 2.1 Privileged containers

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | [((.spec.containers//[])+(.spec.initContainers//[]))[]
     | select(.securityContext.privileged==true or ((.securityContext.capabilities.add//[])|index("SYS_ADMIN"))!=null)
     | .name] as $bad
 | select(($bad|length)>0) | "\(.kind)/\(.ns)/\(.name): \($bad|join(","))"'
```

- **Flag when:** a container or initContainer sets `securityContext.privileged: true`, or adds capability `SYS_ADMIN`.
- **Do NOT flag:** universal suppressions; CSI node drivers and CNI agents shipped as GKE add-ons; Autopilot clusters; `allowPrivilegeEscalation: true` on its own — that is the Kubernetes default and would fire on nearly every workload.
- **Severity:** `critical` — a privileged container is one escape away from owning the node and every workload on it.
- **Impact:** "Container has full host device and kernel access; compromising this workload compromises the node."
- **Remediation:** `kind: manual`. Dropping privilege can break a workload that needs one specific capability, so the owner confirms. Note the minimal replacement: remove `privileged`, add only the required `capabilities.add` entries.

#### 2.2 Host namespace sharing

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | select(.spec.hostNetwork==true or .spec.hostPID==true or .spec.hostIPC==true)
 | "\(.kind)/\(.ns)/\(.name): hostNetwork=\(.spec.hostNetwork//false) hostPID=\(.spec.hostPID//false) hostIPC=\(.spec.hostIPC//false)"'
```

- **Flag when:** the pod spec sets `hostNetwork`, `hostPID`, or `hostIPC` to `true`.
- **Do NOT flag:** universal suppressions; Autopilot clusters; ingress/gateway data-plane DaemonSets that legitimately bind host ports — verify `hostNetwork` is the only flag set **and** a `hostPort` is declared, then record `minor` rather than suppressing silently.
- **Severity:** `critical` when `hostPID` or `hostIPC` is set (direct visibility into other tenants' processes and memory); `major` when only `hostNetwork` is set — it bypasses NetworkPolicy enforcement and exposes node loopback, but does not cross the process boundary.
- **Impact:** "Workload shares the node's process/IPC/network namespace, bypassing pod isolation and NetworkPolicy enforcement."
- **Remediation:** `kind: manual`. Name the field to remove; for `hostNetwork`, note that a `NodePort` Service or a Gateway listener is the supported replacement for `hostPort`.

#### 2.3 hostPath volume mounts

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | [(.spec.volumes//[])[]|select(.hostPath)|{n:.name,p:.hostPath.path}] as $hv | select(($hv|length)>0)
 | [((.spec.containers//[])+(.spec.initContainers//[]))[]|(.volumeMounts//[])[]|{n:.name,ro:(.readOnly//false)}] as $m
 | [$hv[] as $v | ($m[]|select(.n==$v.n)|"\($v.p) readOnly=\(.ro)")] as $used | select(($used|length)>0)
 | "\(.kind)/\(.ns)/\(.name): \($used|join("; "))"'
```

- **Flag when:** the pod spec declares a `hostPath` volume that a container actually mounts.
- **Do NOT flag:** universal suppressions; Autopilot clusters; a declared-but-unmounted `hostPath`; the log-shipper pattern (`/var/log`, `/var/lib/docker/containers`) when **every** mount of it is `readOnly: true` — record those `minor`.
- **Severity:** `critical` when the path is `/`, `/etc`, `/proc`, `/var/run/docker.sock`, `/run/containerd/containerd.sock`, or under `/var/lib/kubelet`, **or** when any mount of it is writable — those are node takeover or credential theft. `major` otherwise: still a persistence and cross-tenant leak path.
- **Impact:** "Workload mounts a node filesystem path, giving it access to state belonging to the node and to other tenants' pods."
- **Remediation:** `kind: manual`. Name the replacement — a PersistentVolumeClaim, a ConfigMap/Secret projection, or the CSI driver appropriate to the data.

#### 2.4 `cluster-admin` bound to non-system subjects

```bash
kubectl get clusterrolebindings -o json | jq -r '.items[]
 | select(.roleRef.name=="cluster-admin") | . as $b | (.subjects//[])[]
 | select((.kind=="ServiceAccount" and ((.namespace//"")|test("^(kube-system|gke-.*|gmp-system|cnrm-system|configconnector-operator-system|krmapihosting-system|config-management-.*)$")|not))
       or ((.kind=="User" or .kind=="Group") and ((.name|startswith("system:"))|not)
           and ((.name|test("^(gke-|service-[0-9]+@)|gserviceaccount\\.com$"))|not)))
 | "\($b.metadata.name) -> \(.kind)/\(.namespace//"-")/\(.name)"'
```

- **Flag when:** a ClusterRoleBinding to `cluster-admin` names a ServiceAccount outside the system namespaces above, or a `User`/`Group` that is neither a `system:` principal nor a Google-managed service identity.
- **Do NOT flag:** `Group/system:masters` (the GKE bootstrap binding); GKE-installed `gce:*` / `system:*` bindings; `cnrm-system/cnrm-controller-manager`, which requires it by design. A `Group` that is an organisation email domain is an intentional human-admin grant — downgrade to `minor` and name the group rather than suppressing it.
- **Severity:** `critical` — a `cluster-admin` ServiceAccount turns any pod compromise in its namespace into full cluster compromise.
- **Impact:** "Subject holds unrestricted read/write on every resource in the cluster, including Secrets in every namespace."
- **Remediation:** `kind: manual`. Give the binding name, the subject, and the verification step a human runs first: `kubectl auth can-i --list --as=system:serviceaccount:<ns>:<sa>`, then delete the binding and substitute a scoped Role.

#### 2.5 Wildcard verbs/resources in bound Roles and ClusterRoles

```bash
kubectl get clusterroles,roles -A -o json | jq -r '.items[]
 | select(((.metadata.labels//{})["kubernetes.io/bootstrapping"])!="rbac-defaults" and ((.metadata.name|startswith("system:"))|not))
 | . as $r | [(.rules//[])[]|select(((.verbs//[])|index("*"))!=null
     and (((.resources//[])|index("*"))!=null or ((.apiGroups//[])|index("*"))!=null))] as $w
 | select(($w|length)>0) | "\($r.kind)/\($r.metadata.namespace//"-")/\($r.metadata.name): \($w|tojson)"'
kubectl get clusterrolebindings,rolebindings -A -o json | jq -r '.items[]
 | "\(.roleRef.kind)/\(.roleRef.name) <- \(.kind)/\(.metadata.name) subjects=\([(.subjects//[])[]|"\(.kind):\(.namespace//"-"):\(.name)"]|join(","))"'
```

Intersect the two and report only wildcard roles the second command shows bound to a non-system subject (same subject test as 2.4). An unbound over-broad role grants nothing.

- **Flag when:** a Role/ClusterRole has a rule with `verbs: ["*"]` **and** `resources: ["*"]` or `apiGroups: ["*"]`, and a binding grants it to a non-system subject.
- **Do NOT flag:** roles labelled `kubernetes.io/bootstrapping=rbac-defaults` or named `system:*`; **unbound** roles; a wildcard confined to one vendor apiGroup (`apiGroups: ["kubeagents.io"], resources: ["*"]`) — that is the operator-owns-its-own-CRDs pattern, not an escalation. A wildcard over the core group (`apiGroups: [""]`) is never suppressed.
- **Severity:** `critical` in a ClusterRole (fleet-wide blast radius); `major` in a namespaced Role, where damage is bounded to one tenant.
- **Impact:** "Subject can perform any verb on any resource in this scope, including reading Secrets and creating privileged pods — an unbounded escalation path."
- **Remediation:** `kind: manual`. Include the `kubectl auth can-i --list --as=...` output as the starting point for an enumerated replacement rule set.

#### 2.6 Namespaces with no enforcing NetworkPolicy

```bash
comm -23 \
  <(kubectl get ns -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -Ev "$SYS" | sort) \
  <(kubectl get netpol -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' | sort -u)
kubectl get netpol -A -o json | jq -r '.items[]
 | select(.spec.podSelector=={} and (((.spec.ingress//[])|any(.=={})) or ((.spec.policyTypes//[])|length)==0))
 | "\(.metadata.namespace)/\(.metadata.name): allow-all"'
```

- **Flag when:** a non-system namespace has **zero** NetworkPolicies, or every policy in it is an allow-all (`podSelector: {}` with an empty ingress rule). Both are a default-allow posture.
- **Do NOT flag:** universal suppressions; namespaces with zero workloads (`kubectl get pods -n <ns> --no-headers | wc -l` is `0`) — no exposure, pure churn; namespaces already covered by a cluster-wide policy under Dataplane V2 (`kubectl get ccnp -o name`).
- **Severity:** `major` for zero policies — unrestricted lateral movement, though the namespace boundary and RBAC still hold. `minor` for allow-all-only: the team engaged with NetworkPolicy and the fix is a one-line edit.
- **Impact:** "Every pod in this namespace accepts traffic from every pod in the cluster; a compromise anywhere reaches these workloads unimpeded."
- **Remediation:** `kind: manifest`, path `remediations/compliance-audit/<cluster>/<namespace>-default-deny.yaml`. Generate a `NetworkPolicy` `default-deny-ingress` (`podSelector: {}`, `policyTypes: [Ingress]`, no `ingress` rules) plus an `allow-dns-egress` policy permitting UDP/TCP 53 to `kube-system`. `remediation.note` must say it is deliberately ingress-only and the team adds per-service allow rules before merge.

#### 2.7 Default ServiceAccount token automounting

```bash
kubectl get sa -A --field-selector metadata.name=default -o json \
  | jq -r '.items[]|select(.automountServiceAccountToken!=false)|.metadata.namespace'
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | select(((.spec.serviceAccountName // .spec.serviceAccount)//"default")=="default")
 | select(.spec.automountServiceAccountToken!=false) | "\(.kind)/\(.ns)/\(.name)"'
```

- **Flag when:** a workload resolves to the `default` ServiceAccount **and** neither the pod spec nor the `default` SA object sets `automountServiceAccountToken: false`. Both commands must agree — the SA-level setting suppresses the pod-level default.
- **Do NOT flag:** universal suppressions; workloads using a dedicated named ServiceAccount — a mounted token there is intentional, and whether its RBAC is right is 2.4/2.5's job; namespaces whose `default` SA already sets `automountServiceAccountToken: false`.
- **Severity:** `major` — the mounted token is a live API credential handed to a workload that by definition did not ask for one, and it is the standard first move after a container compromise.
- **Impact:** "Workload mounts an API-server credential it does not use, handing an attacker an authenticated foothold for free."
- **Remediation:** `kind: manifest`, path `remediations/compliance-audit/<cluster>/<namespace>-default-sa-automount.yaml` — the namespace's `default` ServiceAccount with `automountServiceAccountToken: false`. One file fixes every workload in that namespace, so emit it once per namespace and point all of that namespace's findings at the same path.

#### 2.8 Workload Identity not enabled on the cluster

```bash
gcloud container clusters describe "$C" --location="$L" --project="$PROJECT" \
  --format='json(workloadIdentityConfig.workloadPool,nodeConfig.serviceAccount)'
```

- **Flag when:** `workloadIdentityConfig.workloadPool` is absent or empty.
- **Do NOT flag:** Autopilot clusters — Workload Identity is always on and the field always populated; clusters in `scope.skipped`.
- **Severity:** `critical` — without it every pod authenticates to Google Cloud as the node service account, so all workloads on a node share one identity and pod-level IAM is impossible.
- **Impact:** "All pods on this cluster share the node service account's Google Cloud permissions; there is no per-workload IAM boundary."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <C> --location=<L> --project=<PROJECT> --workload-pool=<PROJECT>.svc.id.goog`. Note that node pools must then move to `GKE_METADATA` (2.9) and that this recreates nodes.

#### 2.9 Node pool exposes the legacy GCE metadata endpoint

```bash
gcloud container node-pools list --cluster="$C" --location="$L" --project="$PROJECT" \
  --format='value(name,config.workloadMetadataConfig.mode)'
```

- **Flag when:** a node pool's `config.workloadMetadataConfig.mode` is empty or `GCE_METADATA` — metadata concealment is off and any pod can read `169.254.169.254`.
- **Do NOT flag:** Autopilot clusters (no user-managed node pools, skipped in §1); pools already reporting `GKE_METADATA`. Detection is configuration-only **by design** — probing the endpoint live would need `kubectl run`/`exec`, both write verbs forbidden by the Red Lines, and the node pool mode is authoritative for this control anyway.
- **Severity:** `critical` — one unauthenticated HTTP request from any pod to a node-wide credential.
- **Impact:** "Any pod on this node pool can read the node service account's access token from the legacy metadata endpoint and escalate to that identity's full Google Cloud permissions."
- **Remediation:** `kind: gcloud` — `gcloud container node-pools update <POOL> --cluster=<C> --location=<L> --project=<PROJECT> --workload-metadata=GKE_METADATA`. Note that this drains and recreates the pool's nodes.

#### 2.10 Public control plane with no authorized networks

```bash
gcloud container clusters describe "$C" --location="$L" --project="$PROJECT" \
  --format='json(privateClusterConfig.enablePrivateEndpoint,masterAuthorizedNetworksConfig.enabled,masterAuthorizedNetworksConfig.cidrBlocks,controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint)'
```

- **Flag when:** the public endpoint is reachable (`privateClusterConfig.enablePrivateEndpoint` not `true`, or `controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint` is `true`) **and** either `masterAuthorizedNetworksConfig.enabled` is not `true` or its `cidrBlocks` contain `0.0.0.0/0`.
- **Do NOT flag:** clusters with `enablePrivateEndpoint: true` — there is no public endpoint, so authorized networks are moot; a narrow but unfamiliar CIDR list. Judging whether a specific CIDR _should_ be allowed needs an external source of truth this audit does not have; only a literally unrestricted list is a finding.
- **Severity:** `critical` — the API server is exposed to the entire internet with only credentials in front of it.
- **Impact:** "The cluster's API server accepts connections from any address on the internet; credential compromise or an API-server CVE is directly exploitable from outside the network."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <C> --location=<L> --project=<PROJECT> --enable-master-authorized-networks --master-authorized-networks=<CIDR[,CIDR...]>`. The CIDR list must come from a human; say so in `remediation.note` and do not invent one.

#### 2.11 Pod Security `restricted` profile gaps

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | . as $o | [((.spec.containers//[])+(.spec.initContainers//[]))[]
     | select(((.securityContext.runAsNonRoot // $o.spec.securityContext.runAsNonRoot)!=true)
           or ((.securityContext.runAsUser // $o.spec.securityContext.runAsUser)==0)
           or ((((.securityContext.seccompProfile.type // $o.spec.securityContext.seccompProfile.type)//"")|test("^(RuntimeDefault|Localhost)$"))|not))
     | .name] as $bad
 | select(($bad|length)>0) | "\(.kind)/\(.ns)/\(.name): \($bad|join(","))"'
```

- **Flag when:** a container neither inherits nor sets `runAsNonRoot: true`, or explicitly sets `runAsUser: 0`, or has no `seccompProfile.type` of `RuntimeDefault`/`Localhost`.
- **Do NOT flag:** universal suppressions; any workload already reported by 2.1 — the privileged finding subsumes this one, never emit both; namespaces labelled `pod-security.kubernetes.io/enforce=restricted`, where admission already guarantees it.
- **Severity:** `minor` — these are defence-in-depth defaults rather than live escalation paths, and the fix is mechanical. Rating them `major` would drown the critical findings, which is how an audit becomes noise.
- **Impact:** "Containers run as root and/or without a seccomp filter, so a runtime escape has an unfiltered syscall surface and immediate root in the namespace it reaches."
- **Remediation:** `kind: manifest`, path `remediations/compliance-audit/<cluster>/<namespace>-<workload>-securitycontext.yaml` — a strategic-merge patch setting `spec.template.spec.securityContext` to `{runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}` and each container's `securityContext` to `{allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}`. `remediation.note` states the UID is a placeholder the image owner must confirm.

### 3. Generate remediation artifacts

- Write every `kind: manifest` file into the repo working tree **before** calling `finish`. The helper checks that each `remediation.path` exists; a missing file hard-fails the run.
- `remediation.path` is repo-root relative, always under `remediations/compliance-audit/<cluster>/`, and must match the file you wrote exactly.
- One file per remediation. Two findings share a path only where 2.7 says so (the per-namespace `default` ServiceAccount patch).
- Manifests are proposals. Never `kubectl apply` them and never embed a live `resourceVersion`.
- For `kind: gcloud` and `kind: manual`, write no file and **omit `remediation.path` entirely** — the helper rejects a path on a non-manifest remediation. Put the full command or ordered human steps in `remediation.note`, with real cluster, location, project, and object names substituted — no angle-bracket placeholders except the human-supplied CIDR in 2.10.
- A `kind: gcloud` `note` is rendered into the PR **inside a bash fence**, so it must be shell-pasteable: commands on their own lines, and caveats (2.8 and 2.9 both recreate nodes; 2.10 needs a human-supplied CIDR) as `#` comment lines above the command they guard. Prose in a `gcloud` note renders as broken shell. A `kind: manual` note is rendered as prose and should read as prose.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "compliance-audit"`, `scope.clusters` listing every cluster queried, and `scope.skipped` carrying the rest plus the Autopilot entry from §1. Self-check before writing:

- Every finding has a non-empty `evidence.command` that is the literal command run. Drop anything else.
- `id`s are unique in the file and re-derived by the §2 rule — never copied from a previous run.
- `namespace` is `""` for cluster-scoped findings (2.4, 2.5 ClusterRoles, 2.8, 2.9, 2.10); `object` is `<Kind>/<name>` (`Deployment/api`, `ClusterRoleBinding/dev-admin`, `NodePool/pool-1`, `Cluster/prod-usc1`).
- `remediation.path` is present iff `kind == "manifest"`, that file exists on disk, and no finding references a cluster/check pair recorded in `scope.skipped`.

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_pr.py finish --audit compliance-audit \
  --findings-file /opt/data/scratch/findings_compliance-audit.json
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","pr_url":...,"new":n,"resolved":m}
```

- `status == "CLEAN"` → your final response is exactly `[SILENT]`. No preamble, no "no issues found". A clean fleet is a silent fleet.
- `status == "OPENED"` or `"UPDATED"` → one line, then stop: `Security & RBAC posture audit: <new> new, <resolved> resolved across <count(scope.clusters)> clusters — <pr_url>`
- If `finish` reports a schema error, fix the findings file and re-run `finish`. Do not work around the validator and do not open a PR by hand.

## Red Lines

- **Read-only.** No `kubectl apply|patch|create|delete|edit|scale|exec|run|port-forward|cp`, no `gcloud container clusters|node-pools update`, no write of any kind against any cluster. `gcloud container clusters get-credentials` is the sole exception and touches only a local kubeconfig.
- **No hand-written PR body, branch, commit, or `gh` call.** `audit_pr.py` owns the entire git/GitHub path; do not invoke `submit-suggestion` from this SOP.
- **No unreproducible findings.** No `evidence.command`, no finding. Never soften something you could not verify into a lower severity or a "possible issue" — delete it.
- **No unstable ids.** Never derive an `id` from a pod suffix, ReplicaSet hash, timestamp, or loop counter; unstable ids make every run look like a fleet of new problems and destroy the delta.
- **No inference from an unaudited cluster.** An unreachable cluster, or a check that is admission-enforced or structurally inapplicable on Autopilot, goes in `scope.skipped` with a reason — never into `findings`.
- **No forbidden sources.** No BigQuery, Prometheus, Policy Controller / Gatekeeper, Security Command Center, external blueprint, or CMDB — and no kanban delegation to Cluster Agents. This audit runs entirely in the Platform Agent.
- **Never print raw credentials.** ServiceAccount tokens, kubeconfig contents, and Secret values never appear in `evidence.excerpt` — record the object reference instead.
