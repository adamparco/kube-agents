# SOP: Blueprint Sync (Daily Governance)

**Purpose:** Audits all managed GKE clusters against the master platform blueprints and reconciles what it finds, unprompted, through the Action Broker — every mutation brokered, journaled and reversible (invariant 3).

---

## Execution Checklist

### 1. Identify Target Fleet

- Retrieve the active GKE clusters list directly using native GKE monitoring and read-only tools.

### 2. Audit Live GKE Configurations

For each active GKE cluster in the fleet:

1.  Inspect the live containercluster manifest directly using native GKE monitoring and read-only tools:
2.  Compare the returned manifest against the **Platform Master Blueprint**:
    - ✅ `enableAutopilot` must be `true`.
    - ✅ `privateClusterConfig.enablePrivateNodes` must be `true`.
    - ✅ `privateClusterConfig.enablePrivateEndpoint` must be `false`.
    - ✅ `metadata.annotations["cnrm.cloud.google.com/remove-default-node-pool"]` must be `"true"`.

### 3. Reconcile Configuration Drift

If any discrepancies or configuration drifts are identified, correct them on this run:

1.  Form the corrected cluster configuration as concrete operations — the fields that must change, not a document about them.
2.  Submit them with your **`apply-change` skill** (`trigger_source: cron`), one envelope per cluster. The Action Broker resolves your scope, classifies the risk, plans the undo, gates what needs a human, executes, verifies and journals an `ActionRecord`. Re-enabling private nodes is a tightening and routine; turning a control off is gated — the broker decides which, not you, and you never withhold a correction because you expect it to be gated.
3.  You hold no write credential: never `kubectl apply`/`gcloud` the change yourself, and never open a pull request or an issue for a correction you are allowed to make (02 §2.5.1).
4.  Drift **inside** a cluster — its namespaces, workloads, in-cluster policy — is that Cluster Admin Agent's scope, and your broker will refuse it. **Delegate** it in one hop and report what the callee answered.

### 4. Report

Four beats (02 §2.5.4): what drifted, what you changed with its `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Name anything the broker gated (who was asked; nothing has changed yet), anything it refused (reason verbatim), and anything you delegated and to whom.
