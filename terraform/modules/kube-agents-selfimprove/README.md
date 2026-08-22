# Self-Improvement Loop Identity Module

The Google half of the self-improvement loop: the investigator's service account and its
read-only telemetry grants, plus — only for the modes that open pull requests — the loop's own
token-minter account and KMS signing key. The Kubernetes half is the chart's
`selfImprovement.*` values.

Two service accounts, not one. The investigator reads Cloud Logging, Cloud Trace and Cloud
Monitoring and holds nothing else; the minter signs GitHub App assertions and reads no telemetry.
Neither can do the other's job, so a compromise of the investigation cannot open a pull request
and a compromise of the minter cannot see what the install is doing.

The investigator's grant is three viewer roles and stops there. It has no `container.*` role at
all: Kubernetes reads go through the pod's Kubernetes service account, which the chart binds to
`view` in a single namespace, so that half is bounded by RBAC rather than by project-level IAM.
Adding `roles/container.viewer` here would quietly widen it to every cluster in the project.

This module is deliberately separate from `kube-agents-iam`, which grants the Platform Agent
`container.admin` and the rest of what fleet management needs. The loop must inherit none of it —
an agent that can modify the cluster it is investigating cannot honestly report on it — and a
separate module also means an install can destroy the loop's identity without touching the
product's.

## Report-only needs nothing else

`create_minter = false` (the default) matches `selfImprovement.mode: report-only`, where the run
has no GitHub identity, no credential proxy and no write path of any kind. Its entire output is a
ledger ConfigMap in the release namespace. Leave it false unless you have set the chart to `fork`
or `upstream`.

## The App key import

As with `github-minter`, the KMS key is created **import-only and empty**
(`skip_initial_version_creation = true`). Importing the GitHub App private key PEM is a separate
one-shot step, so the PEM never enters Terraform state:

```bash
git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git /tmp/minty
cd /tmp/minty && go run ./cmd/minty tools import-pk \
  -project-id=<project> -location=<region> \
  -key-ring=selfimprove-token-minter-keyring -key=selfimprove-token-minter-key \
  -private-key=@/path/to/selfimprove-app-private-key.pem
```

Use a **different** GitHub App from the Platform Agent's. Two Apps is what makes the isolation
structural: the loop's token cannot reach the GitOps repositories, the agent's cannot reach this
repository, and either can be revoked without disturbing the other. Until the key version is
ENABLED the minter pod fails readiness and the loop files nothing — the correct failure, since
the alternative is discovering the problem at the end of an hour's investigation.

> **KMS resources cannot be deleted.** Cloud KMS key rings and keys are never actually
> destroyed — `terraform destroy` only removes them from state, and a subsequent apply with the
> same names fails with a 409. Recover by importing the existing resources back into state or by
> choosing new `kms_keyring_name`/`kms_key_name` values.

## Names have to match the chart

Three values are agreed between this module and the chart, and nothing checks them at apply time:

| Terraform            | Chart                             |
| -------------------- | --------------------------------- |
| `service_account_id` | `selfImprovement.github.gsaName`  |
| `ksa_name`           | `selfImprovement.github.ksaName`  |
| `kms_keyring_name` / `kms_key_name` | `selfImprovement.github.kms.*` |

`service_account_id` is capped at 23 characters rather than the usual 30, because the minter's
account is this plus `-minter` and GCP caps a service account id at 30. Both the variable
validation here and the chart's render-time guard check it, since either can be applied without
the other.

## Usage

```hcl
module "selfimprove" {
  source = "../../modules/kube-agents-selfimprove"

  project_id = var.project_id
  location   = var.location
  namespace  = "kubeagents-system"

  # Only for mode = fork or upstream.
  create_minter = false
}
```
