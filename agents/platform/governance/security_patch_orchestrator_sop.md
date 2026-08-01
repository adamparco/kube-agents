# SOP: Security Patch Orchestrator (Daily Governance)

**Purpose:** Scans the GKE fleet for outdated Kubernetes control plane and node versions, audits active security CVEs, and coordinates the staggered, zero-downtime rollout of GKE upgrades.

---

## Execution Checklist

### 1. Audit GKE Control Plane & Node Versions

For each active GKE cluster retrieved directly using native GKE monitoring and read-only tools:

1.  Inspect the active GKE master and node versions directly using native GKE monitoring and read-only tools:
2.  Query the GCP GKE regional server configuration to find the latest available GKE security patches in the target region:
    ```bash
    gcloud container get-server-config --region="<location>" --project="agentic-harness-demo" --format="json"
    ```

### 2. Identify Security Vulnerabilities

- Compare the active GKE version against the **Latest Stable Security Patch** returned by the server configuration.
- Identify if the active GKE version contains any known high-severity GKE CVEs (Common Vulnerabilities and Exposures).

### 3. Drive the Staggered Zero-Downtime Rollout

If a security patch upgrade is required, you carry it out — the staggering below is the safety
property, and it is preserved exactly:

1.  **Dev/staging first:**
    - Submit the version change for the development/staging cluster (e.g. `mercury-03`) with your **`apply-change` skill**, `trigger_source: cron`. Run `plan_action` first if you want the classification and blast radius before you commit.
    - The Action Broker resolves scope, classifies, snapshots, plans the undo, executes and journals an `ActionRecord`. Never `gcloud container clusters upgrade` it yourself; the identity in your pod cannot, by design.
2.  **Soak, and verify before you promote:**
    - Wait until the upgraded cluster has been provisioned and **healthy for 30 minutes** — control plane and node versions reported at the target, no new crash-looping or unschedulable workloads. This wait is a real precondition, not a formality; do not promote on an unverified dev upgrade.
3.  **Then production — and expect the gate:**
    - Submit the production cluster's version change (e.g. `mercury-04`) the same way. A production control-plane upgrade is **gated**: the broker parks it for a human on the approval roster, and nothing changes until that person approves. That human step is retained deliberately — it exists because the change is high-blast-radius, not because you cannot act.
    - Submit it anyway, name who was asked, say plainly that nothing has changed yet, and **do not idle waiting for the approval** — carry on with the rest of the sweep. Never re-shape a gated upgrade into something that would classify lower.
4.  **Report:**
    - Four beats (02 §2.5.4) per cluster: what you noticed, what you did with its `ActionRecord` ID (or what is parked and with whom), how you verified it, and the undo handle (`/kage undo <action-id>`). State the current position in the staggered rollout and which clusters remain exposed.
