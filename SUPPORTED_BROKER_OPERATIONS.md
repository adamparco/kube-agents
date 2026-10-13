# Supported Action Broker & ChatOps Operations (Live System Verified)

**Cluster:** `kube-agents-host` (`adamparco-kage`, `us-east4`)  
**Namespace:** `kubeagents-system`  
**ChatOps Bot:** `kaap` (`spaces/xL_K1KAAAAE`)  
**Latest Deployed Images:**
- `kage-broker`: `us-east4-docker.pkg.dev/adamparco-kage/kube-agents/kage-broker@sha256:51c8a6772e16aaaef052c41a2d53a10be85f99aea856dd7475c54b429d0e1f39`
- `chatops-gateway`: `us-east4-docker.pkg.dev/adamparco-kage/kube-agents/chatops-gateway@sha256:3871ef5ef9629ccd8079841d57f45d3b0b53faa29d5d7800a0f5ef770d0abe9d`

---

## 1. Mutating Envelope Verbs & Undo Strategies

Every mutating operation flows through the 11-step Action Broker pipeline (`authenticate` -> `validate` -> `resolve-scope` -> `classify` -> `brake` -> `undo-plan` -> `gate` -> `snapshot` -> `execute` -> `verify` -> `journal`) and is recorded as an immutable `ActionRecord` in `kubeagents-system`.

| Verb | Target Kind(s) Tested Live | Risk Class & Rule(s) | Undo Strategy | Live Action IDs & Verification Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **`scale`** | `apps/v1 Deployment` (`test-nginx`) | `gated` (`gate-deployment-scale`) | `restore` (`scale` to prior replicas) | `01M4HEPHMMDXES6ARS6NBVGWYD` -> `/approve` -> `Verified` (`3/3 replicas available`) -> `/undo` (`01M4HERVJEV51HJ5P7XC4SEFDK`) restored `2/2` replicas & marked `contested: true` -> `/uncontest` cleared contested |
| **`create` (Undoable)** | `v1 ConfigMap` (`broker-live-cm`), `v1 ServiceAccount` (`broker-test-sa`), `v1 Secret` (`broker-test-secret`) | `elevated` (`novel-action`) or `gated` (`identity-change`, `secret-write`) | `delete` (`UndoDelete` with post-create UID binding) | `01M4HPCVWJHPFMWW514XEV6HE0` -> `Verified` -> `/undo` (`01M4HPD1FGV02B9G144DR234KS`) deleted `ConfigMap/broker-live-cm` via `UndoDelete` -> `/uncontest` cleared contested |
| **`create` (No-Undo / Effectful)** | `batch/v1 Job` (`broker-test-job2`) | `gated` (`no-undo-plan`: `effectfulKinds`, `novel-action`) | `none` (`UndoNone`) | `01M4HQ645Z2RP178QPZ6VJA0WJ` -> `/approve` executed & verified `Job/broker-test-job2` and reported `Undo Plan: none (this action cannot be automatically undone)` -> `/undo` cleanly refused (`strategy is none`) |
| **`patch`** | `v1 ConfigMap` (`broker-demo-patch`), `v1 LimitRange` (`broker-test-lr`) | `routine` (familiar kind/verb) or `elevated` (`novel-action`) | `restore` (`apply` pre-state snapshot) | `01M4HQ4R2F7SK3XY8HX461W1A9` (`mode=v2`) -> `Verified` -> `/undo` (`01M4HQ4VJ5BXEHBS15215RVDGD`) restored `mode=v1` & marked `contested: true` -> `/uncontest` -> re-patch `01M4HQ59K2DJPVPQBC0TEK95CC` (`Verified`) |
| **`apply` (Server-Side Apply)** | `v1 ConfigMap` (`broker-demo-apply`) | `routine` (familiar kind/verb) | `restore` (`apply` pre-state snapshot) | `01M4HQ5G6GKXMW37N2PA4EVHH8` (`mode=ssa-updated`) -> `Verified` -> `/undo` (`01M4HQ5NRS1K5H9WGH7ZNVA1M4`) restored `mode=initial` -> `/uncontest` cleared contested |
| **`delete` (UndoRecreate)** | `v1 ConfigMap` (`broker-test-del-recreate`, no inbound references) | `gated` (`delete` code-floor rule) | `recreate` (`apply` sanitized pre-state snapshot) | `01M4HQPNE1N8DW7N81BEQJJ3QZ` -> `/approve` deleted object & verified `object-absent[0]` -> `/undo` (`01M4HQQHX2N8ARWFN3GEPNW278`) recreated `ConfigMap/broker-test-del-recreate` with `{"preserve":"true"}` -> `/uncontest` |
| **`delete` (Inbound `ownerReference` Downgrade)** | `v1 ConfigMap` (`broker-test-owner` referenced by `broker-test-dependent` via `ownerReference`) | `gated` (`delete` + `no-undo-plan` downgrade) | `none` (downgraded from `recreate` by `refindex.Source`) | `01M4HQRJJ6RQTN7FA8V6TNXYCT` -> `refindex.Source` detected `ConfigMap kubeagents-system/broker-test-dependent (via ownerReference (controller))` and downgraded `strategy` to `none` -> `/reject` cancelled |
| **`delete` (Stateful / Non-Recreatable)** | `v1 PersistentVolumeClaim` (`broker-test-pvc`) | `gated` (`destructive-stateful-delete` + `no-undo-plan`) | `none` (`nonRecreatableKinds`) | `01M4HQW66TA9E76E1WT6ZB0SR1` -> classified `gated` with `destructive-stateful-delete` and `strategy: none` -> `/reject` cancelled |
| **`--dry-run`** | `v1 ConfigMap` (`broker-dryrun-cm`) | `routine` | `restore` (validated via dry-run, not mutated) | `01M4HR0WD2JY86M4AXEPRF68RA` -> `phase: "DryRun"`, `decision: "accepted"`, live `ConfigMap` untouched |

---

## 2. Risk Classifier & Code-Floor Rules Verified Live

| Rule Name | Classification Effect | Trigger Condition | Live Action ID / Verification |
| :--- | :--- | :--- | :--- |
| **`forbidden-set`** | `forbidden` (`Rejected`) | Mutating self-referential control-plane resources (`Agent`, `ChangePolicy`, `ApprovalRoster`, `FleetFreeze`, `ActionRecord`, `UndoRequest`, webhooks, `kube-system`) | `01M4HQZW0JEXD2A24D09YAPNB8`: `patch ApprovalRoster/default` refused (`403 Forbidden`, `phase: "Rejected"`, `reason: "forbidden"`) |
| **`out-of-scope`** | `out-of-scope` (`Rejected`) | Target namespace/cluster/project outside agent's `ScopeSpec` | `patch ConfigMap` in `default` namespace refused by NATS subject/scope isolation |
| **`security-loosen`** | `gated` (`PendingApproval`) | Loosening pod/container `securityContext` (`runAsNonRoot: false`, `privileged: true`, `hostNetwork: true`, etc.) or deleting/widening `NetworkPolicy` | `01M4HR9C6RHHXW1GEWS3Q09XSA` (`patch Deployment/test-nginx` `runAsNonRoot: false`) & `01M4HR9Q6HVVY3Y66BZXG1N4Q4` (`delete NetworkPolicy/broker-test-netpol`) -> `gated` -> `/reject` |
| **`public-exposure`** | `gated` (`PendingApproval`) | Changing `Service.spec.type` to `LoadBalancer`/`NodePort` or widening `Ingress`/`Gateway` exposure | `01M4HRA723RZ84QE1JZK9FCR54` (`patch Service/broker-test-svc` `type: NodePort`) -> `gated` -> `/reject` |
| **`traffic-shift-production`** | `gated` (`PendingApproval`) | Modifying `Service.spec.selector` or route weights on a `Service`/`HTTPRoute`/`Ingress` | `01M4HRA723RZ84QE1JZK9FCR54` (`patch Service/broker-test-svc` `spec.selector`) -> `gated` -> `/reject` |
| **`identity-change`** | `gated` (`PendingApproval`) | Creating/modifying `ServiceAccount`, `Role`, `RoleBinding`, `ClusterRole`, `ClusterRoleBinding`, or `spec.serviceAccountName` | `01M4HQY7Q28DDS6SZ0GPRG98GG` (`create ServiceAccount/broker-test-sa`) -> `gated` -> `/approve` -> `Verified` -> `/undo` (`UndoDelete`) -> `/uncontest` |
| **`secret-write` + SHA-256 Redaction** | `elevated` (`+1` to `gated` when novel) | Creating/updating a `v1/Secret`; secret values in `ActionRecord` preState, undo plan, and applied diff are replaced with `sha256:<hex>` digests | `01M4HQYZSQNAZ0PNNFQ0T1ZC0T` (`create Secret/broker-test-secret`) -> `/approve` -> `Verified`; `/data/api_key` stored as `sha256:92478d06df298c7b74d002eb6e4154e8b86f945f49de7b767e82dd0e1762c6d9` |
| **`secret-material-egress`** | `gated` (`PendingApproval`) | Writing known `Secret` material into a non-`Secret` resource (`ConfigMap`, `Deployment` env, etc.) | `01M4HRAH4RAWCY9GJ6Y1GPY5VE` (`patch ConfigMap/broker-egress-target2` containing value from `Secret/broker-egress-src`) -> `gated` via `secret-material-egress` -> `/reject` |
| **`destructive-stateful-delete`** | `gated` (`PendingApproval`) | Deleting stateful resources (`PersistentVolumeClaim`, `StatefulSet`, `Namespace`, `CustomResourceDefinition`) | `01M4HQW66TA9E76E1WT6ZB0SR1` (`delete PersistentVolumeClaim/broker-test-pvc`) -> `gated` -> `/reject` |
| **`no-undo-plan` / `undo-plan-unusable`** | `gated` (`PendingApproval`) | Envelope has no validated automatic undo plan (`Strategy: none`) | `01M4HQ645Z2RP178QPZ6VJA0WJ` (`create Job/broker-test-job2`), `01M4HQRJJ6RQTN7FA8V6TNXYCT` (ownerRef downgrade) |
| **`object-override`** | Floor raised to annotation value (`gated` / `elevated`) | Target carries `kube-agents/risk-class: <class>` annotation | `01M4HQZJKWN8YB321EHWF782R7` (`patch ConfigMap/broker-override-cm` with `kube-agents/risk-class: gated`) -> `gated` -> `/reject` |
| **`production-environment`** | `+1` class step (`routine` -> `elevated`, `elevated` -> `gated`) | Target or namespace carries `kube-agents/environment: production` | `01M4HRB1YSPN13B5BSPNSCC8MB` (`patch ConfigMap/broker-prod-cm` with `kube-agents/environment=production`) -> escalated `routine` -> `elevated` -> `Verified` |
| **`novel-action`** | `+1` class step (`routine` -> `elevated`, `elevated` -> `gated`) | Agent has no prior `Verified` non-dry-run record for `(verb, group/kind, namespace)` | Verified across first-time `create`/`patch`/`apply` operations on each kind |
| **`caller-requested-approval`** | `gated` (`PendingApproval`) | Agent sets `requireApproval: true` (`--require-approval`) on the envelope | `01M4HRB58ACM6C08M4E55DM8E8` (`patch ConfigMap/broker-reqapp-cm --require-approval`) -> `gated` -> `/approve` -> `Verified` |
| **`blast-radius-cap`** | `gated` (`PendingApproval`) | Envelope object count exceeds effective `maxObjects` cap (`min(50, caller.maxObjects, policy.maxObjects)`) | `01M4HX7V851DRD7CKWKGWP4ZE3` (3-object `ConfigMap` create envelope with `maxObjects: 2`) -> `gated` (`blast-radius-cap`: `"affects 3 objects, over the 2 that run without approval"`, `blastRadius: {"cap": 2, "fractionOfScope": "0.03", "objects": 3}`) -> `/approve` via `kaap` -> `Verified` -> `/undo` + `/uncontest` via `kaap` |
| **`blast-radius-hard-cap`** | `abort` (`422 Unprocessable Entity`, `Rejected`) | Envelope affects `> 100` objects OR `fractionOfScope > 0.50` (`AbortScopeFraction = 0.5`) | `01M4HX93KSGAWME173R9N36C5N` (50-object `ConfigMap` create envelope in `kubeagents-system`, `50 / 86 = 58% > 50%`) -> refused outright with `reason: "blast-radius-cap-exceeded"`, `message: "blast-radius-hard-cap: affects 50 objects, 58% of the objects in scope, over the hard cap of 50%"` |

---

## 3. Brake, Envelope Schema & Recovery Ladder Controls Verified Live

| Control / Refusal Reason | Stage | Trigger Condition | Live Verification Outcome |
| :--- | :--- | :--- | :--- |
| **Multi-Object Atomic Envelope + Multi-Step `/undo`** | Steps 4–10 (`classify` -> `execute` -> `verify` -> `undo`) | Envelope contains `2..50` operations within blast-radius caps | `01M4HW262A4XWBZFAGAR8A8CHM` (2-op `create` of `ConfigMap/blast-ok-1` and `ConfigMap/blast-ok-2`) -> `Verified` (`blastRadius: {"objects": 2, "fractionOfScope": "0.01"}`) -> `/undo` via `kaap` deleted both ConfigMaps in reverse order (`1 -> 0`) -> `/uncontest` cleared contested state |
| **Automatic Rung-2 Rollback (`RolledBack`)** | Step 10 (`verify`) | Target fails verification or its settle window expires (`SettleWindowExpired (Terminal)`); driver climbs to Rung 3 (`RungRollback`) and replays the validated undo plan | `01M4HRNAZGDGFDV67195YKRZG9` (`create Deployment/broker-rollback-deploy2` -> `RolledBack` via `UndoDelete`) and `01M4HSH9NWWZQG7FQRJ8J806SX` (`patch LimitRange/broker-test-lr` -> 15s settle window expired -> `RolledBack` via `UndoRestore` to `stage=v1`) |
| **`cooldown-active`** | Step 5 (`brake`) | Target is inside its exponential post-rollback quiet period (`BaseCooldown = 5m`, doubling per consecutive rollback up to `8h`) | Attempting `patch LimitRange/broker-test-lr` after `01M4HSH9NWWZQG7FQRJ8J806SX` rolled back was immediately refused with `decision: "refused"`, `reason: "cooldown-active"`, `message: "target LimitRange kubeagents-system/broker-test-lr is in post-rollback cooldown until 2026-10-10T02:26:48Z"` |
| **`target-contested`** | Step 5 (`brake`) | Target was reverted by a human via `/undo` (`status.contested: true`) and has not yet been cleared via `/uncontest` | Attempting `patch ConfigMap/broker-demo-patch` after `/undo 01M4HQ4R2F7SK3XY8HX461W1A9` was refused with `decision: "refused"`, `reason: "target-contested"` until `/uncontest 01M4HQ4R2F7SK3XY8HX461W1A9` was run |
| **`flap-detected`** | Step 5 (`brake`) | Target has been mutated 3 times within the 30-minute flap window (`FlapThreshold = 3`) | 4th mutation on `ConfigMap/broker-demo` within 30m refused with `decision: "refused"`, `reason: "flap-detected"` |
| **`blast-radius-cap-exceeded` (`AbortScopeFraction > 0.5`)** | Step 4 (`classify`) | Operation or multi-operation envelope affects `> 50%` of workload objects in scope (or `> 100` objects total) | `01M4HX93KSGAWME173R9N36C5N` (50-object envelope in `kubeagents-system`, `58%` of scope) refused with `422 Unprocessable Entity`, `decision: "refused"`, `reason: "blast-radius-cap-exceeded"` |
| **`invalid-envelope` (`MaxLiteralOperations`)** | Step 2 (`validate`) | Envelope contains `> 50` operations (`MaxLiteralOperations = 50`) | 51-operation envelope refused with `decision: "refused"`, `reason: "invalid-envelope"`, `message: "51 operations, the limit is 50"` |
| **`selector-expansion-unavailable`** | Step 3 (`resolve-scope`) | Operation uses `targetSelector` instead of explicit `target` | Refused with `decision: "refused"`, `reason: "selector-expansion-unavailable"` |
| **`cloud-target-unavailable`** | Step 3 (`resolve-scope`) | Operation uses `cloudTarget` on K8s-only broker | Refused with `decision: "refused"`, `reason: "cloud-target-unavailable"` |

---

## 4. ChatOps Commands (`kaap` Bot) & LLM Journal Queries

| Command / Query | Syntax | Live Behavior & Enriched Output |
| :--- | :--- | :--- |
| **`/approve`** | `/approve <ACTION_ID>` | Records roster approval with NATS capability ref (`root.approve-<id>@<rev>`), waits up to 15s for `ResumeController` execution + verification, and replies with full action summary (`Intent`, `Risk Class`, `Targets`, `Phase: Verified`, `Verification` details, and `Undo: /undo <ID>` or `Undo Plan: none`). |
| **`/reject`** | `/reject <ACTION_ID> [reason...]` | Transitions `PendingApproval` -> `Rejected` and replies with `Intent`, `Targets`, `Rejected by`, and `Reason`. |
| **`/undo`** | `/undo <ACTION_ID> [reason...]` | Creates an `UndoRequest` (`markContested: true`), waits up to 15s for `UndoReconciler` to replay the undo plan (`restore`, `delete`, or `recreate`), transitions the original record to `Phase: Undone` + `Contested: true`, and replies with the reverse steps executed and `/uncontest <ACTION_ID>` instructions. Refuses cleanly when `spec.undo.strategy == "none"`. |
| **`/uncontest`** | `/uncontest <ACTION_ID>` | Clears `status.contested` on the `ActionRecord`, syncs the broker's `ContestedIndex`, and replies confirming that the agent may act on the target(s) again. |
| **LLM Status Query (`--op status`)** | `python3 /opt/scripts/broker_client.py --op status --action-id <ACTION_ID>` | Queries the live `ActionRecord` (including `phase`, `classification`, `approvals`, `verification`, `recovery`, `contested`, and `undo`) so `kaap` can explain any action's live state in natural language. |
| **LLM List Query (`--op list`)** | `python3 /opt/scripts/broker_client.py --op list --limit <N>` | Lists the most recent `ActionRecord`s in `kubeagents-system` sorted newest-first. |

---

## 5. Issues Discovered, Fixed, and Rolled Out During Live Testing

1. **Enriched ChatOps Replies & Synchronous Completion Waiting ([`dispatch.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/approval/gateway/dispatch.go), [`cmd/chatops-gateway/main.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/cmd/chatops-gateway/main.go)):**
   - Added synchronous polling (`WaitTimeout: 15s`) on `/approve` and `/undo` plus rich multi-line status summaries (`Intent`, `Targets`, `Verification`, `Reverse Steps`, `Contested` status, and `Undo Plan: none` notices for non-undoable actions).
2. **NATS Capability Ref Threading for `ResumeController` ([`a2a/gateway/gateway.go`](file:///usr/local/google/home/adamparco/kube-agents/a2a/gateway/gateway.go), [`approval/write.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/approval/write.go)):**
   - Threaded the minted `root.approve-<id>@<rev>` capability reference into `ar.Status.Approvals.Granted[].Comment` so `ResumeController` can authorize resumption over NATS.
3. **`409 Conflict` Retry in `journal.Store.SetPhase` ([`journal/store.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/journal/store.go)):**
   - Wrapped `SetPhase` status updates in `retry.RetryOnConflict` to prevent transient resourceVersion conflicts between the broker and controller-manager.
4. **`UndoDelete` Post-Execution UID Binding ([`execute/apply.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/execute/apply.go), [`undo/replay.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/undo/replay.go), [`pipeline/pipeline.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/pipeline/pipeline.go)):**
   - Recorded `/metadata/uid` in `status.applied[].diff` on `create` and added `undo.HydratePlanFromRecord` so `UndoDelete` steps have the created object's UID precondition required by `rollback.Replayer`.
5. **Server-Side Apply Self-Conflict Retry in `ClientApplier.Apply` ([`execute/client.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/execute/client.go)):**
   - Added `isOnlySelfFieldManagerConflict` so `UndoRestore` (or `apply` after `patch`) retries with `client.ForceOwnership` when the only conflicting field manager is the broker's own `kube-agents/<tier>/<scope>` manager, while still refusing foreign field manager conflicts.
6. **`Resume` Rejecting Approved `no-undo-plan` Actions at `stepBrake` ([`pipeline/pipeline.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/pipeline/pipeline.go)):**
   - Fixed `stepBrake` during `Resume` so actions already classified and approved with `Spec.Classification.Undoable == false` (such as `create batch/v1 Job` or `delete PersistentVolumeClaim`) pass `UndoPlan: broker.BrakeOK` at row 5 rather than re-raising `BrakeRaiseToGated` and failing resumption.
7. **`refindex.Source` Discovery Read RBAC on `ClusterRole/kube-agents-broker-actor-role`:**
   - Granted `get, list, watch` across API groups on `ClusterRole/kube-agents-broker-actor-role` so `refindex.Source.InboundReferences` can scan all namespaced kinds for inbound `ownerReferences` when planning `delete` operations (`UndoRecreate` vs `UndoNone` downgrade).
8. **Post-Rollback Cooldown Enforcement in `stepBrake` ([`pipeline/pipeline.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/pipeline/pipeline.go)):**
   - Wired `p.cfg.Verifier.Cooldown.Active(ctx, verify.TargetKey(t), s.at)` into `stepBrake` so targets in post-rollback quiet period are refused with `429 TooManyRequests` (`reason: "cooldown-active"`).
9. **`PodSpec.serviceAccount` Deprecated Alias in `CheckIntegrity` & `Resume` Failure Phase ([`execute/integrity.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/execute/integrity.go), [`pipeline/resume.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/pipeline/resume.go)):**
   - Taught `coveredBy` in `execute/integrity.go` to recognize Kubernetes's automatic sync between `serviceAccountName` and deprecated `serviceAccount` in `PodSpec`, and updated `Resume` to mark the `ActionRecord` `Failed` if `stepExecute`/`stepVerify` returns an error after transitioning to `Executing`.
10. **Multi-Operation Blast Radius Counting, `EffectiveMaxObjects` Gate & `ActionRecord.Spec.Classification.BlastRadius` Recording ([`pipeline/pipeline.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/pipeline/pipeline.go), [`classify/classify.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/classify/classify.go), [`broker_client.py`](file:///usr/local/google/home/adamparco/kube-agents/agents/platform/scripts/broker_client.py)):**
    - Updated `rawOps` in `pipeline.go` so multi-operation envelopes count distinct targets per namespace (instead of hardcoding `ObjectCount: 1` per operation), populated `spec.classification.blastRadius` (`objects`, `fractionOfScope`, `cap`) on every `ActionRecord`, enforced `EffectiveMaxObjects` (`min(GateObjectThreshold, caller.MaxObjects, policy.MaxObjects)`) in `classifyOne`, and wired `maxObjects` / `--max-objects` in `broker_client.py`.
11. **Workload Denominator Exclusions for Broker Audit & Ephemeral Per-Pod Resources ([`classify/blast.go`](file:///usr/local/google/home/adamparco/kube-agents/k8s-operator/internal/broker/classify/blast.go)):**
    - Added `ActionRecord`, `UndoRequest` (`kubeagents.x-k8s.io`), `Lease` (`coordination.k8s.io`), `Endpoints` (`v1`), `PodMetrics` (`metrics.k8s.io`), and `CiliumEndpoint` (`cilium.io`) to `ExcludedFromDenominator` so accumulated audit records and per-pod ephemeral objects do not inflate the workload denominator and disarm `AbortScopeFraction` (`> 50%` hard cap).

