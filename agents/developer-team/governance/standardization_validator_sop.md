# SOP: Standardization Validator (Weekly Governance)

**Purpose:** Performs a deep-diff structural audit between your namespace's live workload/manifest configurations and the standard corporate architectural patterns to prevent configuration drift and metadata chaos.

---

## Execution Checklist

### 1. Confirm Namespace Scope

- You steward **one** namespace. Audit only the workloads inside that single namespace directly using native GKE monitoring and read-only tools. You cannot read other namespaces or any cluster-scoped resource; do not enumerate or act on anything beyond your namespace.

### 2. Standardization Verification Rules

Within your one namespace, run these read-only standardization audits directly using native GKE monitoring and read-only tools:

1.  **Resource Labeling Compliance:**
    - Query: `"kubectl get deployments,services -n <namespace> -o json"`
    - 🚨 **Standard Violation:** Every active deployment and service **must** possess the following standard metadata labels:
      - `app.kubernetes.io/name` (identifying the application)
      - `owner` (identifying the engineering team)
      - `environment` (identifying `dev`, `staging`, or `prod`)
    - Any resource lacking these three labels is a Non-Standard Violation.
2.  **Private Service Exposition compliance:**
    - Query: `"kubectl get services -n <namespace> -o jsonpath='{.items[?(@.spec.type=="LoadBalancer")].status.loadBalancer.ingress[*].ip}'"`
    - 🚨 **Standard Violation:** No Service in your namespace is allowed to expose a **public External LoadBalancer IP** unless it has the explicit annotation `platform.harness.io/public-exposition-approved: "true"`. Public endpoints exposed without this approval represent a High-Risk Architectural Violation.

### 3. Correct the Non-Standard Resources

Close the diff on this run rather than restating it weekly:

1.  **Submit each correction** with your **`apply-change` skill** (`trigger_source: cron`) — the three missing standard labels patched on, the unapproved public LoadBalancer withdrawn or made internal.
2.  **The two are not the same kind of change, and the broker is what knows that.** Adding a label is routine and simply happens. Withdrawing a public endpoint another team may still be calling is traffic-affecting and will likely be **gated** — submit it, name who was asked, say plainly that nothing has changed yet, and move on. Never re-shape a gated correction into something that would classify lower, and never leave a High-Risk Architectural Violation unsubmitted because you assumed it would be blocked.
3.  You never mutate the cluster directly and never open a pull request for a correction you are allowed to make. A standard that can only be fixed above your namespace goes one hop up with **`escalate`**.

### 4. Report

Four beats (02 §2.5.4): the non-standard resources found in your namespace, what you corrected with each `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Include the remaining diff — what is parked for approval and with whom, what was refused and why, and what you escalated.
