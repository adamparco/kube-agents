# SOP: Obtainability Audit (Daily Governance)

**Purpose:** Audits your namespace's workload configurations for rigid, high-risk resource allocations (e.g. hardcoded hostname bindings, static zone selectors) and realigns them with flexible, schedulable capacity — through the Action Broker, in the same run.

---

## Execution Checklist

### 1. Confirm Namespace Scope

- You steward **one** namespace. Inspect workload configuration rigidity only within that single namespace, using native GKE monitoring and read-only tools. You cannot read other namespaces or any cluster-scoped resource; do not enumerate or act on anything beyond your namespace.

### 2. Obtainability & Rigidity Auditing Rules

Within your one namespace, inspect workload configuration rigidity directly:

1.  **Static Node Bindings Audits:**
    - Query: `"kubectl get deployments,statefulsets -n <namespace> -o json"`
    - 🚨 **Rigid Allocation:** Any workload utilizing `nodeSelector` targeting a specific hostname (e.g., `kubernetes.io/hostname`) or a specific zone (e.g., `topology.kubernetes.io/zone: <zone>`) is flagged.
    - _Why:_ This prevents the cluster autoscaler from dynamically scheduling pods across flexible node pools, leading to capacity bottlenecks.
2.  **Autoscaling Compliance Audits:**
    - Query: `"kubectl get deployments,horizontalpodautoscalers -n <namespace> -o json"`
    - 🚨 **Rigid Allocation:** Any deployment running with `replicas: > 3` that **lacks** an associated `HorizontalPodAutoscaler` (HPA) resource is flagged as a rigid capacity allocation.

### 3. Apply the Remediation

If rigid allocations are identified, unstick them on this run. Generating a patch and stopping there is a defect (02 §2.5.1).

1.  **Form the operations:**
    - Remove static node selectors and replace them with flexible scheduling (e.g. referencing an approved `ComputeClass` provided by the cluster) so the autoscaler can place pods freely.
    - Create the missing `HorizontalPodAutoscaler` for the rigid deployment.
2.  **Submit them with your `apply-change` skill** (`trigger_source: cron`), one envelope per workload. The Action Broker resolves your scope, classifies the risk, plans the undo, gates what needs a human, executes, verifies and journals an `ActionRecord`. Adding an HPA is routine; anything that could drop capacity under a live workload may not be — submit it and let the broker decide.
3.  **Above your ceiling goes up:** node pools, ComputeClass definitions and cluster-level scheduling constraints are not yours — your broker refuses them. **Escalate** those one hop to your parent Cluster Admin Agent and act on the structured reply.
4.  **Report** in four beats (02 §2.5.4): what was rigid, what you changed with its `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Name what the broker gated, what it refused, and what you escalated.
