---
name: review-security-k8s-agents-main
description: Orchestrates comprehensive Kubernetes security audits specifically tailored for AI agent workloads.
---

# Task

Coordinate AI agent security review sub-agents, gather findings, and produce a summarized JSON report.

# Workflow

## 1. Context Ingestion

Pass project context (from `review-security-k8s-understand`) to sub-agents.

## 2. Parallel Reviews

Launch in parallel sub-agents:

- `review-security-k8s-agents-sandbox`
- `review-security-k8s-agents-firewall`
- `review-security-k8s-agents-credentials`
- `review-security-k8s-agents-prompt-injection`
- `review-security-k8s-agents-data-exfil`
- `review-security-k8s-agents-audit-logs`

**CRITICAL**: Instruct each to output JSON:

```json
[
  {
    "agent": "<skill-name>",
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

**Every finding MUST carry a `severity`** — one of `critical`, `high`, `medium`, `low`. Tag it with this agent-workload rubric (kube-agents review-gate, [06 §7](../../../docs/design/06-api-and-data-contracts.md)):

- **critical** — a path that lets the agent be driven to act outside its read-only ceiling or leak long-lived credentials: a prompt-injection → mutation path (the agent can be steered to write/escalate); a reachable long-lived cloud credential or a cloud-metadata endpoint (`169.254.169.254`); a **direct agent-to-agent call path** (breaks invariant 3 — coordination must be indirect via GitOps/OKF).
- **high** — a serious exfiltration or attribution weakening: a missing egress allowlist / default-deny for an agent (open data-exfil surface); untrusted code executed **outside** a sandbox; a write-capable token mounted into a read-only agent; a mutation with **no audit trail / requester attribution**.
- **medium** — a hardening gap with limited blast radius: missing `readOnlyRootFilesystem` on an agent container; logging that could leak prompt/context; missing size/rate limits on tool output.
- **low** — advisory / style: naming, labels, non-security lint.

The gate blocks merge on any **unmitigated `critical`/`high`**; `medium`/`low` are advisory ([06 §7](../../../docs/design/06-api-and-data-contracts.md)). Do not omit `severity`; a finding without one is treated as `high` by the scorer.

## 3. Triage & Filtering

Evaluate the raw findings against the project context to determine actual risk. Filter out findings that are functionally required by the workload's specific role or adequately mitigated by broader architectural controls.

- _Example:_ Filter out missing egress proxy warnings if the agent's execution sandbox is completely air-gapped and the main control loop is strictly allowlisted to a single LLM API.
- _Example:_ Filter out root execution warnings _inside_ the execution sandbox if the context confirms the sandbox utilizes a secure VM-based `RuntimeClass` (e.g. gVisor or Kata Containers) providing a secure sandbox isolation boundary.

## 4. Aggregation

Merge the filtered findings into a single JSON array. Output MUST be valid JSON string (markdown blocks okay). Omit agents with no findings or return empty `findings`.
