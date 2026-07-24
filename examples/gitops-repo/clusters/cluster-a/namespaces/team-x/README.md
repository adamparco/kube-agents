# clusters/cluster-a/namespaces/team-x/

Namespace-scoped desired state for the `team-x` tenant — the home + isolation boundary of one
**Developer Team Agent** (the read-only, namespace-scoped leaf tier). The cluster-admin tier proposes
this whole bundle via the `propose-developer-team` cascade (a `submit-suggestion` PR); a human reviews
and merges; `cluster-a`'s pipeline applies it in the `namespaces/` wave — **before** `agents/`, and
within this directory in lexical (numeric-prefix) order so the Namespace exists first.

| File                                   | Task  | Purpose                                                                                                                                                                       |
| -------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `00-namespace.yaml`                    | P3-T6 | The tenant `Namespace` (the blast-radius boundary; labeled for NetworkPolicy selection).                                                                                      |
| `10-resourcequota.yaml`                | P3-T6 | `ResourceQuota` bounding the tenant's aggregate compute + object footprint (03 §3).                                                                                           |
| `20-netpol-default-deny.yaml`          | P3-T6 | Default-deny `NetworkPolicy` (all pods, ingress + egress) — the zero-trust isolation baseline (03 §10).                                                                       |
| `30-netpol-developer-team-egress.yaml` | P3-T6 | The agent pod's egress allowlist (selected by `kube-agents/tier: developer-team`); adds back only DNS, the kubeagents-system alias hop, and the external CIDRs.               |
| `40-service-aliases.yaml`              | P3-T6 | `ExternalName` aliases (`litellm`, `github-token-minter`) so the controller's namespace-local rendered hostnames resolve to the shared kubeagents-system services (A4 graft). |
| `50-developer-team-identity.yaml`      | P3-T5 | Pre-created **read-only** identity: namespaced `ServiceAccount` + `Role` + `RoleBinding` (get/list/watch only). The controller references it, never mints it (08 §4).         |
| `60-developer-team-agent.yaml`         | P3-T5 | The developer-team `Agent` CR (placed in `team-x` per the A1 placement clause: `metadata.namespace == spec.scope.namespace`).                                                 |

The Developer Team Agent that lives here is **read-only** and confined to this one namespace by RBAC
(a namespaced `Role`) + the agent-read-only ValidatingAdmissionPolicy backstop; its only write path is
proposing changes to this namespace as a reviewed GitOps PR.
