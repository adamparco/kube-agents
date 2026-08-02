# SOP: Security & RBAC Posture Audit (Daily Governance)

**Purpose:** A read-only, fleet-wide security sweep of every managed GKE cluster. Detects privilege-escalation surfaces, over-broad RBAC, missing network isolation, and cluster-level identity misconfiguration, then emits reproducible findings into one GitHub issue — the stream's ledger — with narrow remediation Pull Requests hung off it. Cron id `compliance-audit`, schedule `20 6 * * *` (daily 06:20 UTC).

**Data sources:** `kubectl` read verbs, `gcloud container clusters|node-pools list|describe`, and the `gke` MCP server. Nothing else. There are **no external inputs** — no blueprint, no CMDB, no BigQuery, no Prometheus, no Policy Controller / Gatekeeper, no Security Command Center, no kanban delegation to Cluster Agents. Every finding comes from a command this SOP runs itself, in this run.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit
# -> {"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/org__repo",
#     "findings_path":"/opt/data/scratch/findings_compliance-audit.json",
#     "pending_remediation_requests":["<finding-id>", ...]}
```

`findings_path` is the only file you write findings to. `issue` is the stream's open ledger issue, or `null` when the stream has none. `pending_remediation_requests` is the set of finding ids a repo writer asked for with a `/remediate` comment on the ledger — write a `kind: manifest` file for every one of them during §2 and §3, whether or not this SOP would have promoted it on its own.

`workspace` is the GitOps clone. You do **not** start in a checkout: `start` clones the repository to that directory itself, and every `remediation.path` in §3 is resolved against it — a manifest written anywhere else is a file the harness never sees. There is no report branch, and `start` creates none. Do not run `git`, `gh`, or `submit-suggestion` anywhere in this SOP — `audit_report.py` owns the write path and renders the ledger issue and every remediation PR. **Never hand-write an issue body or a PR body.**

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

**The one-question scope rule.** A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; if some checks did not run there, name them in that cluster's `limitations`. No → `scope.skipped`. Nothing goes in both, and nothing in `scope.skipped` may appear in a finding.

`scope.skipped` is only for clusters you could **not** read, as `{cluster, reason}`:

- `status != RUNNING` → `"cluster status <STATUS>, not queried"`.
- `get-credentials` / `auth can-i` fails → `"no read access: <trimmed stderr>"`. Never infer a finding from a cluster you could not reach.

`scope.clusters[].limitations` is an optional string on a cluster you **did** read, naming the checks that did not run there and why. When present it must be non-empty and must name the checks by number. Partial coverage is never a reason to move the cluster to `scope.skipped` — that would suppress every real finding from the checks that _did_ run.

- **Autopilot** (`autopilot.enabled == true`): checks 2.1–2.3 are rejected by admission and 2.9 has no user-managed node pools, so all four are inapplicable. The cluster still belongs in `scope.clusters`, carrying `"limitations": "Autopilot cluster: checks 2.1-2.3 are admission-enforced and 2.9 has no user-managed node pools; those four did not run."` Checks 2.4–2.8, 2.10 and 2.11 run there exactly as on a Standard cluster and **their findings are real** — an Autopilot cluster is audited, not skipped. A privileged-container finding on Autopilot is a false positive by construction; a missing-NetworkPolicy finding on Autopilot is not.
- Any other gap on a reachable cluster — a check whose command errored, an API group that is absent — is recorded the same way, in that cluster's `limitations`, naming the check and the reason.

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

**Finding identity.** `<check-slug>` is the backticked token in each `####` heading below. Derive `id` deterministically from it — never from a timestamp, counter, pod suffix, or ReplicaSet hash:

```
id = "<check-slug>.<cluster>.<namespace-or-_>.<kind>-<name>"    lowercased, [^a-z0-9.-] -> "-"
```

e.g. `privileged-container.prod-usc1.payments.deployment-api`. One finding per (check, workload): three privileged containers in one Deployment is **one** finding listing all three in `evidence.excerpt`.

The result must match `^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$`, contain no `..` segment, and not end in `.lock`. The id is the join key of the ledger's hidden delta block and of the `audit-persists:<id>` marker — both line-anchored regexes — and an operator types it by hand in `/remediate <id>`, so a colon, a space, a `*`, a `..`, or a `.lock` suffix is rejected outright. Cap at 100 characters by trimming the object name from the right, then strip any leading or trailing `.` or `-`; never drop the leading slug, and never substitute a hash.

**Evidence.** `evidence.command` is mandatory and must be the literal command run, with `$WL`/`$SYS`/`$PRE` expanded so a human can paste it unchanged. **A finding you cannot reproduce is dropped, not softened** — there is no "possible" severity.

**Credential hygiene.** Never paste a Secret's `data:` block, a ServiceAccount token, a kubeconfig, or a private key into `evidence.excerpt`. Re-run the command with a field selector or an `-o jsonpath` that omits the value and quote that output instead — the object reference proves the finding, the credential never does. The harness redacts high-confidence credential shapes as a backstop, not as the primary control.

#### 2.1 Privileged containers (`privileged-container`)

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | [((.spec.containers//[])+(.spec.initContainers//[]))[]
     | select(.securityContext.privileged==true or ((.securityContext.capabilities.add//[])|index("SYS_ADMIN"))!=null)
     | .name] as $bad
 | select(($bad|length)>0) | "\(.kind)/\(.ns)/\(.name): \($bad|join(","))"'
```

- **Flag when:** a container or initContainer sets `securityContext.privileged: true`, or adds capability `SYS_ADMIN`.
- **Do NOT flag:** universal suppressions; CSI node drivers and CNI agents shipped as GKE add-ons; Autopilot clusters — the check does not run there and §1 records that in the cluster's `limitations`; `allowPrivilegeEscalation: true` on its own — that is the Kubernetes default and would fire on nearly every workload.
- **Severity:** `critical` — a privileged container is one escape away from owning the node and every workload on it.
- **Impact:** "Container has full host device and kernel access; compromising this workload compromises the node."
- **Remediation:** `kind: manual`. Dropping privilege can break a workload that needs one specific capability, so the owner confirms. Note the minimal replacement: remove `privileged`, add only the required `capabilities.add` entries.

#### 2.2 Host namespace sharing (`host-namespace`)

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | select(.spec.hostNetwork==true or .spec.hostPID==true or .spec.hostIPC==true)
 | "\(.kind)/\(.ns)/\(.name): hostNetwork=\(.spec.hostNetwork//false) hostPID=\(.spec.hostPID//false) hostIPC=\(.spec.hostIPC//false)"'
```

- **Flag when:** the pod spec sets `hostNetwork`, `hostPID`, or `hostIPC` to `true`.
- **Do NOT flag:** universal suppressions; Autopilot clusters (§1 `limitations`); ingress/gateway data-plane DaemonSets that legitimately bind host ports — verify `hostNetwork` is the only flag set **and** a `hostPort` is declared, then record `minor` rather than suppressing silently.
- **Severity:** `critical` when `hostPID` or `hostIPC` is set (direct visibility into other tenants' processes and memory); `major` when only `hostNetwork` is set — it bypasses NetworkPolicy enforcement and exposes node loopback, but does not cross the process boundary.
- **Impact:** "Workload shares the node's process/IPC/network namespace, bypassing pod isolation and NetworkPolicy enforcement."
- **Remediation:** `kind: manual`. Name the field to remove; for `hostNetwork`, note that a `NodePort` Service or a Gateway listener is the supported replacement for `hostPort`.

#### 2.3 hostPath volume mounts (`hostpath-mount`)

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | [(.spec.volumes//[])[]|select(.hostPath)|{n:.name,p:.hostPath.path}] as $hv | select(($hv|length)>0)
 | [((.spec.containers//[])+(.spec.initContainers//[]))[]|(.volumeMounts//[])[]|{n:.name,ro:(.readOnly//false)}] as $m
 | [$hv[] as $v | ($m[]|select(.n==$v.n)|"\($v.p) readOnly=\(.ro)")] as $used | select(($used|length)>0)
 | "\(.kind)/\(.ns)/\(.name): \($used|join("; "))"'
```

- **Flag when:** the pod spec declares a `hostPath` volume that a container actually mounts.
- **Do NOT flag:** universal suppressions; Autopilot clusters (§1 `limitations`); a declared-but-unmounted `hostPath`; the log-shipper pattern (`/var/log`, `/var/lib/docker/containers`) when **every** mount of it is `readOnly: true` — record those `minor`.
- **Severity:** `critical` when the path is `/`, `/etc`, `/proc`, `/var/run/docker.sock`, `/run/containerd/containerd.sock`, or under `/var/lib/kubelet`, **or** when any mount of it is writable — those are node takeover or credential theft. `major` otherwise: still a persistence and cross-tenant leak path.
- **Impact:** "Workload mounts a node filesystem path, giving it access to state belonging to the node and to other tenants' pods."
- **Remediation:** `kind: manual`. Name the replacement — a PersistentVolumeClaim, a ConfigMap/Secret projection, or the CSI driver appropriate to the data.

#### 2.4 `cluster-admin` bound to non-system subjects (`cluster-admin-binding`)

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

#### 2.5 Wildcard verbs/resources in bound Roles and ClusterRoles (`wildcard-rbac`)

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

#### 2.6 Namespaces with no enforcing NetworkPolicy (`netpol-missing`)

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
- **Remediation:** the two flag conditions are two different problems, and only one of them is fixed by adding a file.
  - **Zero policies** — the object does not exist, so §3's create rule applies: `kind: manifest`, path `remediations/compliance-audit/<cluster>/<namespace>-default-deny.yaml`. Generate **exactly one** `NetworkPolicy`, `default-deny-ingress` (`podSelector: {}`, `policyTypes: [Ingress]`, no `ingress` rules), and nothing else.
  - **Allow-all only** — the offending policy _is_ the finding, and adding a second file does not fix it. NetworkPolicy is additive: a pod is reachable if **any** policy selecting it permits the traffic, so a deny-everything policy sitting alongside an allow-all one changes nothing. Emitting it produces a pull request that merges cleanly, closes the finding for exactly one run, and leaves the namespace as open as it was — worse than no fix, because it also spends the reviewer's trust. Name the allow-all policy in `object` as `NetworkPolicy/<name>` and fix _that_ object, under §3's change-an-existing-object rule: `kind: manifest` **only** when the GitOps repo already declares it — `remediation.path` is that existing file, rewritten as the policy's complete desired manifest with the empty `ingress` rule removed (`podSelector: {}`, `policyTypes: [Ingress]`, no `ingress`) and its name unchanged — and `kind: manual` otherwise, because the harness will not edit or delete a live object it cannot find declared. Never write a second file under `remediations/` for this branch.

  In both branches `remediation.note` says the policy is ingress-only and that the team adds per-service allow rules before merge. **Never add a second policy that lists `Egress` in `policyTypes`** — including an "allow DNS" companion. Any policy naming `Egress` makes every pod it selects egress-isolated, so pairing one with the default-deny does not soften it: it default-denies all outbound traffic for those pods and permits only what that policy allows. Egress isolation is a separate, deliberate, breaking change, and this audit does not make it.

#### 2.7 Default ServiceAccount token automounting (`default-sa-automount`)

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
- **Remediation:** the namespace's `default` ServiceAccount with `automountServiceAccountToken: false`. That ServiceAccount already exists in the cluster, so §3's rule applies: `kind: manifest` only when the repo already declares it — rewrite that declaration complete — and `kind: manual` otherwise. One file fixes every workload in that namespace, so emit it once per namespace and point all of that namespace's findings at the same path.

#### 2.8 Workload Identity not enabled on the cluster (`workload-identity-off`)

```bash
gcloud container clusters describe "$C" --location="$L" --project="$PROJECT" \
  --format='json(workloadIdentityConfig.workloadPool,nodeConfig.serviceAccount)'
```

- **Flag when:** `workloadIdentityConfig.workloadPool` is absent or empty.
- **Do NOT flag:** Autopilot clusters — the check runs there, but Workload Identity is always on and the field always populated, so it simply never fires; clusters in `scope.skipped`, which you could not read and about which you therefore have no evidence either way. A `limitations` note is **not** a suppression: on a cluster in `scope.clusters`, run every check its `limitations` string does not name.
- **Severity:** `critical` — without it every pod authenticates to Google Cloud as the node service account, so all workloads on a node share one identity and pod-level IAM is impossible.
- **Impact:** "All pods on this cluster share the node service account's Google Cloud permissions; there is no per-workload IAM boundary."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <C> --location=<L> --project=<PROJECT> --workload-pool=<PROJECT>.svc.id.goog`. Note that node pools must then move to `GKE_METADATA` (2.9) and that this recreates nodes.
- **Ownership.** This check owns the Workload Identity verdict for the whole fleet; the Fleet Consistency Drift audit defers its `workload-identity` facet here (its §4.2) rather than reporting the same cluster in a second ledger. An absolute check is strictly stronger than a majority vote — a fleet that has Workload Identity off everywhere produces no drift finding at all, and still every cluster is wrong.

#### 2.9 Node pool exposes the legacy GCE metadata endpoint (`legacy-metadata`)

```bash
gcloud container node-pools list --cluster="$C" --location="$L" --project="$PROJECT" \
  --format='value(name,config.workloadMetadataConfig.mode)'
```

- **Flag when:** a node pool's `config.workloadMetadataConfig.mode` is empty or `GCE_METADATA` — metadata concealment is off and any pod can read `169.254.169.254`.
- **Do NOT flag:** Autopilot clusters — there are no user-managed node pools, and §1 records that in the cluster's `limitations`; pools already reporting `GKE_METADATA`. Detection is configuration-only **by design** — probing the endpoint live would need `kubectl run`/`exec`, both write verbs forbidden by the Red Lines, and the node pool mode is authoritative for this control anyway.
- **Severity:** `critical` — one unauthenticated HTTP request from any pod to a node-wide credential.
- **Impact:** "Any pod on this node pool can read the node service account's access token from the legacy metadata endpoint and escalate to that identity's full Google Cloud permissions."
- **Remediation:** `kind: gcloud` — `gcloud container node-pools update <POOL> --cluster=<C> --location=<L> --project=<PROJECT> --workload-metadata=GKE_METADATA`. Note that this drains and recreates the pool's nodes.

#### 2.10 Public control plane with no authorized networks (`public-control-plane`)

```bash
gcloud container clusters describe "$C" --location="$L" --project="$PROJECT" \
  --format='json(privateClusterConfig.enablePrivateEndpoint,masterAuthorizedNetworksConfig.enabled,masterAuthorizedNetworksConfig.cidrBlocks,controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint)'
```

- **Flag when:** the public endpoint is reachable (`privateClusterConfig.enablePrivateEndpoint` not `true`, or `controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint` is `true`) **and** either `masterAuthorizedNetworksConfig.enabled` is not `true` or its `cidrBlocks` contain `0.0.0.0/0`.
- **Do NOT flag:** clusters with `enablePrivateEndpoint: true` — there is no public endpoint, so authorized networks are moot; a narrow but unfamiliar CIDR list. Judging whether a specific CIDR _should_ be allowed needs an external source of truth this audit does not have; only a literally unrestricted list is a finding.
- **Severity:** `critical` — the API server is exposed to the entire internet with only credentials in front of it.
- **Impact:** "The cluster's API server accepts connections from any address on the internet; credential compromise or an API-server CVE is directly exploitable from outside the network."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <C> --location=<L> --project=<PROJECT> --enable-master-authorized-networks --master-authorized-networks=<CIDR[,CIDR...]>`. The CIDR list must come from a human; say so in `remediation.note` and do not invent one.

#### 2.11 Pod Security `restricted` profile gaps (`podsecurity-gaps`)

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | . as $o | [((.spec.containers//[])+(.spec.initContainers//[]))[]
     | . as $c
     | (if (($c.securityContext//{})|has("runAsNonRoot")) then $c.securityContext.runAsNonRoot
        elif (($o.spec.securityContext//{})|has("runAsNonRoot")) then $o.spec.securityContext.runAsNonRoot
        else null end) as $nonroot
     | select(($nonroot!=true)
           or (($c.securityContext.runAsUser // $o.spec.securityContext.runAsUser)==0)
           or (((($c.securityContext.seccompProfile.type // $o.spec.securityContext.seccompProfile.type)//"")|test("^(RuntimeDefault|Localhost)$"))|not))
     | .name] as $bad
 | select(($bad|length)>0) | "\(.kind)/\(.ns)/\(.name): \($bad|join(","))"'
```

**Resolve `runAsNonRoot` with `has()`, never with `//`.** `//` is jq's _alternative_ operator: it fires on `false` exactly as it fires on `null`, so `(.securityContext.runAsNonRoot // $o.spec.securityContext.runAsNonRoot)` turns a container that explicitly sets `runAsNonRoot: false` over a compliant pod-level `true` into `true` — the check silently passes the one input it exists to catch. The `has()` ladder above distinguishes absent from false. `runAsUser` and `seccompProfile.type` keep `//` safely: neither `0` nor a string is falsy in jq, so the alternative fires only on a genuinely absent field.

- **Flag when:** a container neither inherits nor sets `runAsNonRoot: true` — **including a container that explicitly sets `runAsNonRoot: false` over a compliant pod-level default** — or explicitly sets `runAsUser: 0`, or has no `seccompProfile.type` of `RuntimeDefault`/`Localhost`.
- **Do NOT flag:** universal suppressions; any workload already reported by 2.1 — the privileged finding subsumes this one, never emit both; namespaces labelled `pod-security.kubernetes.io/enforce=restricted`, where admission already guarantees it.
- **Severity:** `minor` — these are defence-in-depth defaults rather than live escalation paths, and the fix is mechanical. Rating them `major` would drown the critical findings, which is how an audit becomes noise.
- **Impact:** "Containers run as root and/or without a seccomp filter, so a runtime escape has an unfiltered syscall surface and immediate root in the namespace it reaches."
- **Remediation:** the workload already exists, so §3's rule applies. When the repo declares it, `kind: manifest` at that declaration's own path, rewritten as the workload's **complete** desired manifest with `spec.template.spec.securityContext` set to `{runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}` and each container's `securityContext` to `{allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}`, everything else carried over unchanged. `remediation.note` states the UID is a placeholder the image owner must confirm. When you cannot find a declaration, `kind: manual` with the same change spelled out in `recommendation.action`.

### 3. Generate remediation artifacts

- Write every `kind: manifest` file into the `workspace` clone §0 named, **before** calling `finish`. A path with no file behind it no longer kills the run: that one finding degrades to `kind: manual`, keeps its evidence and recommendation, and says in the ledger that the audit named the fix but never wrote it — the report still publishes. Treat a degrade as a defect in your own work, not a fallback: it converts a fix a reviewer could have merged into one a human now writes by hand. This includes every finding named in `pending_remediation_requests` from §0 — a `/remediate` request with no manifest on disk cannot be promoted.
- **Where the file goes depends on whether the object already exists.** A remediation that _creates_ an object the cluster does not have — 2.6's NetworkPolicy on a namespace that has **zero** of them — is written as a complete, appliable object under `remediations/compliance-audit/<cluster>/`. A remediation that _changes an object that already exists_ — 2.7's `default` ServiceAccount, 2.11's workload, and 2.6's other branch where the namespace already has an allow-all policy — goes to that object's **existing declaration in the GitOps repo**: locate it (`grep -rl "name: <object>" --include='*.yaml' .`), name that file as `remediation.path`, and rewrite it as the object's complete desired manifest. Never write a patch fragment: a file carrying `metadata.name` and a partial `spec` is not valid `kubectl apply` input, and a second file under `remediations/` claiming an object the repo already declares is a duplicate resource id that both Config Sync and Argo reject.
- **An object that already exists and has no declaration you can find is `kind: manual`.** Describe the change in `recommendation.action`, write no file, and omit `remediation.path`. Never invent a new path for it.
- `remediation.path` is relative to the repository root — which is `workspace`, not the directory you happen to be in — and must match the file you wrote exactly. No `..`, no glob metacharacter (`*`, `?`, `[`, `]`), no leading `:` — the helper rejects all of them.
- One file per remediation. Two findings share a path only where 2.7 says so (the per-namespace `default` ServiceAccount).
- **Findings that share a path share a Pull Request.** The promotion unit is the group of findings whose `remediation.path` values intersect, unioned transitively. 2.7 is the one case in this SOP that produces a group: every finding in a namespace points at that namespace's single `default` ServiceAccount declaration, so all of them are one group, on one branch, in one PR. Every other check here is one finding, one path, one PR.
- Manifests are proposals. Never `kubectl apply` them and never embed a live `resourceVersion`.
- For `kind: gcloud` and `kind: manual`, write no file and **omit `remediation.path` entirely** — the helper rejects a path on a non-manifest remediation. Put the full command or ordered human steps in `remediation.note`, with real cluster, location, project, and object names substituted — no angle-bracket placeholders except the human-supplied CIDR in 2.10. Neither kind is ever promotable to a PR; a `/remediate` request naming one is refused.
- A `kind: gcloud` `note` is rendered into the ledger issue **inside a bash fence**, so it must be shell-pasteable: commands on their own lines, and caveats (2.8 and 2.9 both recreate nodes; 2.10 needs a human-supplied CIDR) as `#` comment lines above the command they guard. Prose in a `gcloud` note renders as broken shell. A `kind: manual` note is rendered as prose and should read as prose.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "compliance-audit"`, `scope.clusters` listing every cluster you queried — each carrying the `limitations` string §1 recorded for it, where there is one — and `scope.skipped` listing only the clusters you could not read. Self-check before writing:

- Every finding has a non-empty `evidence.command` that is the literal command run. Drop anything else.
- `id`s are unique in the file, re-derived by the §2 rule from the check's slug, and match the §2 charset — never copied from a previous run.
- `namespace` is `""` for cluster-scoped findings (2.4, 2.5 ClusterRoles, 2.8, 2.9, 2.10); `object` is `<Kind>/<name>` (`Deployment/api`, `ClusterRoleBinding/dev-admin`, `NodePool/pool-1`, `Cluster/prod-usc1`).
- Every finding carries a complete `recommendation` — see below.
- `remediation.path` is present iff `kind == "manifest"` and that file exists on disk.
- No cluster appears in both scope lists, and no finding names a cluster in `scope.skipped`. The validator rejects the document on either. A `limitations` note suppresses nothing: findings from the checks that _did_ run on that cluster belong in the file.

Emit the complete set of findings. The harness bounds the rendering, not you: it caps the issue body at 60,000 characters, trims each excerpt to 40 lines / 2,000 chars and each command to 2,000 chars, and caps the scope tables at 60 rows. When findings do not fit, the body says so and the title's counts remain the true totals — so trim `evidence.excerpt` to the lines that prove the finding rather than pasting a dump, and never drop a real finding to keep the ledger short.

**`recommendation` — required on every finding.** Three non-empty strings, no exceptions, on `gcloud` and `manual` findings that will never become a PR just as much as on promotable ones. You write it now because the evidence is in front of you now; deferring it to the moment a human asks for the fix is how the reasoning gets lost.

- `action` — what to do. Imperative, one or two sentences.
- `rationale` — why **this** fix and not the obvious alternative. Name the alternative you considered and say why you rejected it.
- `risk` — what breaks when it is applied, and the read-only check to run first.

Worked example, for a 2.6 finding on the `payments` namespace:

```json
"recommendation": {
  "action": "Apply a default-deny NetworkPolicy to the payments namespace.",
  "rationale": "Namespace-scoped default-deny is the smallest change that closes east-west exposure without touching mesh config; a mesh AuthorizationPolicy would also work but takes effect only for injected pods.",
  "risk": "Any unlabelled cross-namespace traffic into payments breaks on apply. Enumerate what currently reaches it first with `kubectl get svc,endpoints -n payments`, and land the per-service allow rules in the same change."
}
```

Three `rationale`/`risk` pairs in this SOP are check-specific and must not be written generically: 2.8 and 2.9 both recreate nodes, so say so in `risk`; 2.10's `risk` must state that an incomplete CIDR list locks every operator out of the API server, which is why the list comes from a human.

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit compliance-audit \
  --findings-file /opt/data/scratch/findings_compliance-audit.json
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[]}
```

`finish` owns publication end to end. Tier 1 is one ledger issue for this stream, rewritten in place every run and labelled `agent:audit`, `audit:compliance-audit`, `severity:<highest>`; a clean run closes it as completed. Tier 2 is a narrow remediation PR per remediation group, branched `platform-agent/fix-compliance-audit-<slug>-<digest>` off `main` — the digest is taken over the group's sorted remediation paths, so the branch is keyed on the files the fix touches and stays put across runs even though the finding ids are re-derived every morning — linked with `Part of #<issue>` and additionally labelled `audit:remediation`. A PR opens automatically only for a finding that is `critical` **and** `manifest` **and** has no **live** pull request on its branch: one the harness closed itself carries `audit:stale-closed` and may be promoted again, while one a human closed and one that merged may not, because re-opening either would overrule a person every morning. Capped at five per run, with any withheld findings named in the ledger. Everything else waits for a repo writer to comment `/remediate <finding-id>` or `/remediate all`. A comment naming ids arrives as `pending_remediation_requests` on the next run's `start`, so you know which manifests to write while inspecting; `all` does not, because it names no particular file — it is expanded at `finish` against that run's manifest findings. Every such comment gets exactly one answer on the ledger — an acknowledgement naming each target and what became of it, or a single refusal saying why (the commenter has no write access, an id is not in the current document, or the target is not a `manifest`) — so a standing request is answered once, not re-answered every run.

**Partial coverage.** `partial` is `true` when this run cannot speak for the whole fleet: anything in `scope.skipped`, or any cluster carrying a `limitations` string, and `coverage_gaps` names each one in a readable sentence. It constrains what the run is allowed to conclude, because a finding absent from a cluster you never read is not a finding that was fixed. So a partial run reports `resolved: 0` and posts no resolved-delta, closes no remediation PR as stale, and leaves the ledger open even with zero findings — `status` is still `CLEAN`, but the issue survives with a comment naming the gaps. Note the reach of this on a fleet with Autopilot clusters: the `limitations` string §1 records for them makes every run partial, which is intended — checks 2.1–2.3 and 2.9 never ran there, so the run is in no position to announce one of their findings fixed. The flag means coverage and only coverage — it is `true` if and only if `coverage_gaps` is non-empty. §4's body budget dropping findings from the description does not raise it: those findings were seen, the title's counts include them, and the body names what it dropped.

**No finding this SOP produces meets the auto-promotion bar.** Every `manifest` check here is `major` or `minor` (2.6, 2.7, 2.11) and every `critical` check is `gcloud` or `manual`, so every remediation PR from this stream is human-requested. That is deliberate: the fixes worth shipping unattended are the ones a reviewer can merge without a conversation, and none of these are. Never inflate a severity to force a PR open.

- `status == "CLEAN"` with `resolved: 0` **and** `partial: false` → the ledger issue closes as completed and your final response is exactly `[SILENT]`. No preamble, no "no issues found". A clean fleet is a silent fleet.
- `status == "CLEAN"` with `resolved: > 0` → the fleet was carrying findings and is not any more. Report it: the issue URL, and how many findings closed with it. This is the one piece of good news the audit produces, and swallowing it while reporting every failure teaches the operator that the audit only ever brings problems.
- `status == "CLEAN"` with `partial: true` → not silent. The ledger stayed open because the run could not see the whole fleet: one line reporting the clean result and `coverage_gaps`, then stop.
- `status == "UPDATED"` with `new: 0` **and** `resolved: 0` **and** `partial: false` → also exactly `[SILENT]`. Nothing moved; the ledger already says everything you would. All three conditions must hold — a run that found nothing new but skipped a cluster still has something to report.
- `status == "OPENED"`, or `"UPDATED"` with a non-zero `new` or `resolved` → one line, then stop: `Security & RBAC posture audit: <new> new, <resolved> resolved across <count(scope.clusters)> clusters — <issue_url>`
- Exit 2 means the validator rejected the document and nothing was published: fix the findings file and re-run `finish`. Exit 1 is fatal. Exit 0 published. Do not work around the validator, and never open the issue or a PR by hand.
- A finding that still reproduces after its remediation PR merged renders in the ledger with a `⚠ fix merged, still reproduces` warning and the merged PR gets one comment. The audit never reopens it, and neither do you — re-verify the finding and let the next run carry it.

## Red Lines

- **Read-only.** No `kubectl apply|patch|create|delete|edit|scale|exec|run|port-forward|cp`, no `gcloud container clusters|node-pools update`, no write of any kind against any cluster. `gcloud container clusters get-credentials` is the sole exception and touches only a local kubeconfig.
- **No hand-written issue body, PR body, branch, commit, or `gh` call.** `audit_report.py` owns the entire git/GitHub path. Never call `gh issue create` — one stream has one ledger and `finish` owns it — never open a remediation PR yourself, and do not invoke `submit-suggestion` from this SOP.
- **No unreproducible findings.** No `evidence.command`, no finding. Never soften something you could not verify into a lower severity or a "possible issue" — delete it.
- **No finding without a `recommendation`.** All three sub-fields, non-empty, on every finding, written while the evidence is still in front of you.
- **No unstable ids.** Never derive an `id` from a pod suffix, ReplicaSet hash, timestamp, or loop counter; unstable ids make every run look like a fleet of new problems and destroy the delta. An id that violates the §2 charset is rejected outright — it has to be a legal git branch component.
- **No inference from an unaudited cluster.** A cluster you could not read goes in `scope.skipped` and never appears in a finding. A cluster you read where some checks did not run — Autopilot's 2.1–2.3 and 2.9, a command that errored — stays in `scope.clusters` with a `limitations` string. Never demote a partially-checked cluster to `scope.skipped`: that silently discards every real finding from the checks that did run on a cluster you were told to audit.
- **No forbidden sources.** No BigQuery, Prometheus, Policy Controller / Gatekeeper, Security Command Center, external blueprint, or CMDB — and no kanban delegation to Cluster Agents. This audit runs entirely in the Platform Agent.
- **Never print raw credentials.** ServiceAccount tokens, kubeconfig contents, Secret `data:` blocks, and private keys never appear in `evidence.excerpt` — record the object reference, or re-run with a field selector or `-o jsonpath` that omits the value. The harness's redaction is a backstop, not permission.
