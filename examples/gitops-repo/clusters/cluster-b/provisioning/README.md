# clusters/cluster-b/provisioning/

The **Terraform HCL** arm of the `spec.iac.format` seam (06 §1.1, §4) — the parity twin of
`clusters/cluster-a/provisioning/` (KCC YAML). `cluster-b` is the same cluster shape a `kcc` target
would produce, expressed as Terraform because its proposing agent's `spec.iac.format` is `terraform`
(enum `kcc | terraform`, default `kcc`;
[`common_types.go`](../../../../../k8s-operator/api/v1alpha1/common_types.go) `IACFormat`). It
demonstrates the cloud-agnostic **direction** of [01](../../../../../docs/design/01-vision-scope.md)
§6 without changing any trust boundary.

## Why a separate cluster dir

The reference actuation pipeline
([`apply.yml`](../../../../.github/workflows/apply.yml) `apply_path()`) dispatches **per directory**:
a provisioning dir holding `*.tf` is applied with `terraform init && terraform apply`; otherwise
`*.y*ml` is applied with `kubectl apply --server-side`. If `.tf` and `.yaml` lived in the **same**
dir, Terraform would win and the YAML would be silently skipped. So each format lives in its own
cluster dir — `cluster-a` = KCC, `cluster-b` = Terraform — and never collides.

## Files

- [`cluster.tf`](cluster.tf) — `terraform{}` provider pin + `google_container_cluster` +
  `google_container_node_pool` (GKE Standard, regional, STABLE channel, private VPC-native + Workload
  Identity + Shielded nodes, e2-standard-4 / 100 GB, node autoscaling 1–4). Semantically equivalent
  to [`../../cluster-a/provisioning/cluster-a.yaml`](../../cluster-a/provisioning/cluster-a.yaml).
- [`variables.tf`](variables.tf) — `project_id` (required, supplied by CI/CD) and `region` (defaults
  to the KCC twin's location).

`dev/tests/iac-parity.py` asserts the HCL is **structurally valid** and **semantically
equivalent** to the KCC twin, and that `apply.yml` routes each format correctly.

## Trust boundary (unchanged)

This HCL is the **customer's actuation input**, applied by the customer CI/CD's least-privilege
per-target deploy credential — the agent holds **no** cloud/cluster write credential and only
proposes this file via a reviewed GitOps PR (invariant 2). A full `cluster-b` target would add
`bootstrap/`, `namespaces/`, and `agents/` siblings exactly like `cluster-a`; only `provisioning/`
is shown here as the format exemplar.

## Deferred-not-faked

`terraform validate` / `fmt` / `apply` and a **live** apply against a real second cloud (EKS/AKS
with IRSA / AAD Workload Identity for the KSA→cloud-IAM seam) need a `terraform` binary and a billable
account — neither is present on the build host. Structural + semantic parity is proven hermetically
instead (same pattern as Calico standing in for kindnet's missing NetworkPolicy enforcement in
earlier phases). See [`docs/build/phase-7.md`](../../../../../docs/build/phase-7.md) D1/D2.
