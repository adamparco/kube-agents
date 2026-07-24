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

### 3. Generate Standardization Audit Log

- List all non-standard resources and violations in your namespace in a structured weekly diff report. Where a fix is warranted, propose the corrected manifest through your **`submit-suggestion` skill** (in your `developer-team-agent/` branch namespace) as a reviewed Pull Request; never mutate the cluster directly.
</content>
