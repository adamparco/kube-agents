# SOP: Obtainability Audit (Daily Governance)

**Purpose:** Audits this cluster's workload configurations to identify rigid, high-risk node resource allocations (e.g., hardcoded hostname bindings, static zone selectors) and generates remediation YAML patches to align them with flexible capacity pools.

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

### 3. Generate Remediation Recommendations

If rigid allocations are identified:

1.  **Synthesize YAML patches:** Dynamically generate the recommended K8s YAML patches:
    - Remove static node selectors and replace them with standard `ComputeClass` node tolerations.
    - Generate an `HorizontalPodAutoscaler` (HPA) spec for the rigid deployment.
2.  **Propose, don't apply:** Submit the patches through your **`submit-suggestion` skill** (in your `cluster-admin-agent/` branch namespace) for human review; never apply them directly to the cluster.
3.  **Log in daily report:** Document the list of audited workloads and generated patches in this cluster's daily Obtainability Report.
