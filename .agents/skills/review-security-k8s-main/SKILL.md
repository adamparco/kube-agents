---
name: review-security-k8s-main
description: Orchestrates comprehensive Kubernetes security reviews.
---

# Task

Coordinate Kubernetes security review sub-agents, gather findings, and produce a summarized JSON report.

# Workflow

## 1. Context

Invoke `review-security-k8s-understand`. Wait for summary.

## 2. Parallel Reviews

Pass context and launch in parallel sub-agents:

- `review-security-k8s-rbac`
- `review-security-k8s-nodes`
- `review-security-k8s-network`
- `review-security-k8s-gateway`
- `review-security-k8s-namespaces`
- `review-security-k8s-service-accounts`
- `review-security-k8s-storage`
- `review-security-k8s-admission`
- `review-security-k8s-pod`
- `review-security-k8s-agents-main`

**CRITICAL**: Instruct each to output JSON:

```json
[
  {
    "agent": "<skill>",
    "findings": [
      {
        "message": "<desc>",
        "file": "<name>",
        "line": "<num>",
        "severity": "<critical|high|medium|low>"
      }
    ]
  }
]
```

(Return empty list if no findings). Wait for completion.

**Every finding MUST carry a `severity`** — one of `critical`, `high`, `medium`, `low`. Tag it with this rubric (kube-agents review-gate, [06 §7](../../../docs/design/06-api-and-data-contracts.md)):

- **critical** — an active break of the read-only ceiling or a direct privilege-escalation / cluster-takeover path: an agent `Role`/`ClusterRole` granting a write or `*` verb; a binding to `cluster-admin`; `privileged: true`, `hostPID`/`hostNetwork`/`hostIPC`, or a `hostPath` mount that exposes the node; a reachable cloud-metadata endpoint (`169.254.169.254`); a `0.0.0.0/0` egress rule.
- **high** — a serious, likely-exploitable weakening: missing default-deny `NetworkPolicy` on an agent namespace; a ServiceAccount token automounted where unused; missing `runAsNonRoot` / `allowPrivilegeEscalation: false` / `capabilities.drop: ["ALL"]` / `seccompProfile`; an over-broad RBAC scope (a tier granted the wrong scope, e.g. developer-team getting a `ClusterRole`).
- **medium** — a hardening gap with limited blast radius: missing `readOnlyRootFilesystem`; an unpinned image (tag, no digest); a namespace missing the PSS `enforce: restricted` label where its pods already comply.
- **low** — advisory / style: naming, labels, non-security lint.

The gate blocks merge on any **unmitigated `critical`/`high`**; `medium`/`low` are advisory ([06 §7](../../../docs/design/06-api-and-data-contracts.md)). Do not omit `severity`; a finding without one is treated as `high` by the scorer.

## 3. Triage & Filtering

Evaluate the raw findings against the project context to determine actual risk. Filter out findings that are functionally required by the workload's specific role or adequately mitigated by broader architectural controls.

- _Example:_ Filter out `hostPath` or `privileged` warnings for recognized infrastructure daemonsets (e.g., CSI drivers).
- _Example:_ Downgrade or filter missing `NetworkPolicy` warnings if the context confirms a strict Service Mesh is handling all routing and authorization.

## 4. Aggregation

Merge the filtered findings into a single JSON array. Output MUST be valid JSON string (markdown blocks okay). Omit agents with no findings or return empty `findings`.
