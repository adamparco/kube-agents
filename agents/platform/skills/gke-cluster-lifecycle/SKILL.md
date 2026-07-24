---
name: gke-cluster-lifecycle
description: Guidance on managing the lifecycle and upgrades of GKE clusters declaratively — every change is a reviewed GitOps Pull Request, never a direct gcloud mutation.
---

# GKE Cluster Lifecycle and Upgrades (GitOps)

This skill provides guidance on managing the lifecycle and upgrades of Google Kubernetes Engine (GKE)
clusters. Managing cluster upgrades is crucial for security and access to new features.

> **The Platform Agent is read-only and never mutates clusters directly.** Every lifecycle change is
> made by **editing the cluster's declarative artifact** (the KCC `ContainerCluster`/`ContainerNodePool`
> under `clusters/<cluster>/provisioning/`, or its Terraform equivalent) and opening a PR with the
> [submit-suggestion](../submit-suggestion/SKILL.md) skill. There is no `gcloud container clusters
update` path — the customer's CI/CD pipeline applies the merged change. (06 §9, §4)

## How to Propose a Lifecycle Change

1. **Locate the artifact**: find the cluster's declarative source at
   `clusters/<cluster>/provisioning/<cluster>.yaml` (KCC) or the matching Terraform file. If it isn't in
   the repo yet, ask the user to import it first — never fall back to a direct mutation.
2. **Edit the field, not the cluster**: change the relevant field (release channel, upgrade settings, a
   new node pool) in the artifact, preserving the security defaults (private nodes, VPC-native, Workload
   Identity, Shielded Nodes).
3. **Propose via GitOps**: hand off to [submit-suggestion](../submit-suggestion/SKILL.md) on a
   `platform-agent/upgrade-<cluster>` branch, staging only the edited provisioning file(s). Return the PR
   URL. The change takes effect only when a human reviews and merges it.

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
(Blue), shift workloads, then remove the old pool — all declaratively, across one or more PRs so each
step is reviewable and revertible:

1. **PR 1 — add the Green pool**: author a second `ContainerNodePool` artifact with the new version and
   appropriate taints (`spec.nodeConfig.taint`), leaving the Blue pool in place. Merge and let workloads
   migrate.
2. **PR 2 — remove the Blue pool**: delete the old `ContainerNodePool` artifact once it is drained and
   empty.

Cordon/drain of the old pool is an operational step performed by the customer's SRE (or their pipeline)
against the read replicas — the agent's role is limited to authoring the declarative add/remove PRs.

## Best Practices

1. **Everything through a PR**: never run `gcloud`/`kubectl` mutations — edit the artifact and open a PR
   so every lifecycle change is reviewed, attributable, and revertible.
2. **Use Release Channels**: always enroll production clusters in a release channel (preferably `Stable`
   or `Regular`).
3. **Configure Surge Upgrades**: use `maxSurge`/`maxUnavailable` to ensure availability during upgrades.
4. **Use Maintenance Windows**: configure maintenance windows so upgrades only happen off-peak (see
   [gke-reliability](../gke-reliability/SKILL.md)).
5. **Test in Non-Prod**: always test upgrades in a staging environment before proposing them for
   production.
