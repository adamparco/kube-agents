---
name: gke-cluster-creator
description: Authors declarative GKE cluster artifacts (KCC or Terraform) from best-practice templates and proposes them as a GitOps Pull Request for review. Never creates clusters directly.
---

# GKE Cluster Creation Skill (GitOps)

This skill helps propose new Google Kubernetes Engine (GKE) clusters by authoring a **declarative
infrastructure artifact** — Config Connector (KCC) YAML by default, or Terraform HCL — and routing it
through a reviewed Pull Request. The customer's CI/CD pipeline applies the artifact on merge.

> **The Platform Agent is read-only and never provisions infrastructure directly.** There is no
> `create_cluster` tool and no `gcloud container clusters create` path. The **only** way to provision a
> cluster is to author a declarative artifact and open a PR with the [submit-suggestion](../submit-suggestion/SKILL.md)
> skill. This is the load-bearing Phase 1 property (06 §9, §4).

## core_behavior

1. **Template Selection**:
   - Present the available templates to the user if they haven't specified one.
   - Explain the trade-offs (e.g., Cost vs. Availability, Autopilot vs. Standard).
2. **Customization**:
   - Once a template is selected, present the default artifact (KCC YAML, or Terraform if the agent's
     `spec.iac.format` is `terraform`).
   - Ask the user for essential missing information: `project_id`, `location`, `cluster_name`.
   - Ask if they want to modify optional fields (e.g., `machineType`, node counts, network, channel).
3. **Validation**:
   - Ensure `project_id`, `location`, and `cluster_name` are set.
   - Ensure the artifact is well-formed and preserves the security defaults below (private, VPC-native,
     Workload Identity, Shielded Nodes, a release channel).
4. **Propose via GitOps** (there is no direct execution step):
   - Write the artifact into the GitOps repo under `clusters/<cluster_name>/provisioning/`.
   - Hand off to the [submit-suggestion](../submit-suggestion/SKILL.md) skill to open a PR on a
     `platform-agent/provision-<cluster_name>` branch.
   - Return the PR URL to the user; the cluster is created only when a human reviews and merges the PR.

## best_practices

When generating configurations, adhere to the following GKE cluster creation best practices. The
templates below already encode them — keep them intact when customizing.

### Security

1. **Private Clusters**: Default to private clusters with a private control plane and restricted public endpoints to minimize attack surface.
2. **VPC-Native Networking**: Use VPC-native clusters to enable alias IP ranges, which allows pod-level firewall rules and better network security.
3. **Workload Identity**: Prefer Workload Identity for securely granting GKE workloads access to Google Cloud services instead of using static service account keys.
4. **Shielded GKE Nodes**: Enable Shielded GKE Nodes to protect against rootkits and bootkits.
5. **Least Privilege (RBAC)**: Institute strict Role-Based Access Control limits granting minimal privilege to users and workloads.

### Cost Optimization

1. **Autoscaling**: Enable Cluster Autoscaler and Horizontal Pod Autoscaler to adjust resources based on demand.
2. **Right-Sizing**: Choose the appropriate machine types and node counts. Consider Spot VMs for fault-tolerant, non-critical workloads.

### High Availability & Reliability

1. **Regional Clusters**: Use Regional Clusters for production environments to ensure control plane replication across multiple zones. (Note: standard regional creates nodes across 3 zones by default).
2. **Pod Disruption Budgets**: Recommend setting Pod Disruption Budgets for application stability during node maintenance.
3. **Release Channels**: Subscribe to a release channel (e.g., Regular or Stable) for automated and safer cluster upgrades.

## templates

These are **Config Connector (KCC)** resources — the default when the agent's `spec.iac.format` is
`kcc`. Apply the placeholders `{PROJECT_ID}`, `{CLUSTER_NAME}`, and `{ZONE}`/`{REGION}`, and set
`metadata.namespace` to the project's Config Connector namespace. Standard templates remove the default
node pool and manage nodes with a separate `ContainerNodePool` so node changes never recreate the
cluster. If `spec.iac.format` is `terraform`, author the equivalent `google_container_cluster` /
`google_container_node_pool` HCL instead (see the Terraform note at the end).

### 1. Standard Zonal (Cost-Effective Dev/Test)

Best for: Development, testing, non-critical workloads.

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerCluster
metadata:
  name: "{CLUSTER_NAME}"
  namespace: config-control
  annotations:
    cnrm.cloud.google.com/project-id: "{PROJECT_ID}"
spec:
  location: "{ZONE}"
  initialNodeCount: 1
  removeDefaultNodePool: true
  networkingMode: VPC_NATIVE
  ipAllocationPolicy: {} # request GKE-managed alias IP ranges
  releaseChannel:
    channel: REGULAR
  workloadIdentityConfig:
    workloadPool: "{PROJECT_ID}.svc.id.goog"
  privateClusterConfig:
    enablePrivateNodes: true
    enablePrivateEndpoint: false
---
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerNodePool
metadata:
  name: "{CLUSTER_NAME}-pool"
  namespace: config-control
  annotations:
    cnrm.cloud.google.com/project-id: "{PROJECT_ID}"
spec:
  location: "{ZONE}"
  clusterRef:
    name: "{CLUSTER_NAME}"
  autoscaling:
    minNodeCount: 1
    maxNodeCount: 3
  management:
    autoRepair: true
    autoUpgrade: true
  nodeConfig:
    machineType: e2-medium
    diskSizeGb: 50
    shieldedInstanceConfig:
      enableSecureBoot: true
      enableIntegrityMonitoring: true
    oauthScopes:
      - "https://www.googleapis.com/auth/cloud-platform"
```

### 2. Standard Regional (High Availability)

Best for: Production workloads requiring high availability.
_Note: a regional cluster replicates the control plane and node pool across the region's zones._

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerCluster
metadata:
  name: "{CLUSTER_NAME}"
  namespace: config-control
  annotations:
    cnrm.cloud.google.com/project-id: "{PROJECT_ID}"
spec:
  location: "{REGION}"
  initialNodeCount: 1
  removeDefaultNodePool: true
  networkingMode: VPC_NATIVE
  ipAllocationPolicy: {}
  releaseChannel:
    channel: STABLE
  workloadIdentityConfig:
    workloadPool: "{PROJECT_ID}.svc.id.goog"
  privateClusterConfig:
    enablePrivateNodes: true
    enablePrivateEndpoint: false
---
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerNodePool
metadata:
  name: "{CLUSTER_NAME}-pool"
  namespace: config-control
  annotations:
    cnrm.cloud.google.com/project-id: "{PROJECT_ID}"
spec:
  location: "{REGION}"
  clusterRef:
    name: "{CLUSTER_NAME}"
  autoscaling:
    minNodeCount: 1 # per zone; a regional pool multiplies this across the region's zones
    maxNodeCount: 4
  management:
    autoRepair: true
    autoUpgrade: true
  upgradeSettings:
    maxSurge: 2
    maxUnavailable: 0
  nodeConfig:
    machineType: e2-standard-4
    diskSizeGb: 100
    shieldedInstanceConfig:
      enableSecureBoot: true
      enableIntegrityMonitoring: true
    oauthScopes:
      - "https://www.googleapis.com/auth/cloud-platform"
```

### 3. Autopilot (Operations-Free)

Best for: Most workloads where you don't want to manage nodes. Autopilot enables VPC-native networking,
Workload Identity, and Shielded Nodes by default.

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerCluster
metadata:
  name: "{CLUSTER_NAME}"
  namespace: config-control
  annotations:
    cnrm.cloud.google.com/project-id: "{PROJECT_ID}"
spec:
  location: "{REGION}"
  enableAutopilot: true
  releaseChannel:
    channel: REGULAR
  privateClusterConfig:
    enablePrivateNodes: true
    enablePrivateEndpoint: false
```

### 4. GPU Inference (L4)

Best for: AI/ML Inference, small model serving.
_Note: Requires `g2-standard-4` quota._

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerNodePool
metadata:
  name: "{CLUSTER_NAME}-gpu-l4"
  namespace: config-control
  annotations:
    cnrm.cloud.google.com/project-id: "{PROJECT_ID}"
spec:
  location: "{REGION}"
  clusterRef:
    name: "{CLUSTER_NAME}"
  autoscaling:
    minNodeCount: 0 # scale to zero when idle to control GPU cost
    maxNodeCount: 3
  management:
    autoRepair: true
    autoUpgrade: true
  nodeConfig:
    machineType: g2-standard-4
    diskSizeGb: 100
    guestAccelerator:
      - type: nvidia-l4
        count: 1
    shieldedInstanceConfig:
      enableSecureBoot: true
      enableIntegrityMonitoring: true
    oauthScopes:
      - "https://www.googleapis.com/auth/cloud-platform"
```

### 5. AI Hypercompute (A3 HighGPU)

Best for: Large Model Training/Inference.
_Note: High cost and strict quota requirements._

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerNodePool
metadata:
  name: "{CLUSTER_NAME}-a3-highgpu"
  namespace: config-control
  annotations:
    cnrm.cloud.google.com/project-id: "{PROJECT_ID}"
spec:
  location: "{REGION}"
  clusterRef:
    name: "{CLUSTER_NAME}"
  nodeCount: 1
  management:
    autoRepair: true
    autoUpgrade: true
  nodeConfig:
    machineType: a3-highgpu-8g
    diskSizeGb: 200
    guestAccelerator:
      - type: nvidia-h100-80gb
        count: 8
    shieldedInstanceConfig:
      enableSecureBoot: true
      enableIntegrityMonitoring: true
    oauthScopes:
      - "https://www.googleapis.com/auth/cloud-platform"
```

## instructions

- **ALWAYS** ask for the `project_id` if it is not in the context.
- **ALWAYS** ask for the `location` (Region or Zone).
- **ALWAYS** ask for a unique `cluster_name`.
- **PRESERVE** the security defaults (private nodes, VPC-native, Workload Identity, Shielded Nodes, a
  release channel) when customizing a template.
- **WARN** the user about cost if they select GPU or regional clusters.
- **AUTHOR, DON'T EXECUTE**: write the finished artifact to
  `clusters/<cluster_name>/provisioning/<cluster_name>.yaml` in the GitOps repo (use the Terraform
  equivalent under the same path if `spec.iac.format` is `terraform` — see **Terraform alternative**
  below). Do **not** attempt to run `gcloud`, `kubectl apply`, or any cluster-creating tool — there is
  none, and direct mutation is forbidden.
- **PROPOSE**: invoke the [submit-suggestion](../submit-suggestion/SKILL.md) skill with a branch named
  `platform-agent/provision-<cluster_name>`, staging only the provisioning files you authored. Present
  the returned PR URL to the user. The cluster is created only on human merge, when the CI/CD pipeline
  applies the artifact.

### Terraform alternative

If the agent's `spec.iac.format` is `terraform`, author the equivalent HCL instead of KCC — a
`google_container_cluster` (with `remove_default_node_pool`, `networking_mode = "VPC_NATIVE"`,
`workload_identity_config`, `release_channel`, `private_cluster_config`) plus one or more
`google_container_node_pool` resources carrying the same machine type, autoscaling, shielded-node, and
accelerator settings shown above.

**Match the canonical exemplar shape.** The repo ships a matched, parity-checked pair of committed
exemplars — copy their structure rather than inventing your own so the artifact matches the tested
shape:

- **KCC** (`iac.format: kcc`, default) —
  `examples/gitops-repo/clusters/cluster-a/provisioning/cluster-a.yaml`
- **Terraform** (`iac.format: terraform`) —
  `examples/gitops-repo/clusters/cluster-b/provisioning/{cluster.tf,variables.tf}`

The two are asserted **semantically equivalent** by `dev/tests/iac-parity.py` (same location,
release channel, node machine type/count, VPC-native / Workload-Identity / Shielded / private shape).
Keep each format in its **own** cluster dir: the actuation pipeline dispatches per directory (`*.tf` →
`terraform apply`, else `*.y*ml` → `kubectl apply`), so `.tf` and `.yaml` in one provisioning dir would
collide. Write the Terraform artifact to the cluster's own `clusters/<cluster_name>/provisioning/`
path; the pipeline runs `terraform apply` on merge.

## example_usage

**User**: "I want to create a GKE cluster."
**Model**: "I can help propose one. What kind of cluster do you need?

1. **Standard Zonal**: Good for dev/test.
2. **Standard Regional**: High availability.
3. **Autopilot**: Fully managed.
4. **GPU Enabled**: For AI/ML workloads."

**User**: "Standard Zonal, please."
**Model**: "Great. I'll need a few details:

- Project ID
- Zone (e.g., us-central1-a)
- Cluster Name"

**User**: "Project `my-proj`, zone `us-west1-b`, name `dev-cluster`."
**Model**: "Here is the KCC artifact I'll propose (private, VPC-native, Workload Identity, Shielded
Nodes, Regular channel):
[KCC YAML view]
I'll write it to `clusters/dev-cluster/provisioning/dev-cluster.yaml` and open a PR for review. Shall I
proceed?"

**User**: "Yes."
**Model**: "Opened the PR — here's the link. `dev-cluster` will be created once an SRE reviews and merges
it." _(No cluster is created until merge; the CI/CD pipeline applies the artifact.)_
