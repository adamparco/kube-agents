# clusters/cluster-b/provisioning/cluster.tf
#
# The CANONICAL Terraform HCL provisioning exemplar for cluster-b — the `terraform` arm of the
# `spec.iac.format` seam (06 §1.1, §4; enum kcc|terraform). This is the artifact a platform agent
# authors via PR when the proposing agent's spec.iac.format is `terraform`, per
# agents/platform/skills/gke-cluster-creator/SKILL.md. Its parity twin is the KCC YAML exemplar at
# ../../cluster-a/provisioning/cluster-a.yaml (iac.format: kcc) — the two describe the SAME cluster
# shape in the two supported formats, and local-dev/tests/iac-parity.py asserts they stay equivalent
# (location, release channel, node machine type/count, networking / WI / shielded / private shape).
#
# The reference actuation pipeline applies this dir with `terraform init && terraform apply` because
# it holds *.tf (apply.yml apply_path(): *.tf → terraform, else *.y*ml → kubectl). Keep this dir
# Terraform-only; a KCC target lives in a SEPARATE cluster dir (cluster-a) so the two formats never
# collide in one provisioning dir (terraform would win and the YAML would be silently skipped).
#
# Trust boundary is unchanged (invariant 2): this HCL is the CUSTOMER'S actuation input, applied by
# the customer CI/CD's per-target deploy credential — the agent holds no cloud/cluster write cred and
# only proposes this file via a reviewed PR.
#
# Shape mirrors the gke-cluster-creator "Standard Regional" template (SKILL.md §2): GKE Standard,
# regional, STABLE channel, private VPC-native + Workload Identity + Shielded nodes, e2-standard-4 /
# 100GB, node autoscaling 1–4. `terraform validate`/`fmt`/`apply` are the production checks and are
# deferred-not-faked here (no terraform binary on the build host; structural + semantic parity is
# proven hermetically by iac-parity.py instead).

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

resource "google_container_cluster" "cluster_b" {
  name     = "cluster-b"
  project  = var.project_id
  location = var.region

  # Node lifecycle is owned by the node pool below, so the default pool is removed and node count is
  # managed there (avoids cluster recreation on node changes) — equivalent to KCC removeDefaultNodePool.
  remove_default_node_pool = true
  initial_node_count       = 1

  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {}

  release_channel {
    channel = "STABLE"
  }

  workload_identity_config {
    # Workload Identity is the KSA→GSA seam the read-only agent identities depend on (06 §2). On a
    # non-GKE target this is IRSA (EKS) / AAD Workload Identity (AKS) — deferred-not-faked (D1).
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }
}

resource "google_container_node_pool" "cluster_b_pool" {
  name     = "cluster-b-pool"
  project  = var.project_id
  location = var.region
  cluster  = google_container_cluster.cluster_b.name

  autoscaling {
    min_node_count = 1
    max_node_count = 4
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = "e2-standard-4"
    disk_size_gb = 100

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }
}
