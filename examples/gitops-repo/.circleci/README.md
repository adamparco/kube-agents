# .circleci/ — second reference actuation pipeline (CircleCI)

kube-agents is **unopinionated** about CI/CD (06 §4). This directory is the **parity twin** of
[`.github/workflows/apply.yml`](../.github/workflows/apply.yml) (GitHub Actions), showing the same
actuation on a **different pipeline** — the concrete evidence for 07 "Phase 7" Accept (b): _a second
target using the customer's pipeline of choice._

## What it proves

- **Same dispatch.** [`config.yml`](config.yml)'s `apply_path()` is byte-for-byte the same routing as
  `apply.yml`: a provisioning dir with `*.tf` → `terraform apply`, otherwise `*.y*ml` →
  `kubectl apply --server-side`. The KCC/HCL seam (`spec.iac.format`) is pipeline-independent.
- **Same trigger.** The workflow is filtered to `main` — applies happen only **after** a reviewed PR
  is merged, never on a feature branch.
- **Same least privilege.** One **CircleCI context per target** (`kube-agents-apply-cluster-a`,
  `…-cluster-b`, `…-fleet`) is the analogue of `apply.yml`'s per-target **GitHub Environment**: each
  context holds only that target's keyless OIDC deploy identity (a Workload Identity Federation
  provider + a per-target Google service account). A `cluster-a` apply can never use `cluster-b`'s
  credentials.
- **Same trust boundary (invariant 2).** Auth is **keyless** (`CIRCLE_OIDC_TOKEN` → WIF); there are
  **no long-lived JSON keys** and **no agent-held write credential**. The pipeline is the sole
  privileged writer (03 §4); adding it introduces no new write path from any agent
  ([`docs/build/phase-7.md`](../../../docs/build/phase-7.md) D4).

`dev/tests/circleci-parity.py` asserts all of the above against `apply.yml` (dispatch parity +
main-only trigger + per-target contexts + no static key), plus a malformed-config negative control.

## Deferred-not-faked

`circleci config validate` (schema validation) and a **live** CircleCI run against a real cluster need
the `circleci` CLI and a billable account — neither is present on the build host. Structure + dispatch
parity is proven **hermetically** instead (same pattern as Calico standing in for kindnet's missing
NetworkPolicy enforcement in earlier phases). Pin the `docker` image and the Terraform version to
immutable digests in production.
