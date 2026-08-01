# SOP: Policy Propagation (Hourly Governance)

**Purpose:** Proactively propagates the latest security, networking, and resource policy changes from the platform defaults down to all active GKE clusters and managed namespaces.

---

## Execution Checklist

### 1. Target Selection

- Retrieve the active GKE clusters list directly using native GKE monitoring and read-only tools.

### 2. Distribute Policies

For each active GKE cluster in the fleet:

1.  **Sync Pod Security Policies:**
    - Read your local default templates folder: `/opt/defaults/templates/`.
    - Extract the latest baseline `NetworkPolicy` and `ResourceQuota` YAML manifests.
2.  **Compare against live:**
    - Inspect the live policies inside GKE directly using native GKE monitoring and read-only tools, and list every baseline object that is missing or has diverged.
3.  **Apply the baseline:**
    - Submit each missing or diverged object with your **`apply-change` skill** (`trigger_source: cron`). The Action Broker resolves your scope, classifies the risk, plans the undo, gates what needs a human, executes, verifies and journals an `ActionRecord`. Adding a baseline `NetworkPolicy` or `ResourceQuota` is a tightening and routine; relaxing an existing one is gated — the broker decides, you submit.
    - Namespace-scoped tenancy objects inside a cluster are the Cluster Admin Agent's to apply: **delegate** those in one hop rather than reaching into the cluster. Your broker refuses them anyway.
    - If the baseline you were given is itself wrong for a cluster, say so and escalate it — never diverge from the baseline silently, and never edit the templates to match reality.

### 3. Report

Four beats (02 §2.5.4): which clusters and namespaces were missing the baseline, what you applied with each `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Name what the broker gated (who was asked; nothing has changed yet), what it refused (reason verbatim), and what you delegated to whom.
