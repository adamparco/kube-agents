#!/usr/bin/env bash
# closed-allowlist-l2.sh — the LIVE half of V-CTR-014 and the V-7 slice of V-CTR-002 (Phase 8, P8-T1).
#
# The L0 validator (local-dev/tests/closed-allowlist.py) proves the forbidden SHAPE is absent from the
# tree. It cannot prove the rules WORK, for one specific reason: nothing in the Go build ever compiles
# CEL. `controller-gen` copies the marker string verbatim, `go vet` does not read it, and `go test`
# never instantiates an API server. A CEL rule can be syntactically invalid and every L0/L1 gate stays
# green — which is exactly what happened in P8-T1 (gofmt rewrote the `''` empty-string literal into
# U+201D; see LSN-016). The API server is the first thing in the chain that parses the expression, so
# this script is the first thing that can fail on a broken one.
#
# What it proves, on a live cluster:
#   L2-1  The CRD INSTALLS. Applying it compiles every x-kubernetes-validations rule; a malformed CEL
#         expression is rejected here and nowhere earlier.
#   L2-2  V-CTR-002 (06 §1.2 V-7 slice): each degenerate allowlist shape — absent, empty list, a single
#         blank entry, all-whitespace entries — is REFUSED for BOTH chat platforms, and the refusal
#         names the field path (`spec.integration.<platform>.allowedUsers`). The field path is not a
#         nicety: V-CTR-002 requires it, because a rejection that does not say what to fix sends the
#         operator to the CRD instead of to their own manifest.
#   L2-3  Positive control: the same CR with one real principal is ADMITTED. Without this, a webhook
#         that rejected everything would score a perfect L2-2.
#   L2-4  V-CTR-014 (live half): no Deployment the operator renders — from the fixture below or from
#         any Agent CR already in the cluster — carries a `*_ALLOW_ALL_USERS` env var, and the
#         allowlist env it does carry holds exactly the non-blank entries.
#
# The fixture is a single platform-tier Agent under a probe-only projectId, applied and deleted by this
# script. Every negative case runs with --dry-run=server: admission (CEL + webhook) executes in full,
# and nothing is persisted.
#
# DESTRUCTIVE-TEST GUARD: Kind / scratch-GKE contexts only.
# Usage: local-dev/kind/closed-allowlist-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed: LSN-001 and LSN-002 each
# recurred against scripts whose authors believed the preconditions held.
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager — the webhook and CRD under test are served
#      BY the operator image. A stale operator validates with the old rules and every negative in
#      L2-2 passes for the wrong reason. Asserted via p1_assert_build_under_test.
#   P3 admission-recreate: the CRD itself (L2-1 re-applies it server-side before anything is asserted about its rules) and
#      every Agent fixture, all of which are submitted with --dry-run=server so admission runs in
#      full on a fresh object each time.
#   P6 runtime-authoritative: the live CRD and the live Deployments the operator rendered — not the checked-in CRD YAML, which
#      is an input. L2-4 reads the running Deployment's env, which is what the router actually sees.
set -uo pipefail

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$CTX" in
  kind-*|gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind cluster (destructive-test guard)." >&2; exit 2 ;;
esac

K="kubectl --context $CTX"
CRD="$REPO_ROOT/k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agents.yaml"
NS=kubeagents-system
PROBE=vctr-l2-allowlist-probe
PROJECT=vctr-l2-probe

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
cd "$REPO_ROOT"

echo "===================================================================="
echo " V-CTR-002 (V-7) + V-CTR-014 — live allowlist enforcement — ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# --- P1: the webhook doing the rejecting must be the build under test ---------------------------
# L2-2 asserts that four degenerate allowlist shapes are REFUSED. A stale operator refuses them with
# the old rules -- or, if the CRD's CEL is doing the work, refuses them for a reason the webhook
# change under test had nothing to do with. Either way the run is green about code that is not
# there, which is LSN-001's shape and the reason it recurred three times.
. "$REPO_ROOT/local-dev/kind/lib/preconditions.sh"
p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running operator is the build under test" ;;
  3) echo "DEFERRED: P1 unverifiable (see above). L2-1 and the CEL claims below would still be"
     echo "  meaningful, but L2-2/L2-4 are webhook and controller behaviour and would not be."
     exit 3 ;;
  *) bad "P1: the cluster is not running the build under test"; exit 1 ;;
esac

# --- L2-1: the CRD installs (this is what compiles the CEL) -----------------------------------
echo; echo "== L2-1: CRD applies (compiles every CEL rule) =="
out="$($K apply --server-side --force-conflicts -f "$CRD" 2>&1)"
if [ $? -eq 0 ]; then
  pass "CRD applied — all x-kubernetes-validations rules compile"
else
  bad "CRD REJECTED by the API server — a CEL rule does not compile: $out"
  echo "  (this is the failure L0 cannot see; see LSN-016)"
  exit 1
fi

# --- fixture ------------------------------------------------------------------------------------
# $1 = the allowedUsers YAML fragment to splice under googleChat; $2 = same for slack.
# Emitting the platform block only when its fragment is non-empty keeps each case single-variable.
render() { # <platform> <allowlist-fragment>
  local platform="$1" frag="$2"
  cat <<EOF
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $PROBE
  namespace: $NS
spec:
  tier: platform
  scope:
    projectId: $PROJECT
  harness:
    clusterName: kube-agents-dev
    location: us-central1
    hermes:
      dashboardEnabled: false
      apiServerSecretRef:
        name: platform-agent-secrets
        key: API_SERVER_KEY
  security:
    serviceAccountName: platform-agent
  deployment:
    image: ghcr.io/gke-labs/kube-agents/platform-agent
    tag: v0.1.0
    imagePullPolicy: IfNotPresent
  integration:
EOF
  if [ "$platform" = googlechat ]; then
    cat <<EOF
    googleChat:
      enabled: true
      projectId: $PROJECT
      topicName: kubeagents-$PROBE-events
      subscriptionName: kubeagents-$PROBE-events-sub
$frag
EOF
  else
    cat <<EOF
    slack:
      enabled: true
      botTokenSecretRef:
        name: platform-agent-secrets
        key: SLACK_BOT_TOKEN
      appTokenSecretRef:
        name: platform-agent-secrets
        key: SLACK_APP_TOKEN
$frag
EOF
  fi
}

# --- L2-2: every degenerate shape is refused, with the field path ---------------------------------
echo; echo "== L2-2: V-CTR-002 — degenerate allowlists refused with the field path =="

declare -a SHAPES=(
  "absent|"
  "empty list|      allowedUsers: []"
  "single blank entry|      allowedUsers:\n        - \"\""
  "single space|      allowedUsers:\n        - \" \""
  "all whitespace|      allowedUsers:\n        - \"  \"\n        - \"\t\""
)

for platform in googlechat slack; do
  case "$platform" in
    googlechat) path="spec.integration.googleChat.allowedUsers" ;;
    slack)      path="spec.integration.slack.allowedUsers" ;;
  esac
  for entry in "${SHAPES[@]}"; do
    label="${entry%%|*}"
    frag="$(printf '%b' "${entry#*|}")"
    out="$(render "$platform" "$frag" | $K apply --dry-run=server -f - 2>&1)"; rc=$?
    if [ $rc -eq 0 ]; then
      bad "$platform / $label: ADMITTED — an allowlist that names nobody was accepted (06 §1.2 V-7)"
      continue
    fi
    if printf '%s' "$out" | grep -qF "$path"; then
      pass "$platform / $label: refused, message names $path"
    else
      bad "$platform / $label: refused, but the message does not name $path (V-CTR-002 requires it): $out"
    fi
  done
done

# --- L2-3: positive control — a real principal is admitted ----------------------------------------
echo; echo "== L2-3: positive control — a real allowlist is admitted =="
for platform in googlechat slack; do
  case "$platform" in
    googlechat) real='      allowedUsers:\n        - "users/1234567890"' ;;
    slack)      real='      allowedUsers:\n        - "U02ABCDEF"' ;;
  esac
  out="$(render "$platform" "$(printf '%b' "$real")" | $K apply --dry-run=server -f - 2>&1)"
  if [ $? -eq 0 ]; then
    pass "$platform: a one-real-principal allowlist is ADMITTED (the rules refuse blanks, not everything)"
  else
    bad "$platform: a valid allowlist was rejected — the rule is over-tight: $out"
  fi
done

# A list whose blanks surround a real entry must also be admitted: `exists` is the correct quantifier,
# and a rule written with `all` instead would reject this and still pass every case above.
echo; echo "== L2-3b: quantifier control — blanks alongside a real principal are admitted =="
mixed='      allowedUsers:\n        - ""\n        - "users/1234567890"\n        - "   "'
out="$(render googlechat "$(printf '%b' "$mixed")" | $K apply --dry-run=server -f - 2>&1)"
if [ $? -eq 0 ]; then
  pass "mixed blank + real allowlist ADMITTED (exists, not all)"
else
  bad "mixed allowlist rejected — the CEL quantifier is wrong: $out"
fi

# --- L2-4: V-CTR-014 live — nothing rendered carries the retired escape hatch ---------------------
echo; echo "== L2-4: V-CTR-014 — no rendered Deployment carries *_ALLOW_ALL_USERS =="

cleanup() { $K -n "$NS" delete agent "$PROBE" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

if render googlechat "$(printf '%b' '      allowedUsers:\n        - ""\n        - "users/1234567890"\n        - "   "')" \
     | $K apply -f - >/dev/null 2>&1; then
  # The operator renders the Deployment on reconcile; poll rather than sleep a fixed interval.
  dep=""
  for _ in $(seq 1 30); do
    dep="$($K -n "$NS" get deploy -l kube-agents/agent="$PROBE" -o name 2>/dev/null | head -1)"
    [ -n "$dep" ] && break
    dep="$($K -n "$NS" get deploy "${PROBE}-gateway" -o name 2>/dev/null)"
    [ -n "$dep" ] && break
    sleep 2
  done
  if [ -z "$dep" ]; then
    bad "the operator rendered no Deployment for the probe Agent within 60s"
  else
    envjson="$($K -n "$NS" get "$dep" -o jsonpath='{.spec.template.spec.containers[*].env[*].name}' 2>/dev/null)"
    if printf '%s' "$envjson" | grep -q "ALLOW_ALL_USERS"; then
      bad "rendered Deployment carries a *_ALLOW_ALL_USERS env var (V-CTR-014): $envjson"
    else
      pass "rendered Deployment carries no *_ALLOW_ALL_USERS env var"
    fi
    val="$($K -n "$NS" get "$dep" -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="GOOGLE_CHAT_ALLOWED_USERS")].value}' 2>/dev/null)"
    if [ "$val" = "users/1234567890" ]; then
      pass "GOOGLE_CHAT_ALLOWED_USERS holds exactly the non-blank entries ('$val')"
    else
      bad "GOOGLE_CHAT_ALLOWED_USERS is '$val', expected 'users/1234567890' (blanks must be dropped, not joined)"
    fi
  fi
else
  bad "the probe Agent with a valid allowlist was rejected — cannot evaluate the rendered pod"
fi

# Every Agent already in the cluster must satisfy the same property.
echo; echo "== L2-4b: no EXISTING rendered Deployment carries the retired hatch =="
found_hatch=0
while read -r ns name; do
  [ -z "$name" ] && continue
  names="$($K -n "$ns" get deploy "$name" -o jsonpath='{.spec.template.spec.containers[*].env[*].name}' 2>/dev/null)"
  if printf '%s' "$names" | grep -q "ALLOW_ALL_USERS"; then
    bad "$ns/$name carries a *_ALLOW_ALL_USERS env var (V-CTR-014)"
    found_hatch=1
  fi
done < <($K get deploy -A -l app.kubernetes.io/managed-by=kube-agents-operator --no-headers 2>/dev/null | awk '{print $1, $2}')
# The label selector above is the operator's own; fall back to a whole-cluster sweep so a renaming of
# that label cannot turn this check into a silent no-op.
while read -r ns name; do
  [ -z "$name" ] && continue
  names="$($K -n "$ns" get deploy "$name" -o jsonpath='{.spec.template.spec.containers[*].env[*].name}' 2>/dev/null)"
  if printf '%s' "$names" | grep -q "ALLOW_ALL_USERS"; then
    bad "$ns/$name carries a *_ALLOW_ALL_USERS env var (V-CTR-014, cluster-wide sweep)"
    found_hatch=1
  fi
done < <($K get deploy -A --no-headers 2>/dev/null | awk '{print $1, $2}')
[ "$found_hatch" -eq 0 ] && pass "no Deployment in the cluster carries a *_ALLOW_ALL_USERS env var"

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " V-CTR-002 (V-7) + V-CTR-014 at L2: ALL CHECKS PASSED"
else
  echo " V-CTR-002 (V-7) + V-CTR-014 at L2: FAILURES ABOVE"
fi
echo "===================================================================="
exit "$fail"
