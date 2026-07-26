#!/usr/bin/env bash
# V-CTN-020 at L2 — "Egress default-deny holds while Workload Identity still functions" (BLOCKING-ALWAYS).
#
# WHAT MAKES THIS DIFFERENT FROM dev/tests/egress-enforcement.sh. That suite proves the
# MECHANISM: a policy of the right shape blocks an off-allowlist destination on any dataplane P4
# accepts as enforcing. It builds its own synthetic policy, so it stays green no matter what the
# shipped policies say. This suite renders
# THE ACTUAL SHIPPED POLICY — the same `render_egress_policy` the installer calls — and asserts
# against that. A regression in the template is invisible to the first suite and fatal to this one.
#
# THE CONJUNCTION, AND WHICH HALF IS HONESTLY PROVABLE HERE. V-CTN-020 is deliberately an AND:
# default-deny holds *and* Workload Identity still works. Either half alone is easy and wrong — an
# allow-nothing policy passes the first, an allow-everything policy passes the second.
#
#   * Default-deny holds .......................... PROVEN here, on a real enforcing dataplane.
#   * Port narrowing is enforced, not decorative .. PROVEN here. This is the property that makes the
#     narrow metadata allow safe: without it, `cidr: 169.254.169.254/32` with a ports: list would be
#     no better than a whole-host allow, and the WI rule would hand over the raw metadata endpoint.
#   * The metadata rule is absent unless WI is on . PROVEN here, structurally, both directions.
#   * The rule's IP↔port pairs are the documented .. PROVEN here, structurally. Getting these wrong
#     ones for each dataplane                       is the single easiest mistake in the policy and
#                                                   it surfaces as an auth timeout with no mention of
#                                                   the network.
#   * A live GKE pod actually mints a WI token .... NOT PROVABLE HERE and NOT FAKED. Kind has no
#     under the policy                              metadata server. Carried as an L3 deferral with a
#                                                   named blocker (LEDGER Deferrals: no Dataplane V2
#                                                   on the live cluster, no scratch GKE).
#
# Every negative is preceded by a baseline probe with NO policy in place, so a later failure to reach
# a destination can only be the policy — never a missing listener, a slow pull, or broken DNS.
#
# DESTRUCTIVE-TEST GUARD: Kind / scratch-GKE contexts only, anchored (never a substring glob).
# Exit: 0 = V-CTN-020 (L2 half) PROVEN · 1 = FAILED (halt) · 2 = refused target · 3 = DEFERRED.
# Usage: dev/verify/egress-enforcement-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed: LSN-001 and LSN-002 each
# recurred against scripts whose authors believed the preconditions held.
#   P1 image-under-test:  none — nothing here is served by an operator image. The policy under test is rendered by this
#      script through the installer's own `render_egress_policy`, and enforcement is the dataplane's, not
#      the operator's. A stale operator cannot make an off-allowlist packet arrive.
#   P3 admission-recreate: the NetworkPolicy objects and every probe pod. Section 3 deletes and re-applies the policy
#      between stages, and each probe pod is created after the policy it is probing — a pod that
#      predated the policy would be reporting on the rules in force when it started.
#   P6 runtime-authoritative: the rendered policy YAML this script produces from `common.sh`, plus the live NetworkPolicy
#      objects read back from the API server. Not the checked-in exemplars, which are a derived
#      artifact and can drift from the renderer without either side noticing.
set -uo pipefail # -e omitted deliberately: probe exit codes are inspected, not fatal.

CTX="${1:-kind-kube-agents-dev}"
K="kubectl --context $CTX"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS="$REPO/k8s-operator/scripts"
# shellcheck source=dev/lib/preconditions.sh
. "$REPO/dev/lib/preconditions.sh"

CTRL_NS="kubeagents-system"  # where the policy and the agent pod live
TENANT_NS="egress-l2-tenant" # a foreign namespace the policy must NOT admit
TIER="platform"
SERVER_IMG="python:3.12-alpine"
CLIENT_IMG="curlimages/curl:8.10.1"

case "$CTX" in
  kind-* | gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a Kind/scratch cluster (destructive-test guard)." >&2
    exit 2
    ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad() {
  echo "FAIL: $1"
  fail=1
}
note() { echo "  ...: $1"; }

cleanup() {
  $K delete namespace "$TENANT_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  $K -n "$CTRL_NS" delete networkpolicy platform-egress --ignore-not-found >/dev/null 2>&1 || true
  $K -n "$CTRL_NS" delete pod svc-allowed-port svc-denied-port egress-client \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- 0) preconditions: reachable cluster + an ENFORCING dataplane ------------------------------------
echo "== 0) preconditions =="
if ! $K version >/dev/null 2>&1; then
  echo "DEFERRED: context '$CTX' is not reachable. Stand it up: dev/cluster/up.sh"
  exit 3
fi
# P10 (LSN-026), before any claim: can this cluster still RUN the experiment? Rationale and the
# three false failures that bought it are at the definition site. rc 2 = could-not-run, never 1.
# One of those three was this file. Every negative below is "the probe pod could not reach X", and
# a probe pod that never got scheduled cannot reach anything — which is what a working default-deny
# looks like right up to the moment the script concludes the default-deny is missing.
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2
# P4, from the library. This used to hard-require `ds/calico-node`, which is not the property — the
# property is "a dataplane that ENFORCES NetworkPolicy", and Calico is one of at least three. On a
# GKE Dataplane V2 cluster the old line deferred a suite that would have passed, which is the quiet
# half of a badly-scoped precondition: not a false green, a permanent silence.
p4_assert_enforcing_dataplane "$K" || exit 3
pass "enforcing dataplane present ($P4_DATAPLANE)"

# --- 1) render the SHIPPED policy, both ways ---------------------------------------------------------
echo
echo "== 1) rendering the shipped per-tier policy (the installer's own code path) =="
render() { # render <wi-enabled> <dataplane>
  (
    cd "$SCRIPTS" || exit 1
    SCRIPT_DIR="$SCRIPTS"
    # shellcheck disable=SC1091
    source ./common.sh --dry-run >/dev/null 2>&1
    WORKLOAD_IDENTITY_ENABLED="$1" GKE_DATAPLANE="$2" \
      render_egress_policy "platform-egress" "$CTRL_NS" "$TIER"
  )
}
POLICY_NOWI="$(render false auto)"
POLICY_WI="$(render true auto)"
if [ -z "$POLICY_NOWI" ] || [ -z "$POLICY_WI" ]; then
  bad "render_egress_policy produced nothing — cannot judge a policy that does not exist"
  exit 1
fi
pass "policy renders in both Workload-Identity modes"

# --- 2) structural assertions on the rendered policy -------------------------------------------------
echo
echo "== 2) structure of the rendered policy =="

# These three assertions are about what the policy ALLOWS, and a comment allows nothing. The
# template's own header says "there is deliberately NO 0.0.0.0/0 rule" — matching that sentence as a
# violation would make the suite fire on the documentation of its own invariant.
BODY_NOWI="$(printf '%s\n' "$POLICY_NOWI" | grep -v '^[[:space:]]*#')"

if printf '%s' "$BODY_NOWI" | grep -q '0\.0\.0\.0/0'; then
  bad "the policy contains a 0.0.0.0/0 rule — allow-with-exceptions makes 'arbitrary hosts are unreachable' false"
else
  pass "no 0.0.0.0/0 rule (pure allowlist)"
fi

if printf '%s' "$BODY_NOWI" | grep -q 'REPLACE_WITH_\|PLACEHOLDER'; then
  bad "the rendered policy still carries a placeholder token (V-CMP-003)"
else
  pass "no placeholder tokens in the rendered policy (V-CMP-003)"
fi

# The metadata server must be absent by omission when WI is off. This is 03 §11's load-bearing
# negative: 169.254.169.254:80 serves the NODE's service account on a non-WI cluster.
if printf '%s' "$BODY_NOWI" | grep -q '169\.254\.169\.'; then
  bad "WI is OFF yet the policy allows a metadata address — the raw metadata endpoint is reachable"
else
  pass "WI off: no metadata address in the allowlist (raw node credentials unreachable by omission)"
fi

# ...and present, narrow, and correctly paired when WI is on. The pairings are dataplane-specific;
# 169.254.169.254 with port 988 is the plausible-looking combination that does not work.
check_pair() { # check_pair <cidr> <port>...
  local cidr="$1"
  shift
  local blk p
  blk="$(printf '%s' "$POLICY_WI" | awk -v c="$cidr" '
    $0 ~ "cidr: " c {found=1} found {print} found && /port: / && ++n>=2 {exit}')"
  if [ -z "$blk" ]; then
    bad "WI on: no rule for $cidr"
    return
  fi
  for p in "$@"; do
    if printf '%s' "$blk" | grep -q "port: $p\$"; then
      pass "WI on: $cidr is allowed on port $p"
    else
      bad "WI on: $cidr is missing port $p — WI will time out with an auth error that never mentions the network"
    fi
  done
}
check_pair "169.254.169.252/32" 988 987 # Dataplane V1 / Calico, GKE >= 1.21.0-gke.1000
check_pair "169.254.169.254/32" 80 8080 # Dataplane V2

# A metadata rule with no ports: list is a whole-host allow wearing a narrow rule's clothing.
if printf '%s' "$POLICY_WI" |
  awk '/cidr: 169\.254\.169\./{f=1} f&&/ports:/{ok=1} /^    - to:/{if(f&&!ok&&seen)bad=1; f=0; ok=0; seen=1} END{exit bad?1:0}'; then
  pass "WI on: every metadata rule is port-bound (no whole-host allow)"
else
  bad "WI on: a metadata rule has no ports: list — that is a whole-host allow"
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "V-CTN-020 (L2): STRUCTURAL FAILURES ABOVE — not proceeding to live enforcement."
  exit 1
fi

# --- 3) fixtures ---------------------------------------------------------------------------------
echo
echo "== 3) fixtures =="
$K create namespace "$CTRL_NS" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1
$K create namespace "$TENANT_NS" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1

serve() { # serve <ns> <name> <port>
  $K -n "$1" run "$2" --image="$SERVER_IMG" --restart=Never \
    --command -- python3 -m http.server "$3" >/dev/null 2>&1
}
# Same namespace, ALLOWED port (rule 2 admits :80 into the control namespace).
serve "$CTRL_NS" svc-allowed-port 80
# Same namespace, port NOT in the allowlist. This pair is what proves port narrowing is real.
serve "$CTRL_NS" svc-denied-port 9999
# A foreign namespace — never admitted by any rule.
serve "$TENANT_NS" svc-tenant 80

$K -n "$CTRL_NS" run egress-client --image="$CLIENT_IMG" --restart=Never \
  --labels="kube-agents/tier=$TIER" --command -- sleep 3600 >/dev/null 2>&1

for spec in "$CTRL_NS/svc-allowed-port" "$CTRL_NS/svc-denied-port" "$TENANT_NS/svc-tenant" "$CTRL_NS/egress-client"; do
  if ! $K -n "${spec%%/*}" wait --for=condition=Ready "pod/${spec##*/}" --timeout=180s >/dev/null 2>&1; then
    bad "fixture ${spec} never became Ready — cannot prove enforcement"
    exit 1
  fi
done

IP_ALLOWED="$($K -n "$CTRL_NS" get pod svc-allowed-port -o jsonpath='{.status.podIP}')"
IP_DENIED_PORT="$($K -n "$CTRL_NS" get pod svc-denied-port -o jsonpath='{.status.podIP}')"
IP_TENANT="$($K -n "$TENANT_NS" get pod svc-tenant -o jsonpath='{.status.podIP}')"
note "allowed=$IP_ALLOWED:80  denied-port=$IP_DENIED_PORT:9999  tenant=$IP_TENANT:80"
if [ -z "$IP_ALLOWED" ] || [ -z "$IP_DENIED_PORT" ] || [ -z "$IP_TENANT" ]; then
  bad "could not resolve fixture pod IPs — setup error, not a policy result"
  exit 1
fi

probe() { # probe <ip> <port>; 0 = reachable
  $K -n "$CTRL_NS" exec egress-client -- \
    curl -s -o /dev/null --max-time 5 "http://$1:$2/" >/dev/null 2>&1
}

# --- 4) baseline with NO policy: everything reachable ------------------------------------------------
echo
echo "== 4) baseline (no policy) — every destination must be reachable =="
for t in "$IP_ALLOWED 80 same-ns:80" "$IP_DENIED_PORT 9999 same-ns:9999" "$IP_TENANT 80 tenant-ns:80"; do
  set -- $t
  if probe "$1" "$2"; then
    pass "baseline: $3 reachable"
  else
    bad "baseline: $3 UNREACHABLE with no policy — setup error, so no later deny would be evidence"
    exit 1
  fi
done

INTERNET_BASELINE=0
if probe 1.1.1.1 443 || $K -n "$CTRL_NS" exec egress-client -- \
  curl -s -o /dev/null --max-time 5 https://1.1.1.1/ >/dev/null 2>&1; then
  INTERNET_BASELINE=1
  pass "baseline: an arbitrary internet host (1.1.1.1:443) is reachable"
else
  note "baseline: no internet egress from this cluster — the arbitrary-host negative will be SKIPPED,"
  note "          not counted as a pass (a check that cannot fail is not evidence, 09 §6)."
fi

# --- 5) apply the SHIPPED policy and re-probe --------------------------------------------------------
echo
echo "== 5) applying the shipped policy (WI off — the Kind posture) =="
printf '%s\n' "$POLICY_NOWI" | $K apply -f - >/dev/null || {
  bad "the shipped policy was REJECTED by the API server — it is not applicable as rendered"
  exit 1
}
pass "the shipped policy applies cleanly"
sleep 6 # let the dataplane program it

echo
echo "== 6) enforcement assertions =="
if probe "$IP_ALLOWED" 80; then
  pass "on-allowlist (control namespace, :80) is ALLOWED — the policy does not over-block"
else
  bad "on-allowlist (control namespace, :80) is BLOCKED — the agent would lose inference and token minting"
fi

if probe "$IP_DENIED_PORT" 9999; then
  bad "PORT NARROWING IS NOT ENFORCED: :9999 on an allowlisted namespace is reachable. The narrow metadata allow would be a whole-host allow."
else
  pass "off-allowlist PORT (:9999, allowlisted namespace) is DENIED — port narrowing is enforced by the dataplane"
fi

if probe "$IP_TENANT" 80; then
  bad "a foreign namespace is reachable — tenant isolation is not enforced by this policy"
else
  pass "off-allowlist NAMESPACE ($TENANT_NS:80) is DENIED"
fi

if [ "$INTERNET_BASELINE" -eq 1 ]; then
  if $K -n "$CTRL_NS" exec egress-client -- curl -s -o /dev/null --max-time 5 https://1.1.1.1/ >/dev/null 2>&1; then
    bad "an arbitrary internet host is still reachable — egress is not contained"
  else
    pass "arbitrary internet host (1.1.1.1:443) is DENIED"
  fi
else
  echo "SKIP: arbitrary-host negative (no internet baseline to distinguish a deny from no route)"
fi

# --- 7) negative control: the suite must be able to fail --------------------------------------------
echo
echo "== 7) negative control — remove the policy and confirm the denies come back =="
$K -n "$CTRL_NS" delete networkpolicy platform-egress >/dev/null 2>&1
sleep 6
if probe "$IP_DENIED_PORT" 9999; then
  pass "control: with the policy removed, :9999 is reachable again (the denies above were the policy)"
else
  bad "control: :9999 is STILL unreachable with no policy — the 'deny' above was not the policy, and this suite proves nothing"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "V-CTN-020 (L2 half): PROVEN — default-deny holds, port narrowing is enforced, the metadata"
  echo "  allow is absent without WI and correctly paired with it. The live-WI half is an L3 deferral."
else
  echo "V-CTN-020 (L2): FAILURES ABOVE (halt condition)"
fi
exit "$fail"
