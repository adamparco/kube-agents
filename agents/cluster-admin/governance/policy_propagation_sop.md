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
2.  **Propagate and Verify:**
    - Inspect and verify that the policies are active in this cluster's namespaces directly using native GKE monitoring and read-only tools.
    - Where a namespace is missing a baseline policy, propose the corrected manifest through your **`submit-suggestion` skill** (in your `cluster-admin-agent/` branch namespace); never apply it directly to the cluster.

### 3. Log Sync Completion

- Record the list of successfully synchronized namespaces in this cluster in the cron job run log.
