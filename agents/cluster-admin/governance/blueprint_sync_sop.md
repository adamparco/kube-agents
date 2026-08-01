# SOP: Blueprint Sync (Daily Governance)

**Purpose:** Audits this cluster and its namespaces against the master platform blueprints and reconciles what it finds, unprompted, through the Action Broker — every mutation brokered, journaled and reversible (invariant 3).

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

If any discrepancies or configuration drifts are identified, correct them on this run:

1.  Form the corrected configuration as concrete operations — the fields that must change, not a document about them.
2.  Submit them with your **`apply-change` skill** (`trigger_source: cron`). The Action Broker resolves your scope, classifies the risk, plans the undo, gates what needs a human, executes, verifies and journals an `ActionRecord`. Re-enabling private nodes is a tightening and routine; turning a control off is gated — the broker decides which, not you, and you never withhold a correction because you expect it to be gated.
3.  You hold no write credential: never `kubectl apply`/`gcloud` the change yourself, and never open a pull request or an issue for a correction you are allowed to make (02 §2.5.1).
4.  **Boundaries:** a deviation in a namespace's own workloads is that Developer Team Agent's work — **delegate** it in one hop and report what the callee answered. A cluster-level setting the Platform tier owns is above your ceiling — **escalate** it one hop up and act on the structured reply.

### 4. Report

Four beats (02 §2.5.4): what drifted, what you changed with its `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Name anything the broker gated (who was asked; nothing has changed yet), anything it refused (reason verbatim), and anything you delegated or escalated and to whom.
