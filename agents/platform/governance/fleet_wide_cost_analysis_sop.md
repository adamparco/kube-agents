# SOP: Fleet-wide Cost Analysis (Daily Governance)

**Purpose:** Aggregates node instance type layouts and cluster resource requests across the GKE fleet to identify daily cost deltas and compute right-sizing optimization opportunities.

---

## Execution Checklist

### 1. Gather Node Topology & Billing Layouts

For each GKE cluster retrieved directly using native GKE monitoring and read-only tools:

1.  Inspect active node configurations directly using native GKE monitoring and read-only tools:
2.  Extract:
    - Instance Types (e.g., `e2-standard-4`, `n2-highmem-8`).
    - Pricing Model (Spot VMs vs. Standard On-Demand).
    - Unused/idle CPU and Memory allocations.

### 2. Compute Optimization Opportunities

1.  **Spot VM Candidate Search:** Identify namespaces running non-critical, stateless development workloads on expensive standard On-Demand VMs.
2.  **Idle Capacity Reclamation:** Identify nodes where aggregate Pod CPU/Memory _requests_ are less than `40%` of the node's capacity.
3.  **Orphaned Spend:** Identify unattached persistent disks, reserved capacity nothing is scheduling onto, and orphaned static addresses.

### 3. Reclaim It

Savings you identified and did not act on are not savings. Take the ones that are yours to take:

1.  **Submit the reclamation** with your **`apply-change` skill** (`trigger_source: cron`) — release the orphaned address, drop the idle reservation, shift the eligible node pool to Spot.
2.  **Deletion is where the gate lives.** Removing anything stateful or non-reconstructable — a persistent disk, a snapshot, a reserved commitment — is **gated** by the broker and parks for a human on the approval roster. That is deliberate: the change is irreversible in a way a config edit is not. Submit it anyway, name who was asked, say plainly that nothing has been deleted yet, and keep going. Never re-shape a gated deletion into something that would classify lower.
3.  **Workload right-sizing belongs to the tier that owns the workload.** Pod requests and limits inside a namespace are the Developer Team Agent's; node pools inside a cluster are the Cluster Admin Agent's. **Delegate** in one hop with the evidence and the number, and report what the callee answered — do not reach into their scope, and do not settle for emailing them a chart.

### 4. Report

Four beats (02 §2.5.4), plus the comparative billing picture: what you noticed (the daily cost delta and where it comes from), what you reclaimed with each `ActionRecord` ID and the monthly USD it saves, how you verified it, and the undo handle (`/kage undo <action-id>`). List savings parked for approval and with whom, savings delegated and to whom, and savings refused with the reason.
