#!/usr/bin/env bash
# ==============================================================================
# 🛡️  Step 13: Apply the agent network policies — per-tier egress allowlist, then the tenant floor
# ==============================================================================
# Three correct per-tier egress policies have existed since Phase 5, and a tenant default-deny since
# Phase 3, and NO INSTALL PATH APPLIED ANY OF THEM. A policy that is written, reviewed, structurally
# validated and never applied contains nothing — this is LSN-006 ("well-formed is not enforced") in
# its purest form, and it is why 09 separates "the manifest is correct" from "the dataplane refuses
# the packet".
#
# This step renders netpol-agent-egress.yaml.template per tier and applies it, then renders
# netpol-tenant-default-deny.yaml.template and applies the tenant namespace's zero-trust floor. It
# runs last, after every agent tier exists, because applying a default-deny policy to a namespace
# whose supporting Services are not up yet turns a slow rollout into a confusing one.
#
# ORDER WITHIN THIS STEP: allowlist first, floor second. NetworkPolicies are additive so the end
# state is identical either way, but floor-first opens a window in which a Ready agent pod is fully
# cut off from DNS and inference, and it is cut off for exactly as long as the next kubectl call
# takes. Allowlist-first has no such window. See the template header.
#
# ENFORCEMENT IS A PROPERTY OF THE CNI, NOT OF THIS SCRIPT. kindnet accepts a NetworkPolicy and
# enforces nothing; GKE enforces only with Dataplane V2 or Calico. This step therefore reports
# whether the cluster can enforce at all and says so out loud rather than implying containment it
# cannot deliver. `binding.md` precondition P4 says the same thing to the harness: on a
# non-enforcing dataplane an egress claim is `deferred`, never `pass`.
#
# Knobs:
#   EGRESS_POLICIES_ENABLED=false      skip entirely
#   WORKLOAD_IDENTITY_ENABLED=true     append the narrow metadata-server allow (see common.sh)
#   GKE_DATAPLANE=auto|v1|v2           which metadata IP↔port pair to emit
#   HUB_INFERENCE_CIDR / HUB_MINTY_CIDR / MCP_GROUNDING_CIDRS   remote-hub topology
#   KUBE_APISERVER_CIDR=<csv>          override the auto-detected API-server address(es)
#   KUBE_APISERVER_EGRESS_ENABLED=false  omit rule 9 — see below before you do
#
# THE API-SERVER RULE IS NOT OPTIONAL IN THE WAY THE OTHERS ARE. Every knob above defaults to the
# narrow answer, because for those the cost of being wrong in the permissive direction is an open
# path and the cost of being wrong in the restrictive direction is a feature that does not work.
# Rule 9 inverts that: without it the BROKER cannot TokenReview, read a FleetFreeze or write an
# ActionRecord (pipeline steps 1, 5 and 11), so every write in the system fails, and it fails
# reported as an authentication error that never mentions the network. So this step RESOLVES the
# address and REFUSES TO APPLY if it cannot — an install that silently shipped a policy without
# rule 9 is the hole P9-T7d-4 exists to close, and a default of "absent" would rebuild it.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

print_step "Checking Local Prerequisites"
check_prereqs "kubectl" "envsubst"

print_step "Setting up Configuration State for Egress Policies"
load_state

: "${NAMESPACE:=kubeagents-system}"
export CONTROL_NAMESPACE="${NAMESPACE}"

export EGRESS_POLICIES_ENABLED="${EGRESS_POLICIES_ENABLED:-true}"
export CLUSTER_ADMIN_ENABLED="${CLUSTER_ADMIN_ENABLED:-true}"
export DEVELOPER_TEAM_NAMESPACE="${DEVELOPER_TEAM_NAMESPACE:-team-x}"

# Workload Identity is on for a GKE install and off everywhere else. Getting this wrong in the
# permissive direction opens the raw metadata endpoint; getting it wrong in the restrictive
# direction costs the agents their cloud identity. Neither is silent, so default to the safe one.
export WORKLOAD_IDENTITY_ENABLED="${WORKLOAD_IDENTITY_ENABLED:-false}"
export GKE_DATAPLANE="${GKE_DATAPLANE:-auto}"

if [ "${EGRESS_POLICIES_ENABLED}" != "true" ]; then
  print_warning "EGRESS_POLICIES_ENABLED=${EGRESS_POLICIES_ENABLED} — skipping. Agent pods will have UNRESTRICTED egress."
  exit 0
fi

# ------------------------------------------------------------------------------
# Resolve the kube-apiserver address, or refuse
# ------------------------------------------------------------------------------
# Before the dataplane report, because this can end the step and the report cannot.
print_step "Resolving the kube-apiserver address for egress rule 9"

export KUBE_APISERVER_EGRESS_ENABLED="${KUBE_APISERVER_EGRESS_ENABLED:-true}"
export KUBE_APISERVER_CIDRS=""

if [ "${KUBE_APISERVER_EGRESS_ENABLED}" != "true" ]; then
  print_warning "KUBE_APISERVER_EGRESS_ENABLED=${KUBE_APISERVER_EGRESS_ENABLED} — rule 9 will be OMITTED."
  print_warning "The broker cannot TokenReview, read a FleetFreeze or write an ActionRecord without it."
  print_warning "Every write will fail, and it will be reported as an authentication error."
elif KUBE_APISERVER_CIDRS="$(resolve_apiserver_cidrs)"; then
  export KUBE_APISERVER_CIDRS
  if [ -n "${KUBE_APISERVER_CIDR:-}" ]; then
    print_success "API-server egress: ${KUBE_APISERVER_CIDRS} (from KUBE_APISERVER_CIDR)"
  else
    print_success "API-server egress: ${KUBE_APISERVER_CIDRS} (auto-detected from the cluster)"
  fi
else
  print_error "Could not resolve a kube-apiserver address, and rule 9 cannot be rendered without one."
  print_error "Neither the 'kubernetes' Service ClusterIP nor the current context's server URL gave an"
  print_error "IPv4 literal. A hostname is deliberately not resolved here — a policy pinned to whatever"
  print_error "DNS answered at install time stops matching after a control-plane rotation, silently."
  print_error "Set KUBE_APISERVER_CIDR in vars.sh to the address (or master range) pods actually reach,"
  print_error "or set KUBE_APISERVER_EGRESS_ENABLED=false if you have decided the broker may not write."
  exit 1
fi

# ------------------------------------------------------------------------------
# Report the dataplane's actual capability before claiming anything
# ------------------------------------------------------------------------------
print_step "Checking whether this cluster can enforce NetworkPolicy"

ENFORCING="unknown"
if kubectl -n kube-system get daemonset calico-node >/dev/null 2>&1; then
  ENFORCING="calico"
elif kubectl -n kube-system get daemonset anetd >/dev/null 2>&1 ||
  kubectl -n kube-system get daemonset cilium >/dev/null 2>&1; then
  ENFORCING="dataplane-v2"
elif kubectl -n kube-system get daemonset kindnet >/dev/null 2>&1; then
  ENFORCING="kindnet"
fi

case "${ENFORCING}" in
  calico | dataplane-v2)
    print_success "Enforcing dataplane detected (${ENFORCING}) — these policies will actually block traffic."
    ;;
  kindnet)
    print_warning "kindnet detected. It ACCEPTS NetworkPolicy objects and ENFORCES NOTHING."
    print_warning "The policies below will be created and will contain no traffic. Do not read their"
    print_warning "presence as containment; use dev/cluster/up.sh (Calico) to prove egress."
    ;;
  *)
    print_warning "Could not identify the CNI. Whether these policies are enforced is UNVERIFIED."
    ;;
esac

# ------------------------------------------------------------------------------
# Render + apply, one policy per tier that exists
# ------------------------------------------------------------------------------
print_step "Applying per-tier egress policies"

apply_tier_policy() { # apply_tier_policy <netpol-name> <namespace> <tier>
  local name="$1" ns="$2" tier="$3" rendered

  if ! kubectl get namespace "${ns}" >/dev/null 2>&1; then
    print_info "Namespace '${ns}' does not exist — skipping ${name}."
    return 0
  fi

  rendered="$(render_egress_policy "${name}" "${ns}" "${tier}")"

  # Fail before applying rather than after: an unsubstituted token is a broken manifest, and the
  # API server's error for one buried in a CIDR field is far less obvious than this line
  # (V-CMP-003).
  if printf '%s' "${rendered}" | grep -q 'REPLACE_WITH_\|PLACEHOLDER'; then
    print_error "Rendered policy '${name}' still contains a placeholder token — refusing to apply."
    exit 1
  fi

  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[dry-run] would apply NetworkPolicy/${name} in ${ns}"
    printf '%s\n' "${rendered}" | kubectl apply --dry-run=server -f - >/dev/null
    print_success "NetworkPolicy/${name} validates against the API server"
    return 0
  fi

  printf '%s\n' "${rendered}" | kubectl apply -f -
  print_success "NetworkPolicy/${name} applied in ${ns}"
}

apply_tier_policy "platform-egress" "${NAMESPACE}" "platform"

if [ "${CLUSTER_ADMIN_ENABLED}" = "true" ]; then
  apply_tier_policy "cluster-admin-egress" "${NAMESPACE}" "cluster-admin"
fi

if [ -n "${DEVELOPER_TEAM_NAMESPACE}" ]; then
  apply_tier_policy "developer-team-egress" "${DEVELOPER_TEAM_NAMESPACE}" "developer-team"
fi

# ------------------------------------------------------------------------------
# The tenant floor — applied last, deliberately (see the header)
# ------------------------------------------------------------------------------
# The per-tier policy above governs egress for pods carrying `kube-agents/tier`. It says nothing
# about ingress, and nothing at all about the tenant's own workloads. This floor covers both: every
# pod in the namespace, both directions, deny by default.
print_step "Applying the tenant namespace default-deny floor"

if [ -z "${DEVELOPER_TEAM_NAMESPACE}" ]; then
  print_info "DEVELOPER_TEAM_NAMESPACE is empty — no tenant namespace to isolate."
elif ! kubectl get namespace "${DEVELOPER_TEAM_NAMESPACE}" >/dev/null 2>&1; then
  print_info "Namespace '${DEVELOPER_TEAM_NAMESPACE}' does not exist — skipping the default-deny floor."
else
  DENY="$(render_tenant_default_deny "${DEVELOPER_TEAM_NAMESPACE}")"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[dry-run] would apply the default-deny floor in ${DEVELOPER_TEAM_NAMESPACE}"
    printf '%s\n' "${DENY}" | kubectl apply --dry-run=server -f - >/dev/null
    print_success "Tenant default-deny validates against the API server"
  else
    printf '%s\n' "${DENY}" | kubectl apply -f -
    print_success "Tenant default-deny applied in ${DEVELOPER_TEAM_NAMESPACE} (ingress + egress)."
  fi
fi

print_step "Network policies applied"
if [ "${WORKLOAD_IDENTITY_ENABLED}" = "true" ]; then
  print_info "Workload Identity metadata allow: RENDERED (dataplane=${GKE_DATAPLANE}, ports only)."
else
  print_info "Workload Identity metadata allow: ABSENT — the raw metadata endpoint is unreachable."
fi
if [ -n "${KUBE_APISERVER_CIDRS}" ]; then
  print_info "kube-apiserver allow (rule 9): RENDERED for ${KUBE_APISERVER_CIDRS} on :443."
else
  print_warning "kube-apiserver allow (rule 9): ABSENT. The broker's write path is closed at the network."
fi
