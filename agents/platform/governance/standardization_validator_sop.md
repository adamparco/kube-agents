# SOP: Standardization Validator (Weekly Governance)

**Purpose:** Performs a deep-diff structural audit between the live GKE configurations and standard corporate architectural patterns to prevent configuration drift and metadata chaos.

---

## Execution Checklist

### 1. Auditing Target Fleet

- Retrieve the active GKE clusters list directly using native GKE monitoring and read-only tools.

### 2. Standardization Verification Rules

For each active GKE cluster, run these standardization audits directly using native GKE monitoring and read-only tools:

1.  **Resource Labeling Compliance:**
    - Query: `"kubectl get deployments,services -A -o json"`
    - 🚨 **Standard Violation:** Every active deployment and service **must** possess the following standard metadata labels:
      - `app.kubernetes.io/name` (identifying the application)
      - `owner` (identifying the engineering team)
      - `environment` (identifying `dev`, `staging`, or `prod`)
    - Any resource lacking these three labels is a Non-Standard Violation.
2.  **Private Service Exposition compliance:**
    - Query: `"kubectl get services -A -o jsonpath='{.items[?(@.spec.type=="LoadBalancer")].status.loadBalancer.ingress[*].ip}'"`
    - 🚨 **Standard Violation:** No GKE Service inside a development namespace is allowed to expose a **public External LoadBalancer IP** unless it has the explicit annotation `platform.harness.io/public-exposition-approved: "true"`. Public endpoints exposed without this approval represent a High-Risk Architectural Violation.

### 3. Correct the Non-Standard Resources

Close the diff on this run rather than restating it weekly:

1.  **Submit each correction** with your **`apply-change` skill** (`trigger_source: cron`) — the three missing standard labels patched on, the unapproved public LoadBalancer withdrawn or made internal.
2.  **The two are not the same kind of change, and the broker is what knows that.** Adding a label is routine and simply happens. Withdrawing a public endpoint a team may still be depending on is a traffic-affecting change and will be classified accordingly, likely **gated** — submit it, name who was asked, say plainly that nothing has changed yet, and move on to the next finding. Never re-shape a gated correction into something that would classify lower, and never leave a High-Risk Architectural Violation unsubmitted because you assumed it would be blocked.
3.  **Stay inside your surface:** cluster internals and namespace workloads belong to the tier that owns them — **delegate** in one hop and report what the callee answered.

### 4. Report

Four beats (02 §2.5.4): the non-standard resources found, what you corrected with each `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Include the remaining diff — what is parked for approval and with whom, what was refused and why, and what you delegated.
