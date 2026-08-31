resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = var.display_name
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}

# `gcloud compute networks subnets list-usable` reports a subnet's
# ipUtilization only for subnets the caller holds compute.subnetworks.use on,
# and that field is the sole data source for the gcp-networking-fabric-audit
# `subnet-ip-exhaustion` check. Without the permission the command does not
# fail -- it exits 0 with an empty list, which the audit could not tell apart
# from "this project has no subnets" until the collector learned to
# corroborate against `subnets list`. On the deployed install that was 42
# subnets reported as zero, every run.
#
# No predefined role carries this permission without also carrying write-side
# ones: roles/compute.networkUser is the usual home and brings addresses.use
# and forwardingRules.use with it. A custom role with the single permission
# keeps the agent's project bindings read-only in substance as well as name.
#
# Gated on project_roles being non-empty so that `project_roles = []` still
# means "grant nothing and manage roles outside the module", as its
# description promises.
resource "google_project_iam_custom_role" "subnet_utilization_reader" {
  count = length(var.project_roles) > 0 ? 1 : 0

  project     = var.project_id
  role_id     = "kubeagentsSubnetUtilizationReader"
  title       = "Kube-Agents Subnet Utilization Reader"
  description = "Grants only the three permissions the fleet audit's subnet-ip-exhaustion check needs: compute.subnetworks.use to see subnets at all, plus the list and get on the Network Analyzer insight that carries their utilization."
  permissions = [
    "compute.subnetworks.use",
    # `list-usable` turned out to answer only half the question: it reports
    # which subnets exist but carries no ipUtilization field on any API
    # version, so the check still had nothing to measure. Network Analyzer
    # publishes that measurement as google.networkanalyzer.vpcnetwork.
    # ipAddressInsight, and only roles/recommender.viewer carries the read --
    # a role that grants viewer on every recommender in the project, far
    # wider than one check needs.
    "recommender.networkAnalyzerIpAddressInsights.list",
    "recommender.networkAnalyzerIpAddressInsights.get",
  ]
  stage = "GA"
}

resource "google_project_iam_member" "subnet_utilization_reader" {
  count = length(var.project_roles) > 0 ? 1 : 0

  project = var.project_id
  role    = google_project_iam_custom_role.subnet_utilization_reader[0].id
  member  = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_project_iam_member" "agent_roles" {
  #checkov:skip=CKV_GCP_41:Platform agent requires serviceAccountUser role to manage agent workload identities
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin project roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:Platform agent requires serviceAccountUser role to manage agent workload identities
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only telemetry and cluster observability
  for_each = toset(var.project_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent.email}"
}
