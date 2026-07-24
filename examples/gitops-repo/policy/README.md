# policy/

Cluster admission policies that enforce the security model at apply time — the runtime backstop for
attenuation (03 §4, §11). The load-bearing ones:

- **`vap-agent-readonly.yaml`** (Phase 0) — a `ValidatingAdmissionPolicy` that **hard-denies** any
  `Role`/`ClusterRole` selected by the `kube-agents/tier` label whose own `rules` grant an agent
  ServiceAccount a **write verb** or a **wrong-scope** grant (e.g. cluster-scoped for a namespace
  tier). CEL is scoped to the role's own `rules`. This rejects a bad-RBAC PR **at apply time even if
  it was merged** — the negative test in 03 §11.

- **`vap-agent-pod-hardening.yaml`** (Phase 5) — a `ValidatingAdmissionPolicy` that **hard-denies** any
  `Pod` selected by the `kube-agents/tier` label whose containers (or init containers) do not set
  `securityContext.readOnlyRootFilesystem: true`. This is the focused **complement to PSS
  `enforce: restricted`** (a namespace label on the agent namespaces): restricted-PSS enforces the rest
  of the hardened context (runAsNonRoot, seccomp RuntimeDefault, drop-ALL, no privesc) but does **not**
  cover `readOnlyRootFilesystem`. Governs **only** agent pods (tier label), so tenant/system workloads
  are untouched by it; it **composes with** `vap-agent-readonly` (different resource, different policy
  name — neither weakens the other).

Both require K8s ≥ 1.30 (VAP GA). Optional Gatekeeper/Kyverno policies may also live here. Applied by
CI/CD on merge; human-reviewed.
