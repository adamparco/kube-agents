---
title: Agent CRD
description: The single custom resource the operator reconciles, across all three tiers.
sidebar:
  order: 1
---

The `Agent` resource declares everything the operator needs to run one agent instance: which tier it
is, where it operates, which Hermes image, which service account, which chat integrations, and which
framework-level toggles.

One kind serves all three tiers. `spec.tier` is the discriminator — it selects the persona, the
containment level, and which of the other fields are required.

- **API group / version**: `kubeagents.x-k8s.io/v1alpha1`
- **Kind**: `Agent` (list kind `AgentList`, plural `agents`, singular `agent`)
- **Source**: [`k8s-operator/api/v1alpha1/agent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/agent_types.go)
- **Sample**: [`k8s-operator/examples/agent.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/agent.yaml)

## Top-level shape

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: agent
  namespace: kubeagents-system
  labels:
    kube-agents/tier: platform
spec:
  tier: platform # platform | cluster-admin | developer-team. Immutable.
  scope: { ... } # where the agent operates — required fields vary by tier
  parentRef: { ... } # required for every non-platform tier
  harness: { ... } # execution environment + framework
  deployment: { ... } # container image, pull policy, resources
  security: { ... } # service account + Workload Identity
  integration: { ... } # Google Chat, Slack, GitHub
  iac: { ... } # which IaC artifact this agent authors when it proposes a change
```

## `spec.tier`

| Tier             | Scope                 | Parent                | Cloud permissions |
| ---------------- | --------------------- | --------------------- | ----------------- |
| `platform`       | fleet (1 per project) | none — it is the root | viewer-only       |
| `cluster-admin`  | one cluster           | the platform agent    | viewer-only       |
| `developer-team` | one namespace         | a cluster-admin agent | viewer-only       |

Defaults to `platform`. **Immutable after creation** — a CEL rule on the field rejects any change,
because the tier is what every downstream containment decision is derived from. To re-tier an agent,
delete it and create a new one.

Every tier is read-only at the cloud boundary. The only write path is a reviewed GitOps PR applied by
the CI/CD pipeline.

## `spec.scope` and `spec.parentRef`

`scope` names where the agent operates; which subfield is required depends on the tier. `parentRef`
links a non-platform agent to the agent above it and is **required for every tier except
`platform`** — the admission webhook rejects a cluster-admin or developer-team `Agent` without one,
so no agent can exist outside the chain of custody.

## `spec.harness`

Framework-level settings passed to Hermes.

| Field                                    | Type   | Purpose                                                                              |
| ---------------------------------------- | ------ | ------------------------------------------------------------------------------------ |
| `clusterName`                            | string | Logical cluster name (e.g. `cluster-a`). Surfaces in observability and chat replies. |
| `location`                               | string | Cloud region (e.g. `us-central1-a`).                                                 |
| `hermes.dashboardEnabled`                | bool   | Toggle the Hermes dashboard endpoint. Default `true`.                                |
| `hermes.pluginsDebug`                    | bool   | Enable plugin-level debug logging. Default `false`.                                  |
| `hermes.apiServerSecretRef.name` + `key` | string | `Secret` holding the Hermes API server key.                                          |

## `spec.deployment`

Standard container spec: `image`, `imagePullPolicy`, `resources`, node selectors, tolerations. The
controller synthesises a `Deployment` from these plus the workspace ConfigMaps.

Default image: `ghcr.io/gke-labs/kube-agents/platform-agent`. Rebuild with
`make dev-rebuild-agent ARGS="platform"` for local iteration.

## `spec.security`

- `serviceAccountName` — the KSA the pod runs as. `kubeagents-platform-agent` by convention for the
  platform tier; each subordinate tier gets its own.
- `serviceAccountAnnotations` — passed through to the KSA. Typically holds
  `iam.gke.io/gcp-service-account` for Workload Identity binding.

The Workload Identity target GSA (`kubeagents-platform-gsa@<project>.iam.gserviceaccount.com`) is
created and bound by `provision_04_gcp_iam.sh` with one of these permission sets:

- `read-only` (default) — viewer-only GKE, monitoring, and logging roles.
- `custom` — explicit, named roles for operators who must extend the set (an auditable, opt-in
  deviation).

The `gke-admin` preset is retired: a stale value is coerced to `read-only`, and any admin bindings
left from a prior `gke-admin` run are actively removed on the next provision.

## `spec.integration`

Enables external integrations. Only the enabled ones need to be present.

- **`googleChat`** — Pub/Sub subscription name, project ID, allowed users. Populated by
  `provision_05_gcp_gchat.sh`.
- **`slack`** — token Secret refs, home channel, allowed users. Populated by
  `provision_06_slack.sh` when `SLACK_ENABLED=true`.
- **`github`** — Minty endpoint, GitOps repo URL. Populated by
  `provision_10_deploy_github_minter.sh`.

:::caution[`allowedUsers` is required, not advisory]
When a chat integration is enabled, `allowedUsers` must contain at least one non-blank entry. An
empty — or all-whitespace — list is **rejected by admission**, and the `Agent` is never created.
There is no permissive mode and no allow-all sentinel to fall back to.
:::

See [`k8s-operator/api/v1alpha1/agent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/agent_types.go)
for the exact struct definitions.

## Reconcile behavior

- On create/update, the controller ensures the Deployment, Service, ServiceAccount, and ConfigMaps
  match the spec.
- On delete, it garbage-collects owned resources.
- The admission webhook (behind cert-manager) validates the spec before it's persisted — the tier
  discriminator, the per-tier required fields, the parent link, and the chat allowlists.
- `provision_08_deploy_platform_agent.sh` renders and applies the platform-tier CR;
  `provision_12_deploy_agent_tiers.sh` does the same for the cluster-admin and developer-team tiers.
  You can also edit either directly with `kubectl edit`.

## Where to go next

- [Development](/kube-agents/operator/development/) — build and test the controller locally.
- [Provisioning scripts](/kube-agents/operator/provisioning-scripts/) — how the CR gets applied in a
  fresh install.
