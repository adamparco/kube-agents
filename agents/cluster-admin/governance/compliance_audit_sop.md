# SOP: Compliance Audit (Weekly Governance)

**Purpose:** Performs a security and architectural policy audit across all of this cluster's namespaces.

---

## Execution Checklist

### 1. Confirm Cluster Scope

- You administer **one** cluster. Audit only this cluster's namespaces directly using native GKE monitoring and read-only tools. Do not enumerate or act on any other cluster.

### 2. GKE Security Auditing Rules

Across this cluster's namespaces, execute these auditing checks directly using native GKE monitoring and read-only tools:

1.  **Workload Hardening Audits:**
    - Query: `"kubectl get pods -A -o jsonpath='{.items[*].spec.containers[*].securityContext.privileged}'"`
    - 🚨 **Policy Violation:** Any container running with `privileged: true` must be logged immediately as a Critical Violation.
2.  **Namespace Isolation Audits:**
    - Query: `"kubectl get networkpolicies -A"`
    - 🚨 **Policy Violation:** Every namespace (except `kube-system` and `cnrm-system`) **must** possess an active `NetworkPolicy` that restricts ingress/egress. Any namespace lacking an active `NetworkPolicy` is a Major Violation.
3.  **RBAC Over-Privilege Audits:**
    - Query: `"kubectl get clusterrolebindings -o json"`
    - 🚨 **Policy Violation:** Verify that no non-system service accounts have been granted the `cluster-admin` role. Wildcard `*` bindings on resources are strictly forbidden for non-system workloads.

### 3. Remediate the Violations

A compliance finding you are allowed to fix and did not fix is not a finding, it is a defect (02 §2.5.1). Close each one on this run:

1.  **Submit the fix** with your **`apply-change` skill** (`trigger_source: cron`), one envelope per violation — the missing `NetworkPolicy` created, the over-privileged binding withdrawn.
2.  **Let the broker classify it.** Adding a missing `NetworkPolicy` or quota is a tightening and routine. Withdrawing a `cluster-admin` binding is an identity change and is **gated**: it parks for a human on the approval roster and nothing changes until that person approves. That gate is deliberate — submit the change anyway, name who was asked, say plainly that nothing has changed yet, and keep working the rest. Never re-shape a gated fix into something that would classify lower.
3.  **Stay inside your surface.** Workload-level hardening inside a namespace belongs to its Developer Team Agent: **delegate** in one hop and report what the callee answered. Anything above your cluster ceiling goes up with **`escalate`**.
4.  You hold no write credential — never mutate the cluster directly, and never open a pull request or an issue in place of acting.

### 4. Report

Four beats (02 §2.5.4) for this cluster: the violations found with exact namespaces and object names; what you fixed with each `ActionRecord` ID; how you verified it; and the undo handle (`/kage undo <action-id>`). List separately what is parked for approval and with whom, what was refused and why, and what you delegated or escalated. Failures and unresolved violations first, unsoftened.
