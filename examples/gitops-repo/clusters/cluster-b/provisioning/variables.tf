# clusters/cluster-b/provisioning/variables.tf
#
# Inputs for the cluster-b Terraform exemplar. `project_id` is required (the customer CI/CD supplies
# it, exactly as CI substitutes PROJECT_ID in the KCC exemplar). `region` defaults to us-central1 so
# the exemplar matches the KCC twin's location without extra wiring (iac-parity.py resolves this
# default when comparing locations).

variable "project_id" {
  type        = string
  description = "GCP project ID that owns cluster-b (supplied by the customer CI/CD)."
}

variable "region" {
  type        = string
  description = "GCP region for the regional cluster; matches the KCC twin's location."
  default     = "us-central1"
}
