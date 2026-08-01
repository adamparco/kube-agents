# SOP: Policy Propagation (Hourly Governance)

**Purpose:** Proactively propagates the latest security, networking, and resource policy changes from the platform defaults down to this cluster's managed namespaces.

---

## Execution Checklist

### 1. Confirm Cluster Scope

- You administer **one** cluster. Operate only on this cluster's namespaces, using native GKE monitoring and read-only tools. Do not enumerate or act on any other cluster.

### 2. Distribute Policies

Across this cluster's managed namespaces:

1.  **Sync Pod Security Policies:**
    - Read your local default templates folder: `/opt/defaults/templates/`.
    - Extract the latest baseline `NetworkPolicy` and `ResourceQuota` YAML manifests.
2.  **Compare against live:**
    - Inspect the live policies in this cluster's namespaces directly using native GKE monitoring and read-only tools, and list every baseline object that is missing or has diverged.
3.  **Apply the baseline:**
    - Submit each missing or diverged object with your **`apply-change` skill** (`trigger_source: cron`). The Action Broker resolves your scope, classifies the risk, plans the undo, gates what needs a human, executes, verifies and journals an `ActionRecord`. Adding a baseline `NetworkPolicy` or `ResourceQuota` is a tightening and routine; relaxing an existing one is gated — the broker decides, you submit.
    - Namespace tenancy objects are yours to apply; the **workloads inside** a namespace are not — **delegate** those to the owning Developer Team Agent in one hop and report what the callee answered.
    - If the baseline you were given is itself wrong for this cluster, **escalate** it rather than diverging from it silently, and never edit the templates to match reality.

### 3. Report

Four beats (02 §2.5.4): which namespaces were missing the baseline, what you applied with each `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Name what the broker gated (who was asked; nothing has changed yet), what it refused (reason verbatim), and what you delegated or escalated.
