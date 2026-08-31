# Kube-Agents IAM & Workload Identity Module

Reusable Terraform module for provisioning the Platform Agent's Google Service Account (GSA), its Workload Identity binding, and its project-level IAM roles.

## Relationship to the install

This is the module the full-install composition (and therefore `install.sh`) uses for the
agent's identity. The canonical identifiers (GSA `kubeagents-platform-gsa`, KSA
`kubeagents-platform-agent`, namespace `kubeagents-system`) also appear in
`k8s-operator/scripts/common.sh` for the dev tooling, and the module's defaults mirror
them.

By default the module grants the read-only role set (the composition's
`permission_set = "read-only"`, also the installer's default). Pass `project_roles = []` to grant
nothing and manage roles yourself — but note the agent fails every GCP call until an
equivalent role set exists.

Whenever `project_roles` is non-empty the module also defines a project-level custom role,
`kubeagentsSubnetUtilizationReader`, and binds it to the same GSA. Two things follow that the
role list alone does not tell you. Its `compute.subnetworks.use` is a consumption permission
rather than a read, so the grant is not read-only in substance — see
[Security & IAM](../../../docs/site/src/content/docs/reference/security-and-iam.md) for why the
narrower custom role is still the better of the two options. And `role_id` is a constant, not
derived from `service_account_id`, so two instantiations of this module in one project collide on
it; that is the same constraint the GSA's own default name already imposes.

There is no admin preset to mirror: the `gke-admin` bundle was removed (see
[Security & IAM](../../../docs/site/src/content/docs/reference/security-and-iam.md)),
and this module has never had one. Passing admin roles through `project_roles` is
possible and is the module's equivalent of `permission_set = "custom"` — it puts
the grant in your Terraform, where it is reviewed.

## Usage

```hcl
module "kube_agents_iam" {
  source             = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/kube-agents-iam?ref=1.2.0"
  project_id         = "my-gcp-project"
  service_account_id = "kubeagents-platform-gsa"
  namespace          = "kubeagents-system"
  ksa_name           = "kubeagents-platform-agent"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
