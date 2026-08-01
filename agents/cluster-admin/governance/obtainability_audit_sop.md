# SOP: Obtainability Audit (Daily Governance)

**Purpose:** Audits this cluster's workload configurations for rigid, high-risk node resource allocations (e.g. hardcoded hostname bindings, static zone selectors) and realigns them with flexible capacity pools — through the Action Broker, in the same run.

---

## Execution Checklist

### 1. Confirm Cluster Scope

- You administer **one** cluster. Inspect workload configuration rigidity only across this cluster's namespaces, using native GKE monitoring and read-only tools. Do not enumerate or act on any other cluster.

### 2. Obtainability & Rigidity Auditing Rules

Across this cluster's namespaces, inspect workload configuration rigidity directly:

1.  **Static Node Bindings Audits:**
    - Query: `"kubectl get deployments,statefulsets -A -o json"`
    - 🚨 **Rigid Allocation:** Any workload utilizing `nodeSelector` targeting a specific hostname (e.g., `kubernetes.io/hostname`) or a specific zone (e.g., `topology.kubernetes.io/zone: <zone>`) is flagged.
    - _Why:_ This prevents the cluster autoscaler from dynamically scheduling pods across flexible node pools, leading to capacity bottlenecks.
2.  **Autoscaling Compliance Audits:**
    - Query: `"kubectl get deployments -A -o json"`
    - 🚨 **Rigid Allocation:** Any deployment running with `replicas: > 3` that **lacks** an associated `HorizontalPodAutoscaler` (HPA) resource is flagged as a rigid capacity allocation.

### 3. Apply the Remediation

If rigid allocations are identified, unstick them on this run. Generating a patch and stopping there is a defect (02 §2.5.1).

1.  **Form the operations:**
    - Remove static node selectors and replace them with standard `ComputeClass` node tolerations.
    - Create the missing `HorizontalPodAutoscaler` for the rigid deployment.
2.  **Submit them with your `apply-change` skill** (`trigger_source: cron`), one envelope per workload. The Action Broker resolves your scope, classifies the risk, plans the undo, gates what needs a human, executes, verifies and journals an `ActionRecord`. Adding an HPA is routine; anything that could drop capacity under a live workload may not be — submit it and let the broker decide.
3.  **Respect the boundary:** node pools, ComputeClasses and cluster-level scheduling constraints are yours to apply. Workload specs inside a namespace belong to its Developer Team Agent — **delegate** those in one hop and report what the callee answered; your broker refuses them anyway.
4.  **Report** in four beats (02 §2.5.4): what was rigid, what you changed with its `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Name what the broker gated, what it refused, and what you delegated to whom.
