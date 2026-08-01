# SOP: Global Capacity Orchestrator (Hourly Governance)

**Purpose:** Audits aggregate resource utilization across all GKE clusters and regions, finds the hot spots, and rebalances the fleet — scaling what it owns through the Action Broker and delegating the in-cluster half to the tier that owns it.

---

## Execution Checklist

### 1. Gather Resource Metrics

For each active GKE cluster in the fleet (retrieved directly using native GKE monitoring and read-only tools):

1.  Inspect GKE resource metrics directly using native GKE monitoring and read-only tools:
2.  Calculate the total capacity vs. active utilization:
    - **Aggregate CPU Utilization (%)**
    - **Aggregate Memory Utilization (%)**

### 2. Audit Capacity Limits

Evaluate the metrics against the following **SRE Capacity Thresholds**:

- 🔴 **Critical ( > 85% Utilization):** Risk of node resource exhaustion.
- 🟢 **Under-Utilized ( < 30% Utilization):** Waste of project billing resources.

### 3. Orchestrate the Rebalance

You rebalance the fleet; you do not file recommendations about it (02 §2.5.1, 04 §4.1).

1.  **Scale up/down:** if a cluster exceeds `85%` utilization, check whether Autopilot is scaling nodes successfully. If it is not, find the unschedulable Pods and submit the fleet-level capacity change — the ComputeClass or capacity-pool adjustment that unblocks them — with your **`apply-change` skill** (`trigger_source: cron`). Adding capacity is routine; taking capacity away from a running workload is not, and the broker classifies it accordingly.
2.  **Cross-region imbalance:** if one region is consistently overloaded while another has surplus, act on it at the level you own — fleet capacity distribution — and **delegate** the in-cluster node-pool and workload-placement changes to the Cluster Admin Agent that owns each cluster. One hop, and report what the callee answered; never reach into a cluster's internals.
3.  **Under-utilization:** reclaim what is clearly waste. If reclaiming it would remove capacity a tenant may still need, submit it and let the broker gate it rather than deciding for yourself.
4.  **Report** in four beats (02 §2.5.4): the current Fleet Resource Map, what you changed with each `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Name each delegation and its callee handle, and anything the broker gated or refused.
