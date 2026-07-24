# SOP: Obtainability Audit (Daily Governance)

**Purpose:** Audits your namespace's workload configurations to identify rigid, high-risk resource allocations (e.g., hardcoded hostname bindings, static zone selectors) and generates remediation YAML patches to align them with flexible, schedulable capacity.

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

### 3. Generate Remediation Recommendations

If rigid allocations are identified:

1.  **Synthesize YAML patches:** Dynamically generate the recommended K8s YAML patches:
    - Remove static node selectors and replace them with flexible scheduling (e.g., referencing an approved `ComputeClass` provided by the cluster) so the autoscaler can place pods freely.
    - Generate a `HorizontalPodAutoscaler` (HPA) spec for the rigid deployment.
2.  **Propose, don't apply:** Submit the patches through your **`submit-suggestion` skill** (in your `developer-team-agent/` branch namespace) as a reviewed Pull Request; never apply them directly to the cluster.
3.  **Log in daily report:** Document the list of audited workloads and generated patches in your namespace's daily Obtainability Report.
</content>
