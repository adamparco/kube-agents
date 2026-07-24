# SOP: Blueprint Sync (Daily Governance)

**Purpose:** Audits this cluster and its namespaces against the master platform blueprints to ensure configuration consistency and propose reconciliation of any drift.

---

## Execution Checklist

### 1. Confirm Cluster Scope

- You administer **one** cluster. Operate only on this cluster's own configuration and its namespaces, using native GKE monitoring and read-only tools. Do not enumerate or act on any other cluster.

### 2. Audit This Cluster's Live Configuration

For this cluster:

1.  Inspect the live cluster and namespace manifests directly using native GKE monitoring and read-only tools.
2.  Compare the returned configuration against the **Platform Master Blueprint**:
    - ✅ `enableAutopilot` must be `true`.
    - ✅ `privateClusterConfig.enablePrivateNodes` must be `true`.
    - ✅ `privateClusterConfig.enablePrivateEndpoint` must be `false`.
    - ✅ `metadata.annotations["cnrm.cloud.google.com/remove-default-node-pool"]` must be `"true"`.

### 3. Reconcile Configuration Drift

If any discrepancies or configuration drifts are identified:

1.  Generate the corrected declarative YAML for the drifted resource.
2.  **Do NOT apply the changes directly to the cluster control plane.**
3.  Exclusively utilize your **`submit-suggestion` skill** to commit the corrected manifest to a GitOps branch (in your `cluster-admin-agent/` branch namespace) and **submit a GitHub Pull Request (PR)** for human review and approval. Cluster-level settings that belong to the Platform tier are escalated upward to the Platform Agent rather than changed here.
4.  Log a detailed summary of the drift and the submitted PR link in your session output.
