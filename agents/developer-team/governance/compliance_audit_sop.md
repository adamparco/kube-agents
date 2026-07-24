# SOP: Compliance Audit (Weekly Governance)

**Purpose:** Performs a read-only security and architectural policy audit of the workloads inside your one assigned namespace.

---

## Execution Checklist

### 1. Confirm Namespace Scope

- You steward **one** namespace. Audit only the workloads inside that single namespace directly using native GKE monitoring and read-only tools. You cannot read other namespaces or any cluster-scoped resource; do not enumerate or act on anything beyond your namespace. If a finding implies a cluster-scoped fix, escalate it to your parent Cluster Admin Agent.

### 2. GKE Security Auditing Rules

Within your one namespace, execute these read-only auditing checks directly using native GKE monitoring and read-only tools:

1.  **Workload Hardening Audits:**
    - Query: `"kubectl get pods -n <namespace> -o jsonpath='{.items[*].spec.containers[*].securityContext.privileged}'"`
    - 🚨 **Policy Violation:** Any container running with `privileged: true` must be logged immediately as a Critical Violation.
2.  **Namespace Isolation Audits:**
    - Query: `"kubectl get networkpolicies -n <namespace>"`
    - 🚨 **Policy Violation:** Your namespace **must** possess an active `NetworkPolicy` that restricts ingress/egress. A namespace lacking an active `NetworkPolicy` is a Major Violation.
3.  **Workload Privilege Audits:**
    - Query: `"kubectl get pods,deployments -n <namespace> -o json"`
    - 🚨 **Policy Violation:** Flag any workload requesting host-level access (`hostNetwork`, `hostPID`, `hostIPC`, or `hostPath` volumes) or running as root (`runAsNonRoot` absent/false). These over-privileged workload settings are a Major Violation.

### 3. Report & Warn

- Generate a formatted compliance markdown report for your namespace.
- If violations are found, present them clearly to the namespace's engineers with exact workload names, pod names, and remediation instructions (e.g., recommended NetworkPolicy or `securityContext` YAML). Propose any fix through your **`submit-suggestion` skill** (in your `developer-team-agent/` branch namespace) as a reviewed Pull Request; never mutate the cluster directly.
</content>
