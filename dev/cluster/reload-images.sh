#!/usr/bin/env bash
# reload-images.sh — build kube-agents images FROM YOUR WORKING TREE on Cloud Build, push them to
# Artifact Registry, and point the running workloads at the resulting DIGEST, so the inner-loop
# gates test YOUR code and can prove they are doing so.
#
# WHY THIS EXISTS
#   `make deploy` does NOT build — it only runs `kustomize set image` + `kubectl apply`. Deploying
#   the published tag therefore tests the UPSTREAM binary (ghcr.io/gke-labs/...), not your changes.
#   This script does build -> push -> repoint -> restart, which is the whole "test my changes"
#   loop in one command.
#
# WHY CLOUD BUILD AND NOT `docker build`
#   The nodes are amd64 and the developer host is arm64. A local build produces images the cluster
#   cannot execute, and the failure surfaces as CrashLoopBackOff with `exec format error` several
#   minutes later, in a different component. Cloud Build is not the slow option here; it is the
#   only correct one for the agent tiers, which are FROM nousresearch/hermes-agent — a whole
#   userspace, not one static binary, so the $PREBUILT_BINARY cross-compile hatch does not apply.
#
# WHAT REPLACED THE imagePullPolicy TRAP
#   The Kind version of this script side-loaded a fixed `:dev` tag and depended on
#   `imagePullPolicy: IfNotPresent`, which is the LSN-001 stale-image trap in script form: same tag
#   + IfNotPresent means the kubelet keeps a copy it already has, so a rebuilt image silently does
#   not take effect. This deploys by DIGEST. A digest cannot be stale — it names one immutable
#   manifest — so the trap stops being something P1 has to detect and becomes something the
#   deployment mechanism makes unrepresentable. It also makes the pull policy irrelevant rather
#   than load-bearing.
#
# Usage: dev/cluster/reload-images.sh [operator|router|agents|all|digest|digest-router] [kube-context]
#   operator (default)  build+push the controller image, repoint + restart the controller
#   router              build+push the kage-router image, repoint + restart the router
#   agents              build+push the three tier agent images, repoint every Agent CR of each tier
#   all                 all three
#   digest              build+push the controller image and print its DIGEST reference on stdout,
#   digest-router       same for the router. Both touch no cluster. They are for up.sh on a cluster
#                       that has no Deployment to repoint yet: `make deploy` needs an IMG and a
#                       ROUTER_IMG, and the alternative — deploy the upstream tag as a placeholder,
#                       then repoint — means rolling out somebody else's binary inside a script
#                       whose job is to install YOURS. It is also not available: the published
#                       `ghcr.io/gke-labs/kube-agents/kage-router:v0.1.0` answers an anonymous pull
#                       with 403, so a cluster brought up on the default ROUTER_IMG gets a router
#                       stuck in ImagePullBackOff — which is 09 §11.9 ("built, never wired") sitting
#                       in the inner loop's own bring-up. Progress goes to stderr so `$(...)`
#                       captures the reference alone.
#
# Exit codes (contract shared with up.sh, and relied on by the L2 suites):
#   0 ok · 1 usage · 2 refused (guard) · 3 required tool missing · 4 an image did not materialise
#   5 the digest IS deployed but the workload did not become Ready — `router` only; see reload_router
set -uo pipefail

TARGET="${1:-operator}"
CTX="${2:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get core/project 2>/dev/null)}"
REGION="${REGION:-us-east4}"
AR_REPO="${AR_REPO:-kube-agents}"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO"
NS=kubeagents-system

# --- DESTRUCTIVE-TEST GUARD: only touch an ephemeral scratch cluster -----------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` would accept `my-gke-scratch-of-prod`, and
# the live install `platform-agent-host` is one `*` away from every script in this directory. The
# default arm exits non-zero; that is the half that makes the rest of it a guard.
case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not an ephemeral scratch cluster (image-reload guard)." >&2
     echo "  This script repoints running workloads. Name the dev cluster explicitly:" >&2
     echo "    $0 $TARGET gke-scratch-kube-agents-dev" >&2
     exit 2 ;;
esac

for tool in gcloud kubectl git; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is not installed." >&2; exit 3; }
done
[ -n "$PROJECT_ID" ] || { echo "ERROR: no GCP project set (gcloud config set project ...)." >&2; exit 3; }
cd "$REPO_ROOT" || { echo "ERROR: cannot enter $REPO_ROOT." >&2; exit 3; }

K="kubectl --context $CTX"

# The tag is derived, never chosen. P1 reads the short sha back out of the deployed reference to
# answer "is the cluster running the current source", so the tag has to CARRY that answer -- and a
# dirty tree has to be visibly not a commit, or P1 would certify uncommitted work as `dev-abc1234`.
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  TAG="dev-$SHA-dirty-$(date +%s)"
else
  TAG="dev-$SHA"
fi
# shellcheck disable=SC1091
. "$REPO_ROOT/tags.env"

# build_and_resolve <image-name> <extra-substitutions> -> echoes repo/name@sha256:...
#
# Two steps that must not be collapsed into one. `gcloud builds submit` succeeding tells you a
# build ran; it does not tell you what is now in the registry under that tag, and the difference is
# exactly LSN-021 -- a command that "ran" and left nothing behind. Reading the digest back is a
# question asked of the registry the cluster pulls from, not of the builder.
build_and_resolve() {
  local name="$1" subs="$2"
  local uri="$REGISTRY/$name:$TAG"
  local cache="$REGISTRY/$name:buildcache"
  echo "-> building $name:$TAG on Cloud Build" >&2
  if ! gcloud builds submit \
      --config deploy/docker/cloudbuild.yaml \
      --project "$PROJECT_ID" \
      --substitutions="_IMAGE_URI=$uri,_CACHE_URI=$cache,$subs" \
      . >&2; then
    echo "ERROR: Cloud Build failed for $name." >&2
    return 4
  fi
  local digest
  digest="$(gcloud artifacts docker images describe "$uri" \
    --project "$PROJECT_ID" --format='value(image_summary.digest)' 2>/dev/null)"
  if [ -z "$digest" ]; then
    # Not a warning. The build reported success, so an absent digest means the tag does not resolve
    # in the registry the cluster pulls from -- and every result downstream would describe whatever
    # was there before.
    echo "ERROR: $uri built but does not resolve in Artifact Registry; nothing to deploy." >&2
    return 4
  fi
  echo "   $name -> ${digest:0:19}..." >&2
  echo "$REGISTRY/$name@$digest"
}

# build_concurrently <name>:<subs> [<name>:<subs> ...] -> writes $BUILD_REFS/<name>.ref per image
#
# Cloud Build gives every submission its own worker, so these were never competing for anything --
# the serial version simply blocked this laptop on `Waiting for build to complete` once per image.
# Measured 2026-07-26: ~5 min each, so `all` was ~25 min of wall clock to do ~5 min of work.
#
# Every PID is waited on INDIVIDUALLY. A bare `wait` yields 0 no matter what the jobs did, which
# here would mean reporting a green reload for images that never reached the registry -- the exact
# "the command ran and left nothing behind" shape build_and_resolve's two-step exists to catch.
# Substitutions may not contain a colon; none do, and the `%%`/`#` split below assumes it.
BUILD_REFS=""
trap '[ -n "$BUILD_REFS" ] && rm -rf "$BUILD_REFS"' EXIT
build_concurrently() {
  local spec name pids='' rc=0
  # Removed on EXIT by the trap above. Left in place while the script runs because the refs are read
  # back after the deploy steps, and a ref file deleted early reads as "that image was never built".
  BUILD_REFS="$(mktemp -d)" || return 4
  for spec in "$@"; do
    name="${spec%%:*}"
    build_and_resolve "$name" "${spec#*:}" >"$BUILD_REFS/$name.ref" &
    pids="$pids $!:$name"
  done
  echo "-- $# image(s) building concurrently on Cloud Build --" >&2
  for spec in $pids; do
    wait "${spec%%:*}" || { rc=4; echo "ERROR: build failed for ${spec##*:}." >&2; }
  done
  return $rc
}

# ref_for <name> -> the digest reference build_concurrently resolved, or empty
ref_for() { tr -d '\n' <"$BUILD_REFS/$1.ref" 2>/dev/null; }

# The build half and the deploy half are separate functions ONLY so that `all` can put all five
# builds on Cloud Build at once and then deploy them. Each `reload_*` still reads as build-then-deploy.
OPERATOR_SPEC='k8s-operator:_CONTEXT=k8s-operator,_DOCKERFILE=k8s-operator/Dockerfile'
ROUTER_SPEC='kage-router:_CONTEXT=k8s-operator,_DOCKERFILE=k8s-operator/Dockerfile.router'
agent_spec() { echo "$1-agent:_TARGET=$1,_HERMES_AGENT_TAG=$HERMES_AGENT_TAG"; }

deploy_operator() {
  local ref="$1"
  [ -n "$ref" ] || return 4
  echo "-> repointing the controller at $ref"
  $K -n "$NS" set image deploy/kubeagents-controller-manager "manager=$ref" || return 4
  $K -n "$NS" rollout status deploy/kubeagents-controller-manager --timeout=180s || return 4
  echo "OK: controller now running $ref"
}

reload_operator() {
  echo "== operator =="
  build_concurrently "$OPERATOR_SPEC" || return 4
  deploy_operator "$(ref_for k8s-operator)"
}

# The router is a separate image from the same tree (Dockerfile.router), not a variant of the
# operator: 05 C15 makes it the read-only ChatOps front door, with its own SA and its own role.
#
# THE ONE PLACE THIS SCRIPT SPLITS "no image" FROM "image, no Ready pod", AND WHY.
#   Everywhere else the two are the same failure. Here they are not, because the router is KNOWN not
#   to start on an unwired cluster: config/router/deployment.yaml ships KAGE_PROJECT_ID and
#   KAGE_INBOUND_SUBSCRIPTION as empty strings (deliberately -- V-CMP-003, and an empty value makes
#   the process name the variable instead of failing later inside the Pub/Sub client), and its SA
#   carries no Workload Identity annotation, so it
#   exits on `missing required --project-id` before it can reach Pub/Sub. That is a disclosed gap in
#   the CONFIG, and it is not evidence about the image -- which is the thing this script exists to
#   put on the cluster. Collapsing them would make every inner-loop bring-up fail on a condition
#   nobody in the inner loop can fix, and the usual response to that is to stop running the step,
#   which is how an image goes back to being never built. So: rc 4 if the registry has nothing
#   (fatal anywhere), rc 5 if the digest is deployed and the pod will not come up (the caller
#   decides, and up.sh discloses it).
deploy_router() {
  local ref="$1"
  [ -n "$ref" ] || return 4
  echo "-> repointing the router at $ref"
  $K -n "$NS" set image deploy/kubeagents-router "router=$ref" || return 4
  if ! $K -n "$NS" rollout status deploy/kubeagents-router --timeout=180s; then
    echo "NOTE: the router is deployed at $ref but did not become Ready." >&2
    echo "  The image is the one under test; the pod's CONFIG is not wired. Read the reason:" >&2
    echo "    kubectl --context $CTX -n $NS logs deploy/kubeagents-router" >&2
    return 5
  fi
  echo "OK: router now running $ref"
}

reload_router() {
  echo "== router =="
  build_concurrently "$ROUTER_SPEC" || return 4
  deploy_router "$(ref_for kage-router)"
}

# patch_agent_crs — repoint every Agent CR at the digest built for ITS tier.
#
# Runs once, after all three images exist, rather than once per tier inside the build loop. The CR
# list is a property of the cluster and does not change between builds, so re-listing it three times
# was three answers to the same question.
patch_agent_crs() {
  local patched=0 crs ns name crtier ref
  # An empty spec.deployment.image does NOT fall back to anything local -- the controller resolves
  # ghcr.io/gke-labs/kube-agents/<tier>-agent:v0.1.0, which is the upstream build. So the CRs are
  # patched, not merely reported on. resolveAgentImage() already treats a reference containing '@'
  # as complete and passes it through untouched, so a digest needs no `tag:`.
  crs="$($K get agents -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}/{.spec.tier}{"\n"}{end}' 2>/dev/null)"
  while IFS=/ read -r ns name crtier; do
    [ -n "$name" ] || continue
    # An absent spec.tier IS platform (agentindex.EffectiveTier), so the default has to be applied
    # here too, or platform agents written without the field silently keep the upstream image
    # while this script reports three tiers rebuilt.
    [ -n "$crtier" ] || crtier=platform
    ref="$(ref_for "$crtier-agent")"
    # A CR naming a tier nobody built is not a no-op to pass over: it would keep the upstream image
    # while this script reported every CR repointed.
    [ -n "$ref" ] || { echo "ERROR: agent $ns/$name has tier '$crtier', which was not built." >&2; return 4; }
    $K -n "$ns" patch agent "$name" --type=merge \
      -p "{\"spec\":{\"deployment\":{\"image\":\"$ref\"}}}" >/dev/null || return 4
    echo "   patched agent $ns/$name -> $crtier digest"
    patched=$((patched + 1))
  done <<<"$crs"
  AGENT_CRS_PATCHED="$patched"
}

reload_agents() {
  echo "== agents =="
  local tier built=0 patched=0
  build_concurrently "$(agent_spec platform)" "$(agent_spec cluster-admin)" "$(agent_spec developer-team)" || return 4
  for tier in platform cluster-admin developer-team; do
    [ -n "$(ref_for "$tier-agent")" ] && built=$((built + 1))
  done
  if [ "$built" -ne 3 ]; then
    echo "ERROR: $built of 3 agent images built — refusing to report success." >&2
    return 4
  fi
  patch_agent_crs || return 4
  patched="$AGENT_CRS_PATCHED"
  # Zero CRs is legitimate: up.sh runs this on a cluster that has no fixtures yet. It is reported as
  # a count rather than passed over in silence, because "built and deployed nothing" and "built and
  # deployed everything" must not print the same thing.
  echo "OK: 3 agent images built and pushed at $TAG; $patched Agent CR(s) repointed at their digest."
  if [ "$patched" -eq 0 ]; then
    echo "   (no Agent CRs exist on $CTX yet — seed fixtures, then re-run to repoint them)"
  fi
}

# `all` is not `operator && router && agents`: that chain is five Cloud Build submissions one after
# another, and the deploy step of each is seconds of work gating the next five-minute build. One
# concurrent build of everything, then the deploys, is the same work in the time of the slowest image.
#
# The router's rc 5 -- deployed at the right digest, will not start for want of config -- is carried
# through rather than allowed to short-circuit the agents, because it is a disclosed CONFIG gap and
# `&&` would have made it silently skip the rest of the reload.
reload_all() {
  local rc=0 router_rc=0
  build_concurrently "$OPERATOR_SPEC" "$ROUTER_SPEC" \
    "$(agent_spec platform)" "$(agent_spec cluster-admin)" "$(agent_spec developer-team)" || return 4
  echo "== operator =="; deploy_operator "$(ref_for k8s-operator)" || return 4
  echo "== router ==";   deploy_router   "$(ref_for kage-router)" || router_rc=$?
  [ "$router_rc" -eq 4 ] && return 4
  echo "== agents =="
  patch_agent_crs || return 4
  echo "OK: 3 agent images built and pushed at $TAG; $AGENT_CRS_PATCHED Agent CR(s) repointed at their digest."
  [ "$rc" -eq 0 ] && rc=$router_rc
  return $rc
}

case "$TARGET" in
  operator) reload_operator ;;
  router)   reload_router ;;
  agents)   reload_agents ;;
  all)      reload_all ;;
  # The guard above still ran, and deliberately, even though this arm touches no cluster. A guard
  # that applies to some subcommands and not others is a guard someone has to remember the shape
  # of; this one is uniform and costs nothing here.
  digest)        build_and_resolve k8s-operator "_CONTEXT=k8s-operator,_DOCKERFILE=k8s-operator/Dockerfile" ;;
  digest-router) build_and_resolve kage-router "_CONTEXT=k8s-operator,_DOCKERFILE=k8s-operator/Dockerfile.router" ;;
  *) echo "usage: $0 [operator|router|agents|all|digest|digest-router] [kube-context]" >&2; exit 1 ;;
esac
