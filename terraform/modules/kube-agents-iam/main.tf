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
# compute.subnetworks.use is a consumption permission, not a read: it is what
# authorizes attaching a NIC, a node pool or a load balancer to a subnet, and
# roles/compute.viewer does not carry it. So this is a deliberate exception to
# the read-only project grant rather than an instance of it, taken because the
# API offers no read-only route to the field.
#
# The predefined home for it is roles/compute.networkUser, which carries some
# two hundred permissions including networksecurity.sacAttachments.create and
# .delete -- writes. A custom role with three permissions is the narrower of
# the two, which is the whole argument for it; it is not a way of keeping the
# grant read-only, and the docs should not say it is.
#
# Gated on project_roles being non-empty so that `project_roles = []` still
# means "grant nothing and manage roles outside the module", as its
# description promises.
#
# The role id is a variable rather than a literal because of how GCP retires a
# custom role name. `uninstall.sh` reaches terraform destroy, which soft-deletes
# this role; the name is then held for between 7 and 37 days. Reinstalling
# inside the first 7 is fine -- the provider finds the soft-deleted role and
# undeletes it -- but past that the role can be neither created nor changed
# until the window closes, and terraform apply fails with nothing an operator
# can do to the composition to get past it. Setting a different id is that
# something: TF_VAR_subnet_utilization_role_id reaches this through the
# full-install passthrough without editing a generated tfvars file. It is also
# what would let this module be instantiated twice against one project with
# project_roles on both, which a shared literal could not survive.
resource "google_project_iam_custom_role" "subnet_utilization_reader" {
  count = length(var.project_roles) > 0 ? 1 : 0

  project = var.project_id
  role_id = var.subnet_utilization_role_id
  title   = "Kube-Agents Subnet Utilization Reader"
  # This string is what the GCP console shows an operator reviewing the role, so it
  # carries the same correction as the comment above rather than the read-framing the
  # comment rejects. Changing it is an in-place update of the role on the next apply;
  # it touches no permission and no binding.
  description = "Grants only the three permissions the fleet audit's subnet-ip-exhaustion check needs: compute.subnetworks.use, which is a consumption permission and not a read, plus the list and get on the Network Analyzer insight that carries subnet utilization."
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
