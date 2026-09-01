variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "service_account_id" {
  description = "IAM Service Account ID for Kube-Agents"
  type        = string
  default     = "kubeagents-platform-gsa"

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{4,28}[a-z0-9])$", var.service_account_id))
    error_message = "service_account_id must be 6-30 characters, start with a lowercase letter, and contain only lowercase letters, digits, and hyphens."
  }
}

variable "display_name" {
  description = "Display name for the service account. Override when the module is instantiated for something other than the platform agent (e.g. the LiteLLM gateway's Vertex AI identity)."
  type        = string
  default     = "Kube-Agents Platform Agent Service Account"
}

variable "namespace" {
  description = "Kubernetes namespace where Kube-Agents runs"
  type        = string
  default     = "kubeagents-system"
}

variable "ksa_name" {
  description = "Kubernetes Service Account name"
  type        = string
  default     = "kubeagents-platform-agent"
}

variable "project_roles" {
  description = <<-EOT
    Project-level IAM roles granted to the agent's service account. The default
    is the read-only permission set (the full-install composition's
    permission_set = "read-only", which is also the installer's default); see
    the security-and-iam reference for what each role is used for. Set to [] to
    grant nothing and
    manage roles outside the module. Passing null selects this default
    (nullable = false), which lets root modules expose a passthrough variable.
  EOT
  type        = list(string)
  nullable    = false
  default = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/cloudtrace.viewer",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
  ]
}

variable "subnet_utilization_role_id" {
  description = <<-EOT
    Role ID of the custom role carrying the fleet audit's subnet-utilization
    permissions. Change it only to get past a name GCP is still holding: a
    terraform destroy soft-deletes the role and the name stays reserved for
    between 7 and 37 days, during most of which it can be neither created nor
    updated. Ignored when project_roles is empty, since the role is not
    created then.
  EOT
  type        = string
  default     = "kubeagentsSubnetUtilizationReader"

  validation {
    condition     = can(regex("^[a-zA-Z0-9_.]{3,64}$", var.subnet_utilization_role_id))
    error_message = "subnet_utilization_role_id must be 3-64 characters of letters, digits, underscores or dots. GCP rejects hyphens in a custom role id."
  }
}
