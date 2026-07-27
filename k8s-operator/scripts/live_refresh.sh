#!/usr/bin/env bash
# ==============================================================================
# 🚀 live_refresh.sh — rebuild every first-party image and roll the LIVE install
# ==============================================================================
# One command for the outer loop: build all seven images from the working tree on Cloud Build,
# prove each one landed in the registry the cluster pulls from, pin them in vars.sh, run the full
# provisioning pipeline, and then read back what is ACTUALLY RUNNING and compare it to what was
# built. Ends by printing the Slack instructions, because the point of the refresh is to go prompt
# the agents.
#
# WHY THIS EXISTS AT ALL
#   `dev/cluster/reload-images.sh` is the equivalent button for the inner loop, and it refuses any
#   context that is not `gke-scratch-*` (exit 2) — deliberately, because it repoints running
#   workloads and the live install is one `*` away from every script in that directory (LSN-005).
#   `scripts/dev/dev_rebuild_agent.sh` is the only sanctioned live one-shot and covers the PLATFORM
#   AGENT IMAGE ONLY: not the operator, not the router, not the cluster-admin or developer-team
#   tiers, and it does not write the tag back to vars.sh, so the next `provision_08` silently
#   reverts it. So refreshing the live install meant `make cloud-build-push`, hand-editing five
#   lines of vars.sh, and `make gcp-provision` — three steps with a hand-copied tag in the middle,
#   which is the step that gets skipped and the reason an install sits on a stale build.
#
# THE GUARD IS INVERTED, AND THAT IS THE WHOLE DESIGN PROBLEM
#   Every other destructive-capable script in this repo protects the live cluster by refusing to
#   address it. This one cannot: addressing it is the job. So the protection is affirmative instead
#   of negative — it states the target and the exact change, and requires the operator to type the
#   cluster name back. `--yes` skips the prompt for automation; nothing skips the summary. The
#   `gke-scratch-*` arm is kept and inverted too: this script REFUSES a scratch cluster and names
#   reload-images.sh, so the two tools cover disjoint targets and neither is the wrong default.
#
# WHY IT DEPLOYS BY TAG AND VERIFIES BY DIGEST
#   reload-images.sh deploys by digest, which makes LSN-001 (a same-tag image is not evidence of the
#   build under test) unrepresentable rather than merely detectable. That is the better mechanism
#   and it is NOT available here: the provisioning path is tag-based end to end — vars.sh carries
#   AGENT_IMAGE + AGENT_TAG as separate fields, platform-agent.yaml.template renders them as
#   separate fields, and provision_12 derives the two child tiers by string-splitting AGENT_IMAGE's
#   registry and reusing AGENT_TAG. Threading digests through that would change the contract of five
#   scripts and two templates for a script that did not exist yesterday.
#
#   So the guarantee is reconstructed on either side of the deploy instead of built into it:
#
#     BEFORE  every one of the seven tags is resolved to a digest by asking ARTIFACT REGISTRY, not
#             the builder. `gcloud builds submit` exiting 0 says a build ran; it does not say what
#             is now in the registry under that tag, and the difference is LSN-021 — a command that
#             ran and left nothing behind. An unresolvable tag is fatal here, before anything is
#             pinned in vars.sh or applied to the cluster.
#     AFTER   every running container in the namespaces this install owns is matched back to the
#             digest recorded above, read from `.status.containerStatuses[].imageID` — the kubelet's
#             answer to "what did I actually pull", which a tag cannot be stale against.
#
#   A workload still on the old digest is rc 5: the images are correct and published, and the
#   cluster did not converge. That is a different failure from "the image was never built" (rc 4)
#   and it needs a different response, so it does not share an exit code.
#
# Usage: live_refresh.sh [--yes] [--dry-run] [--skip-build] [--allow-dirty] [--tag TAG]
#   --yes, -y      do not prompt for the typed cluster-name confirmation (still prints the summary)
#   --dry-run      resolve and print everything; build nothing, write nothing, apply nothing
#   --skip-build   images for this tag already exist; verify them and go straight to provisioning
#   --allow-dirty  permit a dirty working tree (the tag is suffixed so it cannot pass as a commit)
#   --tag TAG      override the derived tag
#
# Exit codes:
#   0 ok · 1 usage · 2 refused (guard or declined confirmation) · 3 required tool/config missing
#   4 an image did not build or does not resolve in Artifact Registry
#   5 the images are published and a workload did not converge on the new digest
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${OPERATOR_DIR}/.." && pwd)"

# shellcheck source=k8s-operator/scripts/common.sh
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Argument Parsing ─────────────────────────────────────────────────────────
# common.sh has already consumed --dry-run and -y/--no-confirm into DRY_RUN/NO_CONFIRM. Re-walking
# the same list here for this script's own flags means an unknown flag is REJECTED rather than
# ignored: a silently-dropped --skip-build looks exactly like a --skip-build that worked, which is
# the LSN-021 shape from the other direction.
SKIP_BUILD=0
ALLOW_DIRTY=0
TAG_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run | --no-confirm | -y) ;;
    # common.sh's parser knows -y and --no-confirm but not --yes, so a documented --yes would have
    # been accepted here and then prompted anyway. Set the flag rather than only tolerating it.
    --yes) NO_CONFIRM=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --tag)
      shift
      TAG_OVERRIDE="${1:-}"
      [ -n "$TAG_OVERRIDE" ] || {
        print_error "--tag requires a value."
        exit 1
      }
      ;;
    --tag=*) TAG_OVERRIDE="${1#--tag=}" ;;
    -h | --help)
      # The header block between the first and last rule, comment markers stripped. Delimited by
      # the rules rather than by line numbers so editing the header cannot truncate --help
      # mid-sentence — which a fixed `sed -n '2,60p'` already had, twice, while writing it.
      awk '/^# ={20,}$/ {n++; next} n==1 || n==2 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      print_error "Unknown argument '$1'."
      print_error "usage: $0 [--yes] [--dry-run] [--skip-build] [--allow-dirty] [--tag TAG]"
      exit 1
      ;;
  esac
  shift
done

# ─── Prerequisites ────────────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "make" "git"

print_step "Reading the install's configuration"
load_state

# This is a REFRESH of an existing install, so the target is read, never prompted for. init_var
# would helpfully offer `platform-agent-host` as a default to anyone whose vars.sh is missing —
# and the one thing this script must never do is guess which cluster to roll.
for _required in PROJECT_ID REGION CLUSTER_NAME; do
  if [ -z "${!_required:-}" ]; then
    print_error "${_required} is not set in ${VARS_FILE}."
    print_error "live_refresh refreshes an install that already exists; it will not guess a target."
    print_error "Run 'make gcp-provision' from k8s-operator/ to create one first."
    exit 3
  fi
done

AR_REPO="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-kube-agents}"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
KUBE_CONTEXT="${KUBE_CONTEXT:-gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}}"
TENANT_NS="${DEVELOPER_TEAM_NAMESPACE:-team-x}"

# ─── Guard: this script is for the live install, and only for the live install ───────────────────
# The inverse of dev/cluster/reload-images.sh's guard, anchored the same way and for the same reason
# (LSN-005 — `*gke-scratch*` would accept `my-gke-scratch-of-prod`). A scratch cluster is brought up
# by dev/cluster/up.sh and has none of the GCP scaffolding provision_01..13 expects; pointing this
# at one would try to CREATE a GKE cluster, a gVisor pool and a set of GSAs, which is not a refresh.
case "$CLUSTER_NAME" in
  gke-scratch-*)
    print_error "REFUSING: '${CLUSTER_NAME}' is an ephemeral scratch cluster."
    print_error "This script runs the full 13-step provisioner, which would try to CREATE cloud"
    print_error "resources on it. The inner-loop equivalent builds and deploys BY DIGEST:"
    print_error "    bash dev/cluster/reload-images.sh all ${CLUSTER_NAME}"
    exit 2
    ;;
esac

# `make cloud-build-push` derives its registry from `gcloud config get core/project`, NOT from
# PROJECT_ID. If the two disagree the build lands in one project and provisioning deploys from
# another — every image resolves, nothing errors, and the cluster keeps running the old build. Ask
# before spending thirty minutes of Cloud Build on the wrong registry.
ACTIVE_PROJECT="$(gcloud config get core/project 2>/dev/null || echo "")"
if [ "$ACTIVE_PROJECT" != "$PROJECT_ID" ]; then
  print_error "gcloud's active project is '${ACTIVE_PROJECT:-<unset>}' but this install is '${PROJECT_ID}'."
  print_error "'make cloud-build-push' reads the ACTIVE project to pick a registry, so the images"
  print_error "would be pushed somewhere this cluster never pulls from. Align them:"
  print_error "    gcloud config set project ${PROJECT_ID}"
  exit 3
fi

# Same trap one level down: cloud-build-push hardcodes the Artifact Registry repository name.
if [ "$AR_REPO" != "kube-agents" ]; then
  print_error "GCP_ARTIFACT_REGISTRY_REPO_NAME='${AR_REPO}', but 'make cloud-build-push' pushes to"
  print_error "'\${LOCATION}-docker.pkg.dev/\${PROJECT}/kube-agents' unconditionally. The build and"
  print_error "the deploy would name different repositories. Build by hand, then --skip-build."
  exit 3
fi

# ─── The tag ──────────────────────────────────────────────────────────────────
# Derived, never chosen, for the same reason reload-images.sh derives its own: the tag is how anyone
# afterwards answers "which source is this install running", so it has to CARRY that answer. A dirty
# tree must be visibly not a commit, or `src-abc1234` certifies uncommitted work as that commit.
# vars.sh is gitignored, so the pins this script writes never make the tree dirty.
SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY=""
if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
  DIRTY=1
fi

if [ -n "$TAG_OVERRIDE" ]; then
  TAG="$TAG_OVERRIDE"
elif [ -n "$DIRTY" ]; then
  if [ "$ALLOW_DIRTY" -ne 1 ]; then
    print_error "The working tree has uncommitted changes."
    print_error "The tag would be 'src-${SHA}', which names a commit that does not contain them —"
    print_error "and that tag is what the install reports as the source it is running."
    print_error "Commit, or re-run with --allow-dirty to tag it as visibly not a commit."
    exit 3
  fi
  TAG="src-${SHA}-dirty-$(date +%s)"
else
  TAG="src-${SHA}"
fi

# ─── The eight images ─────────────────────────────────────────────────────────
# Kept in step with `make cloud-build-push` by hand, and asserted against it below rather than
# trusted: the target's own help text said "every first-party image" while omitting replay-proxy,
# and a list that silently disagrees would verify seven images and report eight refreshed.
#
# kage-broker is built and its presence in the registry verified, but no BROKER_IMAGE is pinned in
# vars.sh below. That is deliberate and not an omission: nothing reads such a pin yet -- the broker
# has no standalone Deployment, the operator renders one per Agent CR in P9-T7 -- and a variable
# written for a consumer that does not exist is the kind of pin that gets trusted before it is
# wired. P9-T7 adds the pin and its reader together.
IMAGES=(
  platform-agent
  cluster-admin-agent
  developer-team-agent
  credential-proxy
  k8s-operator
  kage-router
  kage-broker
  replay-proxy
)

MAKEFILE="${REPO_ROOT}/Makefile"
# The tiers are NOT named in the Makefile. They are submitted by `for target in $(AGENTS)`, where
# AGENTS is `$(notdir $(wildcard agents/*/))` — so the recipe mentions no tier by name and the only
# evidence that a tier is built is that its directory exists and that loop is still there. Grepping
# the Makefile for the tier name therefore fails for every tier, which is how this guard refused
# `cluster-admin-agent` on an image set `dev/test_live_refresh_image_set.py` was passing on: the
# guard asserted a stronger property than the build actually has. Derive it the same way the
# Makefile and that test do instead. Only the explicitly-named submissions are greppable, and those
# are matched on `submit <name>` rather than anywhere in the file — `platform` was matching a
# comment, so the one tier that "passed" was passing for no reason.
for _img in "${IMAGES[@]}"; do
  case "$_img" in
    *-agent)
      _tier="${_img%-agent}"
      if [ ! -d "${REPO_ROOT}/agents/${_tier}" ]; then
        print_error "'${_img}' is in this script's image list but agents/${_tier}/ does not exist,"
        print_error "so cloud-build-push's \$(AGENTS) loop would not build it and the verification"
        print_error "below would fail on a missing tag. Reconcile the two lists."
        exit 3
      fi
      if ! grep -q 'submit "\$\$target-agent"' "$MAKEFILE"; then
        print_error "The root Makefile no longer submits the agent tiers via the \$(AGENTS) loop."
        print_error "This script's tier images (${_img} and its siblings) would not be built."
        exit 3
      fi
      ;;
    *)
      if ! grep -qE "^[[:space:]]*submit ${_img}([[:space:]]|\$)" "$MAKEFILE"; then
        print_error "'${_img}' is in this script's image list but the root Makefile's"
        print_error "cloud-build-push recipe has no 'submit ${_img}' line. It would not be built and"
        print_error "the verification below would fail on a missing tag. Reconcile the two lists."
        exit 3
      fi
      ;;
  esac
done

# ─── The summary, and the affirmative confirmation ────────────────────────────
print_step "About to refresh a LIVE install"
echo ""
echo -e "  ${C_BOLD}Target${C_RESET}"
echo -e "    project    ${C_WHITE}${PROJECT_ID}${C_RESET}"
echo -e "    region     ${C_WHITE}${REGION}${C_RESET}"
echo -e "    cluster    ${C_WHITE}${CLUSTER_NAME}${C_RESET}"
echo -e "    context    ${C_WHITE}${KUBE_CONTEXT}${C_RESET}"
echo ""
echo -e "  ${C_BOLD}Images${C_RESET}  ${#IMAGES[@]} built on Cloud Build into ${C_WHITE}${REGISTRY}${C_RESET}"
echo -e "    new tag    ${C_WHITE}${TAG}${C_RESET}${DIRTY:+  ${C_YELLOW}(working tree is dirty)${C_RESET}}"
echo -e "    replacing  operator ${C_WHITE}${OPERATOR_IMAGE:-<unset>}${C_RESET}"
echo -e "               router   ${C_WHITE}${ROUTER_IMAGE:-<unset>}${C_RESET}"
echo -e "               agents   ${C_WHITE}${AGENT_IMAGE:-<unset>}:${AGENT_TAG:-<unset>}${C_RESET}"
echo -e "               replay   ${C_WHITE}${REPLAY_IMAGE:-<unset>}${C_RESET}"
echo ""
echo -e "  ${C_BOLD}Then${C_RESET}    all 13 provisioning steps, which will restart every agent pod."
echo -e "          Step 06 (Slack) prompts twice; Enter keeps the existing tokens."
if [ "${EGRESS_POLICIES_ENABLED:-true}" = "true" ]; then
  echo -e "          Step 13 applies the per-tier egress allowlist and a default-deny floor"
  echo -e "          on namespace '${TENANT_NS}'. Set EGRESS_POLICIES_ENABLED=false to skip it."
fi
echo ""

if [ "${DRY_RUN:-0}" -eq 1 ]; then
  print_info "[DRY-RUN] Nothing will be built, written or applied."
elif [ "${NO_CONFIRM:-0}" -ne 1 ]; then
  # A typed cluster name, not y/N. This is the only protection the live install gets from this
  # script — a single keystroke is not a decision, and `y` is muscle memory.
  echo -ne "  ${C_CYAN}Type the cluster name to proceed [${C_WHITE}${CLUSTER_NAME}${C_CYAN}]: ${C_RESET}"
  read -r CONFIRM_INPUT
  if [ "$CONFIRM_INPUT" != "$CLUSTER_NAME" ]; then
    print_error "Got '${CONFIRM_INPUT}', expected '${CLUSTER_NAME}'. Nothing was changed."
    exit 2
  fi
fi

# ─── Build ────────────────────────────────────────────────────────────────────
# LOCATION and TAG are passed explicitly. LOCATION defaults to us-east4 in the root Makefile, which
# happens to be right for this install and would be silently wrong for one in another region;
# make exports command-line variables, so both reach the recipe.
if [ "$SKIP_BUILD" -eq 1 ]; then
  print_step "Skipping the build (--skip-build); verifying tag ${TAG} instead"
elif [ "${DRY_RUN:-0}" -eq 1 ]; then
  print_step "[DRY-RUN] Would run: make -C ${REPO_ROOT} cloud-build-push TAG=${TAG} LOCATION=${REGION}"
else
  print_step "Building ${#IMAGES[@]} images on Cloud Build at tag ${TAG}"
  print_info "Concurrent; roughly the wall-clock of the slowest single image."
  if ! make -C "$REPO_ROOT" cloud-build-push TAG="$TAG" LOCATION="$REGION"; then
    print_error "At least one image did not build. The tag ${TAG} is INCOMPLETE in ${REGISTRY};"
    print_error "deploying from it would mix this build with whatever was there before."
    exit 4
  fi
fi

# ─── Resolve every tag to a digest, by asking the registry ────────────────────
# The build reporting success is not the same fact as the tag resolving in the registry the cluster
# pulls from (LSN-021). Everything downstream — the pins, the deploy, the post-deploy comparison —
# describes whatever was there before if this is not checked here.
print_step "Confirming all ${#IMAGES[@]} images resolve in Artifact Registry"

DIGEST_DIR=""
cleanup_live_refresh() {
  [ -n "$DIGEST_DIR" ] && rm -rf "$DIGEST_DIR"
  tput cnorm 2>/dev/null || true
}
trap cleanup_live_refresh EXIT

DIGEST_DIR="$(mktemp -d)"
MISSING=0
for _img in "${IMAGES[@]}"; do
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[DRY-RUN] would resolve ${REGISTRY}/${_img}:${TAG}"
    continue
  fi
  _digest="$(gcloud artifacts docker images describe "${REGISTRY}/${_img}:${TAG}" \
    --project "$PROJECT_ID" --format='value(image_summary.digest)' 2>/dev/null || echo "")"
  if [ -z "$_digest" ]; then
    print_error "${_img}:${TAG} does not resolve in ${REGISTRY}."
    MISSING=$((MISSING + 1))
    continue
  fi
  printf '%s' "$_digest" >"${DIGEST_DIR}/${_img}"
  print_success "${_img} → ${_digest:0:19}…"
done

if [ "$MISSING" -gt 0 ]; then
  print_error "${MISSING} of ${#IMAGES[@]} images are absent from the registry at tag ${TAG}."
  print_error "Refusing to pin a tag the cluster cannot fully pull. Nothing was changed."
  exit 4
fi

# ─── Pin the new build in vars.sh ─────────────────────────────────────────────
# The step that gets skipped when this is done by hand, and the reason an install sits on a build
# nobody intended. save_var rewrites in place; vars.sh is gitignored and holds the install's secrets,
# so its contents are never echoed here.
print_step "Pinning tag ${TAG} in ${VARS_FILE}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  print_info "[DRY-RUN] would set OPERATOR_IMAGE / ROUTER_IMAGE / AGENT_IMAGE / AGENT_TAG / REPLAY_IMAGE"
else
  save_var "OPERATOR_IMAGE" "${REGISTRY}/k8s-operator:${TAG}"
  save_var "ROUTER_IMAGE" "${REGISTRY}/kage-router:${TAG}"
  save_var "AGENT_IMAGE" "${REGISTRY}/platform-agent"
  save_var "AGENT_TAG" "${TAG}"
  save_var "REPLAY_IMAGE" "${REGISTRY}/replay-proxy:${TAG}"
  print_success "5 image pins updated (provision_12 derives the two child tiers from these)."
fi

# ─── Provision ────────────────────────────────────────────────────────────────
# The existing 13-step pipeline, unmodified. Steps 03, 08, 11 and 12 hardcode `return 1` in their
# verify functions precisely so a re-run always reconciles rather than reporting "already done".
print_step "Running the provisioning pipeline"
PROVISION_ARGS=()
if [ "${DRY_RUN:-0}" -eq 1 ]; then PROVISION_ARGS+=("--dry-run"); fi
if [ "${NO_CONFIRM:-0}" -eq 1 ]; then PROVISION_ARGS+=("--no-confirm"); fi

if ! "${SCRIPT_DIR}/provision.sh" "${PROVISION_ARGS[@]}"; then
  print_error "Provisioning failed. The images are built and published at ${TAG} and vars.sh is"
  print_error "pinned to them, so re-running with --skip-build resumes without rebuilding."
  exit 1
fi

if [ "${DRY_RUN:-0}" -eq 1 ]; then
  print_step "[DRY-RUN] complete — nothing was built, written or applied."
  exit 0
fi

# ─── Let the rollouts finish before asking what is running ────────────────────
# Not cosmetic. provision_12 waits for the two child gateways to be Available, and NOTHING waits for
# the platform gateway — provision_08 applies the Agent CR and returns, leaving the operator to
# reconcile a Deployment on its own schedule. Checking digests at that moment finds a pod that is
# still the old one, has no deletionTimestamp yet, and is therefore indistinguishable from a rollout
# that failed. The comparison below is only meaningful once the cluster has stopped moving, and a
# check that reports a red on a healthy install is a check people learn to skip.
print_step "Waiting for rollouts to settle"

wait_for_rollouts() {
  local ns="$1" dep desired
  kubectl --context "$KUBE_CONTEXT" get namespace "$ns" >/dev/null 2>&1 || return 0
  for dep in $(kubectl --context "$KUBE_CONTEXT" -n "$ns" get deployments \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null); do
    # kage-router is parked at 0 replicas whenever Google Chat is not wired (provision_03 step 5),
    # and `rollout status` on a scaled-to-zero Deployment blocks until the timeout rather than
    # returning. Nothing is rolling out, so there is nothing to wait for.
    desired="$(kubectl --context "$KUBE_CONTEXT" -n "$ns" get deployment "$dep" \
      -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
    [ "${desired:-0}" -gt 0 ] 2>/dev/null || continue
    if kubectl --context "$KUBE_CONTEXT" -n "$ns" rollout status "deployment/${dep}" \
      --timeout=300s >/dev/null 2>&1; then
      print_success "${ns}/${dep} rolled out"
    else
      # A warning, not an exit. The digest comparison below is the authoritative verdict and gives a
      # far more specific answer than "rollout timed out" — including the case where this Deployment
      # is some third-party workload that was already unhealthy before the refresh started.
      print_warning "${ns}/${dep} did not report a complete rollout within 300s."
    fi
  done
}

wait_for_rollouts "${NAMESPACE:-kubeagents-system}"
if [ "$TENANT_NS" != "${NAMESPACE:-kubeagents-system}" ]; then
  wait_for_rollouts "$TENANT_NS"
fi

# ─── Prove it: what is running vs what was built ──────────────────────────────
# The reason this script can claim a refresh happened. Everything above proves images exist and that
# manifests were applied; neither is evidence about the containers now running. imageID is the
# kubelet's own record of what it pulled, so a stale image cannot satisfy it the way a tag can.
print_step "Verifying the running containers match the build"

MATCHED=0
STALE=0
declare -a STALE_LINES=()

check_namespace() {
  local ns="$1" line pod deleted i img id short want
  kubectl --context "$KUBE_CONTEXT" get namespace "$ns" >/dev/null 2>&1 || return 0

  # One call per namespace. The inner range walks containerStatuses within the current pod, so each
  # line is: pod, deletionTimestamp, then (image, imageID) pairs.
  #
  # The separator is '|', not a tab, and that is load-bearing. Tab is IFS *whitespace*: bash
  # collapses runs of it and discards empty fields, so a healthy pod — whose deletionTimestamp is
  # empty — arrived as `pod, image, imageID` and `deleted` held the image name. Non-empty means
  # "being torn down", so the loop skipped every healthy pod and every namespace verified nothing.
  # The MATCHED=0 guard below caught that and refused to report a pass, which is the only reason
  # this surfaced as exit 5 rather than a false green. '|' is not IFS whitespace, so empty fields
  # survive; it cannot occur in a pod name, an image reference or a digest.
  while IFS='|' read -r -a line; do
    [ "${#line[@]}" -ge 3 ] || continue
    pod="${line[0]}"
    deleted="${line[1]}"
    # A pod being torn down after a successful rollout still reports phase Running and still carries
    # the OLD imageID. Counting it as stale would make every successful refresh look failed.
    [ -z "$deleted" ] || continue
    for ((i = 2; i + 1 < ${#line[@]}; i += 2)); do
      img="${line[i]}"
      id="${line[i + 1]}"
      # Only first-party images are in scope. cert-manager, LiteLLM, gVisor and the tenant's own
      # workloads live here too and are not this script's business.
      case "$img" in
        "${REGISTRY}/"*) short="${img#"${REGISTRY}"/}" ;;
        *) continue ;;
      esac
      short="${short%%:*}"
      short="${short%%@*}"
      [ -f "${DIGEST_DIR}/${short}" ] || continue
      want="$(cat "${DIGEST_DIR}/${short}")"
      if [ "${id##*@}" = "$want" ]; then
        MATCHED=$((MATCHED + 1))
        print_success "${ns}/${pod}  ${short} @ ${want:0:19}…"
      else
        STALE=$((STALE + 1))
        STALE_LINES+=("${ns}/${pod}  ${short}  running ${id##*@} — expected ${want}")
        print_error "${ns}/${pod}  ${short} is NOT the build just published."
      fi
    done
  done < <(kubectl --context "$KUBE_CONTEXT" -n "$ns" get pods \
    -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.metadata.deletionTimestamp}{range .status.containerStatuses[*]}{"|"}{.image}{"|"}{.imageID}{end}{"\n"}{end}' 2>/dev/null)
}

check_namespace "${NAMESPACE:-kubeagents-system}"
if [ "$TENANT_NS" != "${NAMESPACE:-kubeagents-system}" ]; then
  check_namespace "$TENANT_NS"
fi

echo ""
if [ "$STALE" -gt 0 ]; then
  print_error "${STALE} container(s) are running something other than ${TAG}:"
  for _l in "${STALE_LINES[@]}"; do
    print_error "  ${_l}"
  done
  print_error "The images ARE published and correct — the cluster did not converge. Check for"
  print_error "pods stuck Pending or in ImagePullBackOff:"
  print_error "    kubectl --context ${KUBE_CONTEXT} -n ${NAMESPACE:-kubeagents-system} get pods"
  exit 5
fi

# Zero matches is not success. A jsonpath that silently returned nothing, a namespace that does not
# exist, a registry rename — all of them produce zero comparisons, and "verified nothing" must not
# print the same thing as "verified everything" (LSN-008, LSN-024).
if [ "$MATCHED" -eq 0 ]; then
  print_error "No first-party container was found to compare against ${REGISTRY}."
  print_error "The refresh cannot be confirmed — this is not a pass. Look at what is running:"
  print_error "    kubectl --context ${KUBE_CONTEXT} -n ${NAMESPACE:-kubeagents-system} get pods -o wide"
  exit 5
fi

print_success "${MATCHED} running container(s) confirmed at tag ${TAG} by digest."

# ─── Go prompt it ─────────────────────────────────────────────────────────────
print_step "Ready to validate"
echo -e "  ${C_CYAN}The install is running ${C_WHITE}${TAG}${C_CYAN} (${SHA}${DIRTY:+, dirty tree}).${C_RESET}"
echo ""
if [ "${SLACK_ENABLED:-false}" = "true" ]; then
  "${SCRIPT_DIR}/print_instructions_slack.sh" || true
else
  print_warning "SLACK_ENABLED is not 'true' — there is no Slack front door on this install."
fi
