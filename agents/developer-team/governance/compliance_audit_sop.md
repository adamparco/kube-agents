# SOP: Compliance Audit (Weekly Governance)

**Purpose:** Audits the workloads inside your one assigned namespace against corporate security and architectural policy, and hardens what it finds — through the Action Broker, in the same run.

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

### 3. Remediate the Violations

A compliance finding you are allowed to fix and did not fix is not a finding, it is a defect (02 §2.5.1). Close each one on this run:

1.  **Submit the fix** with your **`apply-change` skill** (`trigger_source: cron`), one envelope per violation — the missing `NetworkPolicy` created, the privileged `securityContext` dropped, `runAsNonRoot` set, the host-level access removed.
2.  **Let the broker classify it.** All of the above are tightenings, which are routine and simply happen. The Action Broker resolves your scope, plans the undo, gates anything that needs a human, executes, verifies and journals an `ActionRecord`. You never mutate the cluster directly — the identity in your pod has no write verb — and you never open a pull request or an issue in place of acting.
3.  **Above your ceiling goes up:** a finding that needs a cluster-scoped fix goes one hop to your parent Cluster Admin Agent with **`escalate`**. Act on the structured reply; never route around a refusal or a pause.

### 4. Report

Four beats (02 §2.5.4) for your namespace: the violations found with exact workload and pod names; what you fixed with each `ActionRecord` ID; how you verified it; and the undo handle (`/kage undo <action-id>`). List separately what is parked for approval and with whom, what was refused and why, and what you escalated. Failures and unresolved violations first, unsoftened.
