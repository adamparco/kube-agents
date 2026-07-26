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
# Usage: dev/cluster/reload-images.sh [operator|agents|all] [kube-context]
#   operator (default)  build+push the controller image, repoint + restart the controller
#   agents              build+push the three tier agent images, repoint every Agent CR of each tier
#   all                 both
#
# Exit codes (contract shared with up.sh, and relied on by the L2 suites):
#   0 ok · 1 usage · 2 refused (guard) · 3 required tool missing · 4 an image did not materialise
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

reload_operator() {
  echo "== operator =="
  local ref
  ref="$(build_and_resolve k8s-operator "_CONTEXT=k8s-operator,_DOCKERFILE=k8s-operator/Dockerfile")" || return 4
  echo "-> repointing the controller at $ref"
  $K -n "$NS" set image deploy/kubeagents-controller-manager "manager=$ref" || return 4
  $K -n "$NS" rollout status deploy/kubeagents-controller-manager --timeout=180s || return 4
  echo "OK: controller now running $ref"
}

reload_agents() {
  echo "== agents =="
  local built=0 patched=0 tier ref crs ns name crtier
  for tier in platform cluster-admin developer-team; do
    ref="$(build_and_resolve "$tier-agent" "_TARGET=$tier,_HERMES_AGENT_TAG=$HERMES_AGENT_TAG")" || return 4
    built=$((built + 1))

    # An empty spec.deployment.image does NOT fall back to anything local -- the controller
    # resolves ghcr.io/gke-labs/kube-agents/<tier>-agent:v0.1.0, which is the upstream build. So
    # the CRs are patched, not merely reported on. resolveAgentImage() already treats a reference
    # containing '@' as complete and passes it through untouched, so a digest needs no `tag:`.
    crs="$($K get agents -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}/{.spec.tier}{"\n"}{end}' 2>/dev/null)"
    while IFS=/ read -r ns name crtier; do
      [ -n "$name" ] || continue
      # An absent spec.tier IS platform (agentindex.EffectiveTier), so the default has to be applied
      # here too, or platform agents written without the field silently keep the upstream image
      # while this script reports three tiers rebuilt.
      [ -n "$crtier" ] || crtier=platform
      [ "$crtier" = "$tier" ] || continue
      $K -n "$ns" patch agent "$name" --type=merge \
        -p "{\"spec\":{\"deployment\":{\"image\":\"$ref\"}}}" >/dev/null || return 4
      echo "   patched agent $ns/$name -> $tier digest"
      patched=$((patched + 1))
    done <<<"$crs"
  done

  if [ "$built" -ne 3 ]; then
    echo "ERROR: $built of 3 agent images built — refusing to report success." >&2
    return 4
  fi
  # Zero CRs is legitimate: up.sh runs this on a cluster that has no fixtures yet. It is reported as
  # a count rather than passed over in silence, because "built and deployed nothing" and "built and
  # deployed everything" must not print the same thing.
  echo "OK: 3 agent images built and pushed at $TAG; $patched Agent CR(s) repointed at their digest."
  if [ "$patched" -eq 0 ]; then
    echo "   (no Agent CRs exist on $CTX yet — seed fixtures, then re-run to repoint them)"
  fi
}

case "$TARGET" in
  operator) reload_operator ;;
  agents)   reload_agents ;;
  all)      reload_operator && reload_agents ;;
  *) echo "usage: $0 [operator|agents|all] [kube-context]" >&2; exit 1 ;;
esac
