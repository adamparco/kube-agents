# clusters/cluster-a/provisioning/

Cloud + cluster resources for `cluster-a` as **KCC YAML** (this dir is the `kcc` arm of the
`spec.iac.format` seam; the `terraform` arm is the sibling target
[`clusters/cluster-b/provisioning/`](../../cluster-b/provisioning/)). The format is selected by the
proposing agent's `spec.iac.format` (default `kcc`; 06 §1.1, §4). The customer's CI/CD applies these
on merge — `kubectl apply` for KCC (this dir), `terraform apply` for HCL (cluster-b). Agents author
here via PR only.

## Files

- [`cluster-a.yaml`](cluster-a.yaml) — the canonical KCC exemplar: `ContainerCluster` +
  `ContainerNodePool` (GKE Standard, regional, STABLE channel, private VPC-native + Workload Identity
  - Shielded nodes, e2-standard-4 / 100 GB, node autoscaling 1–4). It is **semantically equivalent**
    to the Terraform twin at
    [`../../cluster-b/provisioning/cluster.tf`](../../cluster-b/provisioning/cluster.tf);
    `dev/tests/iac-parity.py` asserts the two stay in parity. Other resources that live here in
    a real target: additional node pools, project IAM (`IAMPolicyMember`), etc.

`PROJECT_ID` is substituted per project by CI/CD (same convention as the identity manifests in
[`../agents/identity/`](../agents/identity/)). Keep this dir **KCC-only** — a Terraform target lives
in a separate cluster dir (cluster-b) because `apply.yml` dispatches per directory (`*.tf` →
terraform, else `*.y*ml` → kubectl) and the two formats must not collide.
