# clusters/cluster-a/bootstrap/ — spoke bootstrap (ordered apply waves)

Resolves the **chicken-and-egg** ([05](../../../../../docs/design/05-system-architecture.md) §7): a fresh
spoke has no controller and no admission policy, so the in-cluster agent cannot install its own runtime.
The **same cluster-provisioning PR** that creates `cluster-a` also lands this bundle, and the pipeline
applies it **after `provisioning/` (the cluster exists) and before `agents/` (the CR + identity)**. The
Platform Agent authors it via the F4 cascade (`propose-cluster-admin`); a human reviews + merges; the
customer's CI/CD (`.github/workflows/apply.yml`) applies the waves in order.

## Why waves (not one recursive apply)

`kubectl apply --recursive -f` gives no ordering, but bootstrap has **hard dependencies**: the controller's
webhook needs cert-manager's serving cert; the `ValidatingAdmissionPolicy` (VAP) must be **active before**
any agent identity is applied, so a write-verb/wrong-scope grant is rejected even during provisioning
(the [03](../../../../../docs/design/03-security-model.md) §11 negative). So the pipeline applies each
numbered subdirectory **in lexical order** and waits for readiness after the critical waves.

| Wave               | Contents                                                                                            | Why here                                                                                                                                                       |
| ------------------ | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `00-cert-manager/` | cert-manager **v1.14.x** (pinned)                                                                   | The controller's admission webhook needs a serving cert. Pipeline waits for its webhook to be Ready.                                                           |
| `10-controller/`   | kube-agents `config/default` (CRD + controller + webhooks + `kage-router`), pinned to a release ref | The per-cluster control plane (05 §7). Reconciles the Agent pod; the router is the read-only ChatOps front door. Pipeline waits for the controller Deployment. |
| `20-policy/`       | the agent-read-only **VAP** + binding                                                               | Must be enforcing **before** `agents/` applies the identity, so a bad-RBAC PR is denied at apply time even if merged (03 §4, §11).                             |

Then — outside this dir, next in the pipeline — `agents/` applies the read-only **identity before** the
`Agent` CR (identity-before-pod), and `agents/netpol-cluster-admin-egress.yaml` locks the pod's egress.

## Invariants this ordering enforces

- **identity-before-pod:** identity (in `../agents/`) is VAP-clean and applied after the VAP is active.
- **VAP-before-identity:** wave 20 precedes the `agents/` apply, so attenuation is enforced on the very
  first identity apply — no window where a bad grant could slip in.
- **controller-before-CR:** wave 10 precedes the `agents/` apply, so the Agent pod is reconciled by an
  already-running controller.

## Pinning

Every wave pins an **immutable version** (cert-manager `v1.14.x`; the kube-agents bases to a release
ref). Repin the kube-agents bases to a **content-addressable ref/digest** for production, exactly as the
image tags note. `cluster-a`/ is illustrative scaffolding — a real spoke substitutes its own name.
