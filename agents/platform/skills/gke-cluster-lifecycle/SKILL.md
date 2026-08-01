---
name: gke-cluster-lifecycle
description: Guidance on managing the lifecycle and upgrades of GKE clusters declaratively — every change is an Action Envelope submitted to the broker, never a direct gcloud mutation.
---

# GKE Cluster Lifecycle and Upgrades

This skill provides guidance on managing the lifecycle and upgrades of Google Kubernetes Engine (GKE)
clusters. Managing cluster upgrades is crucial for security and access to new features.

> **The Platform Agent holds no write credential and never mutates clusters directly.** Every lifecycle
> change is made by **describing the change to the cluster's declarative object** (the KCC
> `ContainerCluster`/`ContainerNodePool`, or its Terraform equivalent) and submitting it with the
> [apply-change](../apply-change/SKILL.md) skill. There is no `gcloud container clusters update`
> path: the broker classifies the envelope, plans an undo, gates it if a human must approve, executes
> it, and journals an `ActionRecord`. (02 §2.2, 06 §4, §9)

## How to Make a Lifecycle Change

1. **Locate the object**: find the cluster's declarative source — the live KCC
   `ContainerCluster`/`ContainerNodePool`, or the mirrored artifact at
   `clusters/<cluster>/provisioning/<cluster>.yaml` and its Terraform equivalent. If the cluster is not
   managed declaratively at all, say so — never fall back to a direct mutation.
2. **Change the field, not the machine**: pick the relevant field (release channel, upgrade settings, a
   new node pool), preserving the security defaults (private nodes, VPC-native, Workload Identity,
   Shielded Nodes).
3. **Submit through the broker**: hand the change to [apply-change](../apply-change/SKILL.md) as the
   operations of one envelope, with an `intent` naming the cluster and the reason. `plan_action` first
   if you are unsure — it returns the risk class and the undo plan and changes nothing. Report the
   `actionId` and what happened: executed, parked for approval, or refused. An upgrade that is parked
   has not happened yet, and must not be described as though it had.

## Workflows

### 1. Select Release Channels

Release channels balance stability against feature availability.

- **Rapid**: Newest features, less tested.
- **Regular** (Default): Good balance.
- **Stable**: Most tested, best for critical production workloads.

**Declarative edit** — set `spec.releaseChannel.channel` on the `ContainerCluster`:

```yaml
spec:
  releaseChannel:
    channel: STABLE
```

_(Terraform equivalent: `release_channel { channel = "STABLE" }` on the `google_container_cluster`.)_

### 2. Configure Surge Upgrades

Surge upgrades specify how many nodes may be created above target size during an upgrade, minimizing
disruption.

**Declarative edit** — set `spec.upgradeSettings` on the `ContainerNodePool`:

```yaml
spec:
  upgradeSettings:
    maxSurge: 2
    maxUnavailable: 0
```

Setting `maxUnavailable: 0` ensures that no nodes are taken offline before new ones are ready.
_(Terraform equivalent: `upgrade_settings { max_surge = 2, max_unavailable = 0 }` on the
`google_container_node_pool`.)_

### 3. Implement Blue/Green Node Pool Upgrades

For high-risk upgrades, add a new node pool (Green) with the new version alongside the existing pool
(Blue), shift workloads, then remove the old pool — all declaratively, and as **two separate
envelopes** so each step is classified, journalled and undoable on its own:

1. **Envelope 1 — add the Green pool**: a second `ContainerNodePool` with the new version and
   appropriate taints (`spec.nodeConfig.taint`), leaving the Blue pool in place. Let workloads migrate
   before going further.
2. **Envelope 2 — remove the Blue pool**: delete the old `ContainerNodePool` once it is drained and
   empty. Deleting a node pool is high blast radius; expect this one to be gated.

Cordon/drain of the old pool is its own change and belongs in its own envelope (or with the customer's
SRE, if they own the migration) — do not fold it into either of the two above, where its undo plan
would be tangled with the pool's.

## Best Practices

1. **Everything through an envelope**: never run `gcloud`/`kubectl` mutations — you hold no credential
   for them. Submitting the change with `apply-change` is what makes it classified, attributable, and
   revertible.
2. **Use Release Channels**: always enroll production clusters in a release channel (preferably `Stable`
   or `Regular`).
3. **Configure Surge Upgrades**: use `maxSurge`/`maxUnavailable` to ensure availability during upgrades.
4. **Use Maintenance Windows**: configure maintenance windows so upgrades only happen off-peak. (The
   `gke-reliability` skill covers this in depth and belongs to the Cluster Admin tier — 02 §2.1 — so it
   is not available here; `delegate` is how that work reaches the tier that holds it.)
5. **Test in Non-Prod**: always test upgrades in a staging environment before submitting them against
   production.
